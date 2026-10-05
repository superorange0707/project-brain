"""Opt-in per-workspace macOS login service, managed by the current user's launchd."""

from __future__ import annotations

import hashlib
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .core import BrainError, Settings
from .platforms import atomic_managed_bytes_write, read_managed_bytes
from .ui import _load_ui_instance, _probe_ui_instance, _ui_command, _ui_lock, _ui_log, ui_instance


def _agent_path(settings: Settings) -> Path:
    key = hashlib.sha256(os.fsencode(settings.config_path.resolve())).hexdigest()[:20]
    return Path.home() / "Library" / "LaunchAgents" / f"com.project-brain.workspace.{key}.plist"


def _definition(settings: Settings) -> dict[str, Any] | None:
    path = _agent_path(settings)
    if not path.exists() and not path.is_symlink():
        return None
    try:
        value = plistlib.loads(read_managed_bytes(path.parent, path, max_bytes=64 * 1024))
        args = value.get("ProgramArguments", [])
        if (value.get("Label") != path.stem or not isinstance(args, list) or len(args) < 8
                or not all(isinstance(arg, str) for arg in args)
                or args[-7:-1] != ["-c", str(settings.config_path.resolve()), "ui", "--foreground", "--no-open", "--port"]
                or not 0 <= int(args[-1]) <= 65535):
            raise ValueError("workspace identity does not match")
        return value
    except (OSError, ValueError, TypeError, AttributeError, IndexError, plistlib.InvalidFileException) as error:
        raise BrainError(f"Cannot validate the Brain login service at {path}; it was preserved.") from error


def installed(settings: Settings) -> bool:
    return _definition(settings) is not None


def _launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BrainError("Could not contact your macOS login service manager; retry from your logged-in Mac session.") from error
    if check and result.returncode:
        raise BrainError(
            f"macOS launchctl {args[0]} failed: {result.stderr.strip()[:1500]}. "
            "Check System Settings > General > Login Items & Extensions and your company background-app policy."
        )
    return result


def _loaded(target: str) -> bool:
    result = _launchctl("print", target, check=False)
    if result.returncode not in {0, 3, 113}:  # ESRCH / missing service in the login domain.
        raise BrainError("Could not confirm the macOS service registration; no service was changed.")
    return result.returncode == 0


def _stable_executable(executable: str) -> str:
    """Retain an installed command symlink rather than pinning a Homebrew Cellar version."""
    if not getattr(sys, "frozen", False):
        return executable
    current = Path(executable).resolve()
    candidates = [shutil.which("brain"), sys.argv[0]]
    if "Cellar" in current.parts:
        index = current.parts.index("Cellar")
        if len(current.parts) > index + 2:
            candidates.insert(0, str(Path(*current.parts[:index]) / "opt" / current.parts[index + 1] / "bin" / "brain"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and Path(candidate).resolve() == current:
            return os.path.abspath(candidate)
    return executable


def _plist(settings: Settings, port: int) -> dict[str, Any]:
    command, environment = _ui_command(settings, port)
    command[0] = _stable_executable(command[0])
    label = _agent_path(settings).stem
    # Persist only launch requirements, never the terminal's tokens, proxies or SSH sockets.
    saved_environment = {
        "PATH": environment.get("PATH") or "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "PROJECT_BRAIN_SERVICE": label,
    }
    if not getattr(sys, "frozen", False):
        saved_environment["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    log = str(settings.state_dir / "ui.log")
    return {
        "Label": label,
        "ProgramArguments": command,
        "WorkingDirectory": str(settings.root),
        "EnvironmentVariables": saved_environment,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "Umask": 0o077,
        "StandardInPath": "/dev/null",
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }


def _stop(settings: Settings) -> None:
    status = ui_instance(settings, "stop")  # Busy/uncertain instances fail closed.
    if not status["running"]:
        return
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        # Shutdown already authenticated. The server removes its record after closing;
        # a health request during that interval can time out even on a clean exit.
        if _load_ui_instance(settings) is None:
            try:
                with _ui_lock(settings, "ui-server.lock"):
                    return
            except BrainError:
                pass  # Record cleanup precedes release of the server's lifetime lease.
        time.sleep(0.1)
    raise BrainError("Brain is still stopping; its macOS service was preserved. Retry shortly.")


def _wait_ready(settings: Settings, label: str, *, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        instance = _load_ui_instance(settings)
        if instance and _probe_ui_instance(instance) and instance.get("service") == label:
            return
        time.sleep(0.1)
    raise BrainError(
        f"The macOS service is installed but not ready. See {settings.state_dir / 'ui.log'}; "
        "check background activity and project-folder access in System Settings. Retry brain service start."
    )


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise BrainError("Login services currently support macOS. Use brain ui for terminal-independent operation on this platform.")


def service(settings: Settings, action: str, *, port: int | None = None) -> dict[str, Any]:
    _require_macos()
    if action not in {"install", "start", "status", "stop", "uninstall"}:
        raise BrainError("Unknown service action")
    if port is not None and (action != "install" or not 0 <= port <= 65535):
        raise BrainError("--port is available only with service install (0 through 65535).")
    path = _agent_path(settings)
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{path.stem}"
    with _ui_lock(settings, "ui-launch.lock"):
        definition = _definition(settings)
        loaded = _loaded(target)
        if action == "install":
            # Explicit installation can take over a detached UI, but never interrupt work.
            installed_port = int(definition["ProgramArguments"][-1]) if definition else 8765
            desired = _plist(settings, installed_port if port is None else port)
            if definition != desired or not loaded:
                _stop(settings)
                with _ui_lock(settings, "ui-server.lock"):
                    if loaded:
                        _launchctl("bootout", target)
                    with _ui_log(settings):
                        pass  # Create a private log before launchd opens its output paths.
                    atomic_managed_bytes_write(path.parent, path, plistlib.dumps(desired))
                _launchctl("bootstrap", domain, str(path))
                loaded = True
            definition = desired
        elif action in {"start", "uninstall"} and definition is None:
            if action == "start":
                raise BrainError("Install this workspace's macOS login service first: brain service install")
            if loaded:
                raise BrainError("The service definition is missing; the registered job was preserved.")
        if action in {"install", "start"}:
            instance = _load_ui_instance(settings)
            ready = bool(instance and _probe_ui_instance(instance))
            if not ready or instance.get("service") != path.stem:
                if ready:
                    _stop(settings)
                if not loaded:
                    _launchctl("bootstrap", domain, str(path))
                else:
                    _launchctl("kickstart", target)  # Never use -k, which kills active work.
                _wait_ready(settings, path.stem)
                loaded = True
        elif action in {"stop", "uninstall"}:
            _stop(settings)
            if definition is not None:
                with _ui_lock(settings, "ui-server.lock"):
                    if loaded:
                        _launchctl("bootout", target)
                    if action == "uninstall":
                        _definition(settings)  # Recheck ownership before removing our one plist.
                        path.unlink()
                        definition = None
                loaded = False
        status = ui_instance(settings, "status")
    return {"installed": definition is not None, "loaded": loaded, "running": status["running"],
            "port": status["port"], "plist": str(path), "log": str(settings.state_dir / "ui.log")}

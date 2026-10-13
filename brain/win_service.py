"""Windows login startup through the per-user Run key: no administrator rights,
no Task Scheduler, no service account. At sign-in Windows runs
``brain -c <workspace>/brain.toml ui --no-open``, which starts the background UI
(and its idle refresh) and exits. Unlike the macOS LaunchAgent nothing restarts
the UI after a crash; ``brain ui`` reopens it.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from .core import BrainError, Settings

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
DEFAULT_PORT = 8765


def _name(settings: Settings) -> str:
    return "ProjectBrain-" + hashlib.sha256(str(settings.config_path.resolve()).encode()).hexdigest()[:12]


def _read(name: str) -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value)
    except FileNotFoundError:
        return None


def _write(name: str, command: str) -> None:
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)


def _delete(name: str) -> None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
    except FileNotFoundError:
        pass


def _command(settings: Settings, port: int) -> str:
    executable = str(Path(sys.executable).resolve())
    prefix = [] if getattr(sys, "frozen", False) else ["-m", "brain.cli"]
    return subprocess.list2cmdline([executable, *prefix, "-c", str(settings.config_path.resolve()),
                                    "ui", "--no-open", "--port", str(port)])


def installed(settings: Settings) -> bool:
    return _read(_name(settings)) is not None


def service(settings: Settings, action: str, *, port: int | None = None) -> dict[str, Any]:
    from .ui import start_ui, ui_instance

    if action not in {"install", "start", "status", "stop", "uninstall"}:
        raise BrainError("Unknown service action")
    if port is not None and (action != "install" or not 0 <= port <= 65535):
        raise BrainError("--port is available only with service install (0 through 65535).")
    name = _name(settings)
    existing = _read(name)
    if action == "install":
        kept = re.search(r"--port (\d+)", existing or "")
        chosen = port if port is not None else int(kept.group(1)) if kept else DEFAULT_PORT
        _write(name, _command(settings, chosen))
        existing = _read(name)
        start_ui(settings, port=chosen, open_browser=False)
    elif action == "start":
        if existing is None:
            raise BrainError("Install this workspace's login startup first: brain service install")
        kept = re.search(r"--port (\d+)", existing)
        start_ui(settings, port=int(kept.group(1)) if kept else DEFAULT_PORT, open_browser=False)
    elif action in {"stop", "uninstall"}:
        ui_instance(settings, "stop")
        if action == "uninstall":
            _delete(name)
            existing = None
    status = ui_instance(settings, "status")
    return {"installed": existing is not None, "loaded": existing is not None, "running": status["running"],
            "port": status["port"], "plist": f"HKCU\\{RUN_KEY}\\{name}", "log": str(settings.state_dir / "ui.log")}

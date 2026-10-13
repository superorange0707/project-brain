"""One-command upgrade of an installed Project Brain.

Homebrew installs are delegated to Homebrew. Standalone archives (the Windows
ZIP, the Linux/macOS installer layouts) are verified against the release's
SHA256SUMS.txt and swapped in place by renaming each old entry aside, which a
running Windows executable allows. Source, pip and uv installs are never
mutated. After a swap the NEW binary runs ``upgrade --finish``, which republishes
the index only when the serving schemas changed and restarts the UI if needed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, url2pathname

from . import __version__
from .core import BrainError, Settings

RELEASE_REPOSITORY = "superorange0707/project-brain"
HOMEBREW_FORMULA = "superorange0707/tap/project-brain"
CHECK_FILE = "upgrade-check.json"
STATE_FILE = "upgrade-state.json"
CHECK_TTL_SECONDS = 24 * 60 * 60
MAX_CHECKSUM_BYTES = 64 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_UNPACKED_BYTES = 2 * 1024 * 1024 * 1024
MAX_MEMBERS = 20_000
ASIDE_SUFFIX = ".brain-upgrade-old"
STAGING_PREFIX = ".brain-upgrade-"
_ASSET_VERSION = re.compile(r"project-brain-v(\d+\.\d+\.\d+)-")
_CHECK_STARTED = [0.0]  # Monotonic start of the last background check in this process.


class UpgradeBusy(BrainError):
    """The local UI is refreshing or investigating and cannot stop yet."""


def checksums(text: str) -> dict[str, str]:
    """Parse SHA256SUMS.txt (`<sha256>  <name>` or `<sha256> *<name>`)."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(\S.*)", line.strip())
        if not match:
            continue
        name = match.group(2).strip()
        if name in result:
            raise ValueError(f"SHA256SUMS lists {name} more than once")
        result[name] = match.group(1).lower()
    return result


def _version_key(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def asset_name(version: str) -> str:
    from .platforms import platform_id

    system, _, machine = platform_id().partition("-")
    system = {"darwin": "macos"}.get(system, system)
    return f"project-brain-v{version}-{system}-{machine}.{'zip' if system == 'windows' else 'tar.gz'}"


def _base_url(version: str | None, release_base_url: str | None) -> str:
    if release_base_url:
        return release_base_url.rstrip("/")
    root = f"https://github.com/{RELEASE_REPOSITORY}/releases"
    return f"{root}/download/v{version}" if version else f"{root}/latest/download"


def _open(settings: Settings, url: str):
    parsed = urlparse(url)
    if parsed.scheme == "file":
        return open(url2pathname(parsed.path), "rb")
    from .models import _approved_pack_url, _open_model_download

    _approved_pack_url(settings, url)
    response = _open_model_download(settings, Request(url, headers={"User-Agent": "Project-Brain-upgrade/1"}), timeout=60)
    try:
        _approved_pack_url(settings, response.geturl(), final=True)
    except ValueError:
        response.close()
        raise
    return response


def _read_bounded(settings: Settings, url: str, max_bytes: int) -> bytes:
    with _open(settings, url) as source:
        data = source.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"{url} exceeds {max_bytes} bytes")
    return data


def _download(settings: Settings, url: str, destination: Path, expected_sha256: str) -> None:
    digest = hashlib.sha256()
    total = 0
    with _open(settings, url) as source, destination.open("wb") as target:
        while chunk := source.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_ARCHIVE_BYTES:
                raise BrainError(f"Release archive exceeds {MAX_ARCHIVE_BYTES} bytes")
            digest.update(chunk)
            target.write(chunk)
    if digest.hexdigest() != expected_sha256.lower():
        raise BrainError("Release archive checksum does not match SHA256SUMS.txt; nothing was changed")


def _now() -> float:
    return time.time()


def check_latest(settings: Settings, *, release_base_url: str | None = None, force: bool = False) -> dict[str, Any]:
    """Latest published version from SHA256SUMS.txt, cached for 24 h; never raises."""
    from .platforms import atomic_managed_text_write

    source = _base_url(None, release_base_url) + "/SHA256SUMS.txt"
    cache_path = settings.state_dir / CHECK_FILE
    if not force:
        cached = cached_check(settings)
        if cached and cached.get("source") == source and _now() - float(cached.get("checked_epoch") or 0) < CHECK_TTL_SECONDS:
            return cached
    result: dict[str, Any] = {
        "current": __version__, "latest": None, "update_available": None, "source": source,
        "checked_at": datetime.fromtimestamp(_now(), UTC).isoformat(), "checked_epoch": _now(),
    }
    try:
        names = checksums(_read_bounded(settings, source, MAX_CHECKSUM_BYTES).decode("utf-8"))
        versions = {match.group(1) for name in names for match in [_ASSET_VERSION.match(name)] if match}
        if not versions:
            raise ValueError("SHA256SUMS.txt names no Project Brain release archive")
        latest = max(versions, key=_version_key)
        result.update(latest=latest, update_available=_version_key(latest) > _version_key(__version__))
    except Exception as error:  # Offline, proxy, TLS or malformed release: report, never raise.
        result["error"] = f"{type(error).__name__}: {error}"[:300]
    try:
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        atomic_managed_text_write(settings.state_dir, cache_path, json.dumps(result, indent=2) + "\n")
    except OSError:
        pass
    return result


def cached_check(settings: Settings) -> dict[str, Any] | None:
    from .platforms import read_managed_text

    try:
        value = json.loads(read_managed_text(settings.state_dir, settings.state_dir / CHECK_FILE, max_bytes=16 * 1024))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def install_owner() -> str:
    """homebrew, standalone (frozen archive install) or python (source/pip/uv/venv)."""
    if not getattr(sys, "frozen", False):
        return "python"  # A venv interpreter may itself live in Homebrew's Cellar.
    parts = Path(sys.executable).resolve().parts
    return "homebrew" if "Cellar" in parts and parts[parts.index("Cellar") + 1:][:1] == ("project-brain",) else "standalone"


def status_update(settings: Settings) -> dict[str, Any] | None:
    """Cached update state for status pages; refreshes in the background, never blocks.

    Only installed binaries check automatically. Source/pip/uv runs (and tests)
    never reach the network from a status call.
    """
    cached = cached_check(settings)
    stale = not cached or _now() - float(cached.get("checked_epoch") or 0) >= CHECK_TTL_SECONDS
    if (stale and install_owner() != "python" and not os.environ.get("PROJECT_BRAIN_NO_UPDATE_CHECK")
            and (not _CHECK_STARTED[0] or time.monotonic() - _CHECK_STARTED[0] >= CHECK_TTL_SECONDS)):
        _CHECK_STARTED[0] = time.monotonic()
        threading.Thread(target=check_latest, args=(settings,), name="brain-update-check", daemon=True).start()
    return cached


def _safe_member(name: str) -> str:
    path = PurePosixPath(name.replace("\\", "/"))
    parts = [part for part in path.parts if part not in {"", "."}]
    if not parts or path.is_absolute() or ".." in parts or ":" in parts[0]:
        raise BrainError(f"Unsafe release archive member: {name}")
    return "/".join(parts)


def _extract(archive: Path, destination: Path) -> None:
    total = 0
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            if len(members) > MAX_MEMBERS:
                raise BrainError("Release archive has too many members")
            for member in members:
                name = _safe_member(member.filename)
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise BrainError(f"Release archive member is a link: {member.filename}")
                total += member.file_size
                if total > MAX_UNPACKED_BYTES:
                    raise BrainError("Release archive unpacks beyond its size limit")
                target = destination / name
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
        return
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        if len(members) > MAX_MEMBERS:
            raise BrainError("Release archive has too many members")
        for member in members:
            name = _safe_member(member.name)
            if not (member.isfile() or member.isdir()):
                raise BrainError(f"Release archive member is not a regular file or directory: {member.name}")
            total += member.size
            if total > MAX_UNPACKED_BYTES:
                raise BrainError("Release archive unpacks beyond its size limit")
            target = destination / name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.extractfile(member) as source, target.open("wb") as output:  # type: ignore[union-attr]
                shutil.copyfileobj(source, output)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)


def _remove(path: Path) -> bool:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def cleanup_retired(directory: Path | None = None) -> None:
    """Delete entries a previous upgrade renamed aside (Windows keeps the running exe)."""
    directory = directory or Path(sys.executable).resolve().parent
    try:
        for item in directory.iterdir():
            if item.name.endswith(ASIDE_SUFFIX) or (item.name.startswith(STAGING_PREFIX) and item.is_dir()):
                _remove(item)
    except OSError:
        pass


def _swap(unpacked: Path, install_dir: Path) -> None:
    """Rename every old top-level entry aside, move the new one in; reverse on failure."""
    # ponytail: one entry at a time, not one directory rename; a crash between two
    # renames leaves *.brain-upgrade-old beside the new files, recoverable by rerun.
    done: list[tuple[Path, Path | None]] = []
    try:
        for entry in sorted(unpacked.iterdir()):
            target = install_dir / entry.name
            aside = None
            if target.exists() or target.is_symlink():
                aside = install_dir / (entry.name + ASIDE_SUFFIX)
                if aside.exists() and not _remove(aside):
                    aside = install_dir / f"{entry.name}{ASIDE_SUFFIX}-{int(_now())}"
                os.replace(target, aside)
            done.append((target, aside))
            os.replace(entry, target)
    except OSError as error:
        for target, aside in reversed(done):
            if target.exists():
                os.replace(target, unpacked / target.name)
            if aside is not None:
                os.replace(aside, target)
        raise BrainError(f"Could not replace {install_dir}: {error}; the previous version was restored") from error
    for target, aside in done:
        if aside is not None:
            _remove(aside)


def _stop_ui(settings: Settings) -> dict[str, bool]:
    from .ui import _load_ui_instance, _probe_ui_instance, ui_instance

    if sys.platform == "darwin":
        from .mac_service import installed, service

        if installed(settings):
            service(settings, "stop")
            return {"service": True, "was_running": True}
    instance = _load_ui_instance(settings)
    try:
        running = ui_instance(settings, "stop")["running"]
    except BrainError as error:
        raise UpgradeBusy(str(error)) from error
    deadline = time.monotonic() + 30
    while running and instance and time.monotonic() < deadline:
        try:
            if not _probe_ui_instance(instance):
                break
        except BrainError:
            pass  # Mid-shutdown resets are expected; keep waiting for a refused port.
        time.sleep(0.2)
    else:
        if running:
            raise BrainError("The UI did not stop within 30 seconds; nothing was changed. Retry brain upgrade.")
    return {"service": False, "was_running": bool(running)}


def _write_state(settings: Settings, value: dict[str, Any]) -> None:
    from .platforms import atomic_managed_text_write

    settings.state_dir.mkdir(parents=True, exist_ok=True)
    atomic_managed_text_write(settings.state_dir, settings.state_dir / STATE_FILE, json.dumps(value) + "\n")


def _run_finish(settings: Settings, executable: Path) -> int:
    command = [str(executable), "-c", str(settings.config_path), "upgrade", "--finish"]
    return subprocess.run(command, check=False, timeout=60 * 60).returncode


def _homebrew_upgrade(settings: Settings) -> int:
    executable = Path(sys.executable).resolve()
    prefix = Path(*executable.parts[:executable.parts.index("Cellar")])
    brew = prefix / "bin" / "brew"
    brew_command = str(brew) if brew.exists() else shutil.which("brew") or "brew"
    _write_state(settings, _stop_ui(settings))
    for arguments, timeout, required in ((["update"], 600, True), (["trust", "--formula", HOMEBREW_FORMULA], 120, False),
                                         (["upgrade", HOMEBREW_FORMULA], 900, True)):
        print(f"$ brew {' '.join(arguments)}", flush=True)
        result = subprocess.run([brew_command, *arguments], check=False, timeout=timeout)
        if result.returncode and required:
            raise BrainError(f"brew {arguments[0]} failed with exit code {result.returncode}; the UI stays stopped, run brain ui to reopen it")
    stable = prefix / "opt" / "project-brain" / "bin" / "brain"
    if sys.platform == "darwin":
        from .mac_service import installed, service

        if installed(settings):
            service(settings, "install")
    return _run_finish(settings, stable if stable.exists() else Path(shutil.which("brain") or executable))


def _standalone_upgrade(settings: Settings, version: str, *, archive: Path | None, sha256: str | None,
                        release_base_url: str | None) -> int:
    executable = Path(sys.executable).resolve()
    install_dir = executable.parent
    name = archive.name if archive else asset_name(version)
    if not sha256:
        sums = checksums(_read_bounded(settings, _base_url(version, release_base_url) + "/SHA256SUMS.txt",
                                       MAX_CHECKSUM_BYTES).decode("utf-8"))
        if name not in sums:
            raise BrainError(f"SHA256SUMS.txt for {version} does not list {name}")
        sha256 = sums[name]
    staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=install_dir))
    try:
        downloaded = staging / name
        source_url = archive.resolve().as_uri() if archive else _base_url(version, release_base_url) + "/" + name
        print(f"Downloading and verifying {name}", flush=True)
        _download(settings, source_url, downloaded, sha256)
        unpacked = staging / "unpacked"
        unpacked.mkdir()
        _extract(downloaded, unpacked)
        new_brain = unpacked / executable.name
        if not new_brain.is_file():
            raise BrainError(f"Release archive does not contain {executable.name}; nothing was changed")
        reported = subprocess.run([str(new_brain), "--version"], capture_output=True, text=True, timeout=60, check=False)
        if reported.stdout.strip() != f"brain {version}":
            raise BrainError(f"Staged executable reports {reported.stdout.strip() or 'nothing'}, expected brain {version}; nothing was changed")
        _write_state(settings, _stop_ui(settings))
        _swap(unpacked, install_dir)
    finally:
        _remove(staging)
    print(f"Installed brain {version} in {install_dir}", flush=True)
    return _run_finish(settings, executable)


def finish(settings: Settings) -> int:
    """Run by the new binary: republish only on schema drift, restart, report the Agent Kit."""
    from .agent import agent_kit_status
    from .catalog import current_generation_ref, generation_predates_binary
    from .platforms import read_managed_text

    print(f"brain {__version__}", flush=True)
    if generation_predates_binary(current_generation_ref(settings)):
        from .ops import refresh_brain

        print("The published index predates this version; republishing it once (pinned tickets are kept).", flush=True)
        refresh_brain(settings, fetch=False, discover=False)
    try:
        state = json.loads(read_managed_text(settings.state_dir, settings.state_dir / STATE_FILE, max_bytes=4096))
        (settings.state_dir / STATE_FILE).unlink(missing_ok=True)
    except (OSError, ValueError):
        state = {}
    if state.get("service") and sys.platform == "darwin":
        from .mac_service import service

        service(settings, "start")
    elif state.get("was_running"):
        from contextlib import redirect_stdout

        from .ui import start_ui

        with redirect_stdout(sys.stderr):
            start_ui(settings, open_browser=False)
        print("Restarted the Project Brain UI.", flush=True)
    kit = agent_kit_status(settings)
    if kit["status"] == "stale":
        print(f"M365 Agent Kit changed ({', '.join(kit['changed'])}): run brain agent-kit m365 and replace it in Agent Builder.")
    return 0


def upgrade(settings: Settings, *, version: str | None = None, archive: str | None = None, sha256: str | None = None,
            release_base_url: str | None = None, check_only: bool = False) -> int:
    if version is not None and not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise BrainError("--version must be X.Y.Z")
    if sha256 is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
        raise BrainError("--sha256 must be 64 hexadecimal characters")
    owner = install_owner()
    if check_only or version is None:
        latest = check_latest(settings, release_base_url=release_base_url, force=True)
        print(f"Current: brain {latest['current']}; latest: {latest['latest'] or 'unknown'}"
              + (f" ({latest['error']})" if latest.get("error") else ""), flush=True)
        if check_only:
            return 1 if latest.get("error") else 0
        version = latest["latest"]
        if version is None and owner != "homebrew":
            raise BrainError("Could not determine the latest release; pass --version X.Y.Z (and --archive for an offline file)")
        if version and not latest.get("update_available"):
            print("Already up to date.")
            return 0
    if owner == "python":
        print("This Brain runs from Python (source, pip or uv); upgrade it with: uv tool upgrade project-brain-context "
              "(or python -m pip install --upgrade with the release wheel). Nothing was changed.")
        return 0
    if owner == "homebrew":
        return _homebrew_upgrade(settings)
    return _standalone_upgrade(settings, str(version), archive=Path(archive) if archive else None, sha256=sha256,
                               release_base_url=release_base_url)

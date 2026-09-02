from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from threading import Event, Lock, Thread
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from .locks import workspace_exclusive
from .platforms import (
    atomic_managed_text_write,
    filesystem_component,
    native_command,
    process_group_kwargs,
    remove_tree,
    run_bounded_process,
    terminate_process_tree,
)

if TYPE_CHECKING:
    from .core import Repository, Settings


_SNAPSHOT_SEAL_CACHE: set[tuple[object, ...]] = set()
MAX_GIT_COMMAND_STDOUT_BYTES = 8 * 1024 * 1024
MAX_GIT_COMMAND_STDERR_BYTES = 1024 * 1024
MAX_GIT_SNAPSHOT_ITEMS = 500_000
MAX_GIT_SNAPSHOT_STAGE_SECONDS = 300.0
MAX_GIT_REFRESH_SCAN_ITEMS = MAX_GIT_SNAPSHOT_ITEMS * 3
MAX_GIT_REFRESH_SCAN_SECONDS = MAX_GIT_SNAPSHOT_STAGE_SECONDS * 3
MAX_GIT_SNAPSHOT_SEAL_BYTES = 256 * 1024 * 1024


@dataclass
class SyncResult:
    repo: str
    status: str
    ref: str | None
    sha: str | None
    snapshot: str | None
    fetched: bool
    warning: str | None = None


def parse_branch_overrides(settings: Settings, values: list[str]) -> dict[str, str]:
    from .core import BrainError

    overrides: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise BrainError(f"Invalid branch override {value!r}; expected REPO=BRANCH")
        repo, branch = (part.strip() for part in value.split("=", 1))
        if not repo or not branch:
            raise BrainError(f"Invalid branch override {value!r}; expected REPO=BRANCH")
        settings.repo(repo)
        overrides[repo] = branch
    return overrides


def _git(
    repo: Repository,
    *args: str,
    binary: bool = False,
    extra_env: dict[str, str] | None = None,
    timeout: float = 120,
) -> subprocess.CompletedProcess:
    environment = os.environ.copy()
    # HTTP credential prompts stay disabled. SSH authentication is handled by
    # the user's ssh process and may ask on its controlling terminal.
    environment["GIT_TERMINAL_PROMPT"] = "0"
    if extra_env:
        environment.update(extra_env)
    command = ["git", *args]
    try:
        result = run_bounded_process(
            command, repo.path,
            environment=environment,
            max_stdout_bytes=MAX_GIT_COMMAND_STDOUT_BYTES,
            max_stderr_bytes=MAX_GIT_COMMAND_STDERR_BYTES,
            timeout=timeout,
        )
        returncode = (
            124 if getattr(result, "timed_out", False)
            else 125 if getattr(result, "output_truncated", False)
            else result.returncode
        )
        if binary:
            return subprocess.CompletedProcess(
                result.args, returncode,
                result.stdout.encode("utf-8", errors="replace"),
                result.stderr.encode("utf-8", errors="replace"),
            )
        if returncode != result.returncode:
            return subprocess.CompletedProcess(result.args, returncode, result.stdout, result.stderr)
        return result
    except OSError:
        empty = b"" if binary else ""
        return subprocess.CompletedProcess(command, 124, empty, empty)


def _git_text(repo: Repository, *args: str) -> str | None:
    result = _git(repo, *args)
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def _git_archive_to_path(
    repo: Repository, ref: str, destination: Path, *, timeout: float = 120,
    max_bytes: int | None = None,
) -> subprocess.CompletedProcess:
    """Stream a Git archive to disk without retaining repository bytes in memory."""
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    command = [native_command("git"), "archive", "--format=tar", ref]
    with destination.open("wb") as output:
        try:
            process = subprocess.Popen(
                command, cwd=repo.path, env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, **process_group_kwargs(),
            )
            exceeded = Event()
            stderr_exceeded = Event()
            writer_failed = Event()
            written = 0
            stderr = bytearray()

            def stream_archive() -> None:
                nonlocal written
                assert process.stdout is not None
                try:
                    while chunk := process.stdout.read(1024 * 1024):
                        if max_bytes is not None and written + len(chunk) > max_bytes:
                            exceeded.set()
                            return
                        output.write(chunk)
                        written += len(chunk)
                except OSError:
                    writer_failed.set()
                finally:
                    process.stdout.close()

            def stream_error() -> None:
                assert process.stderr is not None
                try:
                    while chunk := process.stderr.read(64 * 1024):
                        if len(stderr) + len(chunk) > MAX_GIT_COMMAND_STDERR_BYTES:
                            remaining = max(0, MAX_GIT_COMMAND_STDERR_BYTES - len(stderr))
                            stderr.extend(chunk[:remaining])
                            stderr_exceeded.set()
                            return
                        stderr.extend(chunk)
                except OSError:
                    writer_failed.set()
                finally:
                    process.stderr.close()

            writer = Thread(target=stream_archive, name="brain-git-archive", daemon=True)
            error_reader = Thread(target=stream_error, name="brain-git-archive-error", daemon=True)
            writer.start()
            error_reader.start()
            deadline = time.monotonic() + max(0.1, timeout)
            while process.poll() is None:
                if (
                    exceeded.wait(0.01) or stderr_exceeded.is_set()
                    or writer_failed.is_set() or time.monotonic() >= deadline
                ):
                    terminate_process_tree(process, graceful_timeout=0)
                    break
            returncode = process.wait()
            writer.join(timeout=2)
            error_reader.join(timeout=2)
            if exceeded.is_set() or stderr_exceeded.is_set():
                returncode = 125
            elif writer_failed.is_set() or time.monotonic() >= deadline and returncode != 0:
                returncode = 124
        except OSError:
            returncode = 124
            stderr = bytearray()
    return subprocess.CompletedProcess(command, returncode, b"", bytes(stderr))


def _ssh_endpoint(remote: str | None) -> str | None:
    if not remote:
        return None
    parsed = urlsplit(remote)
    if parsed.scheme in {"ssh", "git+ssh"} and parsed.hostname:
        user = f"{parsed.username}@" if parsed.username else ""
        port = f":{parsed.port}" if parsed.port else ""
        return f"{user}{parsed.hostname.lower()}{port}"
    if "://" in remote or re.match(r"^[A-Za-z]:[\\/]", remote):
        return None
    match = re.match(r"^(?:(?P<user>[^@/:]+)@)?(?P<host>[^/:]+):.+$", remote)
    if not match:
        return None
    user = f"{match.group('user')}@" if match.group("user") else ""
    return f"{user}{match.group('host').lower()}"


def _ssh_auth_failed(result: subprocess.CompletedProcess) -> bool:
    error = (result.stderr or "").lower()
    return any(
        marker in error
        for marker in (
            "permission denied (publickey",
            "incorrect passphrase supplied",
            "authentication failed",
            "no supported authentication methods",
            "host key verification failed",
        )
    )


def _has_ref(repo: Repository, ref: str) -> bool:
    return _git(repo, "rev-parse", "--verify", "--quiet", ref).returncode == 0


def _configured_ref(repo: Repository, branch: str, *, allow_local: bool = True) -> str | None:
    value = branch.strip()
    if not value:
        return None
    if value == "HEAD" or value.startswith("refs/"):
        candidates = [value]
    elif value.startswith("origin/"):
        candidates = [value]
        if allow_local:
            candidates.append(f"refs/heads/{value.removeprefix('origin/')}")
    else:
        candidates = [f"origin/{value}"]
        if allow_local:
            candidates.append(f"refs/heads/{value}")
    return next((candidate for candidate in candidates if _has_ref(repo, candidate)), None)


def _remote_branch_name(branch: str | None) -> str | None:
    value = (branch or "").strip()
    for prefix in ("refs/remotes/origin/", "refs/heads/", "origin/"):
        if value.startswith(prefix):
            value = value.removeprefix(prefix)
            break
    return value if value and value != "HEAD" and not value.startswith("refs/") else None


def _remote_ref(
    repo: Repository,
    branch_priority: list[str],
    branch_override: str | None = None,
) -> tuple[str | None, str | None]:
    requested = branch_override or repo.branch
    if requested:
        selected = _configured_ref(repo, requested)
        if selected:
            local_warning = (
                f"requested branch {requested!r} exists only locally; remote freshness is unverified"
                if selected.startswith("refs/heads/")
                else None
            )
            return selected, local_warning
        warning = f"requested branch {requested!r} is unavailable; used the automatic branch policy"
    else:
        warning = None

    for branch in branch_priority:
        candidate = _configured_ref(repo, branch, allow_local=False)
        if candidate:
            return candidate, warning

    symbolic = _git_text(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if symbolic and _has_ref(repo, symbolic):
        return symbolic, warning
    for candidate in ("origin/main", "origin/master"):
        if _has_ref(repo, candidate):
            return candidate, warning
    upstream = _git_text(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if upstream and upstream.startswith("origin/") and _has_ref(repo, upstream):
        return upstream, warning
    return None, warning


def _safe_component(value: str) -> str:
    return filesystem_component(value)


def _snapshot_seal_path(parent: Path, sha: str) -> Path:
    return parent / f".{filesystem_component(sha)}.brain-snapshot.json"


class _SnapshotScanBudget:
    def __init__(self, max_items: int, deadline: float) -> None:
        self.remaining = max(0, max_items)
        self.deadline = deadline

    def consume(self) -> None:
        if self.remaining <= 0:
            raise ValueError("immutable snapshot refresh exceeded its global item limit")
        if time.monotonic() >= self.deadline:
            raise TimeoutError("immutable snapshot refresh exceeded its global time limit")
        self.remaining -= 1


def _snapshot_seal(
    target: Path,
    sha: str,
    *,
    max_items: int = MAX_GIT_SNAPSHOT_ITEMS,
    deadline: float | None = None,
    max_bytes: int | None = None,
    scan_budget: _SnapshotScanBudget | None = None,
) -> dict[str, object]:
    deadline = deadline if deadline is not None else time.monotonic() + MAX_GIT_SNAPSHOT_STAGE_SECONDS
    max_bytes = MAX_GIT_SNAPSHOT_SEAL_BYTES if max_bytes is None else max_bytes
    files: dict[str, dict[str, object]] = {}
    projected_bytes = len(json.dumps({"version": 3, "sha": sha, "files": {}}, separators=(",", ":")).encode("utf-8"))
    for item_count, path in enumerate(target.rglob("*"), 1):
        if scan_budget is not None:
            scan_budget.consume()
        if item_count > max_items:
            raise ValueError("immutable snapshot exceeds its item limit")
        if time.monotonic() >= deadline:
            raise TimeoutError("immutable snapshot seal exceeded its time limit")
        if path.is_symlink():
            raise ValueError("immutable snapshot contains a symbolic link")
        if not path.is_file():
            continue
        metadata = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                if time.monotonic() >= deadline:
                    raise TimeoutError("immutable snapshot seal exceeded its time limit")
                digest.update(chunk)
        relative = path.relative_to(target).as_posix()
        details = {
            "size": metadata.st_size,
            "mtime_ns": metadata.st_mtime_ns,
            "ctime_ns": metadata.st_ctime_ns,
            "inode": metadata.st_ino,
            "device": metadata.st_dev,
            "mode": stat.S_IMODE(metadata.st_mode),
            "sha256": digest.hexdigest(),
        }
        projected_bytes += len(json.dumps(relative).encode("utf-8")) + len(
            json.dumps(details, separators=(",", ":")).encode("utf-8")
        ) + 2
        if projected_bytes > max_bytes:
            raise ValueError("immutable snapshot seal exceeds its byte limit")
        files[relative] = details
    seal = {"version": 3, "sha": sha, "files": files}
    if len(json.dumps(seal, sort_keys=True).encode("utf-8")) > max_bytes:
        raise ValueError("immutable snapshot seal exceeds its byte limit")
    return seal


def _snapshot_metadata(
    target: Path,
    *,
    max_items: int = MAX_GIT_SNAPSHOT_ITEMS,
    deadline: float | None = None,
    scan_budget: _SnapshotScanBudget | None = None,
) -> dict[str, dict[str, object]]:
    """Verify immutable snapshot membership/identity without rereading all source bytes."""
    deadline = deadline if deadline is not None else time.monotonic() + MAX_GIT_SNAPSHOT_STAGE_SECONDS
    files: dict[str, dict[str, object]] = {}
    for item_count, path in enumerate(target.rglob("*"), 1):
        if scan_budget is not None:
            scan_budget.consume()
        if item_count > max_items:
            raise ValueError("immutable snapshot exceeds its item limit")
        if time.monotonic() >= deadline:
            raise TimeoutError("immutable snapshot metadata exceeded its time limit")
        if path.is_symlink():
            raise ValueError("immutable snapshot contains a symbolic link")
        if not path.is_file():
            continue
        metadata = path.stat()
        files[path.relative_to(target).as_posix()] = {
            "size": metadata.st_size,
            "mtime_ns": metadata.st_mtime_ns,
            "ctime_ns": metadata.st_ctime_ns,
            "inode": metadata.st_ino,
            "device": metadata.st_dev,
            "mode": stat.S_IMODE(metadata.st_mode),
        }
    return files


def _sealed_snapshot_is_intact(
    target: Path,
    seal_path: Path,
    sha: str,
    *,
    max_items: int = MAX_GIT_SNAPSHOT_ITEMS,
    timeout_seconds: float = MAX_GIT_SNAPSHOT_STAGE_SECONDS,
    scan_budget: _SnapshotScanBudget | None = None,
) -> bool:
    if not target.is_dir() or target.is_symlink() or not seal_path.is_file():
        return False
    try:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        if seal_path.is_symlink():
            return False
        with seal_path.open("rb") as source:
            raw = source.read(MAX_GIT_SNAPSHOT_SEAL_BYTES + 1)
        if len(raw) > MAX_GIT_SNAPSHOT_SEAL_BYTES:
            return False
        expected = json.loads(raw.decode("utf-8"))
        expected_files = expected.get("files") if isinstance(expected, dict) else None
        expected_metadata = {
            str(path): {key: value for key, value in details.items() if key != "sha256"}
            for path, details in (expected_files or {}).items()
            if isinstance(details, dict)
        }
        metadata = _snapshot_metadata(
            target, max_items=max_items, deadline=deadline, scan_budget=scan_budget,
        )
        metadata_matches = (
            expected.get("version") == 3 and expected.get("sha") == sha
            and isinstance(expected_files, dict)
            and len(expected_metadata) == len(expected_files)
            and expected_metadata == metadata
        )
        if not metadata_matches:
            return False
        seal_stat = seal_path.stat()
        cache_key = (
            str(target.resolve()), seal_stat.st_dev, seal_stat.st_ino, seal_stat.st_size,
            seal_stat.st_mtime_ns, seal_stat.st_ctime_ns,
            json.dumps(metadata, sort_keys=True, separators=(",", ":")),
        )
        # On POSIX, ctime makes a same-size/restored-mtime mutation observable.
        # Windows ctime is creation time, so it must recheck content hashes.
        if os.name != "nt" and cache_key in _SNAPSHOT_SEAL_CACHE:
            return True
        valid = expected == _snapshot_seal(
            target, sha, max_items=max_items, deadline=deadline, scan_budget=scan_budget,
        )
        if valid and os.name != "nt":
            if len(_SNAPSHOT_SEAL_CACHE) >= 2_048:
                _SNAPSHOT_SEAL_CACHE.clear()
            _SNAPSHOT_SEAL_CACHE.add(cache_key)
        return valid
    except (AttributeError, OSError, TimeoutError, TypeError, ValueError, json.JSONDecodeError):
        return False


_WINDOWS_RESERVED_COMPONENTS = {
    "con", "prn", "aux", "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


def _windows_archive_key(relative: PurePosixPath) -> str:
    normalized: list[str] = []
    for component in relative.parts:
        if (
            not component or component in {".", ".."}
            or component.endswith((" ", "."))
            or any(ord(character) < 32 or character in '<>:"/\\|?*' for character in component)
        ):
            raise ValueError("Git archive path is invalid on Windows")
        canonical = unicodedata.normalize("NFC", component).casefold()
        if canonical.split(".", 1)[0] in _WINDOWS_RESERVED_COMPONENTS:
            raise ValueError("Git archive path uses a reserved Windows device name")
        normalized.append(canonical)
    if not normalized:
        raise ValueError("Git archive path is empty")
    return "/".join(normalized)


def _export_snapshot(
    repo: Repository,
    ref: str,
    sha: str,
    state_dir: Path,
    *,
    max_total_bytes: int | None = None,
    max_items: int = MAX_GIT_SNAPSHOT_ITEMS,
    timeout_seconds: float = MAX_GIT_SNAPSHOT_STAGE_SECONDS,
    windows: bool | None = None,
    scan_budget: _SnapshotScanBudget | None = None,
    accounting: dict[str, int | bool] | None = None,
) -> Path | None:
    if accounting is not None:
        accounting.update({"reused": False, "written_bytes": 0, "items": 0})
    is_windows = os.name == "nt" if windows is None else windows
    parent = (state_dir / "snapshots" / _safe_component(repo.name)).resolve()
    target = parent / filesystem_component(sha)
    if not target.resolve().is_relative_to(state_dir.resolve()):
        return None
    seal_path = _snapshot_seal_path(parent, sha)
    if _sealed_snapshot_is_intact(
        target, seal_path, sha, max_items=max_items, timeout_seconds=timeout_seconds,
        scan_budget=scan_budget,
    ):
        if accounting is not None:
            accounting["reused"] = True
        return target
    parent.mkdir(parents=True, exist_ok=True)
    archive_fd, archive_name = tempfile.mkstemp(prefix="git-archive-", suffix=".tar", dir=parent)
    os.close(archive_fd)
    archive_path = Path(archive_name)
    archive_limit = max_total_bytes // 2 if max_total_bytes is not None else None
    # The selected ref may move after rev-parse (for example during a concurrent
    # fetch). Archive the already-resolved commit so the directory label, seal,
    # and exported bytes always share one immutable identity.
    archive = _git_archive_to_path(repo, sha, archive_path, max_bytes=archive_limit)
    if archive.returncode != 0:
        archive_path.unlink(missing_ok=True)
        return None
    archive_bytes = archive_path.stat().st_size
    temporary = Path(tempfile.mkdtemp(prefix="snapshot-", dir=parent))
    try:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        with tarfile.open(archive_path, mode="r:") as source:
            windows_members: set[str] = set()
            extracted_bytes = 0
            for member_count, member in enumerate(source, 1):
                if scan_budget is not None:
                    scan_budget.consume()
                if member_count > max_items:
                    raise ValueError("Git snapshot archive exceeds its item limit")
                if time.monotonic() >= deadline:
                    raise TimeoutError("Git snapshot extraction exceeded its time limit")
                relative = PurePosixPath(member.name)
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("unsafe Git archive path")
                if is_windows:
                    member_key = _windows_archive_key(relative)
                    if member_key in windows_members:
                        raise ValueError("Git archive has paths that collide on Windows")
                    windows_members.add(member_key)
                destination = (temporary / Path(*relative.parts)).resolve()
                if not destination.is_relative_to(temporary.resolve()):
                    raise ValueError("unsafe Git archive destination")
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    if member.size < 0 or (
                        max_total_bytes is not None
                        and archive_bytes + extracted_bytes + member.size > max_total_bytes
                    ):
                        raise ValueError("Git snapshot exceeds the managed write-capacity limit")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    extracted = source.extractfile(member)
                    if extracted is None:
                        raise ValueError("missing Git archive member")
                    with destination.open("wb") as handle:
                        while chunk := extracted.read(1024 * 1024):
                            if time.monotonic() >= deadline:
                                raise TimeoutError("Git snapshot extraction exceeded its time limit")
                            handle.write(chunk)
                            extracted_bytes += len(chunk)
                    destination.chmod(0o555 if member.mode & stat.S_IXUSR else 0o444)
                # Git trees contain regular files/directories. Links and device
                # entries are intentionally ignored rather than materialized.
        seal = _snapshot_seal(
            temporary, sha, max_items=max_items, deadline=deadline, scan_budget=scan_budget,
        )
        seal_json = json.dumps(seal, sort_keys=True)
        seal_bytes = len(seal_json.encode("utf-8"))
        if seal_bytes > MAX_GIT_SNAPSHOT_SEAL_BYTES:
            raise ValueError("immutable snapshot seal exceeds its byte limit")
        if max_total_bytes is not None and archive_bytes + extracted_bytes + seal_bytes > max_total_bytes:
            raise ValueError("Git snapshot exceeds the managed write-capacity limit")
        backup: Path | None = None
        if target.exists() or target.is_symlink():
            backup = Path(tempfile.mkdtemp(prefix=f".{sha}.stale-", dir=parent))
            backup.rmdir()
            target.rename(backup)
        try:
            temporary.rename(target)
            atomic_managed_text_write(state_dir, seal_path, seal_json)
        except Exception:
            if target.exists():
                remove_tree(target, ignore_errors=True)
            if backup is not None:
                backup.rename(target)
            raise
        if backup is not None:
            remove_tree(backup, ignore_errors=True)
        if accounting is not None:
            accounting.update({
                "written_bytes": extracted_bytes + seal_bytes,
                "items": len(seal.get("files") or {}),
            })
        return target
    except (OSError, TimeoutError, tarfile.TarError, ValueError):
        return None
    finally:
        archive_path.unlink(missing_ok=True)
        if temporary.exists():
            remove_tree(temporary, ignore_errors=True)


@workspace_exclusive
def sync_repositories(
    settings: Settings,
    *,
    fetch: bool = True,
    branch_overrides: dict[str, str] | None = None,
) -> list[SyncResult]:
    """Fetch remotes and create immutable source snapshots without touching branches."""
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    from .ops import remaining_write_capacity

    # One exact workspace scan supplies a serialized shared budget.  Scanning
    # the complete state once per repository made refresh cost grow as
    # repositories x retained generations.
    snapshot_capacity = remaining_write_capacity(settings)
    control_parent = "/tmp" if os.name != "nt" and Path("/tmp").is_dir() else None
    control_directory = tempfile.TemporaryDirectory(prefix="brain-ssh-", dir=control_parent)
    control_path = Path(control_directory.name) / "%C"
    keychain_option = " -o UseKeychain=no" if sys.platform == "darwin" else ""
    endpoint_locks: dict[str, Lock] = {}
    snapshot_export_lock = Lock()
    snapshot_scan_budget = _SnapshotScanBudget(
        MAX_GIT_REFRESH_SCAN_ITEMS,
        time.monotonic() + MAX_GIT_REFRESH_SCAN_SECONDS,
    )
    endpoint_attempted: set[str] = set()
    endpoint_auth_failed: set[str] = set()

    for repo in settings.repositories:
        endpoint = _ssh_endpoint(_git_text(repo, "remote", "get-url", "origin"))
        if endpoint:
            endpoint_locks.setdefault(endpoint, Lock())

    def openssh_command(repo: Repository) -> str | None:
        if os.name == "nt":
            return None
        command = (
            _git_text(repo, "config", "--get", "core.sshCommand")
            or os.environ.get("GIT_SSH_COMMAND")
            or (shlex.quote(os.environ["GIT_SSH"]) if os.environ.get("GIT_SSH") else None)
            or "ssh"
        )
        try:
            executable = Path(shlex.split(command)[0]).name.lower()
        except (ValueError, IndexError):
            return None
        if executable not in {"ssh", "ssh.exe"}:
            return None
        return (
            f"{command} -o ControlMaster=auto -o ControlPersist=10 "
            f"-o ControlPath={shlex.quote(str(control_path))}{keychain_option}"
        )

    def fetch_origin(
        repo: Repository,
        endpoint: str | None,
        requested_branch: str | None,
    ) -> tuple[subprocess.CompletedProcess | None, bool]:
        fetch_args = ["fetch", "--prune", "--quiet", "origin"]
        current_ref, _ = _remote_ref(repo, settings.branch_priority, requested_branch)
        remote_branch = _remote_branch_name(requested_branch) or _remote_branch_name(current_ref)
        # A local/bare origin can be interrogated without a second network/auth
        # round trip, so honour the configured development-branch priority while
        # still fetching only one branch.
        remote_url = _git_text(repo, "remote", "get-url", "origin")
        if not requested_branch and remote_url and Path(remote_url).expanduser().exists():
            for branch in settings.branch_priority:
                probe = _git(repo, "ls-remote", "--heads", "origin", branch, timeout=30)
                if probe.returncode == 0 and str(probe.stdout).strip():
                    remote_branch = branch
                    break
        if settings.sync_fetch_scope == "all-branches":
            remote_branch = None
        if remote_branch:
            fetch_args.append(f"+refs/heads/{remote_branch}:refs/remotes/origin/{remote_branch}")
        if not endpoint:
            return _git(repo, *fetch_args, timeout=300), False
        lock = endpoint_locks[endpoint]
        ssh_command = openssh_command(repo)
        with lock:
            if endpoint in endpoint_auth_failed or (not ssh_command and endpoint in endpoint_attempted):
                return None, True
            if endpoint not in endpoint_attempted:
                endpoint_attempted.add(endpoint)
                result = _git(
                    repo,
                    *fetch_args,
                    extra_env={"GIT_SSH_COMMAND": ssh_command} if ssh_command else None,
                )
                if _ssh_auth_failed(result):
                    endpoint_auth_failed.add(endpoint)
                return result, False
        return _git(
            repo,
            *fetch_args,
            extra_env={"GIT_SSH_COMMAND": f"{ssh_command} -o BatchMode=yes"},
            timeout=300,
        ), False

    def sync_one(repo: Repository) -> SyncResult:
        nonlocal snapshot_capacity
        if _git(repo, "rev-parse", "--git-dir").returncode != 0:
            repo.source_path = None
            repo.source_ref = None
            repo.source_sha = None
            repo.source_status = "non-git"
            repo.source_fetched = False
            repo.source_warning = "searched directly; no remote freshness check"
            return SyncResult(repo.name, "non-git", None, None, None, False, "searched directly; no remote freshness check")

        remotes = (_git_text(repo, "remote") or "").splitlines()
        has_origin = "origin" in remotes
        fetched = False
        warning: str | None = None
        requested_branch = (branch_overrides or {}).get(repo.name) or repo.branch
        if fetch and has_origin:
            endpoint = _ssh_endpoint(_git_text(repo, "remote", "get-url", "origin"))
            fetch_result, skipped_auth = fetch_origin(repo, endpoint, requested_branch)
            fetched = fetch_result is not None and fetch_result.returncode == 0
            if skipped_auth:
                warning = (
                    "SSH already prompted once for this endpoint; skipped another interactive attempt and used the "
                    "newest locally available ref (load the key into a memory-only ssh-agent, then run brain sync)"
                )
            elif not fetched:
                reason = "timed out" if fetch_result.returncode == 124 else f"failed (exit {fetch_result.returncode})"
                warning = f"git fetch {reason}; using the newest locally available ref"
        elif fetch and not has_origin:
            warning = "no origin remote; using local HEAD"

        ref, branch_warning = (
            _remote_ref(repo, settings.branch_priority, (branch_overrides or {}).get(repo.name))
            if has_origin
            else (None, None)
        )
        if branch_warning:
            warning = f"{warning}; {branch_warning}" if warning else branch_warning
        ref = ref or "HEAD"
        sha = _git_text(repo, "rev-parse", "--verify", ref)
        if sha:
            # Capacity is calculated against exact on-disk state. Serialize
            # staging so concurrent repositories cannot reserve the same bytes.
            with snapshot_export_lock:
                accounting: dict[str, int | bool] = {}
                snapshot = _export_snapshot(
                    repo, ref, sha, settings.state_dir,
                    max_total_bytes=snapshot_capacity,
                    scan_budget=snapshot_scan_budget,
                    accounting=accounting,
                )
                if snapshot is not None:
                    snapshot_capacity = max(
                        0, snapshot_capacity - int(accounting.get("written_bytes") or 0),
                    )
        else:
            snapshot = None
        if snapshot:
            status = "current" if (fetched or not fetch or not has_origin) else "fetch-failed"
            repo.source_path = snapshot
            repo.source_ref = ref
            repo.source_sha = sha
            repo.source_status = status
            repo.source_fetched = fetched
            repo.source_warning = warning
        else:
            status = "working-tree-fallback"
            repo.source_path = None
            repo.source_ref = ref if sha else None
            repo.source_sha = sha
            repo.source_status = status
            repo.source_fetched = fetched
            warning = warning or "could not create a commit snapshot; searching the working tree"
            repo.source_warning = warning
        return SyncResult(repo.name, status, repo.source_ref, sha, str(snapshot) if snapshot else None, fetched, warning)

    try:
        workers = min(8, max(1, len(settings.repositories)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="brain-sync") as executor:
            results = list(executor.map(sync_one, settings.repositories))
    finally:
        control_directory.cleanup()

    state = {
        result.repo: {
            **asdict(result),
            "synced_at": datetime.now(UTC).isoformat(),
        }
        for result in results
    }
    target = settings.state_dir / "sources.json"
    atomic_managed_text_write(settings.state_dir, target, json.dumps(state, indent=2) + "\n")
    return results

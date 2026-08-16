from __future__ import annotations

import json
import io
import os
import re
import shlex
import stat
import subprocess
import tarfile
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from threading import Lock
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from .core import Repository, Settings


@dataclass
class SyncResult:
    repo: str
    status: str
    ref: str | None
    sha: str | None
    snapshot: str | None
    fetched: bool
    warning: str | None = None


def _git(
    repo: Repository,
    *args: str,
    binary: bool = False,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    environment = os.environ.copy()
    # HTTP credential prompts stay disabled. SSH authentication is handled by
    # the user's ssh process and may ask on its controlling terminal.
    environment["GIT_TERMINAL_PROMPT"] = "0"
    if extra_env:
        environment.update(extra_env)
    command = ["git", *args]
    try:
        return subprocess.run(
            command,
            cwd=repo.path,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=not binary,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        empty = b"" if binary else ""
        return subprocess.CompletedProcess(command, 124, empty, empty)


def _git_text(repo: Repository, *args: str) -> str | None:
    result = _git(repo, *args)
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


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


def _remote_ref(repo: Repository) -> str | None:
    symbolic = _git_text(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if symbolic:
        return symbolic
    upstream = _git_text(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if upstream and upstream.startswith("origin/"):
        return upstream
    for candidate in ("origin/main", "origin/master"):
        if _git(repo, "rev-parse", "--verify", "--quiet", candidate).returncode == 0:
            return candidate
    return None


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    return cleaned or "repo"


def _export_snapshot(repo: Repository, ref: str, sha: str, state_dir: Path) -> Path | None:
    parent = (state_dir / "snapshots" / _safe_component(repo.name)).resolve()
    target = (parent / sha).resolve()
    if not target.is_relative_to(state_dir.resolve()):
        return None
    if target.is_dir():
        return target
    parent.mkdir(parents=True, exist_ok=True)
    archive = _git(repo, "archive", "--format=tar", ref, binary=True)
    if archive.returncode != 0:
        return None
    temporary = Path(tempfile.mkdtemp(prefix="snapshot-", dir=parent))
    try:
        with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as source:
            for member in source.getmembers():
                relative = PurePosixPath(member.name)
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("unsafe Git archive path")
                destination = (temporary / Path(*relative.parts)).resolve()
                if not destination.is_relative_to(temporary.resolve()):
                    raise ValueError("unsafe Git archive destination")
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    extracted = source.extractfile(member)
                    if extracted is None:
                        raise ValueError("missing Git archive member")
                    with destination.open("wb") as handle:
                        while chunk := extracted.read(1024 * 1024):
                            handle.write(chunk)
                    destination.chmod(0o755 if member.mode & stat.S_IXUSR else 0o644)
                # Git trees contain regular files/directories. Links and device
                # entries are intentionally ignored rather than materialized.
        temporary.rename(target)
        return target
    except (OSError, tarfile.TarError, ValueError):
        return None
    finally:
        if temporary.exists():
            import shutil

            shutil.rmtree(temporary, ignore_errors=True)


def sync_repositories(settings: Settings, *, fetch: bool = True) -> list[SyncResult]:
    """Fetch remotes and create immutable source snapshots without touching branches."""
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    control_parent = "/tmp" if os.name != "nt" and Path("/tmp").is_dir() else None
    control_directory = tempfile.TemporaryDirectory(prefix="brain-ssh-", dir=control_parent)
    control_path = Path(control_directory.name) / "%C"
    can_multiplex = os.name != "nt" and not os.environ.get("GIT_SSH_COMMAND") and not os.environ.get("GIT_SSH")
    ssh_command = (
        "ssh -o ControlMaster=auto -o ControlPersist=10 "
        f"-o ControlPath={shlex.quote(str(control_path))}"
        if can_multiplex
        else None
    )
    endpoint_locks: dict[str, Lock] = {}
    endpoint_ready: set[str] = set()
    endpoint_auth_failed: set[str] = set()

    for repo in settings.repositories:
        endpoint = _ssh_endpoint(_git_text(repo, "remote", "get-url", "origin"))
        if endpoint:
            endpoint_locks.setdefault(endpoint, Lock())

    def fetch_origin(repo: Repository, endpoint: str | None) -> tuple[subprocess.CompletedProcess | None, bool]:
        if not endpoint or not ssh_command or _git_text(repo, "config", "--get", "core.sshCommand"):
            return _git(repo, "fetch", "--prune", "--quiet", "origin"), False
        lock = endpoint_locks[endpoint]
        with lock:
            if endpoint in endpoint_auth_failed:
                return None, True
            if endpoint not in endpoint_ready:
                result = _git(
                    repo,
                    "fetch",
                    "--prune",
                    "--quiet",
                    "origin",
                    extra_env={"GIT_SSH_COMMAND": ssh_command},
                )
                if _ssh_auth_failed(result):
                    endpoint_auth_failed.add(endpoint)
                else:
                    endpoint_ready.add(endpoint)
                return result, False
        return _git(
            repo,
            "fetch",
            "--prune",
            "--quiet",
            "origin",
            extra_env={"GIT_SSH_COMMAND": ssh_command},
        ), False

    def sync_one(repo: Repository) -> SyncResult:
        if _git(repo, "rev-parse", "--git-dir").returncode != 0:
            repo.source_path = None
            repo.source_ref = None
            repo.source_sha = None
            repo.source_status = "non-git"
            return SyncResult(repo.name, "non-git", None, None, None, False, "searched directly; no remote freshness check")

        remotes = (_git_text(repo, "remote") or "").splitlines()
        has_origin = "origin" in remotes
        fetched = False
        warning: str | None = None
        if fetch and has_origin:
            endpoint = _ssh_endpoint(_git_text(repo, "remote", "get-url", "origin"))
            fetch_result, skipped_auth = fetch_origin(repo, endpoint)
            fetched = fetch_result is not None and fetch_result.returncode == 0
            if skipped_auth:
                warning = (
                    f"SSH authentication already failed for {endpoint}; skipped another password prompt and used "
                    "the newest locally available ref (unlock the key with ssh-add, then run brain sync)"
                )
            elif not fetched:
                warning = f"git fetch failed (exit {fetch_result.returncode}); using the newest locally available ref"
        elif fetch and not has_origin:
            warning = "no origin remote; using local HEAD"

        ref = _remote_ref(repo) if has_origin else None
        ref = ref or "HEAD"
        sha = _git_text(repo, "rev-parse", "--verify", ref)
        snapshot = _export_snapshot(repo, ref, sha, settings.state_dir) if sha else None
        if snapshot:
            status = "current" if (fetched or not fetch or not has_origin) else "fetch-failed"
            repo.source_path = snapshot
            repo.source_ref = ref
            repo.source_sha = sha
            repo.source_status = status
        else:
            status = "working-tree-fallback"
            repo.source_path = None
            repo.source_ref = ref if sha else None
            repo.source_sha = sha
            repo.source_status = status
            warning = warning or "could not create a commit snapshot; searching the working tree"
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
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return results

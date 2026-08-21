from __future__ import annotations

import json
import io
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
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
        process = subprocess.Popen(
            command,
            cwd=repo.path,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=not binary,
            start_new_session=os.name != "nt",
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                if os.name == "nt":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
            return subprocess.CompletedProcess(command, 124, stdout, stderr)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    except OSError:
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


def sync_repositories(
    settings: Settings,
    *,
    fetch: bool = True,
    branch_overrides: dict[str, str] | None = None,
) -> list[SyncResult]:
    """Fetch remotes and create immutable source snapshots without touching branches."""
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    control_parent = "/tmp" if os.name != "nt" and Path("/tmp").is_dir() else None
    control_directory = tempfile.TemporaryDirectory(prefix="brain-ssh-", dir=control_parent)
    control_path = Path(control_directory.name) / "%C"
    keychain_option = " -o UseKeychain=no" if sys.platform == "darwin" else ""
    endpoint_locks: dict[str, Lock] = {}
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
        snapshot = _export_snapshot(repo, ref, sha, settings.state_dir) if sha else None
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
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return results

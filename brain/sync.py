from __future__ import annotations

import json
import io
import os
import re
import stat
import subprocess
import tarfile
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

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


def _git(repo: Repository, *args: str, binary: bool = False) -> subprocess.CompletedProcess:
    environment = os.environ.copy()
    # Use the user's configured Git credential helper, but never stop for a
    # terminal password or ask Project Brain to hold credentials.
    environment["GIT_TERMINAL_PROMPT"] = "0"
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
            fetch_result = _git(repo, "fetch", "--prune", "--quiet", "origin")
            fetched = fetch_result.returncode == 0
            if not fetched:
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

    workers = min(8, max(1, len(settings.repositories)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="brain-sync") as executor:
        results = list(executor.map(sync_one, settings.repositories))

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

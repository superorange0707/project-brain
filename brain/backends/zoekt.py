from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..platforms import (
    adjacent_executable,
    filesystem_component,
    logical_path,
    read_managed_text,
    remove_tree,
    run_bounded_process,
    start_managed_process,
    terminate_process_tree,
    trusted_path_executable,
)

if TYPE_CHECKING:
    from ..core import Repository, Settings

SHARD_MANIFEST_VERSION = 3
SHARD_PATH_IDENTITY = "repo-relative-posix-v1"
_ZOEKT_BUILD_WORKERS = 2
_ZOEKT_SEARCH_TIMEOUT_SECONDS = 10.0
_ZOEKT_MAX_RAW_OUTPUT_BYTES = 8_000_000
_ZOEKT_MAX_JSONL_LINE_BYTES = 1_000_000
_ZOEKT_SOURCE_SCAN_ITEMS = 500_000
_ZOEKT_SOURCE_SCAN_SECONDS = 2.0
_ZOEKT_MIN_PROJECTED_SHARD_BYTES = 64 * 1024 * 1024
_ZOEKT_MAX_SHARDS = 256
_ZOEKT_SHARD_DIRECTORY_ITEMS = 1_024
_ZOEKT_SHARD_SCAN_SECONDS = 10.0
_ZOEKT_MAX_SHARD_BYTES = 64 * 1024 * 1024 * 1024
_SHARD_VALIDATION_CACHE: dict[tuple[object, ...], bool] = {}


@dataclass(frozen=True)
class ZoektStatus:
    available: bool
    executable: str | None
    indexer: str | None
    reason: str | None = None


def status() -> ZoektStatus:
    adjacent = adjacent_executable("zoekt")
    adjacent_indexer = adjacent_executable("zoekt-index")
    if adjacent and adjacent_indexer:
        executable = str(adjacent)
        indexer = str(adjacent_indexer)
    else:
        executable = str(trusted_path_executable("zoekt") or "") or None
        indexer = str(trusted_path_executable("zoekt-index") or "") or None
    if executable and indexer:
        return ZoektStatus(True, executable, indexer)
    missing = ", ".join(name for name, value in (("zoekt", executable), ("zoekt-index", indexer)) if not value)
    return ZoektStatus(False, executable, indexer, f"Zoekt {missing} is not installed; SQLite FTS5/ripgrep fallback is active")


def shard_path(state_dir: Path, repo: str, sha: str) -> Path:
    configured_root = state_dir / "zoekt"
    state_root = state_dir.resolve()
    root = configured_root.resolve()
    if configured_root.is_symlink() or root.parent != state_root:
        raise ValueError("Zoekt shard root escapes managed state")
    repository_root = root / filesystem_component(repo)
    target = repository_root / filesystem_component(sha)
    if not target.resolve().is_relative_to(root):
        raise ValueError("Zoekt shard path escapes managed state")
    if repository_root.is_symlink() or target.is_symlink():
        raise ValueError("Zoekt shard path contains a symbolic link")
    return target


def _manifest(target: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(read_managed_text(
            target, target / "brain-shard.json", max_bytes=2 * 1024 * 1024,
        ))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _bounded_shard_paths(target: Path) -> list[Path]:
    deadline = time.monotonic() + _ZOEKT_SHARD_SCAN_SECONDS
    paths: list[Path] = []
    with os.scandir(target) as candidates:
        for number, entry in enumerate(candidates, start=1):
            if number > _ZOEKT_SHARD_DIRECTORY_ITEMS or time.monotonic() >= deadline:
                raise OSError("Zoekt shard directory exceeds its item or time limit")
            if entry.name.endswith(".zoekt"):
                paths.append(type(target)(entry.path))
                if len(paths) > _ZOEKT_MAX_SHARDS:
                    raise OSError("Zoekt shard count exceeds its limit")
    return sorted(paths)


def _shard_entries(target: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    try:
        candidates = _bounded_shard_paths(target)
        deadline = time.monotonic() + _ZOEKT_SHARD_SCAN_SECONDS
        total_bytes = 0
        for path in candidates:
            metadata = path.lstat()
            if path.is_symlink() or not path.is_file():
                continue
            size = metadata.st_size
            if size <= 0:
                continue
            total_bytes += size
            if total_bytes > _ZOEKT_MAX_SHARD_BYTES or time.monotonic() >= deadline:
                raise OSError("Zoekt shard bytes or validation time exceed their limit")
            digest = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    if time.monotonic() >= deadline:
                        raise OSError("Zoekt shard validation time exceeds its limit")
                    digest.update(chunk)
            entries.append({"name": path.name, "size": size, "sha256": digest.hexdigest()})
    except OSError:
        return []
    return entries


def valid_shard_manifest(target: Path, source_sha: str) -> bool:
    """Validate content identity once per immutable artifact-stat projection."""
    manifest = _manifest(target)
    if (
        not manifest
        or manifest.get("manifest_version") != SHARD_MANIFEST_VERSION
        or manifest.get("source_sha") != source_sha
        or manifest.get("path_identity") != SHARD_PATH_IDENTITY
    ):
        return False
    expected = manifest.get("shards")
    if not isinstance(expected, list) or not expected or len(expected) > _ZOEKT_MAX_SHARDS:
        return False
    try:
        manifest_stat = (target / "brain-shard.json").stat()
        shard_stats_list: list[tuple[object, ...]] = []
        total_bytes = 0
        for path in _bounded_shard_paths(target):
            metadata = path.lstat()
            if path.is_symlink() or not path.is_file():
                return False
            total_bytes += metadata.st_size
            if total_bytes > _ZOEKT_MAX_SHARD_BYTES:
                return False
            shard_stats_list.append((
                path.name, metadata.st_dev, metadata.st_ino, metadata.st_size,
                metadata.st_mtime_ns, metadata.st_ctime_ns,
            ))
        shard_stats = tuple(shard_stats_list)
    except OSError:
        return False
    cache_key = (
        str(target), source_sha, manifest_stat.st_dev, manifest_stat.st_ino,
        manifest_stat.st_size, manifest_stat.st_mtime_ns, manifest_stat.st_ctime_ns,
        shard_stats,
    )
    # Windows ctime is creation time and cannot prove an immutable content
    # projection after an in-place same-size rewrite with restored mtime.
    if os.name != "nt" and cache_key in _SHARD_VALIDATION_CACHE:
        return _SHARD_VALIDATION_CACHE[cache_key]
    actual = _shard_entries(target)
    valid = expected == actual
    if os.name != "nt":
        if len(_SHARD_VALIDATION_CACHE) >= 256:
            _SHARD_VALIDATION_CACHE.clear()
        _SHARD_VALIDATION_CACHE[cache_key] = valid
    return valid


def shard_manifest_identity(target: Path, source_sha: str) -> str | None:
    if not valid_shard_manifest(target, source_sha):
        return None
    manifest = _manifest(target)
    if not manifest:
        return None
    logical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(logical.encode("utf-8")).hexdigest()


def serving_shard_manifest_identity(target: Path, source_sha: str) -> str | None:
    """Validate the published manifest and shard stat projection without hot-path rehashing."""
    manifest = _manifest(target)
    if (
        not manifest
        or manifest.get("manifest_version") != SHARD_MANIFEST_VERSION
        or manifest.get("source_sha") != source_sha
        or manifest.get("path_identity") != SHARD_PATH_IDENTITY
    ):
        return None
    expected = manifest.get("shards")
    if not isinstance(expected, list) or not expected or len(expected) > _ZOEKT_MAX_SHARDS:
        return None
    try:
        actual: list[tuple[str, int]] = []
        expected_stats: list[tuple[str, int]] = []
        for item in expected:
            if not isinstance(item, dict):
                return None
            name = str(item.get("name") or "")
            size = int(item.get("size") or 0)
            digest = str(item.get("sha256") or "")
            if (
                not name or Path(name).name != name or not name.endswith(".zoekt")
                or size <= 0 or not re.fullmatch(r"[0-9a-f]{64}", digest)
            ):
                return None
            expected_stats.append((name, size))
        total_bytes = 0
        for path in _bounded_shard_paths(target):
            metadata = path.lstat()
            if path.is_symlink() or not path.is_file() or metadata.st_size <= 0:
                return None
            total_bytes += metadata.st_size
            if total_bytes > _ZOEKT_MAX_SHARD_BYTES:
                return None
            actual.append((path.name, metadata.st_size))
        if expected_stats != actual:
            return None
    except (OSError, TypeError, ValueError):
        return None
    logical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(logical.encode("utf-8")).hexdigest()


def immutable_snapshot_available(repo: Repository) -> bool:
    """Return whether Zoekt can bind its cache to an immutable source export."""
    if not repo.source_sha or not repo.source_path:
        return False
    try:
        return repo.source_path.is_dir() and repo.source_path.resolve() != repo.path.resolve()
    except OSError:
        return False


def _projected_shard_bytes(root: Path) -> int:
    deadline = time.monotonic() + _ZOEKT_SOURCE_SCAN_SECONDS
    pending = [root]
    items = 0
    source_bytes = 0
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as iterator:
            for entry in iterator:
                items += 1
                if items > _ZOEKT_SOURCE_SCAN_ITEMS or time.monotonic() >= deadline:
                    raise OSError("Zoekt source projection budget exceeded")
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(type(root)(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    source_bytes += entry.stat(follow_symlinks=False).st_size
    return max(_ZOEKT_MIN_PROJECTED_SHARD_BYTES, source_bytes * 4)


def _bounded_target_bytes(root: Path) -> int:
    """Account an old shard tree without an unbounded recursive walk."""
    deadline = time.monotonic() + _ZOEKT_SHARD_SCAN_SECONDS
    pending = [root]
    items = 0
    total = 0
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                items += 1
                if items > _ZOEKT_SHARD_DIRECTORY_ITEMS or time.monotonic() >= deadline:
                    raise OSError("Zoekt target accounting exceeds its item or time limit")
                if entry.is_symlink():
                    raise OSError("Zoekt target accounting rejects symbolic links")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(type(root)(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
                    if total > _ZOEKT_MAX_SHARD_BYTES:
                        raise OSError("Zoekt target accounting exceeds its byte limit")
    return total


def build(settings: Settings, repositories: list[Repository]) -> dict[str, dict[str, object]]:
    """Build immutable per-repository/snapshot local shards when Zoekt is installed."""
    available = status()
    if not available.available or not available.indexer or not repositories:
        return {}
    from ..ops import remaining_write_capacity

    shared_capacity = (
        remaining_write_capacity(settings)
        if hasattr(settings, "root")
        else shutil.disk_usage(settings.state_dir).free
    )
    capacity_lock = threading.Lock()

    def build_one(repo: Repository) -> tuple[str, dict[str, object]]:
        nonlocal shared_capacity
        reserved_capacity = 0
        if not immutable_snapshot_available(repo):
            return repo.name, {
                "status": "skipped",
                "reason": "immutable exported snapshot is unavailable; SQLite fallback is authoritative",
            }
        sha = repo.source_sha or "working-tree"
        target = shard_path(settings.state_dir, repo.name, sha)
        if valid_shard_manifest(target, sha):
            return repo.name, {"path": str(target), "source_sha": sha, "status": "current"}
        try:
            projected_bytes = _projected_shard_bytes(repo.scan_path)
        except OSError:
            return repo.name, {"status": "failed", "reason": "zoekt source projection exceeded its capacity budget"}
        with capacity_lock:
            if projected_bytes > shared_capacity:
                return repo.name, {"status": "failed", "reason": "zoekt shard exceeds managed write capacity"}
            shared_capacity -= projected_bytes
            reserved_capacity = projected_bytes
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f"{sha[:12]}-", dir=target.parent))
        started = time.perf_counter()
        try:
            completed = run_bounded_process(
                [available.indexer, "-index", str(temporary), str(repo.scan_path)],
                getattr(settings, "root", settings.state_dir),
                max_stdout_bytes=16 * 1024,
                max_stderr_bytes=64 * 1024,
                timeout=120,
            )
            shards = _shard_entries(temporary)
            if completed.returncode != 0 or not shards:
                return repo.name, {"status": "failed", "reason": "zoekt indexing failed"}
            manifest_bytes = json.dumps({
                "manifest_version": SHARD_MANIFEST_VERSION,
                "source_sha": sha,
                "repo": repo.name,
                "path_identity": SHARD_PATH_IDENTITY,
                "shards": shards,
            }).encode("utf-8")
            artifact_bytes = sum(int(item["size"]) for item in shards) + len(manifest_bytes)
            with capacity_lock:
                available_capacity = shared_capacity + reserved_capacity
                if artifact_bytes > available_capacity:
                    return repo.name, {"status": "failed", "reason": "zoekt shard exceeds managed write capacity"}
                previous_bytes = (
                    _bounded_target_bytes(target)
                    if target.is_dir() and not target.is_symlink() else 0
                )
                (temporary / "brain-shard.json").write_bytes(manifest_bytes)
                if target.exists():
                    remove_tree(target)
                temporary.replace(target)
                shared_capacity = max(0, available_capacity - max(0, artifact_bytes - previous_bytes))
                reserved_capacity = 0
            return repo.name, {"path": str(target), "source_sha": sha, "status": "built", "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)}
        except OSError:
            return repo.name, {"status": "failed", "reason": "zoekt indexing unavailable or timed out"}
        finally:
            if reserved_capacity:
                with capacity_lock:
                    shared_capacity += reserved_capacity
            if temporary.exists():
                remove_tree(temporary, ignore_errors=True)

    result: dict[str, dict[str, object]] = {}
    with ThreadPoolExecutor(
        max_workers=min(_ZOEKT_BUILD_WORKERS, len(repositories)),
        thread_name_prefix="brain-zoekt",
    ) as executor:
        for name, details in executor.map(build_one, repositories):
            result[name] = details
    return result


def _field(value: dict[str, object], *names: str) -> object | None:
    for name in names:
        if name in value:
            return value[name]
    return None


def _line_text(match: dict[str, object]) -> str | None:
    value = _field(match, "Line", "line_text", "text")
    if not isinstance(value, str):
        return None
    # The current Zoekt JSONL CLI serializes Go []byte line content as base64;
    # the compatibility form used by older tools/tests carries plain text.
    if "LineStart" not in match:
        return value
    try:
        return base64.b64decode(value, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _relative_result_path(repo: Repository, value: str) -> str | None:
    """Convert backend-local absolute/backslash paths to stable repo-relative IDs."""
    try:
        candidate = Path(value)
        if candidate.is_absolute():
            return logical_path(candidate.resolve().relative_to(repo.scan_path.resolve()))
    except (OSError, ValueError):
        return None
    normalized = logical_path(value)
    prefix = logical_path(repo.scan_path.name).rstrip("/") + "/"
    return normalized[len(prefix):] if normalized.startswith(prefix) else normalized


def search(
    settings: Settings,
    repo: Repository,
    pattern: str,
    *,
    fixed: bool,
    max_results: int,
    expected_manifest_hash: str | None = None,
    reserve: Callable[[], bool] | None = None,
) -> tuple[list[tuple[str, int, str, float]], dict[str, object]] | None:
    """Use an immutable local shard, or return None so Core can use its fallback."""
    available = status()
    sha = repo.source_sha or "working-tree"
    target = shard_path(settings.state_dir, repo.name, sha)
    manifest_valid = (
        serving_shard_manifest_identity(target, sha) == expected_manifest_hash
        if expected_manifest_hash is not None
        else valid_shard_manifest(target, sha)
    )
    if (
        not available.available
        or not available.executable
        or not immutable_snapshot_available(repo)
        or not manifest_valid
    ):
        return None
    if reserve is not None and not reserve():
        return None
    query = "content:" + json.dumps(pattern) if fixed else "regex:" + pattern
    started = time.perf_counter()
    try:
        process = start_managed_process(
            [available.executable, "-index_dir", str(target), "-jsonl", query],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None
    output: queue.Queue[bytes | None] = queue.Queue(maxsize=8)
    stopped = threading.Event()

    def read_output() -> None:
        def deliver(value: bytes | None) -> bool:
            while not stopped.is_set():
                try:
                    output.put(value, timeout=.05)
                    return True
                except queue.Full:
                    continue
            return False

        try:
            if process.stdout is None:
                deliver(None)
                return
            while not stopped.is_set():
                raw = process.stdout.readline(_ZOEKT_MAX_JSONL_LINE_BYTES + 1)
                if not deliver(raw or None) or not raw:
                    return
        except (OSError, ValueError):
            try:
                output.put_nowait(None)
            except queue.Full:
                pass

    reader = threading.Thread(target=read_output, name="brain-zoekt-output", daemon=True)
    reader.start()
    rows: list[tuple[str, int, str, float]] = []
    rejected_output = False
    raw_bytes = 0
    deadline = time.monotonic() + _ZOEKT_SEARCH_TIMEOUT_SECONDS
    reached_limit = False
    tree_reaped = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            rejected_output = True
            break
        try:
            raw = output.get(timeout=remaining)
        except queue.Empty:
            rejected_output = True
            break
        if raw is None:
            break
        raw_bytes += len(raw)
        if (
            len(raw) > _ZOEKT_MAX_JSONL_LINE_BYTES
            or raw_bytes > _ZOEKT_MAX_RAW_OUTPUT_BYTES
        ):
            rejected_output = True
            break
        try:
            file = json.loads(raw.decode("utf-8", errors="replace"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            rejected_output = True
            break
        if not isinstance(file, dict):
            rejected_output = True
            break
        path = _field(file, "FileName", "file_name", "path")
        matches = _field(file, "LineMatches", "line_matches")
        if not isinstance(path, str) or not isinstance(matches, list):
            rejected_output = True
            break
        relative_path = _relative_result_path(repo, path)
        if relative_path is None:
            rejected_output = True
            break
        score = float(_field(file, "Score", "score") or 0)
        for match in matches:
            if not isinstance(match, dict):
                rejected_output = True
                break
            line = _field(match, "LineNumber", "line_number", "line")
            text = _line_text(match)
            if not isinstance(line, int) or text is None:
                rejected_output = True
                break
            if fixed and pattern not in text:
                continue
            rows.append((relative_path, line, text.rstrip("\n"), score))
            if len(rows) >= max_results:
                reached_limit = True
                break
        if rejected_output:
            break
        if len(rows) >= max_results:
            break
    stopped.set()
    if reached_limit or rejected_output:
        terminate_process_tree(process, graceful_timeout=1)
        tree_reaped = True
    try:
        return_code = process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        terminate_process_tree(process, graceful_timeout=0)
        return_code = process.poll()
        if return_code is None:
            return None
    finally:
        if not tree_reaped:
            terminate_process_tree(process, graceful_timeout=0)
        if process.stdout is not None:
            process.stdout.close()
        reader.join(timeout=1)
    if rejected_output:
        return None
    if return_code and not reached_limit:
        return None
    return rows, {
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "raw_hits": len(rows), "raw_output_bytes": raw_bytes,
        "raw_output_byte_limit": _ZOEKT_MAX_RAW_OUTPUT_BYTES, "shard": str(target),
    }

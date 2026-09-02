from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from .locks import workspace_exclusive, workspace_lock_mode
from .platforms import (
    atomic_managed_text_write,
    executable_filename,
    logical_path,
    read_managed_text,
    run_bounded_process,
    trusted_path_executable,
)

if TYPE_CHECKING:
    from .core import SearchHit, Settings


BACKEND_NAME = "codebase-memory-mcp"
TESTED_BACKEND_VERSION = "0.10.5"
GRAPH_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
GRAPH_MAX_ERROR_BYTES = 64 * 1024
GRAPH_MIN_PROJECTED_CACHE_BYTES = 64 * 1024 * 1024
GRAPH_SOURCE_SCAN_ITEMS = 200_000
GRAPH_SOURCE_SCAN_SECONDS = 1.0
GRAPH_CACHE_SCAN_ITEMS = 500_000
GRAPH_CACHE_SCAN_SECONDS = 2.0


@dataclass
class GraphIndexResult:
    repo: str
    status: str
    detail: str = ""


def find_backend() -> Path | None:
    configured = os.environ.get("PROJECT_BRAIN_GRAPH_BIN")
    packaged_name = executable_filename(BACKEND_NAME)
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path(sys.executable).parent / packaged_name,
        Path(__file__).parent / "libexec" / packaged_name,
        trusted_path_executable(BACKEND_NAME),
    ]
    return next((path.resolve() for path in candidates if path and path.is_file() and os.access(path, os.X_OK)), None)


def backend_version() -> str | None:
    binary = find_backend()
    if not binary:
        return None
    try:
        result = run_bounded_process(
            [str(binary), "--version"], Path.cwd(),
            max_stdout_bytes=16 * 1024, max_stderr_bytes=16 * 1024, timeout=10,
        )
    except OSError:
        return None
    if result.returncode != 0 or getattr(result, "timed_out", False) or getattr(result, "output_truncated", False):
        return None
    match = re.search(r"\d+\.\d+\.\d+", result.stdout + result.stderr)
    return match.group(0) if match else None


def _project(settings: Settings, repo: str) -> str:
    identity = hashlib.sha256(str(settings.root).encode()).hexdigest()[:8]
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", repo).strip("-")
    repo_identity = hashlib.sha256(repo.encode("utf-8")).hexdigest()[:12]
    return f"project-brain-{identity}-{safe[:64]}-{repo_identity}"


def _managed_cache_identity(settings: Settings) -> tuple[Path, tuple[int, int]] | None:
    """Return only a direct graph-cache directory owned by managed state."""
    state = settings.state_dir
    cache = state / "codebase-memory"
    try:
        if state.is_symlink() or not state.is_dir():
            return None
        state_root = state.resolve()
        if cache.is_symlink():
            return None
        if not cache.exists():
            cache.mkdir(parents=False)
        metadata = cache.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or cache.is_symlink()
            or cache.resolve().parent != state_root
        ):
            return None
        return cache, (metadata.st_dev, metadata.st_ino)
    except OSError:
        return None


def _projected_graph_cache_bytes(repo_path: Path) -> int:
    deadline = time.monotonic() + GRAPH_SOURCE_SCAN_SECONDS
    pending = [repo_path]
    items = 0
    source_bytes = 0
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as iterator:
            for entry in iterator:
                items += 1
                if items > GRAPH_SOURCE_SCAN_ITEMS or time.monotonic() >= deadline:
                    raise OSError("graph source projection budget exceeded")
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    source_bytes += entry.stat(follow_symlinks=False).st_size
    return max(GRAPH_MIN_PROJECTED_CACHE_BYTES, source_bytes * 2)


def _safe_graph_cache_bytes(cache: Path) -> int:
    """Measure only a bounded direct-file cache tree; reject link escapes."""
    deadline = time.monotonic() + GRAPH_CACHE_SCAN_SECONDS
    pending = [cache]
    items = 0
    total = 0
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as iterator:
            for entry in iterator:
                items += 1
                if items > GRAPH_CACHE_SCAN_ITEMS or time.monotonic() >= deadline:
                    raise OSError("graph cache validation budget exceeded")
                if entry.is_symlink():
                    raise OSError("graph cache contains a symbolic link")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
                else:
                    raise OSError("graph cache contains an unsupported filesystem entry")
    return total


def _graph_json_output(output: str) -> Any | None:
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        # Some releases emit informational lines before the JSON payload.
        for line in reversed(output.splitlines()):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def _invoke(settings: Settings, tool: str, arguments: dict[str, Any], *, timeout: int = 300) -> tuple[Any | None, str | None]:
    binary = find_backend()
    if not binary:
        return None, "backend not installed"
    projected = 0
    if tool == "index_repository":
        try:
            from .ops import remaining_write_capacity

            projected = _projected_graph_cache_bytes(Path(str(arguments.get("repo_path") or "")))
        except OSError:
            return None, "graph cache capacity is unavailable"
    managed_cache = _managed_cache_identity(settings)
    if managed_cache is None:
        return None, "unsafe managed cache directory"
    cache, cache_identity = managed_cache
    temporary: Path | None = None
    invocation_cache = cache
    if tool == "index_repository":
        try:
            from .ops import remaining_write_capacity

            current_cache_bytes = _safe_graph_cache_bytes(cache)
            # The external backend writes into a complete private clone.  The
            # authoritative cache is unchanged until validation succeeds.
            if current_cache_bytes + projected > remaining_write_capacity(settings):
                return None, "graph cache exceeds managed write capacity"
            temporary = Path(tempfile.mkdtemp(prefix=".graph-cache-stage-", dir=settings.state_dir))
            invocation_cache = temporary / "codebase-memory"
            shutil.copytree(cache, invocation_cache, symlinks=True)
            _safe_graph_cache_bytes(invocation_cache)
        except OSError:
            if temporary is not None:
                shutil.rmtree(temporary, ignore_errors=True)
            return None, "graph cache capacity is unavailable"
    environment = os.environ.copy()
    environment["CBM_CACHE_DIR"] = str(invocation_cache)
    try:
        revalidated = _managed_cache_identity(settings)
        if revalidated is None or revalidated != (cache, cache_identity):
            if temporary is not None:
                shutil.rmtree(temporary, ignore_errors=True)
            return None, "unsafe managed cache directory"
        result = run_bounded_process(
            [str(binary), "cli", "--json", tool],
            settings.root,
            environment=environment,
            input_bytes=json.dumps(arguments).encode("utf-8"),
            max_stdout_bytes=GRAPH_MAX_OUTPUT_BYTES,
            max_stderr_bytes=GRAPH_MAX_ERROR_BYTES,
            timeout=float(timeout),
        )
    except OSError as exc:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)
        return None, type(exc).__name__
    if (
        result.returncode != 0
        or getattr(result, "output_truncated", False)
        or getattr(result, "timed_out", False)
    ):
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)
        return None, f"exit {result.returncode}"
    payload = _graph_json_output(result.stdout.strip())
    if payload is None:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)
        return None, "no JSON response"
    if tool == "index_repository":
        assert temporary is not None
        try:
            from .ops import ensure_write_capacity

            _safe_graph_cache_bytes(invocation_cache)
            ensure_write_capacity(settings)
        except OSError:
            shutil.rmtree(temporary, ignore_errors=True)
            return None, "graph cache exceeded managed write capacity"
        previous = temporary / "previous"
        try:
            os.replace(cache, previous)
            try:
                os.replace(invocation_cache, cache)
            except OSError:
                os.replace(previous, cache)
                raise
        except OSError:
            shutil.rmtree(temporary, ignore_errors=True)
            return None, "graph cache publication failed"
        # The second replace is the commit point.  Failure to reclaim the old
        # private cache must not report that the already-live new cache failed.
        try:
            shutil.rmtree(previous, ignore_errors=True)
        except OSError:
            pass
        shutil.rmtree(temporary, ignore_errors=True)
    return payload, None


@workspace_exclusive
def index_graph(
    settings: Settings,
    *,
    changed_only: bool = True,
    repositories: Iterable[str] | None = None,
    defer_lazy: bool = False,
) -> list[GraphIndexResult]:
    if not settings.graph_enabled:
        return [GraphIndexResult("all", "fallback", "structural graph disabled by config; lexical analysis remains active")]
    if not find_backend():
        return [GraphIndexResult("all", "fallback", f"{BACKEND_NAME} not installed; lexical analysis remains active")]
    selected_names = list(repositories or [])
    if defer_lazy and settings.graph_lazy and not selected_names:
        return [GraphIndexResult("all", "deferred", "structural repositories are indexed on first relevant symbol request")]
    state_path = settings.state_dir / "graphs.json"
    try:
        state = json.loads(read_managed_text(
            settings.state_dir, state_path, max_bytes=4 * 1024 * 1024,
        )) if state_path.is_file() else {}
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        state = {}
    results: list[GraphIndexResult] = []
    for repo in settings.repos(selected_names):
        sha = repo.source_sha or "working-tree"
        if changed_only and (state.get(repo.name) or {}).get("sha") == sha:
            results.append(GraphIndexResult(repo.name, "current", sha[:12]))
            continue
        _, error = _invoke(
            settings,
            "index_repository",
            {
                "repo_path": str(repo.scan_path),
                "name": _project(settings, repo.name),
                "mode": "fast",
                "persistence": True,
            },
        )
        if error:
            results.append(GraphIndexResult(repo.name, "fallback", error))
            continue
        state[repo.name] = {
            "sha": sha,
            "project": _project(settings, repo.name),
            "indexed_at": datetime.now(UTC).isoformat(),
            "backend": f"{BACKEND_NAME} {backend_version() or 'unknown'}",
        }
        results.append(GraphIndexResult(repo.name, "indexed", sha[:12]))
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    atomic_managed_text_write(settings.state_dir, state_path, json.dumps(state, indent=2) + "\n")
    return results


def _graph_state(settings: Settings) -> dict[str, Any]:
    path = settings.state_dir / "graphs.json"
    try:
        loaded = json.loads(read_managed_text(
            settings.state_dir, path, max_bytes=4 * 1024 * 1024,
        ))
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _objects(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)
    elif isinstance(value, str) and value[:1] in "[{":
        try:
            yield from _objects(json.loads(value))
        except json.JSONDecodeError:
            pass


def _table_objects(value: Any, columns: list[str] | None = None) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        own_columns = value.get("columns") or value.get("cols")
        active = [str(item) for item in own_columns] if isinstance(own_columns, list) else columns
        for child in value.values():
            yield from _table_objects(child, active)
    elif isinstance(value, list):
        if columns and len(value) == len(columns) and all(not isinstance(item, (dict, list)) for item in value):
            yield dict(zip(columns, value))
        else:
            for child in value:
                yield from _table_objects(child, columns)


def _hits(settings: Settings, repo_name: str, payload: Any, kind: str) -> list[SearchHit]:
    from .core import SearchHit

    repo = settings.repo(repo_name)
    hits: list[SearchHit] = []
    seen: set[tuple[str, int]] = set()
    for item in [*_objects(payload), *_table_objects(payload)]:
        raw_path = item.get("file") or item.get("path") or item.get("file_path")
        if not isinstance(raw_path, str):
            continue
        path = Path(raw_path)
        if path.is_absolute():
            try:
                path = path.relative_to(repo.scan_path)
            except ValueError:
                continue
        absolute = (repo.scan_path / path).resolve()
        if not absolute.is_relative_to(repo.scan_path.resolve()) or not absolute.is_file():
            continue
        raw_line = item.get("line") or item.get("line_start") or item.get("start_line") or item.get("lines") or 1
        match = re.search(r"\d+", str(raw_line))
        line = int(match.group(0)) if match else 1
        key = logical_path(path), line
        if key in seen:
            continue
        seen.add(key)
        label = item.get("qualified_name") or item.get("qn") or item.get("name") or item.get("label") or kind
        hits.append(SearchHit(repo_name, logical_path(path), line, str(label), kind, 110, [f"{BACKEND_NAME} structural graph"]))
    return hits


def _serves_generation(settings: Settings) -> bool:
    generation = getattr(settings, "atlas_generation", None)
    if generation is not None:
        return generation.component("structural").get("status") == "ready"
    return getattr(settings, "atlas_generation_mode", "current") != "legacy_source_pin"


def graph_symbol_hits(settings: Settings, query: str, repos: Iterable[str] | None = None) -> list[SearchHit]:
    if not _serves_generation(settings) or not settings.graph_enabled or not find_backend():
        return []
    selected_names = list(repos or [])
    if settings.graph_lazy and selected_names and workspace_lock_mode(settings) != "shared":
        index_graph(settings, changed_only=True, repositories=selected_names)
    state = _graph_state(settings)
    hits: list[SearchHit] = []
    for repo in settings.repos(selected_names):
        indexed = state.get(repo.name) or {}
        if not indexed or indexed.get("sha") != (repo.source_sha or "working-tree"):
            continue
        payload, error = _invoke(
            settings,
            "search_graph",
            {"project": indexed.get("project") or _project(settings, repo.name), "query": query, "format": "json", "limit": settings.max_results},
            timeout=60,
        )
        if not error:
            hits.extend(_hits(settings, repo.name, payload, "structural definition"))
    return hits


def graph_trace(settings: Settings, query: str, repos: Iterable[str] | None = None) -> tuple[list[SearchHit], list[str]]:
    if not _serves_generation(settings) or not settings.graph_enabled or not find_backend():
        return [], []
    selected_names = list(repos or [])
    if settings.graph_lazy and selected_names and workspace_lock_mode(settings) != "shared":
        index_graph(settings, changed_only=True, repositories=selected_names)
    state = _graph_state(settings)
    hits: list[SearchHit] = []
    relationships: list[str] = []
    for repo in settings.repos(selected_names):
        indexed = state.get(repo.name) or {}
        if not indexed or indexed.get("sha") != (repo.source_sha or "working-tree"):
            continue
        payload, error = _invoke(
            settings,
            "trace_path",
            {
                "project": indexed.get("project") or _project(settings, repo.name),
                "function_name": query,
                "direction": "both",
                "depth": 4,
                "include_tests": True,
                "include_evidence": True,
                "format": "json",
            },
            timeout=60,
        )
        if error:
            continue
        found = _hits(settings, repo.name, payload, "structural call path")
        hits.extend(found)
        relationships.extend(f"{repo.name}:{hit.path}:{hit.line}  STRUCTURALLY_RELATED  {query}" for hit in found)
    return hits, relationships

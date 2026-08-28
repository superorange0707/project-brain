from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from .locks import workspace_exclusive

if TYPE_CHECKING:
    from .core import SearchHit, Settings


BACKEND_NAME = "codebase-memory-mcp"
TESTED_BACKEND_VERSION = "0.10.5"


@dataclass
class GraphIndexResult:
    repo: str
    status: str
    detail: str = ""


def find_backend() -> Path | None:
    configured = os.environ.get("PROJECT_BRAIN_GRAPH_BIN")
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path(found) if (found := shutil.which(BACKEND_NAME)) else None,
        Path(sys.executable).parent / BACKEND_NAME,
        Path(__file__).parent / "libexec" / BACKEND_NAME,
    ]
    return next((path.resolve() for path in candidates if path and path.is_file() and os.access(path, os.X_OK)), None)


def backend_version() -> str | None:
    binary = find_backend()
    if not binary:
        return None
    try:
        result = subprocess.run([str(binary), "--version"], text=True, capture_output=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"\d+\.\d+\.\d+", result.stdout + result.stderr)
    return match.group(0) if match else None


def _project(settings: Settings, repo: str) -> str:
    identity = hashlib.sha256(str(settings.root).encode()).hexdigest()[:8]
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", repo).strip("-")
    return f"project-brain-{identity}-{safe}"


def _invoke(settings: Settings, tool: str, arguments: dict[str, Any], *, timeout: int = 300) -> tuple[Any | None, str | None]:
    binary = find_backend()
    if not binary:
        return None, "backend not installed"
    cache = settings.state_dir / "codebase-memory"
    cache.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["CBM_CACHE_DIR"] = str(cache)
    try:
        result = subprocess.run(
            [str(binary), "cli", "--json", tool],
            env=environment,
            input=json.dumps(arguments),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, type(exc).__name__
    if result.returncode != 0:
        return None, f"exit {result.returncode}"
    output = result.stdout.strip()
    try:
        return json.loads(output), None
    except json.JSONDecodeError:
        # Some releases emit informational lines before the JSON payload.
        for line in reversed(output.splitlines()):
            try:
                return json.loads(line), None
            except json.JSONDecodeError:
                continue
    return None, "no JSON response"


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
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    except (OSError, json.JSONDecodeError):
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
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return results


def _graph_state(settings: Settings) -> dict[str, Any]:
    path = settings.state_dir / "graphs.json"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, json.JSONDecodeError):
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
        key = str(path), line
        if key in seen:
            continue
        seen.add(key)
        label = item.get("qualified_name") or item.get("qn") or item.get("name") or item.get("label") or kind
        hits.append(SearchHit(repo_name, str(path), line, str(label), kind, 110, [f"{BACKEND_NAME} structural graph"]))
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
    if settings.graph_lazy and selected_names:
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
    if settings.graph_lazy and selected_names:
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

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .catalog import current_generation, diagnose, generation_root
from .editions import capabilities, current_edition

if TYPE_CHECKING:
    from .core import Settings


_GIB = 1024 ** 3


def _directory_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file()) if path.exists() else 0


def ensure_write_capacity(settings: Settings, projected_bytes: int = 0) -> None:
    """Refuse index/model writes before exceeding the configured local disk guard."""
    projected_bytes = max(0, int(projected_bytes))
    current_bytes = _directory_bytes(settings.state_dir)
    usage = shutil.disk_usage(settings.root)
    if settings.max_state_gb and current_bytes + projected_bytes > settings.max_state_gb * _GIB:
        raise OSError("Project Brain state quota would be exceeded; run 'brain gc --dry-run' or raise storage.max_state_gb")
    if settings.minimum_free_disk_gb and usage.free - projected_bytes < settings.minimum_free_disk_gb * _GIB:
        raise OSError("Project Brain free-disk guard would be breached; run 'brain gc --dry-run' or lower storage.minimum_free_disk_gb")


def freshness(settings: Settings) -> dict[str, Any]:
    from .core import git_head, load_index_state

    indexed = load_index_state(settings)
    rows = []
    for repo in settings.repositories:
        source = repo.source_sha or git_head(repo)
        index = (indexed.get(repo.name) or {}).get("sha")
        rows.append({"repo": repo.name, "source_sha": source, "index_sha": index, "current": not index or not source or index == source, "warning": repo.source_warning})
    from .backends.zoekt import status as zoekt_status

    zoekt = zoekt_status()
    return {"generation": current_generation(settings), "repositories": rows, "zoekt": {"available": zoekt.available, "reason": zoekt.reason}}


def storage(settings: Settings) -> dict[str, Any]:
    roots = [settings.state_dir, settings.runs_dir, settings.generated_dir]
    sizes = {str(path): _directory_bytes(path) for path in roots}
    usage = shutil.disk_usage(settings.root)
    return {
        "bytes": sizes,
        "total_bytes": sum(sizes.values()),
        "free_bytes": usage.free,
        "limits": {"max_state_gb": settings.max_state_gb, "minimum_free_disk_gb": settings.minimum_free_disk_gb},
        "catalog_issue": diagnose(settings),
    }


def _pinned_generations(settings: Settings) -> set[int]:
    values: set[int] = set()
    if not settings.runs_dir.is_dir():
        return values
    for session in settings.runs_dir.glob("*/session.json"):
        try:
            generation = json.loads(session.read_text(encoding="utf-8")).get("generation")
            if generation is not None:
                values.add(int(generation))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return values


def _pinned_snapshot_paths(settings: Settings) -> set[Path]:
    """Keep source snapshots named by the current source state or any ticket."""
    from .core import load_source_state

    roots: set[Path] = set()
    for item in load_source_state(settings).values():
        if isinstance(item, dict) and item.get("snapshot"):
            roots.add(Path(str(item["snapshot"])).resolve())
    for session in settings.runs_dir.glob("*/session.json") if settings.runs_dir.is_dir() else []:
        try:
            sources = json.loads(session.read_text(encoding="utf-8")).get("sources") or {}
            for item in sources.values():
                if isinstance(item, dict) and item.get("snapshot"):
                    roots.add(Path(str(item["snapshot"])).resolve())
        except (OSError, json.JSONDecodeError):
            continue
    return {path for path in roots if path.is_relative_to((settings.state_dir / "snapshots").resolve())}


def _snapshot_removals(settings: Settings, keep_recent: int) -> list[Path]:
    root = settings.state_dir / "snapshots"
    pinned = _pinned_snapshot_paths(settings)
    removable: list[Path] = []
    for repository in root.iterdir() if root.is_dir() else []:
        snapshots = sorted((path for path in repository.iterdir() if path.is_dir()), key=lambda path: path.stat().st_mtime, reverse=True)
        for snapshot in snapshots[max(1, keep_recent):]:
            if snapshot.resolve() not in pinned:
                removable.append(snapshot)
    return removable


def _semantic_shard_removals(settings: Settings) -> list[Path]:
    try:
        state = json.loads((settings.state_dir / "semantic-index.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    keep = {
        Path(str(item.get("path"))).resolve()
        for item in state.get("shards") or []
        if isinstance(item, dict) and item.get("path")
    }
    root = settings.state_dir / "semantic-shards"
    return [path for path in root.glob("*.usearch") if path.resolve() not in keep]


def gc(settings: Settings, *, dry_run: bool = True, keep_recent: int = 2) -> dict[str, Any]:
    root = generation_root(settings)
    pinned = _pinned_generations(settings)
    generations = sorted((path for path in root.glob("generation-*") if path.is_dir()), key=lambda path: path.name)
    protected = {path.name for path in generations[-max(1, keep_recent):]}
    removable = [
        path for path in generations
        if path.name not in protected and int(path.name.rsplit("-", 1)[-1]) not in pinned
    ]
    snapshot_removable = _snapshot_removals(settings, keep_recent)
    semantic_removable = _semantic_shard_removals(settings)
    targets = [("generation", path) for path in removable] + [("snapshot", path) for path in snapshot_removable] + [("semantic_shard", path) for path in semantic_removable]
    rows = [{"kind": kind, "path": str(path), "bytes": path.stat().st_size if path.is_file() else sum(item.stat().st_size for item in path.rglob("*") if item.is_file())} for kind, path in targets]
    if not dry_run:
        for _, item in targets:
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink(missing_ok=True)
    return {
        "dry_run": dry_run,
        "pinned_generations": sorted(pinned),
        "pinned_snapshots": [str(path) for path in sorted(_pinned_snapshot_paths(settings))],
        "remove": rows,
        "reclaim_bytes": sum(item["bytes"] for item in rows),
    }


def status(settings: Settings) -> dict[str, Any]:
    return {"edition": current_edition(settings), "capabilities": capabilities(settings), "freshness": freshness(settings), "storage": storage(settings)}

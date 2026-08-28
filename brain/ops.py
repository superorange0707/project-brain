from __future__ import annotations

import json
import shutil
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .catalog import current_generation, current_generation_ref, diagnose, generation_root, generations as catalog_generations
from .editions import capabilities, current_edition
from .locks import workspace_exclusive

if TYPE_CHECKING:
    from .core import Settings


_GIB = 1024 ** 3


Progress = Callable[[dict[str, Any]], None]


_PROGRESS_LABELS = {
    "queued": "Queued",
    "planning": "Planning retrieval",
    "global_discovery": "Global discovery",
    "repo_routing": "Routing repositories",
    "targeted_retrieval": "Retrieving from routed repositories",
    "semantic": "Semantic discovery",
    "candidate_pruning": "Pruning candidates",
    "reranking": "Reranking candidates",
    "hydrating": "Hydrating exact source",
    "packing_context": "Packing context",
    "discovery": "Discovering repositories",
    "sync": "Reconciling repository snapshots",
    "core_index": "Building Core indexes",
    "knowledge": "Building project maps and relationships",
    "graph": "Building graph state",
    "semantic_manifest": "Discovering Semantic cards",
    "semantic_embedding": "Building Semantic index",
    "semantic_shard": "Writing Semantic shards",
    "semantic_reuse": "Reused published Semantic generation",
    "semantic_publish": "Publishing Semantic generation",
    "complete": "Refresh complete",
    "failed": "Refresh failed",
}
_PROGRESS_COUNTS = {
    "repository_current", "repository_total", "repositories_unchanged", "repositories_changed",
    "semantic_repository_current", "semantic_repository_total", "semantic_cards_discovered", "semantic_cards_total",
    "cached_embeddings_reused", "new_embeddings_completed", "remaining_embeddings", "embedding_batch_size",
    "embedding_batches_completed", "semantic_shards_completed", "semantic_shards_total",
    "requested_operations", "effective_operations", "physical_operations_completed",
    "repo_current", "repo_total", "candidate_count", "pruned_candidate_count", "evidence_count",
}
_PROGRESS_STATES = {"generation_state", "semantic_status"}
_SAFE_GENERATION_STATES = {"not-required", "checking", "rebuilding", "rebuilt", "reused", "failed"}
_SAFE_SEMANTIC_STATUSES = {"not-required", "ready", "failed"}
_ADDITIONAL_SAFE_PROGRESS_LABELS = {
    "Starting", "Running", "Completed", "Validating local model pack", "Publishing model operation result",
    "Core refresh complete; Semantic needs attention",
}
_SAFE_PROGRESS_LABELS = frozenset(_PROGRESS_LABELS.values()) | _ADDITIONAL_SAFE_PROGRESS_LABELS


def progress_event(
    phase: str,
    *,
    elapsed_ms: int,
    phase_label: str | None = None,
    **details: Any,
) -> dict[str, Any]:
    """Return a fixed, source-free refresh event safe for all local clients."""
    safe_label = phase_label if phase_label in _SAFE_PROGRESS_LABELS else None
    event: dict[str, Any] = {
        "phase": phase if phase in _PROGRESS_LABELS else "failed",
        "phase_label": safe_label or _PROGRESS_LABELS.get(phase, _PROGRESS_LABELS["failed"]),
        "elapsed_ms": max(0, int(elapsed_ms)),
    }
    for key in _PROGRESS_COUNTS:
        value = details.get(key)
        if value is not None and not isinstance(value, bool):
            try:
                event[key] = max(0, int(value))
            except (TypeError, ValueError):
                continue
    for key in _PROGRESS_STATES:
        value = details.get(key)
        if value is None:
            continue
        text = str(value)
        if key == "generation_state" and text in _SAFE_GENERATION_STATES:
            event[key] = text
        elif key == "semantic_status" and text in _SAFE_SEMANTIC_STATUSES:
            event[key] = text
    return event


@dataclass
class RefreshOutcome:
    """The one authoritative refresh result shared by CLI and UI surfaces."""

    additions: list[Any]
    sync: list[Any]
    graph: list[Any]
    experience: dict[str, Any]
    semantic: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "discovered": [item.name for item in self.additions],
            "sync": [asdict(item) for item in self.sync],
            "graph": [asdict(item) for item in self.graph],
            "experience": self.experience,
            "semantic": self.semantic,
        }


def _progress(callback: Progress | None, started: float, state: dict[str, Any], phase: str, **details: Any) -> None:
    """Merge safe counters so a final event retains the last known totals."""
    state.update({key: value for key, value in details.items() if value is not None})
    if callback is not None:
        callback(progress_event(phase, elapsed_ms=(time.perf_counter() - started) * 1000, **state))


def format_refresh_progress(event: dict[str, Any]) -> str:
    """Render a concise CLI line from the same safe event supplied to the UI."""
    elapsed = max(0, int(event.get("elapsed_ms") or 0)) // 1000
    details: list[str] = []
    if "repository_total" in event:
        details.append(f"repos {event.get('repository_current', 0)}/{event['repository_total']}")
    if "repositories_changed" in event:
        details.append(f"changed {event['repositories_changed']} unchanged {event.get('repositories_unchanged', 0)}")
    if "semantic_cards_total" in event:
        details.append(f"cards {event.get('semantic_cards_discovered', 0)}/{event['semantic_cards_total']}")
    if "cached_embeddings_reused" in event:
        details.append(f"cached {event['cached_embeddings_reused']}")
    if "new_embeddings_completed" in event:
        details.append(f"embedded {event['new_embeddings_completed']}")
    if "remaining_embeddings" in event:
        details.append(f"remaining {event['remaining_embeddings']}")
    if event.get("embedding_batch_size"):
        details.append(f"batch {event['embedding_batch_size']}")
    if "semantic_shards_total" in event:
        details.append(f"shards {event.get('semantic_shards_completed', 0)}/{event['semantic_shards_total']}")
    if event.get("generation_state") in {"reused", "rebuilt", "rebuilding"}:
        details.append(f"generation {event['generation_state']}")
    suffix = " · " + "; ".join(details) if details else ""
    return f"[{elapsed // 60:02d}:{elapsed % 60:02d}] {event.get('phase_label', 'Refreshing')}{suffix}"


def semantic_status(settings: Settings) -> dict[str, Any]:
    """Return safe, snapshot-aware semantic readiness without loading a model."""
    from .semantic import CARD_VERSION, CHUNK_SCHEMA_VERSION, SEMANTIC_EMBEDDING_INPUT_VERSION

    atlas = current_generation_ref(settings)
    component = atlas.component("semantic") if atlas is not None else {}
    if atlas is not None and component.get("status") != "ready":
        state = {}
    else:
        path = (
            settings.state_dir / str(component["artifact_ref"])
            if component.get("artifact_ref")
            else settings.state_dir / "semantic-index.json"
        )
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
    expected = {repo.name: repo.source_sha or "working-tree" for repo in settings.repositories}
    actual: dict[str, str] = {}
    for shard in state.get("shards") or []:
        if isinstance(shard, dict):
            actual[str(shard.get("repo") or "")] = str(shard.get("snapshot") or "")
    for entry in state.get("entries") or []:
        if isinstance(entry, dict):
            actual[str(entry.get("repo") or "")] = str(entry.get("snapshot") or "")
    chunks = len(state.get("entries") or []) + sum(
        len(item.get("entries") or []) for item in state.get("shards") or [] if isinstance(item, dict)
    )
    valid_schema = bool(state) and (
        state.get("chunk_schema_version") == CHUNK_SCHEMA_VERSION
        and state.get("card_version") == CARD_VERSION
        and state.get("embedding_input_version") == SEMANTIC_EMBEDDING_INPUT_VERSION
    )
    missing = sorted(name for name, snapshot in expected.items() if actual.get(name) != snapshot)
    stale = bool(state.get("stale"))
    aligned = valid_schema and not stale and not missing
    if not state:
        reason = "Semantic generation has not been built."
    elif stale:
        reason = str(state.get("stale_reason") or "Semantic generation is stale.")
    elif not valid_schema:
        reason = "Semantic generation schema is incompatible."
    elif missing:
        reason = "Semantic generation does not match current snapshots."
    else:
        reason = None
    return {
        "available": bool(state),
        "chunks": chunks,
        "stale": stale,
        "aligned": aligned,
        "generation": str(state.get("generation") or "unknown")[:12] if state else None,
        "backend": state.get("backend"),
        "pack_id": state.get("pack_id"),
        "reason": reason if atlas is None or component.get("status") == "ready" else "Semantic is unavailable for the current Atlas generation.",
    }


@workspace_exclusive
def refresh_brain(
    settings: Settings,
    *,
    fetch: bool = True,
    branch_values: list[str] | None = None,
    discover: bool = True,
    progress: Progress | None = None,
) -> RefreshOutcome:
    """Refresh all authoritative Core state and Semantic state when required.

    A Semantic failure never publishes partial semantic state.  It is returned as
    an explicit degraded outcome so callers can decide whether a new
    investigation may proceed.
    """
    from .core import discover_and_configure_repositories, generate_map, snapshot_indexes
    from .editions import current_edition
    from .experience import build_experience_index, evaluate_sessions
    from .graph import index_graph
    from .relations import generate_relationship_map
    from .sync import parse_branch_overrides, sync_repositories

    started = time.perf_counter()
    progress_state: dict[str, Any] = {
        "repository_current": 0,
        "repository_total": len(settings.repositories),
        "generation_state": "not-required",
    }

    def emit(phase: str, **details: Any) -> None:
        _progress(progress, started, progress_state, phase, **details)

    emit("discovery")
    additions = discover_and_configure_repositories(settings) if discover else []
    emit("sync", repository_current=0, repository_total=len(settings.repositories))
    results = sync_repositories(settings, fetch=fetch, branch_overrides=parse_branch_overrides(settings, branch_values or []))
    emit("core_index", repository_current=0, repository_total=len(results))
    index_state, updated = snapshot_indexes(settings, changed_only=True, publish=False)
    emit(
        "core_index",
        repository_current=len(results),
        repository_total=len(results),
        repositories_changed=len(updated),
        repositories_unchanged=max(0, len(results) - len(updated)),
    )
    emit("knowledge")
    generate_map(settings)
    generate_relationship_map(settings)
    experience = build_experience_index(settings, changed_only=True)
    evaluation = evaluate_sessions(settings, experience)
    emit("graph")
    graphs = index_graph(settings, defer_lazy=True)
    from .atlas import build_atlas

    atlas_payload = build_atlas(settings, index_state)

    edition = current_edition(settings)
    if edition in {"semantic", "precision"}:
        emit("semantic_manifest", generation_state="checking")
        try:
            from .semantic import build_semantic_index

            def semantic_progress(event: dict[str, object]) -> None:
                details = dict(event)
                phase = str(details.pop("phase", "semantic_embedding"))
                details.pop("phase_label", None)
                details.pop("elapsed_ms", None)
                emit(phase, **details)

            settings.atlas_cards = atlas_payload["cards"]
            try:
                built = build_semantic_index(settings, progress=semantic_progress) if progress is not None else build_semantic_index(settings)
            finally:
                settings.atlas_cards = None
            semantic = {**semantic_status(settings), "required": True, "status": "ready", "build": built}
        except (OSError, RuntimeError, ValueError) as error:
            # Do not leak source, endpoint, proxy, or certificate details to an
            # operations UI.  The semantic layer already retains its prior
            # generation atomically when this path fails.
            semantic = {
                **semantic_status(settings),
                "required": True,
                "status": "failed",
                "error": f"Semantic indexing failed ({type(error).__name__}).",
            }
    else:
        semantic = {**semantic_status(settings), "required": False, "status": "not-required"}
    try:
        from .catalog import collect_generation_components, publish_generation
        from .index import write_state

        components = collect_generation_components(
            settings,
            index_state,
            semantic_failed=semantic["status"] == "failed",
            atlas_payload=atlas_payload,
        )
        atlas = publish_generation(
            settings,
            index_state,
            backends=["sqlite-fts5"] + (["zoekt"] if components["zoekt"]["status"] == "ready" else []),
            components=components,
            atlas_payload=atlas_payload,
        )
        for item in index_state.values():
            if isinstance(item, dict):
                item["generation"] = atlas["generation"]
        write_state(settings, index_state)
    except (OSError, sqlite3.Error):
        # Mandatory component failure leaves the previous Atlas pointer intact,
        # but must not be reported to UI/CLI callers as a successful refresh.
        raise
    if semantic["status"] == "failed":
        emit("complete", phase_label="Core refresh complete; Semantic needs attention", semantic_status="failed")
    else:
        emit("complete", semantic_status="ready" if semantic["status"] == "ready" else "not-required")
    return RefreshOutcome(
        additions=additions,
        sync=results,
        graph=graphs,
        experience={"cases": len(experience.get("cases") or []), "evaluated_sessions": evaluation["evaluated_sessions"]},
        semantic=semantic,
    )


@workspace_exclusive
def change_edition(settings: Settings, edition: str, *, refresh: bool = False, progress: Progress | None = None) -> dict[str, Any]:
    """Validate a capability profile and optionally align its semantic state."""
    from .editions import set_edition

    selected = set_edition(settings, edition)
    outcome = refresh_brain(settings, progress=progress) if refresh else None
    return {
        "edition": selected,
        "capabilities": capabilities(settings),
        "semantic": outcome.semantic if outcome is not None else semantic_status(settings),
        "refresh": outcome.as_dict() if outcome is not None else None,
    }


def model_operation(
    settings: Settings,
    action: str,
    value: str | None = None,
    *,
    samples: int = 3,
    latency_budget_ms: int = 3000,
    expected_sha256: str | None = None,
    official_only: bool = False,
) -> Any:
    """Shared model-pack operation; UI callers may select official aliases only."""
    from .models import installed_packs, official_packs

    action = action.lower().strip()
    if action == "list":
        return {"official": official_packs(), "installed": installed_packs(settings)}
    if action == "status":
        return installed_packs(settings)
    return _model_mutation(
        settings,
        action,
        value,
        samples=samples,
        latency_budget_ms=latency_budget_ms,
        expected_sha256=expected_sha256,
        official_only=official_only,
    )


@workspace_exclusive
def _model_mutation(
    settings: Settings,
    action: str,
    value: str | None,
    *,
    samples: int,
    latency_budget_ms: int,
    expected_sha256: str | None,
    official_only: bool,
) -> Any:
    """Serialize model operations that can write workspace state or runtime data."""
    from .models import (
        OFFICIAL_PACKS,
        autotune_pack,
        benchmark_pack,
        install_official_pack,
        install_pack,
        install_pack_url,
        remove_pack,
        verify_pack,
    )

    if action == "install" and not value:
        raise ValueError("brain model install requires an official pack alias, a local pack path, or an approved HTTPS release URL")
    if not value:
        raise ValueError(f"brain model {action} requires PACK")
    alias = value.lower()
    pack_id = str((OFFICIAL_PACKS.get(alias) or {}).get("pack_id") or value)
    if action == "install":
        if alias in OFFICIAL_PACKS:
            return install_official_pack(settings, alias)
        if official_only:
            raise ValueError("UI model installation accepts only an official Project Brain pack alias")
        if value.startswith("https://"):
            if not expected_sha256:
                raise ValueError("brain model install URL requires --sha256 from the approved release manifest")
            return install_pack_url(settings, value, expected_sha256)
        if "://" in value or value.startswith("github:"):
            raise ValueError("model install accepts a local pack path or approved HTTPS release URL only")
        return install_pack(settings, Path(value))
    if action == "verify":
        return verify_pack(settings, pack_id)
    if action == "benchmark":
        return benchmark_pack(settings, pack_id, samples=samples)
    if action == "autotune":
        return autotune_pack(settings, pack_id, samples=samples, latency_budget_ms=latency_budget_ms)
    if action == "remove":
        remove_pack(settings, pack_id)
        return {"pack_id": pack_id, "removed": True}
    raise ValueError("model action must be list, status, install, verify, benchmark, autotune, or remove")


def model_status(settings: Settings) -> dict[str, Any]:
    """Safe model-pack state for UI rendering; never expose local paths or secrets."""
    from .models import OFFICIAL_PACKS, installed_packs, pack_compatibility_error

    installed = []
    for pack in installed_packs(settings):
        compatible = not bool(pack.get("invalid")) and pack_compatibility_error(pack) is None
        installed.append({
            "pack_id": pack.get("pack_id"),
            "capability": pack.get("capability"),
            "model_family": pack.get("model_family"),
            "verified": bool(pack.get("verified")),
            "compatible": compatible,
            "compatibility_error": pack_compatibility_error(pack) if not compatible and not pack.get("invalid") else "Invalid installed manifest" if pack.get("invalid") else None,
        })
    return {
        "official": [
            {"alias": alias, "pack_id": value.get("pack_id"), "capability": value.get("capability")}
            for alias, value in sorted(OFFICIAL_PACKS.items())
        ],
        "installed": installed,
    }


def dashboard_status(settings: Settings) -> dict[str, Any]:
    """One safe status calculation reused by the operations UI and CLI status."""
    requested = current_edition(settings)
    available = capabilities(settings)
    semantic = semantic_status(settings)
    repo_freshness = freshness(settings)
    core_ready = bool(available.get("lexical_index")) and all(item.get("current") for item in repo_freshness["repositories"])
    if requested == "precision" and available.get("reranker") and available.get("embedding") and semantic["aligned"]:
        effective, reason = "Precision active", None
    elif requested == "precision":
        effective = "Degraded"
        reason = "Verified compatible reranker pack is unavailable" if not available.get("reranker") else semantic["reason"] or "Semantic generation is not aligned"
    elif requested == "semantic" and available.get("embedding") and semantic["aligned"]:
        effective, reason = "Semantic active", None
    elif requested == "core":
        effective, reason = "Core", None
    elif not available.get("embedding"):
        effective, reason = "Degraded", "Verified compatible embedding pack or vector backend is unavailable"
    else:
        effective, reason = "Degraded", semantic["reason"] or "Semantic generation is not aligned"
    if not core_ready:
        health = "Action required"
    elif effective == "Degraded":
        health = "Degraded"
    else:
        health = "Healthy"
    return {
        "version": __import__("brain").__version__,
        "edition": requested,
        "effective": effective,
        "reason": reason,
        "health": health,
        "core": {"ready": core_ready},
        "semantic": semantic,
        "capabilities": available,
        "freshness": repo_freshness,
        "models": model_status(settings),
        "managed_runtime": "loopback direct enforced",
    }


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
    atlas = current_generation_ref(settings)
    rows = []
    for repo in settings.repositories:
        source = repo.source_sha or git_head(repo)
        index = atlas.snapshots.get(repo.name) if atlas is not None else (indexed.get(repo.name) or {}).get("sha")
        rows.append({"repo": repo.name, "source_sha": source, "index_sha": index, "current": not index or not source or index == source, "warning": repo.source_warning})
    from .backends.zoekt import status as zoekt_status

    zoekt = zoekt_status()
    return {
        "generation": current_generation(settings),
        "atlas_identity": atlas.identity if atlas is not None else None,
        "components": {
            name: value.get("status") for name, value in atlas.components.items()
        } if atlas is not None else {},
        "repositories": rows,
        "zoekt": {"available": zoekt.available, "reason": zoekt.reason},
    }


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


def _component_state(settings: Settings, generation: Any, component_name: str) -> dict[str, Any]:
    component = generation.component(component_name)
    if component.get("status") != "ready" or not component.get("artifact_ref"):
        return {}
    path = (settings.state_dir / str(component["artifact_ref"])).resolve()
    if not path.is_relative_to(settings.state_dir.resolve()):
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _semantic_shard_removals(settings: Settings, retained: list[Any]) -> list[Path]:
    keep = {
        Path(str(item.get("path"))).resolve()
        for generation in retained
        for item in _component_state(settings, generation, "semantic").get("shards") or []
        if isinstance(item, dict) and item.get("path")
    }
    root = settings.state_dir / "semantic-shards"
    return [path for path in root.glob("*.usearch") if path.resolve() not in keep]


def _legacy_snapshot_memberships(settings: Settings) -> set[tuple[str, str]]:
    retained: set[tuple[str, str]] = set()
    for session in settings.runs_dir.glob("*/session.json") if settings.runs_dir.is_dir() else []:
        try:
            sources = json.loads(session.read_text(encoding="utf-8")).get("sources") or {}
            retained.update(
                (str(repo), str(item.get("sha") or "working-tree"))
                for repo, item in sources.items()
                if isinstance(item, dict)
            )
        except (OSError, json.JSONDecodeError):
            continue
    return retained


def _zoekt_shard_removals(settings: Settings, retain: set[tuple[str, str]]) -> list[Path]:
    root = settings.state_dir / "zoekt"
    if not root.is_dir():
        return []
    return [
        snapshot
        for repository in root.iterdir() if repository.is_dir()
        for snapshot in repository.iterdir() if snapshot.is_dir()
        if (repository.name, snapshot.name) not in retain
    ]


@workspace_exclusive
def gc(settings: Settings, *, dry_run: bool = True, keep_recent: int = 2) -> dict[str, Any]:
    root = generation_root(settings)
    pinned = _pinned_generations(settings)
    generation_dirs = sorted((path for path in root.glob("generation-*") if path.is_dir()), key=lambda path: path.name)
    protected = {path.name for path in generation_dirs[-max(1, keep_recent):]}
    current = current_generation_ref(settings)
    if current is not None:
        protected.add(f"generation-{current.generation:06d}")
    protected.update(f"generation-{number:06d}" for number in pinned)
    removable = [
        path for path in generation_dirs
        if path.name not in protected and int(path.name.rsplit("-", 1)[-1]) not in pinned
    ]
    protected_numbers = {
        int(name.rsplit("-", 1)[-1]) for name in protected
        if name.rsplit("-", 1)[-1].isdigit()
    }
    retained_generations = [item for item in catalog_generations(settings) if item.generation in protected_numbers]
    retained_memberships = {
        (repo, sha)
        for generation in retained_generations
        for repo, sha in generation.snapshots.items()
    } | _legacy_snapshot_memberships(settings)
    from .index import membership_snapshots

    lexical_removable = sorted(membership_snapshots(settings) - retained_memberships)
    snapshot_removable = _snapshot_removals(settings, keep_recent)
    semantic_removable = _semantic_shard_removals(settings, retained_generations)
    zoekt_removable = _zoekt_shard_removals(settings, retained_memberships)
    targets = (
        [("generation", path) for path in removable]
        + [("snapshot", path) for path in snapshot_removable]
        + [("semantic_shard", path) for path in semantic_removable]
        + [("zoekt_shard", path) for path in zoekt_removable]
    )
    rows = [{"kind": kind, "path": str(path), "bytes": path.stat().st_size if path.is_file() else sum(item.stat().st_size for item in path.rglob("*") if item.is_file())} for kind, path in targets]
    rows.extend({"kind": "lexical_membership", "path": f"{repo}@{sha}", "bytes": 0} for repo, sha in lexical_removable)
    if not dry_run:
        from .catalog import delete_generations
        from .index import prune_memberships

        delete_generations(settings, {int(path.name.rsplit("-", 1)[-1]) for path in removable})
        for _, item in targets:
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink(missing_ok=True)
        prune_memberships(settings, retained_memberships)
    return {
        "dry_run": dry_run,
        "pinned_generations": sorted(pinned),
        "pinned_snapshots": [str(path) for path in sorted(_pinned_snapshot_paths(settings))],
        "remove": rows,
        "reclaim_bytes": sum(item["bytes"] for item in rows),
    }


def status(settings: Settings) -> dict[str, Any]:
    return {"edition": current_edition(settings), "capabilities": capabilities(settings), "freshness": freshness(settings), "storage": storage(settings)}

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import stat
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .catalog import (
    current_generation,
    current_generation_ref,
    generation_root,
    generations as catalog_generations,
    source_signature,
    status_probe,
)
from .editions import capabilities, current_edition
from .locks import workspace_exclusive
from .platforms import read_managed_text

if TYPE_CHECKING:
    from .core import Settings


_GIB = 1024 ** 3
MAX_GC_SESSION_SCAN_ITEMS = 10_000
MAX_GC_SCAN_ITEMS = 500_000
MAX_GC_SCAN_SECONDS = 30.0
MAX_GC_ACCOUNTED_BYTES = 16 * 1024 ** 4
MAX_STORAGE_STATUS_ENTRIES = 5_000
MAX_STORAGE_STATUS_SECONDS = 0.05
MAX_CAPACITY_SCAN_ENTRIES = 500_000
MAX_CAPACITY_SCAN_SECONDS = 30.0
MAX_INVENTORY_DEPTH = 128
STORAGE_STATUS_TTL_SECONDS = 5.0
MAX_FRESHNESS_PROBE_SECONDS = 5.0
MAX_FRESHNESS_REPO_SECONDS = 1.0
_STORAGE_STATUS_CACHE: dict[tuple[str, ...], tuple[float, dict[str, Any]]] = {}
_STORAGE_STATUS_LOCK = threading.Lock()


class StateCapacityError(OSError):
    """A safe, actionable managed-storage refusal."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def recovery(self) -> dict[str, str]:
        if self.code == "state_inventory_unsafe":
            return {
                "code": self.code, "title": "Storage inventory needs diagnosis",
                "message": str(self), "action": "diagnostics", "action_label": "Run diagnostics",
            }
        return {
            "code": self.code,
            "title": "Storage needs attention",
            "message": str(self),
            "action": "safe_gc",
            "action_label": "Safely reclaim unpinned state",
        }


class _GcScanIncomplete(RuntimeError):
    pass


@dataclass
class _GcScanBudget:
    remaining_items: int = field(default_factory=lambda: MAX_GC_SCAN_ITEMS)
    deadline: float = 0.0
    accounted_bytes: int = 0

    def consume(self, count: int = 1) -> None:
        if not self.deadline:
            self.deadline = time.monotonic() + MAX_GC_SCAN_SECONDS
        self.remaining_items -= count
        if self.remaining_items < 0 or time.monotonic() > self.deadline:
            raise _GcScanIncomplete("GC reachability scan exceeded its item/time budget")

    def file_bytes(self, size: int) -> int:
        self.consume()
        return self.account_bytes(size)

    def account_bytes(self, size: int) -> int:
        if size < 0 or self.accounted_bytes + size > MAX_GC_ACCOUNTED_BYTES:
            raise _GcScanIncomplete("GC reclaim accounting exceeded its byte budget")
        self.accounted_bytes += size
        return size


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
    "wave_started": "Starting investigation wave",
    "first_useful_checkpoint": "First useful checkpoint published",
    "anchors_resolved": "Resolving runtime anchors",
    "flow_built": "Building bounded flows",
    "evidence_verified": "Verifying exact evidence",
    "continuation_published": "Publishing checkpoint continuation",
    "wave_complete": "Investigation wave complete",
    "investigation_complete": "Investigation complete",
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
    "wave", "execution_steps", "integration_steps",
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
    context_id = str(details.get("context_id") or "")
    if re.fullmatch(r"CTX-[0-9]{3,}(?:-P[0-9]+)?", context_id):
        event["context_id"] = context_id
    checkpoint_artifact = str(details.get("checkpoint_artifact") or "")
    if re.fullmatch(r"checkpoint-[0-9]{3,}\.md", checkpoint_artifact):
        event["checkpoint_artifact"] = checkpoint_artifact
    continuation_artifact = str(details.get("continuation_artifact") or "")
    if re.fullmatch(r"checkpoint-delta-[0-9]{3,}\.md", continuation_artifact):
        event["continuation_artifact"] = continuation_artifact
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
    from .semantic import semantic_snapshots, semantic_state_compatibility

    atlas = current_generation_ref(settings)
    component = atlas.component("semantic") if atlas is not None else {}
    path = settings.state_dir / "semantic-index.json"
    path_root = settings.state_dir
    component_ready = atlas is not None and component.get("status") == "ready" and component.get("artifact_ref")
    if component_ready:
        path = settings.state_dir / str(component["artifact_ref"])
        path_root = generation_root(settings)
    try:
        state = json.loads(read_managed_text(path_root, path, max_bytes=64 * 1024 * 1024))
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        state = {}
    expected = atlas.snapshots if atlas is not None else {
        repo.name: repo.source_sha or "working-tree" for repo in settings.repositories
    }
    valid, reason = semantic_state_compatibility(
        settings,
        state,
        expected,
        component=component if component_ready else None,
        verify_artifacts=False,
    )
    aligned = valid and bool(component_ready)
    if atlas is None:
        reason = (
            "Semantic was built but is not registered to an authoritative Atlas generation."
            if state else "Semantic is unavailable because no authoritative Atlas generation is published."
        )
    if atlas is not None and not component_ready:
        details = component.get("details") if isinstance(component.get("details"), dict) else {}
        reason = str(details.get("reason") or "Semantic is unavailable for the current Atlas generation.")
    elif atlas is not None and not aligned and not reason:
        reason = "Semantic is unavailable for the current Atlas generation."
    chunks = len(state.get("entries") or []) + sum(
        len(item.get("entries") or []) for item in state.get("shards") or [] if isinstance(item, dict)
    )
    return {
        "available": bool(state),
        "chunks": chunks,
        "stale": bool(state.get("stale")),
        "aligned": aligned,
        "generation": str(state.get("generation") or "unknown")[:12] if state else None,
        "backend": state.get("backend"),
        "pack_id": state.get("pack_id"),
        "snapshots": semantic_snapshots(state) if state else {},
        "reason": reason,
        "atlas_identity": atlas.identity if atlas is not None else None,
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
    # Eager mode builds under the refresh writer lease. Lazy mode remains an
    # explicit deferred/`brain index` workflow because retrieval holds only a
    # shared lease and must never perform a hidden graph mutation.
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
            semantic = {"required": True, "status": "ready", "build": built}
        except (OSError, RuntimeError, ValueError) as error:
            # Do not leak source, endpoint, proxy, or certificate details to an
            # operations UI.  The semantic layer already retains its prior
            # generation atomically when this path fails.
            semantic = {
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
        try:
            write_state(settings, index_state)
        except OSError:
            # The catalog/current-generation transaction is authoritative.
            # indexes.json is a recoverable compatibility projection.
            pass
    except (OSError, sqlite3.Error):
        # Mandatory component failure leaves the previous Atlas pointer intact,
        # but must not be reported to UI/CLI callers as a successful refresh.
        raise
    published_semantic = semantic_status(settings)
    semantic = {**semantic, **published_semantic, "atlas_core_published": True}
    if semantic["status"] == "ready" and not semantic["aligned"]:
        semantic["status"] = "failed"
        semantic["error"] = "Semantic publication is not aligned with the current Atlas generation."
    semantic["precision_ready"] = bool(
        edition == "precision" and semantic["status"] == "ready" and semantic["aligned"]
    )
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
    from .platforms import platform_id

    if action == "install" and not value:
        raise ValueError("brain model install requires an official pack alias, a local pack path, or an approved HTTPS release URL")
    if not value:
        raise ValueError(f"brain model {action} requires PACK")
    alias = value.lower()
    platforms = OFFICIAL_PACKS.get(alias) or {}
    platform_pack = platforms.get(platform_id()) if isinstance(platforms, dict) else None
    pack_id = str((platform_pack or {}).get("pack_id") or value)
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
    from .platforms import platform_id

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
            for alias, platforms in sorted(OFFICIAL_PACKS.items())
            for value in [platforms.get(platform_id())]
            if isinstance(value, dict)
        ],
        "installed": installed,
    }


def dashboard_status(settings: Settings) -> dict[str, Any]:
    """One safe status calculation reused by the operations UI and CLI status."""
    requested = current_edition(settings)
    semantic = semantic_status(settings)
    available = capabilities(settings, semantic=semantic)
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


def _directory_bytes(
    path: Path, *, scan_seconds: float | None = None, scan_entries: int | None = None,
    stop_after: int | None = None, reject_links: bool = False,
) -> int:
    """Stream physical sizes; only optional probes have a total work budget.

    A complete managed write/cleanup is not a status probe. Its workspace may
    legitimately contain millions of retained files. Memory/open directories
    are bounded by depth, not width; no publication-time seal or cached total
    can authorize a write. Early termination is allowed only above a byte limit.
    """
    try:
        path.lstat()
    except FileNotFoundError:
        return 0
    deadline = time.monotonic() + min(MAX_CAPACITY_SCAN_SECONDS, max(0, scan_seconds)) if scan_seconds is not None else None
    item_limit = min(MAX_CAPACITY_SCAN_ENTRIES, max(0, scan_entries)) if scan_entries is not None else None
    scanned = 0
    total = 0

    def linked(info: os.stat_result) -> bool:
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)

    def visit(directory: Path, depth: int) -> bool:
        nonlocal scanned, total
        if depth > MAX_INVENTORY_DEPTH:
            raise StateCapacityError("state_inventory_unsafe", "Managed state exceeds the safe directory-depth limit; run diagnostics")
        before = directory.lstat()
        if linked(before) or not stat.S_ISDIR(before.st_mode):
            raise StateCapacityError("state_inventory_unsafe", "Managed inventory directory is not a direct directory; run diagnostics")
        if deadline is not None and time.monotonic() >= deadline:
            raise StateCapacityError("state_inventory_limit", "Quick storage check is incomplete; a managed refresh performs the full inventory")
        try:
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    scanned += 1
                    if (item_limit is not None and scanned > item_limit) or (deadline is not None and time.monotonic() >= deadline):
                        raise StateCapacityError(
                            "state_inventory_limit",
                            "Quick storage check is incomplete; a managed refresh performs the full inventory",
                        )
                    info = entry.stat(follow_symlinks=False)
                    if linked(info):
                        if reject_links:
                            raise StateCapacityError("state_inventory_unsafe", "GC target contains a symbolic link or reparse point")
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        if visit(Path(entry.path), depth + 1):
                            return True
                    elif stat.S_ISREG(info.st_mode):
                        total += info.st_size
                    if stop_after is not None and total > stop_after:
                        return True
        finally:
            after = directory.lstat()
            if linked(after) or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
                raise StateCapacityError("state_inventory_unsafe", "Managed inventory directory changed during scanning; retry after other operations finish")
        return False

    try:
        visit(path, 0)
    except OSError as error:
        if isinstance(error, StateCapacityError):
            raise
        raise OSError("Project Brain state capacity is unavailable") from error
    return total


def _bounded_storage_summary(roots: list[Path]) -> dict[str, Any]:
    deadline = time.monotonic() + MAX_STORAGE_STATUS_SECONDS
    remaining = MAX_STORAGE_STATUS_ENTRIES
    sizes = {str(path): 0 for path in roots}
    complete = True
    scanned = 0
    for root in roots:
        pending = [root]
        while pending:
            if remaining <= 0 or time.monotonic() >= deadline:
                complete = False
                break
            directory = pending.pop()
            try:
                with os.scandir(directory) as iterator:
                    for entry in iterator:
                        if remaining <= 0:
                            complete = False
                            break
                        remaining -= 1
                        scanned += 1
                        if time.monotonic() >= deadline:
                            complete = False
                            break
                        try:
                            if entry.is_symlink():
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                pending.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                sizes[str(root)] += entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            complete = False
            except OSError:
                complete = False
        if not complete:
            break
    return {
        "bytes": sizes,
        "total_bytes": sum(sizes.values()),
        "complete": complete,
        "scanned_entries": scanned,
    }


def ensure_write_capacity(
    settings: Settings, projected_bytes: int = 0, *,
    scan_seconds: float | None = None, scan_entries: int | None = None,
) -> None:
    """Refuse index/model writes before exceeding the configured local disk guard."""
    projected_bytes = max(0, int(projected_bytes))
    usage = shutil.disk_usage(settings.root)
    if settings.minimum_free_disk_gb and usage.free - projected_bytes < settings.minimum_free_disk_gb * _GIB:
        raise StateCapacityError(
            "free_disk_guard",
            "Project Brain free-disk guard would be breached; safely reclaim unpinned state or free local disk space",
        )
    current_bytes = _directory_bytes(
        settings.state_dir, scan_seconds=scan_seconds, scan_entries=scan_entries,
        stop_after=settings.max_state_gb * _GIB - projected_bytes,
    ) if settings.max_state_gb else 0
    if settings.max_state_gb and current_bytes + projected_bytes > settings.max_state_gb * _GIB:
        raise StateCapacityError(
            "state_quota_exceeded",
            "Project Brain state quota would be exceeded; safely reclaim unpinned state or raise storage.max_state_gb",
        )


def remaining_write_capacity(
    settings: Settings, *, scan_seconds: float | None = None, scan_entries: int | None = None,
) -> int:
    """Return the exact fail-closed byte budget available to managed writers."""
    current_bytes = (
        _directory_bytes(settings.state_dir, scan_seconds=scan_seconds, scan_entries=scan_entries,
                         stop_after=settings.max_state_gb * _GIB)
        if settings.max_state_gb else 0
    )
    usage = shutil.disk_usage(settings.root)
    limits = [max(0, usage.free - max(0, settings.minimum_free_disk_gb) * _GIB)]
    if settings.max_state_gb:
        limits.append(max(0, settings.max_state_gb * _GIB - current_bytes))
    return min(limits)


def freshness(settings: Settings) -> dict[str, Any]:
    from .core import BrainError, git_head, load_index_state

    indexed = load_index_state(settings)
    atlas = current_generation_ref(settings)
    deadline = time.monotonic() + MAX_FRESHNESS_PROBE_SECONDS

    def probe(repo: Any) -> dict[str, Any]:
        warning = repo.source_warning
        git_metadata = repo.path / ".git"
        if git_metadata.exists() or git_metadata.is_symlink():
            # Stored source_sha is the published snapshot, not a live freshness
            # probe. Resolve the selected local ref without fetching so status
            # cannot report G1 current after the same branch advances to G2.
            remaining = deadline - time.monotonic()
            try:
                source = git_head(
                    repo, repo.source_ref or "HEAD",
                    timeout=min(MAX_FRESHNESS_REPO_SECONDS, remaining),
                ) if remaining > 0 else None
            except (BrainError, OSError):
                source = None
            if source is None:
                warning = warning or "Local Git ref could not be verified within the status budget; recheck status or refresh"
        else:
            source = repo.source_sha
        index = atlas.snapshots.get(repo.name) if atlas is not None else (indexed.get(repo.name) or {}).get("sha")
        return {
            "repo": repo.name,
            "source_sha": source,
            "index_sha": index,
            "current": bool(atlas is not None and index and source and index == source),
            "warning": warning,
        }
    # A status request must not turn 100 repositories into 100 × 30 seconds.
    # Queued probes share the deadline and report unknown, never false-current.
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="brain-freshness") as executor:
        rows = list(executor.map(probe, settings.repositories))
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
    key = tuple(str(path) for path in roots)
    now = time.monotonic()
    with _STORAGE_STATUS_LOCK:
        cached = _STORAGE_STATUS_CACHE.get(key)
    if cached is not None and now - cached[0] < STORAGE_STATUS_TTL_SECONDS:
        summary = dict(cached[1])
        summary["bytes"] = dict(summary["bytes"])
    else:
        summary = _bounded_storage_summary(roots)
        summary["catalog_issue"] = status_probe(settings)
        with _STORAGE_STATUS_LOCK:
            if len(_STORAGE_STATUS_CACHE) >= 64:
                _STORAGE_STATUS_CACHE.clear()
            _STORAGE_STATUS_CACHE[key] = (now, summary)
    usage = shutil.disk_usage(settings.root)
    return {
        **summary,
        "free_bytes": usage.free,
        "limits": {"max_state_gb": settings.max_state_gb, "minimum_free_disk_gb": settings.minimum_free_disk_gb},
    }


def _session_reachability(settings: Settings) -> tuple[list[dict[str, Any]], list[str]]:
    from .core import (
        BrainError,
        _read_session_json,
        _validate_session_schema,
        _validated_runs_root,
        _validated_session_snapshot_paths,
    )

    states: list[dict[str, Any]] = []
    blockers: list[str] = []
    retained = catalog_generations(settings)
    by_generation = {item.generation: item for item in retained}
    by_identity = {item.identity: item for item in retained}
    try:
        runs_root = _validated_runs_root(settings)
    except BrainError:
        return states, ["unreadable ticket session root; generation GC is blocked"]
    directories = runs_root.iterdir()
    for index, directory in enumerate(directories):
        if index >= MAX_GC_SESSION_SCAN_ITEMS:
            blockers.append("ticket session scan limit exceeded; generation GC is blocked")
            break
        session = directory / "session.json"
        if directory.is_symlink() or session.is_symlink():
            blockers.append(f"unreadable ticket session: {session}")
            continue
        if not directory.is_dir() or not session.is_file():
            continue
        try:
            value = _read_session_json(session)
            _validate_session_schema(value)
            generation = value.get("generation")
            if generation is not None and (
                isinstance(generation, bool)
                or not isinstance(generation, int)
                or generation < 1
            ):
                raise ValueError("session generation pin is invalid")
            if "sources" in value:
                sources = value["sources"]
                if not isinstance(sources, dict):
                    raise ValueError("session sources pin is invalid")
                for item in sources.values():
                    if not isinstance(item, dict):
                        raise ValueError("session source pin is invalid")
                    snapshot = item.get("snapshot")
                    if snapshot is not None and (not isinstance(snapshot, str) or not snapshot.strip()):
                        raise ValueError("session snapshot pin is invalid")
            else:
                sources = {}
            snapshot_projection = {
                str(name): str(item.get("sha") or "working-tree")
                for name, item in sources.items()
            }
            mode = value.get("generation_mode")
            identity = value.get("atlas_generation_id")
            if mode == "atlas":
                if (
                    not isinstance(identity, str) or not identity
                    or generation is None
                    or by_generation.get(generation) is None
                    or by_identity.get(identity) is not by_generation.get(generation)
                ):
                    raise ValueError("session Atlas generation identity/number pin is inconsistent")
                pinned = by_generation[generation]
                if (
                    snapshot_projection != pinned.snapshots
                    or value.get("source_signature") != pinned.source_signature
                ):
                    raise ValueError("session Atlas source pins are inconsistent")
                _validated_session_snapshot_paths(settings, value, pinned)
            elif mode in {None, "legacy_source_pin"}:
                if identity not in {None, ""} or generation is not None:
                    raise ValueError("legacy session contains an Atlas generation pin")
                stored_signature = value.get("source_signature")
                if stored_signature is not None and stored_signature != source_signature(snapshot_projection):
                    raise ValueError("legacy session source signature is inconsistent")
                _validated_session_snapshot_paths(settings, value, None)
            else:
                raise ValueError("session generation mode is invalid")
            states.append(value)
        except (OSError, ValueError, BrainError, json.JSONDecodeError):
            blockers.append(f"unreadable ticket session: {session}")
    return states, blockers


def _pinned_generations(settings: Settings, states: list[dict[str, Any]] | None = None) -> set[int]:
    values: set[int] = set()
    session_states = states if states is not None else _session_reachability(settings)[0]
    for state in session_states:
        try:
            generation = state.get("generation")
            if generation is not None:
                values.add(int(generation))
        except (TypeError, ValueError):
            continue
    return values


def _pinned_snapshot_paths(settings: Settings, states: list[dict[str, Any]] | None = None) -> set[Path]:
    """Keep source snapshots named by the current source state or any ticket."""
    from .core import load_source_state

    roots: set[Path] = set()
    for item in load_source_state(settings).values():
        if isinstance(item, dict) and item.get("snapshot"):
            roots.add(Path(str(item["snapshot"])).resolve())
    for state in states if states is not None else _session_reachability(settings)[0]:
        sources = state.get("sources") or {}
        for item in sources.values() if isinstance(sources, dict) else []:
            if isinstance(item, dict) and item.get("snapshot"):
                roots.add(Path(str(item["snapshot"])).resolve())
    return {path for path in roots if path.is_relative_to((settings.state_dir / "snapshots").resolve())}


def _direct_component_root(settings: Settings, name: str) -> Path | None:
    configured = settings.state_dir / name
    try:
        root = configured.resolve()
        return root if not configured.is_symlink() and root.parent == settings.state_dir.resolve() else None
    except OSError:
        return None


def _component_tree_safe(root: Path, *, nested: bool = False, budget: _GcScanBudget | None = None) -> bool:
    if not root.exists():
        return True
    try:
        for child in root.iterdir():
            if budget is not None:
                budget.consume()
            if child.is_symlink() or child.resolve().parent != root:
                return False
            if nested and child.is_dir():
                for grandchild in child.iterdir():
                    if budget is not None:
                        budget.consume()
                    if grandchild.is_symlink() or grandchild.resolve().parent != child:
                        return False
        return True
    except OSError:
        return False


def _snapshot_removals(
    settings: Settings, keep_recent: int, states: list[dict[str, Any]] | None = None,
    budget: _GcScanBudget | None = None,
) -> list[Path]:
    root = _direct_component_root(settings, "snapshots")
    if root is None or not _component_tree_safe(root, nested=True, budget=budget):
        return []
    pinned = _pinned_snapshot_paths(settings, states)
    removable: list[Path] = []
    for repository in root.iterdir() if root.is_dir() else []:
        if budget is not None:
            budget.consume()
        snapshots = []
        for path in repository.iterdir():
            if budget is not None:
                budget.consume()
            if path.is_dir():
                snapshots.append(path)
        snapshots.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        for snapshot in snapshots[max(1, keep_recent):]:
            if snapshot.resolve() not in pinned:
                removable.append(snapshot)
    return removable


def _component_state(settings: Settings, generation: Any, component_name: str) -> dict[str, Any]:
    component = generation.component(component_name)
    if component.get("status") != "ready" or not component.get("artifact_ref"):
        return {}
    path = settings.state_dir / str(component["artifact_ref"])
    try:
        value = json.loads(read_managed_text(
            generation_root(settings), path, max_bytes=64 * 1024 * 1024,
        ))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _semantic_shard_removals(
    settings: Settings, retained: list[Any], budget: _GcScanBudget | None = None,
) -> tuple[list[Path], list[str]]:
    keep: set[Path] = set()
    blockers: list[str] = []
    for generation in retained:
        if budget is not None:
            budget.consume()
        component = generation.component("semantic")
        if component.get("status") != "ready":
            continue
        state = _component_state(settings, generation, "semantic")
        shards = state.get("shards") if isinstance(state, dict) else None
        if not isinstance(shards, list):
            blockers.append(
                f"generation {generation.generation} has an unreadable retained Semantic component"
            )
            continue
        keep.update(
            Path(str(item.get("path"))).resolve()
            for item in shards if isinstance(item, dict) and item.get("path")
        )
    if blockers:
        # A corrupt manifest is not evidence that its immutable shards are
        # unreachable. Fail closed and let a later healthy GC pass reclaim them.
        return [], blockers
    root = _direct_component_root(settings, "semantic-shards")
    if root is None or not _component_tree_safe(root, budget=budget):
        return [], ["Semantic shard root escapes managed state"]
    removable = []
    for path in root.iterdir() if root.is_dir() else []:
        if budget is not None:
            budget.consume()
        if path.is_file() and path.suffix == ".usearch" and path.resolve() not in keep:
            removable.append(path)
    return removable, []


def _legacy_snapshot_memberships(
    settings: Settings, states: list[dict[str, Any]] | None = None,
) -> set[tuple[str, str]]:
    retained: set[tuple[str, str]] = set()
    for state in states if states is not None else _session_reachability(settings)[0]:
        sources = state.get("sources") or {}
        retained.update(
            (str(repo), str(item.get("sha") or "working-tree"))
            for repo, item in (sources.items() if isinstance(sources, dict) else [])
            if isinstance(item, dict)
        )
    return retained


def _zoekt_shard_removals(
    settings: Settings, retain: set[tuple[str, str]], budget: _GcScanBudget | None = None,
) -> list[Path]:
    from .backends.zoekt import shard_path

    root = _direct_component_root(settings, "zoekt")
    if root is None or not root.is_dir() or not _component_tree_safe(root, nested=True, budget=budget):
        return []
    retained_paths = {
        shard_path(settings.state_dir, repo, sha).resolve()
        for repo, sha in retain
    }
    removable = []
    for repository in root.iterdir():
        if budget is not None:
            budget.consume()
        if not repository.is_dir():
            continue
        for snapshot in repository.iterdir():
            if budget is not None:
                budget.consume()
            if snapshot.is_dir() and snapshot.resolve() not in retained_paths:
                removable.append(snapshot)
    return removable


def _gc_path_bytes(settings: Settings, path: Path, budget: _GcScanBudget) -> int:
    if path.is_symlink():
        raise _GcScanIncomplete("GC target contains a symbolic link")
    if path.is_file():
        return budget.file_bytes(path.stat().st_size)
    budget.consume()
    started = time.monotonic()
    try:
        # Reachability bounds count generations/memberships/targets, not every
        # source file inside an already selected snapshot. Size checks still
        # finish (and reject links) before the first deletion.
        size = _directory_bytes(path, reject_links=True,
                                stop_after=MAX_GC_ACCOUNTED_BYTES - budget.accounted_bytes)
        return budget.account_bytes(size)
    finally:
        budget.deadline += time.monotonic() - started


def _gc(settings: Settings, *, dry_run: bool, keep_recent: int, budget: _GcScanBudget) -> dict[str, Any]:
    unsafe_roots = [
        name for name in ("generations", "snapshots", "semantic-shards", "zoekt")
        if (
            (root := _direct_component_root(settings, name)) is None
            or not _component_tree_safe(root, nested=name in {"snapshots", "zoekt"}, budget=budget)
        )
    ]
    if unsafe_roots:
        return {
            "dry_run": dry_run,
            "pinned_generations": [], "pinned_snapshots": [], "remove": [],
            "reclaim_bytes": 0, "semantic_gc_blocked": [],
            "reachability_gc_blocked": [
                "managed component root escapes state: " + ", ".join(unsafe_roots)
            ],
        }
    root = generation_root(settings)
    try:
        generation_entries = []
        for entry in root.iterdir() if root.is_dir() else []:
            budget.consume()
            generation_entries.append(entry)
    except OSError:
        generation_entries = []
        reachability_blockers = ["managed generation directory is unreadable"]
    else:
        noncanonical = sorted(
            path.name for path in generation_entries
            if path.name.startswith("generation-")
            and (
                re.fullmatch(r"generation-[0-9]{6,}", path.name) is None
                or not path.is_dir()
            )
        )
        reachability_blockers = [
            "non-canonical managed generation entry blocks GC: " + ", ".join(noncanonical)
        ] if noncanonical else []
    if reachability_blockers:
        return {
            "dry_run": dry_run,
            "pinned_generations": [],
            "pinned_snapshots": [],
            "remove": [],
            "reclaim_bytes": 0,
            "semantic_gc_blocked": [],
            "reachability_gc_blocked": reachability_blockers,
        }
    session_states, reachability_blockers = _session_reachability(settings)
    budget.consume(len(session_states))
    if reachability_blockers:
        return {
            "dry_run": dry_run,
            "pinned_generations": [],
            "pinned_snapshots": [],
            "remove": [],
            "reclaim_bytes": 0,
            "semantic_gc_blocked": [],
            "reachability_gc_blocked": reachability_blockers,
        }
    pinned = _pinned_generations(settings, session_states)
    generation_dirs = sorted(
        (
            path for path in generation_entries
            if re.fullmatch(r"generation-[0-9]{6,}", path.name) and path.is_dir()
        ),
        key=lambda path: path.name,
    )
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
    catalog_rows = catalog_generations(settings, consume=budget.consume)
    retained_generations = [item for item in catalog_rows if item.generation in protected_numbers]
    retained_memberships = {
        (repo, sha)
        for generation in retained_generations
        for repo, sha in generation.snapshots.items()
    } | _legacy_snapshot_memberships(settings, session_states)
    from .index import membership_snapshots

    lexical_memberships = membership_snapshots(settings, consume=budget.consume)
    lexical_removable = sorted(lexical_memberships - retained_memberships)
    snapshot_removable = _snapshot_removals(settings, keep_recent, session_states, budget)
    semantic_removable, semantic_gc_blockers = _semantic_shard_removals(
        settings, retained_generations, budget,
    )
    zoekt_removable = _zoekt_shard_removals(settings, retained_memberships, budget)
    targets = (
        [("generation", path) for path in removable]
        + [("snapshot", path) for path in snapshot_removable]
        + [("semantic_shard", path) for path in semantic_removable]
        + [("zoekt_shard", path) for path in zoekt_removable]
    )
    budget.consume(len(targets) + len(lexical_removable))
    rows = [
        {"kind": kind, "path": str(path), "bytes": _gc_path_bytes(settings, path, budget)}
        for kind, path in targets
    ]
    rows.extend({"kind": "lexical_membership", "path": f"{repo}@{sha}", "bytes": 0} for repo, sha in lexical_removable)
    if not dry_run:
        from .catalog import delete_generations
        from .index import prune_memberships

        delete_generations(settings, {int(path.name.rsplit("-", 1)[-1]) for path in removable})
        for _, item in targets:
            if item.is_dir():
                from .platforms import remove_tree

                remove_tree(item)
            else:
                item.unlink(missing_ok=True)
        prune_memberships(settings, retained_memberships)
    return {
        "dry_run": dry_run,
        "pinned_generations": sorted(pinned),
        "pinned_snapshots": [str(path) for path in sorted(_pinned_snapshot_paths(settings, session_states))],
        "remove": rows,
        "reclaim_bytes": sum(item["bytes"] for item in rows),
        "semantic_gc_blocked": semantic_gc_blockers,
        "reachability_gc_blocked": [],
    }


@workspace_exclusive
def gc(settings: Settings, *, dry_run: bool = True, keep_recent: int = 2) -> dict[str, Any]:
    budget = _GcScanBudget()
    try:
        return _gc(settings, dry_run=dry_run, keep_recent=keep_recent, budget=budget)
    except (OSError, _GcScanIncomplete) as error:
        # All reachability and size accounting completes before the first
        # catalog or filesystem mutation. An incomplete scan is never proof
        # that an artifact is unreachable.
        return {
            "dry_run": dry_run,
            "pinned_generations": [], "pinned_snapshots": [], "remove": [],
            "reclaim_bytes": 0, "semantic_gc_blocked": [],
            "reachability_gc_blocked": [f"GC scan incomplete: {error}"],
        }


def status(settings: Settings) -> dict[str, Any]:
    return {"edition": current_edition(settings), "capabilities": capabilities(settings), "freshness": freshness(settings), "storage": storage(settings)}

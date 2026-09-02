from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .models import installed_packs, pack_compatibility_error
from .platforms import atomic_managed_text_write, read_managed_text

if TYPE_CHECKING:
    from .core import Settings

EDITIONS = ("core", "semantic", "precision")


def _path(settings: Settings) -> Path:
    return settings.state_dir / "edition.json"


def capabilities(settings: Settings) -> dict[str, Any]:
    packs = installed_packs(settings)
    pack_reports = [
        {
            "pack_id": pack.get("pack_id"),
            "capability": pack.get("capability"),
            "verified": pack.get("verified", False),
            "compatibility_error": pack_compatibility_error(pack) if pack.get("verified") and not pack.get("invalid") else None,
        }
        for pack in packs
    ]
    from .catalog import current_generation_ref, generation_root
    from .semantic import semantic_state_compatibility

    atlas = current_generation_ref(settings)
    component = atlas.component("semantic") if atlas is not None else {}
    semantic_path = settings.state_dir / "semantic-index.json"
    semantic_root = settings.state_dir
    component_ready = atlas is not None and component.get("status") == "ready" and component.get("artifact_ref")
    if component_ready:
        semantic_path = settings.state_dir / str(component["artifact_ref"])
        semantic_root = generation_root(settings)
    try:
        semantic_state = json.loads(read_managed_text(
            semantic_root, semantic_path, max_bytes=64 * 1024 * 1024,
        ))
        if not isinstance(semantic_state, dict):
            semantic_state = {}
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        semantic_state = {}
    snapshots = atlas.snapshots if atlas is not None else {
        repo.name: repo.source_sha or "working-tree" for repo in settings.repositories
    }
    semantic_aligned, _ = semantic_state_compatibility(
        settings,
        semantic_state,
        snapshots,
        component=component if component_ready else None,
        verify_artifacts=False,
    )
    semantic_aligned = semantic_aligned and bool(component_ready)
    try:
        from .semantic import _usearch

        vector_backend = _usearch() is not None
    except Exception:
        vector_backend = False
    embedding_pack = any(pack["capability"] in {"embedding", "test"} and pack["verified"] and not pack["compatibility_error"] for pack in pack_reports)
    embedding = embedding_pack and vector_backend
    reranker = any(pack["capability"] == "reranker" and pack["verified"] and not pack["compatibility_error"] for pack in pack_reports)
    semantic_chunks = len(semantic_state.get("entries") or []) + sum(
        len(item.get("entries") or []) for item in semantic_state.get("shards") or [] if isinstance(item, dict)
    )
    from .backends.zoekt import status as zoekt_status

    zoekt = zoekt_status()
    from .index import lexical_generation_ready

    return {
        "lexical_index": lexical_generation_ready(settings, atlas),
        "structural": bool(settings.graph_enabled),
        "embedding": embedding,
        "embedding_pack": embedding_pack,
        "vector_backend": vector_backend,
        "reranker": reranker,
        "semantic_chunks": semantic_chunks,
        "semantic_stale": bool(semantic_state.get("stale")),
        "semantic_aligned": semantic_aligned,
        "semantic_backend": semantic_state.get("backend"),
        "zoekt": {"available": zoekt.available, "reason": zoekt.reason},
        "installed_packs": pack_reports,
    }


def current_edition(settings: Settings) -> str:
    try:
        value = json.loads(read_managed_text(
            settings.state_dir, _path(settings), max_bytes=64 * 1024,
        ))
        edition = str(value.get("edition") or "core")
        return edition if edition in EDITIONS else "core"
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return "core"


def set_edition(settings: Settings, edition: str) -> str:
    edition = edition.lower().strip()
    if edition not in EDITIONS:
        raise ValueError("edition must be core, semantic, or precision")
    available = capabilities(settings)
    if edition in {"semantic", "precision"} and not available["embedding"]:
        if available["embedding_pack"] and not available["vector_backend"]:
            raise ValueError("Semantic edition requires the local USearch vector backend; install project-brain-context[semantic] or a standalone release that includes it")
        reason = next((pack["compatibility_error"] for pack in available["installed_packs"] if pack["capability"] == "embedding" and pack["compatibility_error"]), None)
        raise ValueError("Semantic edition requires an installed, verified compatible local embedding pack" + (f": {reason}" if reason else ""))
    if edition == "precision" and not available["reranker"]:
        reason = next((pack["compatibility_error"] for pack in available["installed_packs"] if pack["capability"] == "reranker" and pack["compatibility_error"]), None)
        raise ValueError("Precision edition requires an installed, verified compatible local reranker pack" + (f": {reason}" if reason else ""))
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    atomic_managed_text_write(
        settings.state_dir, _path(settings), json.dumps({"edition": edition}, indent=2) + "\n",
    )
    return edition

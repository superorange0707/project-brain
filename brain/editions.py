from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .models import installed_packs, pack_compatibility_error

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
    try:
        semantic_state = json.loads((settings.state_dir / "semantic-index.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        semantic_state = {}
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
    return {
        "lexical_index": (settings.state_dir / "search.sqlite3").is_file(),
        "structural": bool(settings.graph_enabled),
        "embedding": embedding,
        "embedding_pack": embedding_pack,
        "vector_backend": vector_backend,
        "reranker": reranker,
        "semantic_chunks": semantic_chunks,
        "semantic_stale": bool(semantic_state.get("stale")),
        "semantic_backend": semantic_state.get("backend"),
        "zoekt": {"available": zoekt.available, "reason": zoekt.reason},
        "installed_packs": pack_reports,
    }


def current_edition(settings: Settings) -> str:
    try:
        value = json.loads(_path(settings).read_text(encoding="utf-8"))
        edition = str(value.get("edition") or "core")
        return edition if edition in EDITIONS else "core"
    except (OSError, json.JSONDecodeError):
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
    _path(settings).write_text(json.dumps({"edition": edition}, indent=2) + "\n", encoding="utf-8")
    return edition

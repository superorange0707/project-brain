from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .core import Settings

SCHEMA_VERSION = 3
ATLAS_MANIFEST_VERSION = 1
COMPONENT_NAMES = (
    "lexical", "zoekt", "structural", "relationships", "experience", "semantic",
    "hierarchy", "typed_graph", "change_intelligence", "semantic_cards",
)


@dataclass(frozen=True)
class AtlasGenerationRef:
    generation: int
    identity: str
    parent_generation: int | None
    source_signature: str
    snapshots: dict[str, str]
    components: dict[str, dict[str, Any]]
    manifest: dict[str, Any]

    def component(self, name: str) -> dict[str, Any]:
        return self.components.get(name, {"status": "unavailable"})


def source_signature(snapshots: dict[str, str]) -> str:
    payload = [[name, snapshots[name]] for name in sorted(snapshots)]
    return "sha256:" + hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode("utf-8")).hexdigest()


def _without_local_state(value: Any) -> Any:
    """Remove publication/runtime fields that cannot define portable identity."""
    if isinstance(value, dict):
        omitted = {"artifact_ref", "created_at", "generated_at", "generation", "indexed_at", "manifest_path"}
        return {
            key: _without_local_state(item)
            for key, item in sorted(value.items())
            if key not in omitted
            and not key.startswith("_")
            and not (key == "path" and isinstance(item, str) and Path(item).is_absolute())
        }
    if isinstance(value, list):
        return [_without_local_state(item) for item in value]
    return value


def canonical_atlas_manifest(manifest: dict[str, Any]) -> str:
    """Serialize only logical serving compatibility, never local paths or timestamps."""
    raw_snapshots = manifest.get("snapshots") or []
    if isinstance(raw_snapshots, dict):
        snapshots = [{"repo": repo, "sha": sha} for repo, sha in raw_snapshots.items()]
    else:
        snapshots = [dict(item) for item in raw_snapshots if isinstance(item, dict)]
    components = manifest.get("components") or {}
    logical = {
        "atlas_manifest_version": int(manifest.get("atlas_manifest_version") or ATLAS_MANIFEST_VERSION),
        "source_signature": str(manifest.get("source_signature") or source_signature({
            str(item.get("repo")): str(item.get("sha") or "working-tree") for item in snapshots
        })),
        "snapshots": sorted(
            ({key: item.get(key) for key in ("repo", "ref", "sha", "parent_snapshot_sha") if item.get(key) is not None} for item in snapshots),
            key=lambda item: (str(item.get("repo")), str(item.get("ref")), str(item.get("sha"))),
        ),
        "schema_manifest": _without_local_state(manifest.get("schema_manifest") or {}),
        "components": {
            name: _without_local_state(components.get(name) or {"status": "unavailable"})
            for name in COMPONENT_NAMES
        },
    }
    return json.dumps(logical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_atlas_identity(manifest: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_atlas_manifest(manifest).encode("utf-8")).hexdigest()


def _content_hash(value: Any) -> str:
    payload = json.dumps(_without_local_state(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def database(settings: Settings) -> Path:
    return settings.state_dir / "catalog.sqlite3"


def connect(settings: Settings) -> sqlite3.Connection:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database(settings), timeout=30)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS repositories (name TEXT PRIMARY KEY, path TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS remote_refs (repo TEXT NOT NULL, ref TEXT NOT NULL, sha TEXT, fetched_at TEXT, PRIMARY KEY(repo, ref));
            CREATE TABLE IF NOT EXISTS snapshots (repo TEXT NOT NULL, ref TEXT, sha TEXT NOT NULL, path TEXT, created_at TEXT NOT NULL, PRIMARY KEY(repo, sha));
            CREATE TABLE IF NOT EXISTS snapshot_files (repo TEXT NOT NULL, sha TEXT NOT NULL, path TEXT NOT NULL, blob_sha TEXT, PRIMARY KEY(repo, sha, path));
            CREATE TABLE IF NOT EXISTS blobs (blob_sha TEXT PRIMARY KEY, size INTEGER, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS paths (repo TEXT NOT NULL, sha TEXT, path TEXT NOT NULL, basename TEXT NOT NULL, stem TEXT NOT NULL, PRIMARY KEY(repo, sha, path));
            CREATE TABLE IF NOT EXISTS symbols (repo TEXT NOT NULL, sha TEXT, symbol TEXT NOT NULL, path TEXT NOT NULL, line INTEGER, PRIMARY KEY(repo, sha, symbol, path, line));
            CREATE TABLE IF NOT EXISTS chunks (chunk_id TEXT PRIMARY KEY, blob_sha TEXT NOT NULL, schema_version TEXT NOT NULL, start_line INTEGER, end_line INTEGER, metadata_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS chunk_membership (chunk_id TEXT NOT NULL, repo TEXT NOT NULL, snapshot_sha TEXT NOT NULL, path TEXT NOT NULL, PRIMARY KEY(chunk_id, repo, snapshot_sha, path));
            CREATE TABLE IF NOT EXISTS index_generations (generation INTEGER PRIMARY KEY, created_at TEXT NOT NULL, manifest_path TEXT NOT NULL, status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS backend_states (backend TEXT NOT NULL, repo TEXT, snapshot_sha TEXT, generation INTEGER, status TEXT NOT NULL, details_json TEXT NOT NULL, PRIMARY KEY(backend, repo, snapshot_sha));
            CREATE TABLE IF NOT EXISTS model_packs (pack_id TEXT PRIMARY KEY, capability TEXT NOT NULL, manifest_json TEXT NOT NULL, installed_at TEXT NOT NULL, verified_at TEXT);
            CREATE TABLE IF NOT EXISTS embedding_cache (cache_key TEXT PRIMARY KEY, pack_id TEXT NOT NULL, dimension INTEGER NOT NULL, vector_json TEXT NOT NULL, created_at TEXT NOT NULL, last_used_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ticket_cases (ticket TEXT PRIMARY KEY, snapshot_signature TEXT, history_cursor TEXT, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS metrics_runs (run_id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS generation_snapshots (
                generation INTEGER NOT NULL,
                repo TEXT NOT NULL,
                snapshot_sha TEXT NOT NULL,
                ref TEXT,
                parent_snapshot_sha TEXT,
                PRIMARY KEY(generation, repo)
            );
            CREATE TABLE IF NOT EXISTS generation_components (
                generation INTEGER NOT NULL,
                component TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                status TEXT NOT NULL,
                content_hash TEXT,
                artifact_ref TEXT,
                details_json TEXT NOT NULL,
                PRIMARY KEY(generation, component)
            );
            CREATE TABLE IF NOT EXISTS atlas_modules (
                module_id TEXT PRIMARY KEY,
                repo TEXT NOT NULL,
                path TEXT NOT NULL,
                name TEXT NOT NULL,
                language TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS generation_modules (
                generation INTEGER NOT NULL,
                module_id TEXT NOT NULL,
                snapshot_sha TEXT NOT NULL,
                PRIMARY KEY(generation, module_id)
            );
            CREATE TABLE IF NOT EXISTS atlas_entities (
                entity_id TEXT PRIMARY KEY,
                repo TEXT NOT NULL,
                module_id TEXT NOT NULL,
                path TEXT NOT NULL,
                line_start INTEGER NOT NULL,
                line_end INTEGER NOT NULL,
                qualified_name TEXT NOT NULL,
                simple_name TEXT NOT NULL,
                signature TEXT NOT NULL,
                language TEXT NOT NULL,
                kind TEXT NOT NULL,
                parent_entity_id TEXT,
                blob_sha TEXT NOT NULL,
                extractor TEXT NOT NULL,
                extractor_version TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS generation_entities (
                generation INTEGER NOT NULL,
                entity_id TEXT NOT NULL,
                snapshot_sha TEXT NOT NULL,
                PRIMARY KEY(generation, entity_id)
            );
            CREATE TABLE IF NOT EXISTS atlas_regions (
                region_id TEXT PRIMARY KEY,
                repo TEXT NOT NULL,
                path TEXT NOT NULL,
                line_start INTEGER NOT NULL,
                line_end INTEGER NOT NULL,
                blob_sha TEXT NOT NULL,
                kind TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS generation_regions (
                generation INTEGER NOT NULL,
                region_id TEXT NOT NULL,
                snapshot_sha TEXT NOT NULL,
                PRIMARY KEY(generation, region_id)
            );
            CREATE TABLE IF NOT EXISTS atlas_edges (
                edge_id TEXT PRIMARY KEY,
                edge_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                repo TEXT NOT NULL,
                path TEXT NOT NULL,
                line_start INTEGER NOT NULL,
                line_end INTEGER NOT NULL,
                blob_sha TEXT NOT NULL,
                extractor TEXT NOT NULL,
                extractor_version TEXT NOT NULL,
                confidence REAL NOT NULL,
                metadata_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS generation_edges (
                generation INTEGER NOT NULL,
                edge_id TEXT NOT NULL,
                snapshot_sha TEXT NOT NULL,
                PRIMARY KEY(generation, edge_id)
            );
            CREATE TABLE IF NOT EXISTS atlas_cards (
                card_id TEXT PRIMARY KEY,
                level TEXT NOT NULL,
                target_id TEXT NOT NULL,
                repo TEXT NOT NULL,
                module_id TEXT,
                entity_id TEXT,
                path TEXT,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS generation_cards (
                generation INTEGER NOT NULL,
                card_id TEXT NOT NULL,
                snapshot_sha TEXT NOT NULL,
                PRIMARY KEY(generation, card_id)
            );
            CREATE TABLE IF NOT EXISTS atlas_changes (
                change_id TEXT PRIMARY KEY,
                repo TEXT NOT NULL,
                commit_sha TEXT NOT NULL,
                committed_at TEXT,
                ticket TEXT,
                path TEXT NOT NULL,
                old_path TEXT,
                status TEXT NOT NULL,
                additions INTEGER,
                deletions INTEGER,
                metadata_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS generation_changes (
                generation INTEGER NOT NULL,
                change_id TEXT NOT NULL,
                snapshot_sha TEXT NOT NULL,
                PRIMARY KEY(generation, change_id)
            );
            CREATE TABLE IF NOT EXISTS atlas_refresh_deltas (
                generation INTEGER PRIMARY KEY,
                parent_generation INTEGER,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS atlas_retrieval_cache (
                generation INTEGER NOT NULL,
                cache_key TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_used_at TEXT NOT NULL,
                PRIMARY KEY(generation, cache_key)
            );
            CREATE TABLE IF NOT EXISTS investigation_records (
                record_id TEXT PRIMARY KEY,
                ticket TEXT NOT NULL,
                generation INTEGER,
                objective TEXT NOT NULL,
                entity_ids_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                outcome TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS atlas_modules_repo_path ON atlas_modules(repo, path);
            CREATE INDEX IF NOT EXISTS atlas_entities_name ON atlas_entities(simple_name, qualified_name);
            CREATE INDEX IF NOT EXISTS atlas_entities_repo_path ON atlas_entities(repo, path);
            CREATE INDEX IF NOT EXISTS atlas_edges_source ON atlas_edges(source_id, edge_type);
            CREATE INDEX IF NOT EXISTS atlas_edges_target ON atlas_edges(target_id, edge_type);
            CREATE INDEX IF NOT EXISTS atlas_cards_repo_level ON atlas_cards(repo, level);
            CREATE INDEX IF NOT EXISTS atlas_changes_repo_path ON atlas_changes(repo, path);
            CREATE INDEX IF NOT EXISTS atlas_cache_last_used ON atlas_retrieval_cache(last_used_at);
            """
        )
        value = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        try:
            old_version = int(value[0]) if value else 0
        except (TypeError, ValueError) as error:
            raise sqlite3.DatabaseError("catalog schema version is invalid") from error
        if old_version > SCHEMA_VERSION:
            raise sqlite3.DatabaseError(f"catalog schema {value[0]} is newer than this Project Brain")
        if old_version < 2:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(embedding_cache)")}
            if "last_used_at" not in columns:
                connection.execute("ALTER TABLE embedding_cache ADD COLUMN last_used_at TEXT")
                connection.execute("UPDATE embedding_cache SET last_used_at=created_at WHERE last_used_at IS NULL")
        if old_version < 3:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(index_generations)")}
            for name, kind in {
                "identity": "TEXT",
                "parent_generation": "INTEGER",
                "source_signature": "TEXT",
                "schema_manifest_json": "TEXT",
            }.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE index_generations ADD COLUMN {name} {kind}")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS index_generations_identity "
            "ON index_generations(identity) WHERE identity IS NOT NULL"
        )
        current = connection.execute("SELECT value FROM metadata WHERE key='current_generation'").fetchone()
        if not current:
            row = connection.execute(
                "SELECT generation FROM index_generations WHERE status='current' ORDER BY generation DESC LIMIT 1"
            ).fetchone()
            if row:
                connection.execute("INSERT INTO metadata(key, value) VALUES('current_generation', ?)", (str(row[0]),))
        connection.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
        connection.commit()
        return connection
    except Exception:
        connection.close()
        raise


def generation_root(settings: Settings) -> Path:
    return settings.state_dir / "generations"


def _generation_ref(connection: sqlite3.Connection, generation: int) -> AtlasGenerationRef | None:
    row = connection.execute(
        "SELECT generation, identity, parent_generation, source_signature, schema_manifest_json, manifest_path "
        "FROM index_generations WHERE generation=? AND status IN ('current', 'retained')",
        (generation,),
    ).fetchone()
    if not row:
        return None
    snapshots = {
        str(repo): str(sha)
        for repo, sha in connection.execute(
            "SELECT repo, snapshot_sha FROM generation_snapshots WHERE generation=? ORDER BY repo", (generation,)
        )
    }
    components: dict[str, dict[str, Any]] = {}
    for name, schema_version, status, content_hash, artifact_ref, details_json in connection.execute(
        "SELECT component, schema_version, status, content_hash, artifact_ref, details_json "
        "FROM generation_components WHERE generation=? ORDER BY component",
        (generation,),
    ):
        try:
            details = json.loads(details_json)
        except (TypeError, json.JSONDecodeError):
            details = {}
        components[str(name)] = {
            "schema_version": str(schema_version),
            "status": str(status),
            "content_hash": content_hash,
            "artifact_ref": artifact_ref,
            "details": details if isinstance(details, dict) else {},
        }
    try:
        manifest = json.loads(Path(str(row[5])).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = {
            "generation": int(row[0]),
            "identity": row[1],
            "parent_generation": row[2],
            "source_signature": row[3],
            "snapshots": [{"repo": repo, "sha": sha} for repo, sha in sorted(snapshots.items())],
            "components": components,
        }
    identity = str(row[1] or manifest.get("identity") or "")
    signature = str(row[3] or manifest.get("source_signature") or source_signature(snapshots))
    return AtlasGenerationRef(int(row[0]), identity, row[2], signature, snapshots, components, manifest)


def resolve_generation(
    settings: Settings,
    *,
    generation: int | None = None,
    identity: str | None = None,
    current: bool = False,
) -> AtlasGenerationRef | None:
    try:
        connection = connect(settings)
    except sqlite3.Error:
        return None
    try:
        if current:
            row = connection.execute("SELECT value FROM metadata WHERE key='current_generation'").fetchone()
            generation = int(row[0]) if row else None
        elif identity is not None:
            row = connection.execute("SELECT generation FROM index_generations WHERE identity=?", (identity,)).fetchone()
            generation = int(row[0]) if row else None
        return _generation_ref(connection, int(generation)) if generation is not None else None
    except (sqlite3.Error, TypeError, ValueError):
        return None
    finally:
        connection.close()


def current_generation_ref(settings: Settings) -> AtlasGenerationRef | None:
    return resolve_generation(settings, current=True)


def current_generation(settings: Settings) -> dict[str, Any] | None:
    ref = current_generation_ref(settings)
    return dict(ref.manifest) if ref is not None else None


def matching_generations(settings: Settings, snapshots: dict[str, str]) -> list[AtlasGenerationRef]:
    try:
        connection = connect(settings)
    except sqlite3.Error:
        return []
    try:
        rows = connection.execute(
            "SELECT generation FROM index_generations WHERE status IN ('current', 'retained') ORDER BY generation"
        )
        return [ref for (number,) in rows if (ref := _generation_ref(connection, int(number))) and ref.snapshots == snapshots]
    finally:
        connection.close()


def generations(settings: Settings) -> list[AtlasGenerationRef]:
    try:
        connection = connect(settings)
    except sqlite3.Error:
        return []
    try:
        return [
            ref for (number,) in connection.execute(
                "SELECT generation FROM index_generations WHERE status IN ('current', 'retained') ORDER BY generation"
            )
            if (ref := _generation_ref(connection, int(number))) is not None
        ]
    finally:
        connection.close()


def delete_generations(settings: Settings, generation_numbers: set[int]) -> None:
    if not generation_numbers:
        return
    connection = connect(settings)
    try:
        current = connection.execute("SELECT value FROM metadata WHERE key='current_generation'").fetchone()
        if current and int(current[0]) in generation_numbers:
            raise sqlite3.IntegrityError("cannot delete the current Atlas generation")
        connection.execute("BEGIN IMMEDIATE")
        for number in sorted(generation_numbers):
            for table in (
                "generation_modules", "generation_entities", "generation_regions", "generation_edges",
                "generation_cards", "generation_changes", "atlas_refresh_deltas", "atlas_retrieval_cache",
            ):
                connection.execute(f"DELETE FROM {table} WHERE generation=?", (number,))
            connection.execute("DELETE FROM generation_components WHERE generation=?", (number,))
            connection.execute("DELETE FROM generation_snapshots WHERE generation=?", (number,))
            connection.execute("DELETE FROM index_generations WHERE generation=? AND status<>'current'", (number,))
        for table, membership, key in (
            ("atlas_modules", "generation_modules", "module_id"),
            ("atlas_entities", "generation_entities", "entity_id"),
            ("atlas_regions", "generation_regions", "region_id"),
            ("atlas_edges", "generation_edges", "edge_id"),
            ("atlas_cards", "generation_cards", "card_id"),
            ("atlas_changes", "generation_changes", "change_id"),
        ):
            connection.execute(
                f"DELETE FROM {table} WHERE NOT EXISTS (SELECT 1 FROM {membership} g WHERE g.{key}={table}.{key})"
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def collect_generation_components(
    settings: Settings,
    state: dict[str, object],
    *,
    semantic_failed: bool = False,
    atlas_payload: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Verify current projections and turn them into one generation component manifest."""
    from .index import SCHEMA_VERSION as LEXICAL_SCHEMA_VERSION, lexical_component
    from .semantic import (
        semantic_schema_version,
        semantic_snapshots,
        semantic_state_compatibility,
    )

    snapshots = {
        name: str(raw.get("sha") or "working-tree")
        for name, raw in state.items()
        if isinstance(raw, dict)
    }
    components: dict[str, dict[str, Any]] = {"lexical": lexical_component(settings, state)}

    zoekt_rows: list[dict[str, str]] = []
    for repo, sha in sorted(snapshots.items()):
        path = settings.state_dir / "zoekt" / repo / sha / "brain-shard.json"
        value = _load_json(path)
        if value.get("source_sha") == sha:
            zoekt_rows.append({"repo": repo, "snapshot": sha})
    components["zoekt"] = {
        "schema_version": "1",
        "status": "ready" if len(zoekt_rows) == len(snapshots) else "unavailable",
        "content_hash": _content_hash(zoekt_rows) if zoekt_rows else None,
        "details": {"shards": zoekt_rows, "reason": None if len(zoekt_rows) == len(snapshots) else "not all immutable shards are available"},
    }

    graph_state = _load_json(settings.state_dir / "graphs.json")
    aligned_graphs = sorted(
        name for name, sha in snapshots.items()
        if isinstance(graph_state.get(name), dict) and str(graph_state[name].get("sha")) == sha
    )
    components["structural"] = {
        "schema_version": "codebase-memory-mcp-v1",
        "status": "degraded",
        "content_hash": _content_hash({name: graph_state[name] for name in aligned_graphs}) if aligned_graphs else None,
        "details": {
            "aligned_repositories": aligned_graphs,
            "reason": "backend projects are not immutable snapshot-addressed artifacts",
        },
    }

    relationships_path = settings.state_dir / "relationships.json"
    relationships = _load_json(relationships_path)
    relationship_sources = {
        str(item[0]): str(item[2] or "working-tree")
        for item in relationships.get("sources") or []
        if isinstance(item, list) and len(item) >= 3
    }
    relationships_ready = relationships.get("version") == 1 and relationship_sources == snapshots
    components["relationships"] = {
        "schema_version": str(relationships.get("version") or 1),
        "status": "ready" if relationships_ready else "unavailable",
        "content_hash": _content_hash(relationships) if relationships_ready else None,
        "details": {"source_signature": source_signature(snapshots)},
        **({"_artifact_source": str(relationships_path)} if relationships_ready else {}),
    }

    experience_path = settings.state_dir / "ticket-history.json"
    experience = _load_json(experience_path)
    experience_sources = {
        str(name): str(value.get("sha") or "working-tree")
        for name, value in (experience.get("repositories") or {}).items()
        if isinstance(value, dict)
    }
    experience_ready = experience.get("version") == 1 and experience_sources == snapshots
    components["experience"] = {
        "schema_version": str(experience.get("version") or 1),
        "status": "ready" if experience_ready else "unavailable",
        "content_hash": _content_hash(experience) if experience_ready else None,
        "details": {"cutoffs": experience_sources},
        **({"_artifact_source": str(experience_path)} if experience_ready else {}),
    }

    semantic_path = settings.state_dir / "semantic-index.json"
    semantic = _load_json(semantic_path)
    semantic_sources = semantic_snapshots(semantic)
    semantic_ready, semantic_reason = semantic_state_compatibility(settings, semantic, snapshots)
    if semantic_failed:
        semantic_ready = False
        semantic_reason = "Semantic construction failed for this Atlas generation."
    components["semantic"] = {
        "schema_version": semantic_schema_version(),
        "status": "ready" if semantic_ready else "unavailable",
        "content_hash": _content_hash(semantic) if semantic_ready else None,
        "details": {
            "pack_id": semantic.get("pack_id"),
            "dimension": semantic.get("dimension"),
            "backend": semantic.get("backend"),
            "snapshots": semantic_sources,
            "source_signature": source_signature(semantic_sources) if semantic_sources else None,
            "reason": semantic_reason,
        },
        **({"_artifact_source": str(semantic_path)} if semantic_ready else {}),
    }
    if atlas_payload is not None:
        from .atlas import atlas_components

        components.update(atlas_components(atlas_payload))
    components["lexical"]["schema_version"] = str(LEXICAL_SCHEMA_VERSION)
    return components


def publish_current_components(
    settings: Settings,
    *,
    semantic_failed: bool = False,
    atlas_payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Republish aligned component state after a standalone optional-backend build."""
    state = _load_json(settings.state_dir / "indexes.json")
    if not state:
        return None
    if atlas_payload is None:
        from .atlas import build_atlas

        atlas_payload = build_atlas(settings, state)
    components = collect_generation_components(
        settings, state, semantic_failed=semantic_failed, atlas_payload=atlas_payload,
    )
    return publish_generation(
        settings,
        state,
        backends=["sqlite-fts5"] + (["zoekt"] if components["zoekt"]["status"] == "ready" else []),
        components=components,
        atlas_payload=atlas_payload,
    )


def publish_generation(
    settings: Settings,
    state: dict[str, object],
    *,
    backends: list[str] | None = None,
    components: dict[str, dict[str, Any]] | None = None,
    atlas_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Publish one immutable serving manifest; the catalog pointer is authoritative."""
    root = generation_root(settings)
    root.mkdir(parents=True, exist_ok=True)
    connection = connect(settings)
    final: Path | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        parent_row = connection.execute("SELECT value FROM metadata WHERE key='current_generation'").fetchone()
        parent = int(parent_row[0]) if parent_row else None
        row = connection.execute("SELECT COALESCE(MAX(generation), 0) FROM index_generations").fetchone()
        number = int(row[0] or 0) + 1
        created_at = datetime.now(UTC).isoformat()
        snapshots = [
            {"repo": name, "ref": data.get("ref"), "sha": data.get("sha") or "working-tree"}
            for name, raw in sorted(state.items())
            if isinstance(raw, dict)
            for data in [raw]
        ]
        component_values = {
            name: dict((components or {}).get(name) or {"schema_version": "1", "status": "unavailable"})
            for name in COMPONENT_NAMES
        }
        if component_values["lexical"].get("status") != "ready":
            raise sqlite3.IntegrityError("Atlas generation requires an aligned lexical component")
        signature = source_signature({str(item["repo"]): str(item["sha"]) for item in snapshots})
        schema_manifest = {
            "catalog": SCHEMA_VERSION,
            "components": {name: str(value.get("schema_version") or "1") for name, value in component_values.items()},
        }
        logical = {
            "atlas_manifest_version": ATLAS_MANIFEST_VERSION,
            "source_signature": signature,
            "snapshots": snapshots,
            "schema_manifest": schema_manifest,
            "components": component_values,
        }
        identity = canonical_atlas_identity(logical)
        existing = connection.execute("SELECT generation FROM index_generations WHERE identity=?", (identity,)).fetchone()
        if existing:
            existing_number = int(existing[0])
            connection.execute("UPDATE index_generations SET status='retained' WHERE status='current' AND generation<>?", (existing_number,))
            connection.execute("UPDATE index_generations SET status='current' WHERE generation=?", (existing_number,))
            connection.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES('current_generation', ?)", (str(existing_number),))
            connection.commit()
            ref = _generation_ref(connection, existing_number)
            if ref is None:
                raise sqlite3.DatabaseError("reused Atlas generation is unavailable")
            _write_current_projection(root, existing_number)
            return dict(ref.manifest)
        manifest = {
            **logical,
            "generation": number,
            "identity": identity,
            "parent_generation": parent,
            "created_at": created_at,
            "backends": backends or ["sqlite-fts5"],
        }
        build = Path(tempfile.mkdtemp(prefix=f"build-{number:06d}-", dir=root))
        try:
            persisted_components: dict[str, dict[str, Any]] = {}
            for name, value in component_values.items():
                persisted = {key: item for key, item in value.items() if not key.startswith("_")}
                source = value.get("_artifact_source")
                if source:
                    source_path = Path(str(source))
                    target = build / f"{name}{source_path.suffix or '.json'}"
                    shutil.copy2(source_path, target)
                    persisted["artifact_ref"] = str(Path("generations") / f"generation-{number:06d}" / target.name)
                persisted_components[name] = persisted
            manifest["components"] = persisted_components
            (build / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            final = root / f"generation-{number:06d}"
            build.replace(final)
        except Exception:
            if build.exists():
                shutil.rmtree(build, ignore_errors=True)
            raise
        connection.execute(
            "INSERT INTO index_generations(generation, created_at, manifest_path, status, identity, parent_generation, source_signature, schema_manifest_json) "
            "VALUES (?, ?, ?, 'building', ?, ?, ?, ?)",
            (number, created_at, str(final / "manifest.json"), identity, parent, signature, json.dumps(schema_manifest, sort_keys=True)),
        )
        for snapshot in snapshots:
            connection.execute(
                "INSERT OR REPLACE INTO repositories(name, path, updated_at) VALUES (?, ?, ?)",
                (snapshot["repo"], str(next(repo.path for repo in settings.repositories if repo.name == snapshot["repo"])), created_at),
            )
            if snapshot["sha"]:
                connection.execute(
                    "INSERT OR REPLACE INTO snapshots(repo, ref, sha, path, created_at) VALUES (?, ?, ?, ?, ?)",
                    (snapshot["repo"], snapshot["ref"], snapshot["sha"], None, created_at),
                )
            connection.execute(
                "INSERT INTO generation_snapshots(generation, repo, snapshot_sha, ref, parent_snapshot_sha) VALUES (?, ?, ?, ?, ?)",
                (number, snapshot["repo"], snapshot["sha"], snapshot.get("ref"), snapshot.get("parent_snapshot_sha")),
            )
        for name, value in manifest["components"].items():
            details = value.get("details") if isinstance(value.get("details"), dict) else {}
            connection.execute(
                "INSERT INTO generation_components(generation, component, schema_version, status, content_hash, artifact_ref, details_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (number, name, str(value.get("schema_version") or "1"), str(value.get("status") or "unavailable"), value.get("content_hash"), value.get("artifact_ref"), json.dumps(details, sort_keys=True)),
            )
        if atlas_payload:
            _publish_atlas_payload(connection, number, snapshots, parent, atlas_payload)
        connection.execute("UPDATE index_generations SET status='retained' WHERE status='current'")
        connection.execute("UPDATE index_generations SET status='current' WHERE generation=?", (number,))
        connection.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES('current_generation', ?)", (str(number),))
        connection.commit()
        _write_current_projection(root, number)
        return manifest
    except Exception:
        connection.rollback()
        if final is not None and final.exists():
            shutil.rmtree(final, ignore_errors=True)
        raise
    finally:
        connection.close()


def _publish_atlas_payload(
    connection: sqlite3.Connection,
    generation: int,
    snapshots: list[dict[str, Any]],
    parent_generation: int | None,
    payload: dict[str, Any],
) -> None:
    """Persist normalized Atlas facts in the generation publication transaction."""
    snapshot_by_repo = {str(item["repo"]): str(item["sha"]) for item in snapshots}
    rows = payload.get("modules") or []
    for item in rows:
        connection.execute(
            "INSERT OR IGNORE INTO atlas_modules(module_id,repo,path,name,language,fingerprint,metadata_json) "
            "VALUES (?,?,?,?,?,?,?)",
            (item["module_id"], item["repo"], item["path"], item["name"], item["language"],
             item["fingerprint"], json.dumps(item.get("metadata") or {}, sort_keys=True)),
        )
        connection.execute(
            "INSERT INTO generation_modules(generation,module_id,snapshot_sha) VALUES (?,?,?)",
            (generation, item["module_id"], snapshot_by_repo[item["repo"]]),
        )
    for item in payload.get("entities") or []:
        connection.execute(
            "INSERT OR IGNORE INTO atlas_entities(entity_id,repo,module_id,path,line_start,line_end,qualified_name,"
            "simple_name,signature,language,kind,parent_entity_id,blob_sha,extractor,extractor_version,fingerprint,metadata_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item["entity_id"], item["repo"], item["module_id"], item["path"], item["line_start"], item["line_end"],
             item["qualified_name"], item["simple_name"], item["signature"], item["language"], item["kind"],
             item.get("parent_entity_id"), item["blob_sha"], item["extractor"], item["extractor_version"],
             item["fingerprint"], json.dumps(item.get("metadata") or {}, sort_keys=True)),
        )
        connection.execute(
            "INSERT INTO generation_entities(generation,entity_id,snapshot_sha) VALUES (?,?,?)",
            (generation, item["entity_id"], snapshot_by_repo[item["repo"]]),
        )
    for item in payload.get("regions") or []:
        connection.execute(
            "INSERT OR IGNORE INTO atlas_regions(region_id,repo,path,line_start,line_end,blob_sha,kind,fingerprint,metadata_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (item["region_id"], item["repo"], item["path"], item["line_start"], item["line_end"], item["blob_sha"],
             item["kind"], item["fingerprint"], json.dumps(item.get("metadata") or {}, sort_keys=True)),
        )
        connection.execute(
            "INSERT INTO generation_regions(generation,region_id,snapshot_sha) VALUES (?,?,?)",
            (generation, item["region_id"], snapshot_by_repo[item["repo"]]),
        )
    for item in payload.get("edges") or []:
        connection.execute(
            "INSERT OR IGNORE INTO atlas_edges(edge_id,edge_type,source_id,target_id,repo,path,line_start,line_end,blob_sha,"
            "extractor,extractor_version,confidence,metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item["edge_id"], item["edge_type"], item["source_id"], item["target_id"], item["repo"], item["path"],
             item["line_start"], item["line_end"], item["blob_sha"], item["extractor"], item["extractor_version"],
             item["confidence"], json.dumps(item.get("metadata") or {}, sort_keys=True)),
        )
        connection.execute(
            "INSERT INTO generation_edges(generation,edge_id,snapshot_sha) VALUES (?,?,?)",
            (generation, item["edge_id"], snapshot_by_repo.get(item["repo"], "working-tree")),
        )
    for item in payload.get("cards") or []:
        connection.execute(
            "INSERT OR IGNORE INTO atlas_cards(card_id,level,target_id,repo,module_id,entity_id,path,content,content_hash,metadata_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (item["card_id"], item["level"], item["target_id"], item["repo"], item.get("module_id"),
             item.get("entity_id"), item.get("path"), item["content"], item["content_hash"],
             json.dumps(item.get("metadata") or {}, sort_keys=True)),
        )
        connection.execute(
            "INSERT INTO generation_cards(generation,card_id,snapshot_sha) VALUES (?,?,?)",
            (generation, item["card_id"], snapshot_by_repo[item["repo"]]),
        )
    for item in payload.get("changes") or []:
        connection.execute(
            "INSERT OR IGNORE INTO atlas_changes(change_id,repo,commit_sha,committed_at,ticket,path,old_path,status,"
            "additions,deletions,metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (item["change_id"], item["repo"], item["commit_sha"], item.get("committed_at"), item.get("ticket"),
             item["path"], item.get("old_path"), item["status"], item.get("additions"), item.get("deletions"),
             json.dumps(item.get("metadata") or {}, sort_keys=True)),
        )
        connection.execute(
            "INSERT INTO generation_changes(generation,change_id,snapshot_sha) VALUES (?,?,?)",
            (generation, item["change_id"], snapshot_by_repo[item["repo"]]),
        )
    connection.execute(
        "INSERT INTO atlas_refresh_deltas(generation,parent_generation,payload_json) VALUES (?,?,?)",
        (generation, parent_generation, json.dumps(payload.get("delta") or {}, sort_keys=True)),
    )


def _write_current_projection(root: Path, generation: int) -> None:
    temporary = root / f"CURRENT-{generation:06d}.tmp"
    try:
        temporary.write_text(f"generation-{generation:06d}\n", encoding="utf-8")
        os.replace(temporary, root / "CURRENT")
    except OSError:
        temporary.unlink(missing_ok=True)


def record_metric_run(settings: Settings, kind: str, payload: dict[str, Any]) -> None:
    import hashlib

    connection = connect(settings)
    try:
        created_at = datetime.now(UTC).isoformat()
        digest = hashlib.sha256((kind + created_at + json.dumps(payload, sort_keys=True)).encode()).hexdigest()
        connection.execute(
            "INSERT INTO metrics_runs(run_id, kind, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (digest, kind, json.dumps(payload, sort_keys=True), created_at),
        )
        connection.commit()
    finally:
        connection.close()


def record_index_catalog(settings: Settings, state: dict[str, object]) -> None:
    """Mirror immutable path/blob identities from the lexical index into the catalog."""
    search_path = settings.state_dir / "search.sqlite3"
    if not search_path.is_file():
        return
    source = sqlite3.connect(search_path)
    target: sqlite3.Connection | None = None
    try:
        target = connect(settings)
        for repo, raw in state.items():
            if not isinstance(raw, dict):
                continue
            sha = str(raw.get("sha") or "working-tree")
            target.execute("DELETE FROM snapshot_files WHERE repo=? AND sha=?", (repo, sha))
            target.execute("DELETE FROM paths WHERE repo=? AND sha=?", (repo, sha))
            version_two = source.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='file_membership'"
            ).fetchone()
            table = "file_membership" if version_two else "files"
            snapshot_filter = " AND f.snapshot_sha=?" if version_two else ""
            arguments = (repo, sha) if version_two else (repo,)
            rows = source.execute(
                f"SELECT f.path, f.blob, b.size FROM {table} f JOIN blobs b ON b.blob=f.blob "
                f"WHERE f.repo=?{snapshot_filter}",
                arguments,
            )
            for path, blob, size in rows:
                value = str(path)
                target.execute("INSERT OR IGNORE INTO blobs(blob_sha, size, created_at) VALUES (?, ?, ?)", (blob, int(size), datetime.now(UTC).isoformat()))
                target.execute("INSERT INTO snapshot_files(repo, sha, path, blob_sha) VALUES (?, ?, ?, ?)", (repo, sha, value, blob))
                file = Path(value)
                target.execute("INSERT INTO paths(repo, sha, path, basename, stem) VALUES (?, ?, ?, ?, ?)", (repo, sha, value, file.name, file.stem))
        target.commit()
    finally:
        source.close()
        if target is not None:
            target.close()


def diagnose(settings: Settings) -> str | None:
    try:
        connection = connect(settings)
        try:
            row = connection.execute("PRAGMA integrity_check").fetchone()
            return None if row and row[0] == "ok" else str(row[0] if row else "unknown catalog integrity failure")
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return str(exc)

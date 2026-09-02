from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .platforms import (
    atomic_managed_bytes_write,
    atomic_managed_text_write,
    connect_managed_sqlite,
    read_managed_bytes,
    read_managed_text,
)

if TYPE_CHECKING:
    from .core import Settings

SCHEMA_VERSION = 12
ATLAS_MANIFEST_VERSION = 1
COMPONENT_NAMES = (
    "lexical", "zoekt", "structural", "relationships", "experience", "semantic",
    "hierarchy", "typed_graph", "change_intelligence", "semantic_cards",
    "runtime_anchors", "java_intelligence",
)

TERM_INDEX_INVALIDATION_TRIGGERS = (
    "invalidate_card_index_generation_insert", "invalidate_card_index_generation_update",
    "invalidate_card_index_generation_delete", "invalidate_card_index_term_insert",
    "invalidate_card_index_term_update", "invalidate_card_index_term_delete",
    "invalidate_card_index_source_update", "invalidate_card_index_source_delete",
    "invalidate_change_index_generation_insert", "invalidate_change_index_generation_update",
    "invalidate_change_index_generation_delete", "invalidate_change_index_term_insert",
    "invalidate_change_index_term_update", "invalidate_change_index_term_delete",
    "invalidate_change_index_source_update", "invalidate_change_index_source_delete",
)

RUNTIME_ANCHOR_INDEX_INVALIDATION_TRIGGERS = (
    "invalidate_anchor_index_generation_insert", "invalidate_anchor_index_generation_update",
    "invalidate_anchor_index_generation_delete", "invalidate_anchor_index_term_insert",
    "invalidate_anchor_index_term_update", "invalidate_anchor_index_term_delete",
    "invalidate_anchor_index_source_update", "invalidate_anchor_index_source_delete",
)

_TERM_INDEX_INVALIDATION_DDL = """
CREATE TRIGGER IF NOT EXISTS invalidate_card_index_generation_insert AFTER INSERT ON generation_cards
BEGIN DELETE FROM generation_card_indexes WHERE generation=NEW.generation; END;
CREATE TRIGGER IF NOT EXISTS invalidate_card_index_generation_update AFTER UPDATE ON generation_cards
BEGIN
    DELETE FROM generation_card_indexes WHERE generation=OLD.generation;
    DELETE FROM generation_card_indexes WHERE generation=NEW.generation;
END;
CREATE TRIGGER IF NOT EXISTS invalidate_card_index_generation_delete AFTER DELETE ON generation_cards
BEGIN DELETE FROM generation_card_indexes WHERE generation=OLD.generation; END;
CREATE TRIGGER IF NOT EXISTS invalidate_card_index_term_insert AFTER INSERT ON atlas_card_terms
BEGIN
    DELETE FROM generation_card_indexes WHERE generation IN
        (SELECT generation FROM generation_cards WHERE card_id=NEW.card_id);
END;
CREATE TRIGGER IF NOT EXISTS invalidate_card_index_term_update AFTER UPDATE ON atlas_card_terms
BEGIN
    DELETE FROM generation_card_indexes WHERE generation IN
        (SELECT generation FROM generation_cards WHERE card_id IN (OLD.card_id, NEW.card_id));
END;
CREATE TRIGGER IF NOT EXISTS invalidate_card_index_term_delete AFTER DELETE ON atlas_card_terms
BEGIN
    DELETE FROM generation_card_indexes WHERE generation IN
        (SELECT generation FROM generation_cards WHERE card_id=OLD.card_id);
END;
CREATE TRIGGER IF NOT EXISTS invalidate_card_index_source_update AFTER UPDATE ON atlas_cards
BEGIN
    DELETE FROM generation_card_indexes WHERE generation IN
        (SELECT generation FROM generation_cards WHERE card_id IN (OLD.card_id, NEW.card_id));
END;
CREATE TRIGGER IF NOT EXISTS invalidate_card_index_source_delete AFTER DELETE ON atlas_cards
BEGIN
    DELETE FROM generation_card_indexes WHERE generation IN
        (SELECT generation FROM generation_cards WHERE card_id=OLD.card_id);
END;

CREATE TRIGGER IF NOT EXISTS invalidate_change_index_generation_insert AFTER INSERT ON generation_changes
BEGIN DELETE FROM generation_change_indexes WHERE generation=NEW.generation; END;
CREATE TRIGGER IF NOT EXISTS invalidate_change_index_generation_update AFTER UPDATE ON generation_changes
BEGIN
    DELETE FROM generation_change_indexes WHERE generation=OLD.generation;
    DELETE FROM generation_change_indexes WHERE generation=NEW.generation;
END;
CREATE TRIGGER IF NOT EXISTS invalidate_change_index_generation_delete AFTER DELETE ON generation_changes
BEGIN DELETE FROM generation_change_indexes WHERE generation=OLD.generation; END;
CREATE TRIGGER IF NOT EXISTS invalidate_change_index_term_insert AFTER INSERT ON atlas_change_terms
BEGIN
    DELETE FROM generation_change_indexes WHERE generation IN
        (SELECT generation FROM generation_changes WHERE change_id=NEW.change_id);
END;
CREATE TRIGGER IF NOT EXISTS invalidate_change_index_term_update AFTER UPDATE ON atlas_change_terms
BEGIN
    DELETE FROM generation_change_indexes WHERE generation IN
        (SELECT generation FROM generation_changes WHERE change_id IN (OLD.change_id, NEW.change_id));
END;
CREATE TRIGGER IF NOT EXISTS invalidate_change_index_term_delete AFTER DELETE ON atlas_change_terms
BEGIN
    DELETE FROM generation_change_indexes WHERE generation IN
        (SELECT generation FROM generation_changes WHERE change_id=OLD.change_id);
END;
CREATE TRIGGER IF NOT EXISTS invalidate_change_index_source_update AFTER UPDATE ON atlas_changes
BEGIN
    DELETE FROM generation_change_indexes WHERE generation IN
        (SELECT generation FROM generation_changes WHERE change_id IN (OLD.change_id, NEW.change_id));
END;
CREATE TRIGGER IF NOT EXISTS invalidate_change_index_source_delete AFTER DELETE ON atlas_changes
BEGIN
    DELETE FROM generation_change_indexes WHERE generation IN
        (SELECT generation FROM generation_changes WHERE change_id=OLD.change_id);
END;
"""

_RUNTIME_ANCHOR_INDEX_INVALIDATION_DDL = """
CREATE TRIGGER IF NOT EXISTS invalidate_anchor_index_generation_insert AFTER INSERT ON generation_runtime_anchors
BEGIN DELETE FROM generation_runtime_anchor_indexes WHERE generation=NEW.generation; END;
CREATE TRIGGER IF NOT EXISTS invalidate_anchor_index_generation_update AFTER UPDATE ON generation_runtime_anchors
BEGIN
    DELETE FROM generation_runtime_anchor_indexes WHERE generation=OLD.generation;
    DELETE FROM generation_runtime_anchor_indexes WHERE generation=NEW.generation;
END;
CREATE TRIGGER IF NOT EXISTS invalidate_anchor_index_generation_delete AFTER DELETE ON generation_runtime_anchors
BEGIN DELETE FROM generation_runtime_anchor_indexes WHERE generation=OLD.generation; END;
CREATE TRIGGER IF NOT EXISTS invalidate_anchor_index_term_insert AFTER INSERT ON atlas_runtime_anchor_terms
BEGIN
    DELETE FROM generation_runtime_anchor_indexes WHERE generation IN
        (SELECT generation FROM generation_runtime_anchors WHERE anchor_id=NEW.anchor_id);
END;
CREATE TRIGGER IF NOT EXISTS invalidate_anchor_index_term_update AFTER UPDATE ON atlas_runtime_anchor_terms
BEGIN
    DELETE FROM generation_runtime_anchor_indexes WHERE generation IN
        (SELECT generation FROM generation_runtime_anchors WHERE anchor_id IN (OLD.anchor_id, NEW.anchor_id));
END;
CREATE TRIGGER IF NOT EXISTS invalidate_anchor_index_term_delete AFTER DELETE ON atlas_runtime_anchor_terms
BEGIN
    DELETE FROM generation_runtime_anchor_indexes WHERE generation IN
        (SELECT generation FROM generation_runtime_anchors WHERE anchor_id=OLD.anchor_id);
END;
CREATE TRIGGER IF NOT EXISTS invalidate_anchor_index_source_update AFTER UPDATE ON atlas_runtime_anchors
BEGIN
    DELETE FROM generation_runtime_anchor_indexes WHERE generation IN
        (SELECT generation FROM generation_runtime_anchors WHERE anchor_id IN (OLD.anchor_id, NEW.anchor_id));
END;
CREATE TRIGGER IF NOT EXISTS invalidate_anchor_index_source_delete AFTER DELETE ON atlas_runtime_anchors
BEGIN
    DELETE FROM generation_runtime_anchor_indexes WHERE generation IN
        (SELECT generation FROM generation_runtime_anchors WHERE anchor_id=OLD.anchor_id);
END;
"""


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
        omitted = {
            "artifact_ref", "created_at", "generated_at", "generation", "indexed_at",
            "manifest_path", "project",
        }
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

    def logical_component(name: str) -> Any:
        component = dict(components.get(name) or {"status": "unavailable"})
        if name == "change_intelligence" and isinstance(component.get("details"), dict):
            details = dict(component["details"])
            # Build counters prove bounded construction but do not alter the
            # serving rows or their registered content identity.
            build = dict(details.get("build") or {})
            build.pop("operations", None)
            build.pop("output_bytes", None)
            if "build" in details:
                details["build"] = build
            component["details"] = details
        return _without_local_state(component)

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
            name: logical_component(name)
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


def _enable_wal(connection: sqlite3.Connection) -> None:
    deadline = time.monotonic() + 30.0
    while True:
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as error:
            if "locked" not in str(error).lower() or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def _preflight_schema_version(connection: sqlite3.Connection) -> int | None:
    metadata = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'",
    ).fetchone()
    if not metadata:
        return None
    row = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
    if not row:
        return 0
    try:
        version = int(row[0])
    except (TypeError, ValueError) as error:
        raise sqlite3.DatabaseError("catalog schema version is invalid") from error
    if version > SCHEMA_VERSION:
        raise sqlite3.DatabaseError(f"catalog schema {row[0]} is newer than this Project Brain")
    return version


def _execute_sql_script(connection: sqlite3.Connection, script: str) -> None:
    """Execute DDL inside the caller's transaction without an implicit commit."""
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            sql = statement.strip()
            if sql:
                connection.execute(sql)
            statement = ""
    if statement.strip():
        raise sqlite3.DatabaseError("incomplete catalog schema statement")


def connect(settings: Settings) -> sqlite3.Connection:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    path = database(settings)
    connection = connect_managed_sqlite(settings.state_dir, path, timeout=30)
    try:
        # Concurrent first openers can race while SQLite changes the persisted
        # journal mode. The connection busy timeout does not reliably cover
        # this PRAGMA on every supported SQLite build, so retry only its
        # explicit lock response within the same bounded timeout.
        preflight_version = _preflight_schema_version(connection)
        connection.execute("PRAGMA foreign_keys=ON")
        if preflight_version == SCHEMA_VERSION:
            return connection
        _enable_wal(connection)
        connection.execute("BEGIN IMMEDIATE")
        # Bind schema validation and all DDL to one writer transaction. A newer
        # binary may have published while this opener waited for WAL or the lock.
        locked_version = _preflight_schema_version(connection)
        if locked_version == SCHEMA_VERSION:
            connection.commit()
            return connection
        _execute_sql_script(
            connection,
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
            CREATE TABLE IF NOT EXISTS atlas_card_terms (
                card_id TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                term TEXT NOT NULL,
                PRIMARY KEY(card_id, schema_version, term)
            );
            CREATE TABLE IF NOT EXISTS generation_card_indexes (
                generation INTEGER PRIMARY KEY,
                schema_version TEXT NOT NULL,
                card_count INTEGER NOT NULL,
                term_count INTEGER NOT NULL,
                projection_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS atlas_change_terms (
                change_id TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                term TEXT NOT NULL,
                PRIMARY KEY(change_id, schema_version, term)
            );
            CREATE TABLE IF NOT EXISTS generation_change_indexes (
                generation INTEGER PRIMARY KEY,
                schema_version TEXT NOT NULL,
                change_count INTEGER NOT NULL,
                term_count INTEGER NOT NULL,
                projection_hash TEXT NOT NULL
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
                payload_hash TEXT,
                created_at TEXT NOT NULL,
                last_used_at TEXT NOT NULL,
                PRIMARY KEY(generation, cache_key)
            );
            CREATE TABLE IF NOT EXISTS atlas_retrieval_cache_registrations (
                generation INTEGER NOT NULL,
                cache_key TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                compatibility_identity TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                seal TEXT,
                registered_at TEXT NOT NULL,
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
            CREATE TABLE IF NOT EXISTS atlas_runtime_anchors (
                anchor_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                value TEXT NOT NULL,
                normalized TEXT NOT NULL,
                repo TEXT NOT NULL,
                module_id TEXT,
                entity_id TEXT,
                path TEXT NOT NULL,
                line INTEGER NOT NULL,
                blob_sha TEXT NOT NULL,
                confidence REAL NOT NULL,
                method TEXT NOT NULL,
                provenance_json TEXT NOT NULL,
                fingerprint TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS generation_runtime_anchors (
                generation INTEGER NOT NULL,
                anchor_id TEXT NOT NULL,
                snapshot_sha TEXT NOT NULL,
                PRIMARY KEY(generation, anchor_id)
            );
            CREATE TABLE IF NOT EXISTS atlas_runtime_anchor_terms (
                anchor_id TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                term TEXT NOT NULL,
                PRIMARY KEY(anchor_id, schema_version, term)
            );
            CREATE TABLE IF NOT EXISTS generation_runtime_anchor_indexes (
                generation INTEGER PRIMARY KEY,
                schema_version TEXT NOT NULL,
                anchor_count INTEGER NOT NULL,
                term_count INTEGER NOT NULL,
                projection_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS atlas_integration_facts (
                fact_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                key_value TEXT NOT NULL,
                normalized TEXT NOT NULL,
                repo TEXT NOT NULL,
                module_id TEXT,
                entity_id TEXT,
                path TEXT NOT NULL,
                line INTEGER NOT NULL,
                blob_sha TEXT NOT NULL,
                direction TEXT NOT NULL,
                framework TEXT NOT NULL,
                confidence REAL NOT NULL,
                provenance_json TEXT NOT NULL,
                fingerprint TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS generation_integration_facts (
                generation INTEGER NOT NULL,
                fact_id TEXT NOT NULL,
                snapshot_sha TEXT NOT NULL,
                PRIMARY KEY(generation, fact_id)
            );
            CREATE TABLE IF NOT EXISTS generation_intelligence_files (
                generation INTEGER NOT NULL,
                repo TEXT NOT NULL,
                path TEXT NOT NULL,
                blob_sha TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                PRIMARY KEY(generation, repo, path)
            );
            CREATE INDEX IF NOT EXISTS atlas_modules_repo_path ON atlas_modules(repo, path);
            CREATE INDEX IF NOT EXISTS atlas_entities_name ON atlas_entities(simple_name, qualified_name);
            CREATE INDEX IF NOT EXISTS atlas_entities_repo_path ON atlas_entities(repo, path);
            CREATE INDEX IF NOT EXISTS atlas_edges_source ON atlas_edges(source_id, edge_type);
            CREATE INDEX IF NOT EXISTS atlas_edges_target ON atlas_edges(target_id, edge_type);
            CREATE INDEX IF NOT EXISTS atlas_cards_repo_level ON atlas_cards(repo, level);
            CREATE INDEX IF NOT EXISTS atlas_card_terms_lookup ON atlas_card_terms(schema_version, term, card_id);
            CREATE INDEX IF NOT EXISTS atlas_change_terms_lookup ON atlas_change_terms(schema_version, term, change_id);
            CREATE INDEX IF NOT EXISTS atlas_changes_repo_path ON atlas_changes(repo, path);
            CREATE INDEX IF NOT EXISTS atlas_changes_old_path_lookup ON atlas_changes(lower(old_path), repo, path);
            CREATE INDEX IF NOT EXISTS atlas_cache_last_used ON atlas_retrieval_cache(last_used_at);
            CREATE INDEX IF NOT EXISTS atlas_cache_registration_payload
                ON atlas_retrieval_cache_registrations(generation, payload_hash);
            CREATE INDEX IF NOT EXISTS atlas_runtime_anchor_lookup ON atlas_runtime_anchors(normalized, kind);
            CREATE INDEX IF NOT EXISTS atlas_runtime_anchor_location ON atlas_runtime_anchors(repo, path);
            CREATE INDEX IF NOT EXISTS atlas_runtime_anchor_path_lookup ON atlas_runtime_anchors(lower(path));
            CREATE INDEX IF NOT EXISTS atlas_runtime_anchor_terms_lookup
                ON atlas_runtime_anchor_terms(schema_version, term, anchor_id);
            CREATE INDEX IF NOT EXISTS atlas_integration_fact_lookup ON atlas_integration_facts(normalized, kind);
            CREATE INDEX IF NOT EXISTS atlas_integration_fact_location ON atlas_integration_facts(repo, path);
            """
        )
        def schema_version() -> tuple[int, object | None]:
            value = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
            try:
                return (int(value[0]) if value else 0), value
            except (TypeError, ValueError) as error:
                raise sqlite3.DatabaseError("catalog schema version is invalid") from error

        old_version, value = schema_version()
        if old_version > SCHEMA_VERSION:
            raise sqlite3.DatabaseError(f"catalog schema {value[0]} is newer than this Project Brain")
        if old_version < SCHEMA_VERSION:
            # PRAGMA table_info followed by ALTER is a single serialized
            # migration decision. A shared retrieval process may be the first
            # opener after upgrade, so workspace writer locks are insufficient.
            old_version, value = schema_version()
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
            if old_version < 7:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(atlas_retrieval_cache)")}
                if "payload_hash" not in columns:
                    connection.execute("ALTER TABLE atlas_retrieval_cache ADD COLUMN payload_hash TEXT")
            if old_version < 9:
                for table in ("generation_card_indexes", "generation_change_indexes"):
                    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                    if "projection_hash" not in columns:
                        connection.execute(f"ALTER TABLE {table} ADD COLUMN projection_hash TEXT")
            if old_version < 10:
                _execute_sql_script(connection, _TERM_INDEX_INVALIDATION_DDL)
            if old_version < 11:
                columns = {
                    row[1] for row in connection.execute(
                        "PRAGMA table_info(atlas_retrieval_cache_registrations)"
                    )
                }
                if "seal" not in columns:
                    connection.execute("ALTER TABLE atlas_retrieval_cache_registrations ADD COLUMN seal TEXT")
            if old_version < 12:
                _execute_sql_script(connection, _RUNTIME_ANCHOR_INDEX_INVALIDATION_DDL)
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
                    connection.execute(
                        "INSERT INTO metadata(key, value) VALUES('current_generation', ?)", (str(row[0]),),
                    )
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),),
            )
        connection.commit()
        return connection
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
        raise


def generation_root(settings: Settings) -> Path:
    configured = settings.state_dir / "generations"
    root = configured.resolve()
    if configured.is_symlink() or root.parent != settings.state_dir.resolve():
        raise ValueError("Atlas generation root escapes managed state")
    return root


def _generation_ref(
    connection: sqlite3.Connection,
    generation: int,
    *,
    consume: Callable[[int], None] | None = None,
) -> AtlasGenerationRef | None:
    row = connection.execute(
        "SELECT generation, identity, parent_generation, source_signature, schema_manifest_json, created_at "
        "FROM index_generations WHERE generation=? AND status IN ('current', 'retained')",
        (generation,),
    ).fetchone()
    if not row:
        return None
    snapshot_rows = []
    for snapshot_row in connection.execute(
        "SELECT repo, snapshot_sha, ref, parent_snapshot_sha FROM generation_snapshots "
        "WHERE generation=? ORDER BY repo",
        (generation,),
    ):
        if consume is not None:
            consume(1)
        snapshot_rows.append(snapshot_row)
    snapshots = {str(repo): str(sha) for repo, sha, _ref, _parent in snapshot_rows}
    components: dict[str, dict[str, Any]] = {}
    for name, schema_version, status, content_hash, artifact_ref, details_json in connection.execute(
        "SELECT component, schema_version, status, content_hash, artifact_ref, details_json "
        "FROM generation_components WHERE generation=? ORDER BY component",
        (generation,),
    ):
        if consume is not None:
            consume(1)
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
        schema_manifest = json.loads(str(row[4] or "{}"))
    except (TypeError, json.JSONDecodeError):
        schema_manifest = {}
    if not isinstance(schema_manifest, dict):
        schema_manifest = {}
    identity = str(row[1] or "")
    signature = str(row[3] or source_signature(snapshots))
    # The normalized catalog is the authoritative serving record.  The JSON
    # manifest is a recoverable projection and must never be able to override
    # generation identity, membership, or component registration.
    manifest = {
        "atlas_manifest_version": ATLAS_MANIFEST_VERSION,
        "generation": int(row[0]),
        "identity": identity,
        "parent_generation": row[2],
        "source_signature": signature,
        "created_at": str(row[5]),
        "snapshots": [
            {
                key: value
                for key, value in {
                    "repo": str(repo),
                    "sha": str(sha),
                    "ref": ref,
                    "parent_snapshot_sha": parent,
                }.items()
                if value is not None
            }
            for repo, sha, ref, parent in snapshot_rows
        ],
        "schema_manifest": schema_manifest,
        "components": components,
    }
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


def generations(
    settings: Settings,
    *,
    consume: Callable[[int], None] | None = None,
) -> list[AtlasGenerationRef]:
    try:
        connection = connect(settings)
    except sqlite3.Error:
        return []
    try:
        refs = []
        for (number,) in connection.execute(
            "SELECT generation FROM index_generations "
            "WHERE status IN ('current', 'retained') ORDER BY generation"
        ):
            if consume is not None:
                consume(1)
            ref = _generation_ref(connection, int(number), consume=consume)
            if ref is not None:
                refs.append(ref)
        return refs
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
                "atlas_retrieval_cache_registrations",
                "generation_runtime_anchors", "generation_integration_facts", "generation_card_indexes",
                "generation_change_indexes", "generation_runtime_anchor_indexes",
                "generation_intelligence_files",
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
            ("atlas_runtime_anchors", "generation_runtime_anchors", "anchor_id"),
            ("atlas_integration_facts", "generation_integration_facts", "fact_id"),
        ):
            connection.execute(
                f"DELETE FROM {table} WHERE NOT EXISTS (SELECT 1 FROM {membership} g WHERE g.{key}={table}.{key})"
            )
        connection.execute(
            "DELETE FROM atlas_card_terms WHERE NOT EXISTS "
            "(SELECT 1 FROM atlas_cards c WHERE c.card_id=atlas_card_terms.card_id)"
        )
        connection.execute(
            "DELETE FROM atlas_change_terms WHERE NOT EXISTS "
            "(SELECT 1 FROM atlas_changes c WHERE c.change_id=atlas_change_terms.change_id)"
        )
        connection.execute(
            "DELETE FROM atlas_runtime_anchor_terms WHERE NOT EXISTS "
            "(SELECT 1 FROM atlas_runtime_anchors a WHERE a.anchor_id=atlas_runtime_anchor_terms.anchor_id)"
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _load_json(settings: Settings, path: Path) -> dict[str, Any]:
    try:
        value = json.loads(read_managed_text(
            settings.state_dir, path, max_bytes=64 * 1024 * 1024,
        ))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def collect_generation_components(
    settings: Settings,
    state: dict[str, object],
    *,
    semantic_failed: bool = False,
    atlas_payload: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Verify current projections and turn them into one generation component manifest."""
    from .backends.zoekt import immutable_snapshot_available, shard_manifest_identity, shard_path
    from .index import LEXICAL_COMPONENT_SCHEMA_VERSION, lexical_component
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
        path = shard_path(settings.state_dir, repo, sha) / "brain-shard.json"
        repository = settings.repo(repo)
        manifest_identity = shard_manifest_identity(path.parent, sha) if immutable_snapshot_available(repository) else None
        if manifest_identity:
            zoekt_rows.append({"repo": repo, "snapshot": sha, "manifest_hash": manifest_identity})
    components["zoekt"] = {
        "schema_version": "3",
        "status": "ready" if len(zoekt_rows) == len(snapshots) else "unavailable",
        "content_hash": _content_hash(zoekt_rows) if zoekt_rows else None,
        "details": {"shards": zoekt_rows, "reason": None if len(zoekt_rows) == len(snapshots) else "not all immutable shards are available"},
    }

    graph_state = _load_json(settings, settings.state_dir / "graphs.json")
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
    relationships = _load_json(settings, relationships_path)
    from .relations import MAX_RELATIONSHIP_ARTIFACT_BYTES, valid_relationship_payload

    try:
        relationships_file_ready = (
            not relationships_path.is_symlink()
            and relationships_path.is_file()
            and relationships_path.stat().st_size <= MAX_RELATIONSHIP_ARTIFACT_BYTES
        )
    except OSError:
        relationships_file_ready = False
    relationships_ready = relationships_file_ready and valid_relationship_payload(relationships, snapshots)
    components["relationships"] = {
        "schema_version": str(relationships.get("version") or 1),
        "status": "ready" if relationships_ready else "unavailable",
        "content_hash": _content_hash(relationships) if relationships_ready else None,
        "details": {"source_signature": source_signature(snapshots)},
        **({"_artifact_source": str(relationships_path)} if relationships_ready else {}),
    }

    experience_path = settings.state_dir / "ticket-history.json"
    experience = _load_json(settings, experience_path)
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
    semantic = _load_json(settings, semantic_path)
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
            "pack_compatibility_identity": semantic.get("pack_compatibility_identity"),
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
    components["lexical"]["schema_version"] = str(LEXICAL_COMPONENT_SCHEMA_VERSION)
    return components


def publish_current_components(
    settings: Settings,
    *,
    semantic_failed: bool = False,
    atlas_payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Republish aligned component state after a standalone optional-backend build."""
    state = _load_json(settings, settings.state_dir / "indexes.json")
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


def _reusable_generation_is_intact(
    connection: sqlite3.Connection,
    settings: Settings,
    generation: int,
    components: dict[str, dict[str, Any]],
    atlas_payload: dict[str, Any] | None,
) -> bool:
    ref = _generation_ref(connection, generation)
    if ref is None:
        return False
    try:
        manifest_row = connection.execute(
            "SELECT manifest_path FROM index_generations WHERE generation=?", (generation,),
        ).fetchone()
        if not manifest_row:
            return False
        root = generation_root(settings)
        manifest_path = Path(str(manifest_row[0]))
        manifest = json.loads(read_managed_text(root, manifest_path, max_bytes=16 * 1024 * 1024))
        if (
            int(manifest.get("generation") or -1) != generation
            or str(manifest.get("identity") or "") != ref.identity
            or str(manifest.get("source_signature") or "") != ref.source_signature
        ):
            return False
        for name, component in components.items():
            source = component.get("_artifact_source")
            if not source:
                continue
            registered = ref.component(name)
            artifact_path = settings.state_dir / str(registered.get("artifact_ref") or "")
            payload = json.loads(read_managed_text(
                root, artifact_path, max_bytes=256 * 1024 * 1024,
            ))
            if not isinstance(payload, dict) or _content_hash(payload) != registered.get("content_hash"):
                return False
            if name == "semantic":
                from .semantic import semantic_state_compatibility

                compatible, _ = semantic_state_compatibility(
                    settings, payload, ref.snapshots, component=registered, require_active_pack=False,
                )
                if not compatible:
                    return False
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    if atlas_payload is None:
        return True
    memberships = (
        ("generation_modules", "module_id", "modules", "module_id"),
        ("generation_entities", "entity_id", "entities", "entity_id"),
        ("generation_regions", "region_id", "regions", "region_id"),
        ("generation_edges", "edge_id", "edges", "edge_id"),
        ("generation_cards", "card_id", "cards", "card_id"),
        ("generation_changes", "change_id", "changes", "change_id"),
        ("generation_runtime_anchors", "anchor_id", "runtime_anchors", "anchor_id"),
        ("generation_integration_facts", "fact_id", "integration_facts", "fact_id"),
    )
    for table, column, payload_key, identity_key in memberships:
        actual = {str(row[0]) for row in connection.execute(
            f"SELECT {column} FROM {table} WHERE generation=?", (generation,),
        )}
        expected = {str(item[identity_key]) for item in atlas_payload.get(payload_key) or []}
        if actual != expected:
            return False
    index_contracts = (
        ("card", "generation_card_indexes", "card_count"),
        ("change", "generation_change_indexes", "change_count"),
        ("anchor", "generation_runtime_anchor_indexes", "anchor_count"),
    )
    for kind, marker_table, count_column in index_contracts:
        schema_version, expected_count, expected_terms, expected_rows = _expected_term_projection(
            atlas_payload, kind,
        )
        marker = connection.execute(
            f"SELECT {count_column},term_count,projection_hash FROM {marker_table} "
            "WHERE generation=? AND schema_version=?",
            (generation, schema_version),
        ).fetchone()
        try:
            expected_hash = _verified_term_projection_hash(
                connection, generation, kind=kind, schema_version=schema_version,
                expected_rows=expected_rows,
            )
        except RuntimeError:
            return False
        if (
            not marker
            or int(marker[0]) != expected_count
            or int(marker[1]) != expected_terms
            or str(marker[2]) != expected_hash
        ):
            return False
    actual_files = {f"{row[0]}\0{row[1]}\0{row[2]}\0{row[3]}" for row in connection.execute(
        "SELECT repo,path,blob_sha,schema_version FROM generation_intelligence_files WHERE generation=?",
        (generation,),
    )}
    expected_files = {
        f"{item['repo']}\0{item['path']}\0{item['blob_sha']}\0{item['schema_version']}"
        for item in atlas_payload.get("v1_files") or []
    }
    return actual_files == expected_files


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
    projection_previous: int | None = None
    projection_changed = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        parent_row = connection.execute("SELECT value FROM metadata WHERE key='current_generation'").fetchone()
        parent = int(parent_row[0]) if parent_row else None
        projection_previous = parent
        row = connection.execute("SELECT COALESCE(MAX(generation), 0) FROM index_generations").fetchone()
        catalog_max = int(row[0] or 0)
        disk_max = 0
        scan_deadline = time.monotonic() + 1.0
        scanned = 0
        with os.scandir(root) as entries:
            for entry in entries:
                scanned += 1
                if scanned > 100_000 or time.monotonic() >= scan_deadline:
                    raise sqlite3.IntegrityError("managed generation allocation scan exceeded its bound")
                if not entry.name.startswith("generation-"):
                    continue
                match = re.fullmatch(r"generation-([0-9]{6,})", entry.name)
                if match is None or entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    raise sqlite3.IntegrityError("managed generation directory contains an unsafe entry")
                value = int(match.group(1))
                if value > 9_000_000_000_000_000_000:
                    raise sqlite3.IntegrityError("managed generation number exceeds its supported bound")
                disk_max = max(disk_max, value)
        # A crash can leave a completely written but unregistered immutable
        # directory. Never overwrite or collide with it: allocate above both
        # catalog authority and direct canonical on-disk generations.
        number = max(catalog_max, disk_max) + 1
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
        semantic_component = component_values.get("semantic") or {}
        if semantic_component.get("status") == "ready":
            from .semantic import semantic_schema_version

            if semantic_component.get("schema_version") != semantic_schema_version():
                legacy_semantic = dict(semantic_component)
                legacy_details = dict(legacy_semantic.get("details") or {})
                legacy_details["reason"] = "retained legacy Semantic schema requires managed rebuild"
                legacy_semantic["details"] = legacy_details
                legacy_semantic["status"] = "degraded"
                component_values["semantic"] = legacy_semantic
        from .index import LEXICAL_COMPONENT_SCHEMA_VERSION

        if (
            component_values["lexical"].get("status") != "ready"
            or component_values["lexical"].get("schema_version") != str(LEXICAL_COMPONENT_SCHEMA_VERSION)
        ):
            raise sqlite3.IntegrityError("Atlas generation requires an aligned lexical component")
        if atlas_payload is not None:
            from .atlas import atlas_components, validate_atlas_payload
            from .investigation import validate_generation_intelligence

            repositories = {str(item["repo"]) for item in snapshots}
            try:
                validate_atlas_payload(atlas_payload, repositories)
            except ValueError as error:
                raise sqlite3.IntegrityError("Atlas payload failed independent publication validation") from error
            try:
                validate_generation_intelligence(
                    atlas_payload, repositories,
                    modules=atlas_payload.get("modules") or [], entities=atlas_payload.get("entities") or [],
                )
            except ValueError as error:
                if "content identity" in str(error):
                    raise
                raise sqlite3.IntegrityError("Atlas payload failed independent publication validation") from error
            authoritative_atlas = atlas_components(atlas_payload)
            for name, expected in authoritative_atlas.items():
                supplied = component_values.get(name) or {}
                supplied_details = supplied.get("details") if isinstance(supplied.get("details"), dict) else {}
                expected_details = expected.get("details") if isinstance(expected.get("details"), dict) else {}
                if (
                    supplied.get("status") != expected.get("status")
                    or supplied.get("schema_version") != expected.get("schema_version")
                    or supplied.get("content_hash") != expected.get("content_hash")
                    or supplied_details.get("count") != expected_details.get("count")
                    or supplied_details.get("build") != expected_details.get("build")
                ):
                    raise sqlite3.IntegrityError(
                        f"Atlas {name} payload does not match its registered component identity"
                    )
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
            if not _reusable_generation_is_intact(
                connection, settings, existing_number, component_values, atlas_payload,
            ):
                lexical = dict(component_values["lexical"])
                details = dict(lexical.get("details") or {})
                details.update({"recovery_of": existing_number, "recovery_sequence": number})
                lexical["details"] = details
                component_values["lexical"] = lexical
                logical["components"] = component_values
                identity = canonical_atlas_identity(logical)
                existing = None
        if existing:
            existing_number = int(existing[0])
            connection.execute("UPDATE index_generations SET status='retained' WHERE status='current' AND generation<>?", (existing_number,))
            connection.execute("UPDATE index_generations SET status='current' WHERE generation=?", (existing_number,))
            connection.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES('current_generation', ?)", (str(existing_number),))
            ref = _generation_ref(connection, existing_number)
            if ref is None:
                raise sqlite3.DatabaseError("reused Atlas generation is unavailable")
            _write_current_projection(root, existing_number)
            projection_changed = True
            connection.commit()
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
                    try:
                        artifact_bytes = read_managed_bytes(
                            settings.state_dir, source_path, max_bytes=64 * 1024 * 1024,
                        )
                        copied_payload = json.loads(artifact_bytes.decode("utf-8"))
                        atomic_managed_bytes_write(build, target, artifact_bytes)
                    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
                        raise sqlite3.IntegrityError(f"copied {name} artifact is not valid JSON") from error
                    if not isinstance(copied_payload, dict) or _content_hash(copied_payload) != persisted.get("content_hash"):
                        raise sqlite3.IntegrityError(f"copied {name} artifact content identity changed before publication")
                    if name == "semantic" and persisted.get("status") == "ready":
                        from .semantic import semantic_state_compatibility

                        compatible, reason = semantic_state_compatibility(
                            settings,
                            copied_payload,
                            {str(item["repo"]): str(item["sha"]) for item in snapshots},
                            component=persisted,
                            require_active_pack=False,
                        )
                        if not compatible:
                            raise sqlite3.IntegrityError(
                                "copied semantic artifact is incompatible before publication: " + str(reason)
                            )
                    persisted["artifact_ref"] = str(Path("generations") / f"generation-{number:06d}" / target.name)
                persisted_components[name] = persisted
            manifest["components"] = persisted_components
            atomic_managed_text_write(
                build, build / "manifest.json", json.dumps(manifest, indent=2) + "\n",
            )
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
        _write_current_projection(root, number)
        projection_changed = True
        connection.commit()
        return manifest
    except Exception:
        connection.rollback()
        if projection_changed:
            try:
                if projection_previous is None:
                    (root / "CURRENT").unlink(missing_ok=True)
                else:
                    _write_current_projection(root, projection_previous)
            except OSError:
                pass
        if final is not None and final.exists():
            shutil.rmtree(final, ignore_errors=True)
        raise
    finally:
        connection.close()


def _term_projection_rows(
    connection: sqlite3.Connection,
    generation: int,
    *,
    kind: str,
    schema_version: str,
) -> Any:
    if kind == "card":
        return connection.execute(
            "SELECT g.card_id,c.content_hash,COALESCE(t.term,'') "
            "FROM generation_cards g JOIN atlas_cards c ON c.card_id=g.card_id "
            "LEFT JOIN atlas_card_terms t ON t.card_id=g.card_id AND t.schema_version=? "
            "WHERE g.generation=? ORDER BY g.card_id,t.term",
            (schema_version, generation),
        )
    elif kind == "change":
        return connection.execute(
            "SELECT g.change_id,c.path,COALESCE(c.ticket,''),c.metadata_json,COALESCE(t.term,'') "
            "FROM generation_changes g JOIN atlas_changes c ON c.change_id=g.change_id "
            "LEFT JOIN atlas_change_terms t ON t.change_id=g.change_id AND t.schema_version=? "
            "WHERE g.generation=? ORDER BY g.change_id,t.term",
            (schema_version, generation),
        )
    elif kind == "anchor":
        return connection.execute(
            "SELECT g.anchor_id,a.fingerprint,COALESCE(t.term,'') "
            "FROM generation_runtime_anchors g JOIN atlas_runtime_anchors a ON a.anchor_id=g.anchor_id "
            "LEFT JOIN atlas_runtime_anchor_terms t ON t.anchor_id=g.anchor_id AND t.schema_version=? "
            "WHERE g.generation=? ORDER BY g.anchor_id,t.term",
            (schema_version, generation),
        )
    else:
        raise ValueError("unknown Atlas term projection kind")


def _projection_hash(rows: Any) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(json.dumps(tuple(row), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _term_projection_hash(
    connection: sqlite3.Connection,
    generation: int,
    *,
    kind: str,
    schema_version: str,
) -> str:
    """Bind a routing projection to membership, source identity, and every term."""
    return _projection_hash(_term_projection_rows(
        connection, generation, kind=kind, schema_version=schema_version,
    ))


def _verified_term_projection_hash(
    connection: sqlite3.Connection,
    generation: int,
    *,
    kind: str,
    schema_version: str,
    expected_rows: Any,
) -> str:
    """Prove shared derived rows exactly match the independently derived payload projection."""
    actual = iter(_term_projection_rows(
        connection, generation, kind=kind, schema_version=schema_version,
    ))
    expected = iter(expected_rows)
    actual_digest = hashlib.sha256()
    expected_digest = hashlib.sha256()
    sentinel = object()
    while True:
        actual_row = next(actual, sentinel)
        expected_row = next(expected, sentinel)
        if actual_row is sentinel or expected_row is sentinel:
            if actual_row is not expected_row:
                raise RuntimeError(f"Atlas {kind} routing projection row count mismatch")
            break
        actual_tuple = tuple(actual_row)
        expected_tuple = tuple(expected_row)
        if actual_tuple != expected_tuple:
            raise RuntimeError(f"Atlas {kind} routing projection content mismatch")
        for digest, row in ((actual_digest, actual_tuple), (expected_digest, expected_tuple)):
            digest.update(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            digest.update(b"\0")
    actual_hash = "sha256:" + actual_digest.hexdigest()
    expected_hash = "sha256:" + expected_digest.hexdigest()
    if actual_hash != expected_hash:
        raise RuntimeError(f"Atlas {kind} routing projection hash mismatch")
    return expected_hash


def _expected_term_projection(
    payload: dict[str, Any], kind: str,
) -> tuple[str, int, int, Any]:
    """Derive a complete routing projection from authoritative Atlas payload rows."""
    from .atlas import (
        ATLAS_CARD_TERM_SCHEMA_VERSION,
        ATLAS_CHANGE_TERM_SCHEMA_VERSION,
        MAX_CARD_ROUTING_TERMS,
        MAX_CHANGE_ROUTING_TERMS,
        _tokens,
    )
    from .investigation import (
        MAX_COMPOUND_ANCHOR_QUERIES,
        RUNTIME_ANCHOR_TERM_SCHEMA_VERSION,
        _compound_terms,
    )

    if kind == "card":
        items = payload.get("cards") or []
        term_count = sum(
            len(sorted(_tokens(str(item["content"])))[:MAX_CARD_ROUTING_TERMS])
            for item in items
        )
        rows = (
            (item["card_id"], item["content_hash"], term)
            for item in sorted(items, key=lambda value: str(value["card_id"]))
            for term in (sorted(_tokens(str(item["content"])))[:MAX_CARD_ROUTING_TERMS] or [""])
        )
        return ATLAS_CARD_TERM_SCHEMA_VERSION, len(items), term_count, rows
    if kind == "change":
        items = payload.get("changes") or []
        term_count = sum(len(sorted(_tokens(
                f"{item.get('ticket') or ''} {item['path']} "
                f"{(item.get('metadata') if isinstance(item.get('metadata'), dict) else {}).get('subject') or ''}"
            ))[:MAX_CHANGE_ROUTING_TERMS])
            for item in items
        )
        rows = (
            (
                item["change_id"], item["path"], item.get("ticket") or "",
                json.dumps(
                    item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
                    sort_keys=True,
                ),
                term,
            )
            for item in sorted(items, key=lambda value: str(value["change_id"]))
            for term in (sorted(_tokens(
                f"{item.get('ticket') or ''} {item['path']} "
                f"{(item.get('metadata') if isinstance(item.get('metadata'), dict) else {}).get('subject') or ''}"
            ))[:MAX_CHANGE_ROUTING_TERMS] or [""])
        )
        return ATLAS_CHANGE_TERM_SCHEMA_VERSION, len(items), term_count, rows
    if kind == "anchor":
        items = payload.get("runtime_anchors") or []
        term_count = sum(len(sorted(set(
                _compound_terms(str(item["normalized"])),
            ))[:MAX_COMPOUND_ANCHOR_QUERIES])
            for item in items
        )
        rows = (
            (item["anchor_id"], item["fingerprint"], term)
            for item in sorted(items, key=lambda value: str(value["anchor_id"]))
            for term in (
                sorted(set(_compound_terms(str(item["normalized"]))))[:MAX_COMPOUND_ANCHOR_QUERIES]
                or [""]
            )
        )
        return RUNTIME_ANCHOR_TERM_SCHEMA_VERSION, len(items), term_count, rows
    raise ValueError("unknown Atlas term projection kind")


def _publish_atlas_payload(
    connection: sqlite3.Connection,
    generation: int,
    snapshots: list[dict[str, Any]],
    parent_generation: int | None,
    payload: dict[str, Any],
) -> None:
    """Persist normalized Atlas facts in the generation publication transaction."""
    from .atlas import (
        ATLAS_CARD_TERM_SCHEMA_VERSION,
        ATLAS_CHANGE_TERM_SCHEMA_VERSION,
        MAX_CARD_ROUTING_TERMS,
        MAX_CHANGE_ROUTING_TERMS,
        _tokens,
    )
    from .investigation import (
        MAX_COMPOUND_ANCHOR_QUERIES,
        RUNTIME_ANCHOR_TERM_SCHEMA_VERSION,
        _compound_terms,
    )

    snapshot_by_repo = {str(item["repo"]): str(item["sha"]) for item in snapshots}

    def insert_immutable(table: str, key: str, columns: tuple[str, ...], values: tuple[Any, ...]) -> None:
        existing = connection.execute(
            f"SELECT {','.join(columns)} FROM {table} WHERE {key}=?",
            (values[columns.index(key)],),
        ).fetchone()
        if existing is None:
            connection.execute(
                f"INSERT INTO {table}({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                values,
            )
        elif tuple(existing) != values:
            raise RuntimeError(f"immutable Atlas row mismatch: {table}.{key}")

    rows = payload.get("modules") or []
    for item in rows:
        insert_immutable(
            "atlas_modules", "module_id",
            ("module_id", "repo", "path", "name", "language", "fingerprint", "metadata_json"),
            (item["module_id"], item["repo"], item["path"], item["name"], item["language"],
             item["fingerprint"], json.dumps(item.get("metadata") or {}, sort_keys=True)),
        )
        connection.execute(
            "INSERT INTO generation_modules(generation,module_id,snapshot_sha) VALUES (?,?,?)",
            (generation, item["module_id"], snapshot_by_repo[item["repo"]]),
        )
    for item in payload.get("entities") or []:
        insert_immutable(
            "atlas_entities", "entity_id",
            ("entity_id", "repo", "module_id", "path", "line_start", "line_end", "qualified_name",
             "simple_name", "signature", "language", "kind", "parent_entity_id", "blob_sha", "extractor",
             "extractor_version", "fingerprint", "metadata_json"),
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
        insert_immutable(
            "atlas_regions", "region_id",
            ("region_id", "repo", "path", "line_start", "line_end", "blob_sha", "kind", "fingerprint", "metadata_json"),
            (item["region_id"], item["repo"], item["path"], item["line_start"], item["line_end"], item["blob_sha"],
             item["kind"], item["fingerprint"], json.dumps(item.get("metadata") or {}, sort_keys=True)),
        )
        connection.execute(
            "INSERT INTO generation_regions(generation,region_id,snapshot_sha) VALUES (?,?,?)",
            (generation, item["region_id"], snapshot_by_repo[item["repo"]]),
        )
    for item in payload.get("edges") or []:
        insert_immutable(
            "atlas_edges", "edge_id",
            ("edge_id", "edge_type", "source_id", "target_id", "repo", "path", "line_start", "line_end",
             "blob_sha", "extractor", "extractor_version", "confidence", "metadata_json"),
            (item["edge_id"], item["edge_type"], item["source_id"], item["target_id"], item["repo"], item["path"],
             item["line_start"], item["line_end"], item["blob_sha"], item["extractor"], item["extractor_version"],
             item["confidence"], json.dumps(item.get("metadata") or {}, sort_keys=True)),
        )
        connection.execute(
            "INSERT INTO generation_edges(generation,edge_id,snapshot_sha) VALUES (?,?,?)",
            (generation, item["edge_id"], snapshot_by_repo.get(item["repo"], "working-tree")),
        )
    card_count = 0
    term_count = 0
    cards = payload.get("cards") or []
    for item in cards:
        insert_immutable(
            "atlas_cards", "card_id",
            ("card_id", "level", "target_id", "repo", "module_id", "entity_id", "path", "content",
             "content_hash", "metadata_json"),
            (item["card_id"], item["level"], item["target_id"], item["repo"], item.get("module_id"),
             item.get("entity_id"), item.get("path"), item["content"], item["content_hash"],
             json.dumps(item.get("metadata") or {}, sort_keys=True)),
        )
        connection.execute(
            "INSERT INTO generation_cards(generation,card_id,snapshot_sha) VALUES (?,?,?)",
            (generation, item["card_id"], snapshot_by_repo[item["repo"]]),
        )
        terms = sorted(_tokens(str(item["content"])))[:MAX_CARD_ROUTING_TERMS]
        connection.executemany(
            "INSERT OR IGNORE INTO atlas_card_terms(card_id,schema_version,term) VALUES (?,?,?)",
            ((item["card_id"], ATLAS_CARD_TERM_SCHEMA_VERSION, term) for term in terms),
        )
        card_count += 1
        term_count += len(terms)
    _, _, _, expected_card_rows = _expected_term_projection(payload, "card")
    card_projection_hash = _verified_term_projection_hash(
        connection, generation, kind="card", schema_version=ATLAS_CARD_TERM_SCHEMA_VERSION,
        expected_rows=expected_card_rows,
    )
    connection.execute(
        "INSERT INTO generation_card_indexes"
        "(generation,schema_version,card_count,term_count,projection_hash) VALUES (?,?,?,?,?)",
        (generation, ATLAS_CARD_TERM_SCHEMA_VERSION, card_count, term_count, card_projection_hash),
    )
    change_count = 0
    change_term_count = 0
    changes = payload.get("changes") or []
    for item in changes:
        insert_immutable(
            "atlas_changes", "change_id",
            ("change_id", "repo", "commit_sha", "committed_at", "ticket", "path", "old_path", "status",
             "additions", "deletions", "metadata_json"),
            (item["change_id"], item["repo"], item["commit_sha"], item.get("committed_at"), item.get("ticket"),
             item["path"], item.get("old_path"), item["status"], item.get("additions"), item.get("deletions"),
             json.dumps(item.get("metadata") or {}, sort_keys=True)),
        )
        connection.execute(
            "INSERT INTO generation_changes(generation,change_id,snapshot_sha) VALUES (?,?,?)",
            (generation, item["change_id"], snapshot_by_repo[item["repo"]]),
        )
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        terms = sorted(_tokens(
            f"{item.get('ticket') or ''} {item['path']} {metadata.get('subject') or ''}"
        ))[:MAX_CHANGE_ROUTING_TERMS]
        connection.executemany(
            "INSERT OR IGNORE INTO atlas_change_terms(change_id,schema_version,term) VALUES (?,?,?)",
            ((item["change_id"], ATLAS_CHANGE_TERM_SCHEMA_VERSION, term) for term in terms),
        )
        change_count += 1
        change_term_count += len(terms)
    _, _, _, expected_change_rows = _expected_term_projection(payload, "change")
    change_projection_hash = _verified_term_projection_hash(
        connection, generation, kind="change", schema_version=ATLAS_CHANGE_TERM_SCHEMA_VERSION,
        expected_rows=expected_change_rows,
    )
    connection.execute(
        "INSERT INTO generation_change_indexes"
        "(generation,schema_version,change_count,term_count,projection_hash) VALUES (?,?,?,?,?)",
        (generation, ATLAS_CHANGE_TERM_SCHEMA_VERSION, change_count, change_term_count, change_projection_hash),
    )
    anchor_count = 0
    anchor_term_count = 0
    runtime_anchors = payload.get("runtime_anchors") or []
    for item in runtime_anchors:
        insert_immutable(
            "atlas_runtime_anchors", "anchor_id",
            ("anchor_id", "kind", "value", "normalized", "repo", "module_id", "entity_id", "path", "line",
             "blob_sha", "confidence", "method", "provenance_json", "fingerprint"),
            (item["anchor_id"], item["kind"], item["value"], item["normalized"], item["repo"],
             item.get("module_id"), item.get("entity_id"), item["path"], item["line"], item["blob_sha"],
             item["confidence"], item["method"], json.dumps(item.get("provenance") or {}, sort_keys=True),
             item["fingerprint"]),
        )
        connection.execute(
            "INSERT INTO generation_runtime_anchors(generation,anchor_id,snapshot_sha) VALUES (?,?,?)",
            (generation, item["anchor_id"], snapshot_by_repo[item["repo"]]),
        )
        terms = sorted(set(_compound_terms(str(item["normalized"]))))[:MAX_COMPOUND_ANCHOR_QUERIES]
        connection.executemany(
            "INSERT OR IGNORE INTO atlas_runtime_anchor_terms(anchor_id,schema_version,term) VALUES (?,?,?)",
            ((item["anchor_id"], RUNTIME_ANCHOR_TERM_SCHEMA_VERSION, term) for term in terms),
        )
        anchor_count += 1
        anchor_term_count += len(terms)
    _, _, _, expected_anchor_rows = _expected_term_projection(payload, "anchor")
    anchor_projection_hash = _verified_term_projection_hash(
        connection, generation, kind="anchor", schema_version=RUNTIME_ANCHOR_TERM_SCHEMA_VERSION,
        expected_rows=expected_anchor_rows,
    )
    connection.execute(
        "INSERT INTO generation_runtime_anchor_indexes"
        "(generation,schema_version,anchor_count,term_count,projection_hash) VALUES (?,?,?,?,?)",
        (
            generation, RUNTIME_ANCHOR_TERM_SCHEMA_VERSION, anchor_count,
            anchor_term_count, anchor_projection_hash,
        ),
    )
    for item in payload.get("integration_facts") or []:
        insert_immutable(
            "atlas_integration_facts", "fact_id",
            ("fact_id", "kind", "key_value", "normalized", "repo", "module_id", "entity_id", "path", "line",
             "blob_sha", "direction", "framework", "confidence", "provenance_json", "fingerprint"),
            (item["fact_id"], item["kind"], item["key"], item["normalized"], item["repo"],
             item.get("module_id"), item.get("entity_id"), item["path"], item["line"], item["blob_sha"],
             item["direction"], item["framework"], item["confidence"],
             json.dumps(item.get("provenance") or {}, sort_keys=True), item["fingerprint"]),
        )
        connection.execute(
            "INSERT INTO generation_integration_facts(generation,fact_id,snapshot_sha) VALUES (?,?,?)",
            (generation, item["fact_id"], snapshot_by_repo[item["repo"]]),
        )
    for item in payload.get("v1_files") or []:
        connection.execute(
            "INSERT INTO generation_intelligence_files(generation,repo,path,blob_sha,schema_version) "
            "VALUES (?,?,?,?,?)",
            (generation, item["repo"], item["path"], item["blob_sha"], item["schema_version"]),
        )
    connection.execute(
        "INSERT INTO atlas_refresh_deltas(generation,parent_generation,payload_json) VALUES (?,?,?)",
        (generation, parent_generation, json.dumps(payload.get("delta") or {}, sort_keys=True)),
    )


def _write_current_projection(root: Path, generation: int) -> None:
    atomic_managed_text_write(root, root / "CURRENT", f"generation-{generation:06d}\n")


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
    from .atlas import MAX_ATLAS_FILES
    from .index import SCHEMA_VERSION as SEARCH_SCHEMA_VERSION
    from .index import _update_membership_digest

    search_path = settings.state_dir / "search.sqlite3"
    if not search_path.is_file():
        return
    source = connect_managed_sqlite(settings.state_dir, search_path, timeout=30)
    target: sqlite3.Connection | None = None
    try:
        source.execute("BEGIN")
        has_metadata = source.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'"
        ).fetchone()
        schema = (
            source.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
            if has_metadata else None
        )
        try:
            source_schema_version = int(schema[0]) if schema else -1
        except (TypeError, ValueError) as error:
            raise RuntimeError("lexical index schema version is invalid") from error
        if source_schema_version != SEARCH_SCHEMA_VERSION:
            raise RuntimeError(
                f"lexical index schema {source_schema_version} is incompatible with "
                f"Atlas publication schema {SEARCH_SCHEMA_VERSION}"
            )
        has_membership = source.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='file_membership'"
        ).fetchone()
        has_seals = source.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='indexed_snapshots'"
        ).fetchone()
        if not has_membership or not has_seals:
            raise RuntimeError("sealed lexical membership is unavailable for Atlas publication")
        deadline = time.monotonic() + 30.0
        planned: list[tuple[str, str, int, int, str]] = []
        total_rows = 0
        total_metadata_bytes = 0
        for repo, raw in state.items():
            if not isinstance(raw, dict):
                continue
            sha = str(raw.get("sha") or "working-tree")
            sealed = source.execute(
                "SELECT file_count,membership_hash FROM indexed_snapshots "
                "WHERE repo=? AND snapshot_sha=?",
                (repo, sha),
            ).fetchone()
            if sealed is None or not sealed[1]:
                raise RuntimeError(f"sealed lexical membership is unavailable for {repo}:{sha}")
            expected_count = int(sealed[0])
            expected_hash = str(sealed[1])
            state_count = raw.get("files")
            if state_count is not None and int(state_count) != expected_count:
                raise RuntimeError(f"lexical state does not match its sealed membership for {repo}:{sha}")
            count = 0
            metadata_bytes = 0
            membership_digest = hashlib.sha256()
            for path, blob in source.execute(
                "SELECT path,blob FROM file_membership "
                "WHERE repo=? AND snapshot_sha=? ORDER BY path",
                (repo, sha),
            ):
                count += 1
                metadata_bytes += len(str(path).encode("utf-8")) + len(str(blob).encode("utf-8"))
                _update_membership_digest(membership_digest, str(repo), sha, path, blob)
                if (
                    count > expected_count
                    or total_rows + count > MAX_ATLAS_FILES
                    or total_metadata_bytes + metadata_bytes > 512 * 1024 * 1024
                    or time.monotonic() >= deadline
                ):
                    raise RuntimeError("lexical catalog projection exceeds its item, byte, or time limit")
            actual_hash = "sha256:" + membership_digest.hexdigest()
            if count != expected_count or actual_hash != expected_hash:
                raise RuntimeError(f"lexical membership seal validation failed for {repo}:{sha}")
            total_rows += count
            total_metadata_bytes += metadata_bytes
            planned.append((str(repo), sha, count, metadata_bytes, expected_hash))
        target = connect(settings)
        copied_rows = 0
        copied_metadata_bytes = 0
        for repo, sha, expected_count, expected_metadata_bytes, expected_hash in planned:
            target.execute("DELETE FROM snapshot_files WHERE repo=? AND sha=?", (repo, sha))
            target.execute("DELETE FROM paths WHERE repo=? AND sha=?", (repo, sha))
            rows = source.execute(
                "SELECT f.path,f.blob,b.size FROM file_membership f JOIN blobs b ON b.blob=f.blob "
                "WHERE f.repo=? AND f.snapshot_sha=? ORDER BY f.path",
                (repo, sha),
            )
            repo_rows = 0
            repo_metadata_bytes = 0
            membership_digest = hashlib.sha256()
            for path, blob, size in rows:
                value = str(path)
                blob_value = str(blob)
                repo_rows += 1
                row_metadata_bytes = len(value.encode("utf-8")) + len(blob_value.encode("utf-8"))
                repo_metadata_bytes += row_metadata_bytes
                copied_rows += 1
                copied_metadata_bytes += row_metadata_bytes
                _update_membership_digest(membership_digest, repo, sha, value, blob_value)
                if (
                    repo_rows > expected_count
                    or copied_rows > MAX_ATLAS_FILES
                    or copied_metadata_bytes > 512 * 1024 * 1024
                    or time.monotonic() >= deadline
                ):
                    raise RuntimeError("lexical catalog projection changed or exceeded its copy budget")
                target.execute("INSERT OR IGNORE INTO blobs(blob_sha, size, created_at) VALUES (?, ?, ?)", (blob, int(size), datetime.now(UTC).isoformat()))
                target.execute("INSERT INTO snapshot_files(repo, sha, path, blob_sha) VALUES (?, ?, ?, ?)", (repo, sha, value, blob))
                file = Path(value)
                target.execute("INSERT INTO paths(repo, sha, path, basename, stem) VALUES (?, ?, ?, ?, ?)", (repo, sha, value, file.name, file.stem))
            copied_hash = "sha256:" + membership_digest.hexdigest()
            if (
                repo_rows != expected_count
                or repo_metadata_bytes != expected_metadata_bytes
                or copied_hash != expected_hash
            ):
                raise RuntimeError("lexical catalog projection changed during copy")
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


def status_probe(settings: Settings) -> str | None:
    """Perform a constant-size catalog probe suitable for dashboard polling."""
    try:
        connection = connect(settings)
        try:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version' LIMIT 1"
            ).fetchone()
            return None if row else "catalog schema metadata is unavailable"
        finally:
            connection.close()
    except (sqlite3.Error, ValueError) as exc:
        return str(exc)

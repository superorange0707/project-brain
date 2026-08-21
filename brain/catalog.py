from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .core import Settings

SCHEMA_VERSION = 2


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
            """
        )
        value = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        old_version = int(value[0]) if value else 0
        if old_version > SCHEMA_VERSION:
            raise sqlite3.DatabaseError(f"catalog schema {value[0]} is newer than this Project Brain")
        if old_version < 2:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(embedding_cache)")}
            if "last_used_at" not in columns:
                connection.execute("ALTER TABLE embedding_cache ADD COLUMN last_used_at TEXT")
                connection.execute("UPDATE embedding_cache SET last_used_at=created_at WHERE last_used_at IS NULL")
        connection.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
        connection.commit()
        return connection
    except Exception:
        connection.close()
        raise


def generation_root(settings: Settings) -> Path:
    return settings.state_dir / "generations"


def current_generation(settings: Settings) -> dict[str, Any] | None:
    pointer = generation_root(settings) / "CURRENT"
    try:
        name = pointer.read_text(encoding="utf-8").strip()
        manifest = generation_root(settings) / name / "manifest.json"
        return json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else None
    except (OSError, json.JSONDecodeError):
        return None


def publish_generation(settings: Settings, state: dict[str, object], *, backends: list[str] | None = None) -> dict[str, Any]:
    """Publish metadata through an atomic pointer; a failed build never moves CURRENT."""
    root = generation_root(settings)
    root.mkdir(parents=True, exist_ok=True)
    connection = connect(settings)
    try:
        row = connection.execute("SELECT COALESCE(MAX(generation), 0) FROM index_generations").fetchone()
        number = int(row[0] or 0) + 1
        created_at = datetime.now(UTC).isoformat()
        snapshots = [
            {"repo": name, "ref": data.get("ref"), "sha": data.get("sha")}
            for name, raw in sorted(state.items())
            if isinstance(raw, dict)
            for data in [raw]
        ]
        manifest = {"generation": number, "created_at": created_at, "snapshots": snapshots, "backends": backends or ["sqlite-fts5"]}
        build = Path(tempfile.mkdtemp(prefix=f"build-{number:06d}-", dir=root))
        try:
            (build / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            final = root / f"generation-{number:06d}"
            build.replace(final)
            temporary = root / f"CURRENT-{number:06d}.tmp"
            temporary.write_text(final.name + "\n", encoding="utf-8")
            os.replace(temporary, root / "CURRENT")
        except Exception:
            if build.exists():
                import shutil

                shutil.rmtree(build, ignore_errors=True)
            raise
        connection.execute(
            "INSERT INTO index_generations(generation, created_at, manifest_path, status) VALUES (?, ?, ?, 'current')",
            (number, created_at, str(final / "manifest.json")),
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
        connection.commit()
        return manifest
    finally:
        connection.close()


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
    target = connect(settings)
    try:
        for repo, raw in state.items():
            if not isinstance(raw, dict) or not raw.get("sha"):
                continue
            sha = str(raw["sha"])
            target.execute("DELETE FROM snapshot_files WHERE repo=? AND sha=?", (repo, sha))
            target.execute("DELETE FROM paths WHERE repo=? AND sha=?", (repo, sha))
            rows = source.execute(
                "SELECT f.path, f.blob, b.size FROM files f JOIN blobs b ON b.blob=f.blob WHERE f.repo=?", (repo,)
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

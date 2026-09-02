from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Callable, Iterable, Iterator

from .platforms import (
    atomic_managed_text_write,
    connect_managed_sqlite,
    logical_path,
    native_command,
    read_direct_file_bytes,
    read_managed_text,
    remove_tree,
    run_bounded_process,
)

if TYPE_CHECKING:
    from .core import Repository, Settings


SCHEMA_VERSION = 3
LEXICAL_COMPONENT_SCHEMA_VERSION = 2
INDEXABLE_NAMES = {
    "Dockerfile", "Jenkinsfile", "Makefile", "Procfile", "build.gradle", "gradlew", "mvnw", "pom.xml",
}
SENSITIVE_FILE_NAMES = {".env", ".envrc", "credentials", "credentials.json", "service-account.json", "id_rsa", "id_ed25519", "keystore"}
SENSITIVE_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".jks"}
_SNAPSHOT_INTEGRITY_CACHE: dict[tuple[object, ...], bool] = {}
_GIT_BLOB_BATCH_BYTES = 32 * 1024 * 1024
_GIT_BLOB_BATCH_ITEMS = 512
_GIT_BLOB_CHECK_BYTES = 64 * 1024
_GIT_MANIFEST_MAX_BYTES = 64 * 1024 * 1024
_GIT_MANIFEST_MAX_ITEMS = 1_000_000
_GIT_MANIFEST_TIMEOUT_SECONDS = 120.0
_NONGIT_SNAPSHOT_MAX_ITEMS = 100_000
_NONGIT_SNAPSHOT_MAX_BYTES = 64 * 1024 * 1024 * 1024
_NONGIT_SNAPSHOT_MAX_SECONDS = 300.0
_NONGIT_SNAPSHOT_MAX_DEPTH = 128
_NONGIT_SNAPSHOT_METADATA_BYTES_PER_ITEM = 1024


def _safe_path(path: str | Path) -> bool:
    value = Path(path)
    return value.name.lower() not in SENSITIVE_FILE_NAMES and value.suffix.lower() not in SENSITIVE_SUFFIXES


def _database(settings: Settings) -> Path:
    return settings.state_dir / "search.sqlite3"


def _connect(settings: Settings) -> sqlite3.Connection:
    database = _database(settings)
    connection = connect_managed_sqlite(settings.state_dir, database, timeout=30)
    try:
        return _initialize_connection(connection)
    except Exception:
        connection.close()
        raise


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
        raise sqlite3.DatabaseError("search schema version is invalid") from error
    if version > SCHEMA_VERSION:
        raise sqlite3.DatabaseError(f"search schema {row[0]} is newer than this Project Brain")
    return version


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


def _execute_sql_script(connection: sqlite3.Connection, script: str) -> None:
    """Execute a script without sqlite3.executescript's implicit pre-commit."""
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            sql = statement.strip()
            if sql:
                connection.execute(sql)
            statement = ""
    if statement.strip():
        raise sqlite3.DatabaseError("incomplete search schema statement")


def _initialize_connection(connection: sqlite3.Connection) -> sqlite3.Connection:
    preflight_version = _preflight_schema_version(connection)
    if preflight_version == SCHEMA_VERSION:
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection
    _enable_wal(connection)
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("BEGIN IMMEDIATE")
    # A newer process may have published while this opener waited for WAL or
    # the writer lock. Recheck under the same transaction as every DDL write.
    locked_version = _preflight_schema_version(connection)
    if locked_version == SCHEMA_VERSION:
        connection.commit()
        return connection
    _execute_sql_script(
        connection,
        """
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS repositories (
            name TEXT PRIMARY KEY,
            sha TEXT,
            indexed_at TEXT NOT NULL,
            file_count INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS blobs (
            blob TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            size INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS files (
            repo TEXT NOT NULL,
            path TEXT NOT NULL,
            blob TEXT NOT NULL REFERENCES blobs(blob),
            PRIMARY KEY (repo, path)
        );
        CREATE INDEX IF NOT EXISTS files_blob ON files(blob);
        CREATE VIRTUAL TABLE IF NOT EXISTS blob_fts USING fts5(
            blob UNINDEXED, content, tokenize='trigram'
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS path_fts USING fts5(
            repo UNINDEXED, path, tokenize='trigram'
        );
        CREATE TABLE IF NOT EXISTS indexed_snapshots (
            repo TEXT NOT NULL,
            snapshot_sha TEXT NOT NULL,
            indexed_at TEXT NOT NULL,
            file_count INTEGER NOT NULL,
            membership_hash TEXT,
            PRIMARY KEY (repo, snapshot_sha)
        );
        CREATE TABLE IF NOT EXISTS file_membership (
            repo TEXT NOT NULL,
            snapshot_sha TEXT NOT NULL,
            path TEXT NOT NULL,
            blob TEXT NOT NULL REFERENCES blobs(blob),
            PRIMARY KEY (repo, snapshot_sha, path)
        );
        CREATE INDEX IF NOT EXISTS file_membership_blob ON file_membership(blob);
        CREATE VIRTUAL TABLE IF NOT EXISTS path_membership_fts USING fts5(
            repo UNINDEXED, snapshot_sha UNINDEXED, path, tokenize='trigram'
        );
        """
    )
    version = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
    try:
        schema_version = int(version[0]) if version else 0
    except (TypeError, ValueError) as error:
        raise sqlite3.DatabaseError("search index schema version is invalid") from error
    if schema_version > SCHEMA_VERSION:
        raise sqlite3.DatabaseError(f"unsupported search index schema {version[0]}")
    if schema_version < SCHEMA_VERSION:
        try:
            current = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
            try:
                current_version = int(current[0]) if current else 0
            except (TypeError, ValueError) as error:
                raise sqlite3.DatabaseError("search index schema version is invalid") from error
            if current_version > SCHEMA_VERSION:
                raise sqlite3.DatabaseError(f"unsupported search index schema {current[0]}")
            if current_version < 2:
                last_blob = ""
                while True:
                    legacy_rows = connection.execute(
                        "SELECT blob,content,size FROM blobs WHERE blob>? ORDER BY blob LIMIT 500",
                        (last_blob,),
                    ).fetchall()
                    if not legacy_rows:
                        break
                    for old_blob, content, size in legacy_rows:
                        last_blob = str(old_blob)
                        if _blob_identity_valid(old_blob, content, size):
                            continue
                        encoded = str(content).encode("utf-8")
                        replacement = _content_blob(encoded)
                        connection.execute(
                            "INSERT OR IGNORE INTO blobs(blob,content,size) VALUES (?,?,?)",
                            (replacement, content, len(encoded)),
                        )
                        connection.execute("DELETE FROM blob_fts WHERE blob=?", (replacement,))
                        connection.execute("INSERT INTO blob_fts(blob,content) VALUES (?,?)", (replacement, content))
                        connection.execute("UPDATE files SET blob=? WHERE blob=?", (replacement, old_blob))
                        connection.execute("DELETE FROM blob_fts WHERE blob=?", (old_blob,))
                        connection.execute("DELETE FROM blobs WHERE blob=?", (old_blob,))
                for repo, path, blob, sha, indexed_at in connection.execute(
                    "SELECT f.repo, f.path, f.blob, COALESCE(r.sha, 'working-tree'), r.indexed_at "
                    "FROM files f JOIN repositories r ON r.name=f.repo"
                ):
                    connection.execute(
                        "INSERT OR IGNORE INTO file_membership(repo, snapshot_sha, path, blob) VALUES (?, ?, ?, ?)",
                        (repo, sha, path, blob),
                    )
                    connection.execute(
                        "INSERT INTO path_membership_fts(repo, snapshot_sha, path) VALUES (?, ?, ?)",
                        (repo, sha, path),
                    )
                connection.execute(
                    "INSERT OR REPLACE INTO indexed_snapshots(repo, snapshot_sha, indexed_at, file_count) "
                    "SELECT name, COALESCE(sha, 'working-tree'), indexed_at, file_count FROM repositories"
                )
                old_count = int(connection.execute("SELECT COUNT(*) FROM files").fetchone()[0])
                migrated_count = int(connection.execute(
                    "SELECT COUNT(*) FROM file_membership m JOIN repositories r ON r.name=m.repo "
                    "WHERE m.snapshot_sha=COALESCE(r.sha, 'working-tree')"
                ).fetchone()[0])
                if old_count != migrated_count:
                    raise sqlite3.DatabaseError("search index v1 migration validation failed")
            if current_version < 3:
                columns = {
                    str(row[1]) for row in connection.execute("PRAGMA table_info(indexed_snapshots)")
                }
                if "membership_hash" not in columns:
                    connection.execute("ALTER TABLE indexed_snapshots ADD COLUMN membership_hash TEXT")
                snapshot_cursor = connection.execute(
                    "SELECT repo,snapshot_sha,file_count FROM indexed_snapshots ORDER BY repo,snapshot_sha"
                )
                while snapshot_batch := snapshot_cursor.fetchmany(128):
                    for repo, snapshot, expected_count in snapshot_batch:
                        actual_count = int(connection.execute(
                            "SELECT COUNT(*) FROM file_membership WHERE repo=? AND snapshot_sha=?",
                            (repo, snapshot),
                        ).fetchone()[0])
                        rows = connection.execute(
                        "SELECT path,blob FROM file_membership "
                        "WHERE repo=? AND snapshot_sha=? ORDER BY path",
                        (repo, snapshot),
                        )
                        identity = (
                            _membership_hash(str(repo), str(snapshot), rows)
                            if actual_count == int(expected_count)
                            else None
                        )
                        connection.execute(
                            "UPDATE indexed_snapshots SET membership_hash=? WHERE repo=? AND snapshot_sha=?",
                            (identity, repo, snapshot),
                        )
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            schema_version = SCHEMA_VERSION
        except Exception:
            connection.rollback()
            raise
    _execute_sql_script(
        connection,
        """
        CREATE TRIGGER IF NOT EXISTS file_membership_identity_insert
        AFTER INSERT ON file_membership BEGIN
            UPDATE indexed_snapshots SET membership_hash=NULL
            WHERE repo=NEW.repo AND snapshot_sha=NEW.snapshot_sha;
        END;
        CREATE TRIGGER IF NOT EXISTS file_membership_identity_delete
        AFTER DELETE ON file_membership BEGIN
            UPDATE indexed_snapshots SET membership_hash=NULL
            WHERE repo=OLD.repo AND snapshot_sha=OLD.snapshot_sha;
        END;
        CREATE TRIGGER IF NOT EXISTS file_membership_identity_update
        AFTER UPDATE ON file_membership BEGIN
            UPDATE indexed_snapshots SET membership_hash=NULL
            WHERE (repo=OLD.repo AND snapshot_sha=OLD.snapshot_sha)
               OR (repo=NEW.repo AND snapshot_sha=NEW.snapshot_sha);
        END;
        """
    )
    connection.commit()
    return connection


class _WalkBudget:
    def __init__(self, max_entries: int, deadline: float, max_depth: int) -> None:
        self.remaining = max(0, max_entries)
        self.deadline = deadline
        self.max_depth = max(0, max_depth)

    def consume(self, depth: int) -> None:
        if self.remaining <= 0 or depth > self.max_depth or time.monotonic() >= self.deadline:
            raise RuntimeError("repository tree exceeded its item or time limit")
        self.remaining -= 1


def _walk_root(
    root: Path,
    suffixes: set[str] | None,
    ignored_dirs: set[str],
    *,
    budget: _WalkBudget,
) -> Iterable[Path]:
    if root.is_symlink() or not root.is_dir():
        return
    pending = [(root, 0)]
    while pending:
        directory, depth = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    budget.consume(depth)
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name not in ignored_dirs:
                            if depth >= budget.max_depth:
                                raise RuntimeError("repository tree exceeded its item or time limit")
                            pending.append((Path(entry.path), depth + 1))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    path = Path(entry.path)
                    if _safe_path(path) and (
                        suffixes is None
                        or path.suffix.lower() in suffixes
                        or entry.name in INDEXABLE_NAMES
                    ):
                        yield path
        except OSError as error:
            raise RuntimeError("authoritative repository tree is unreadable") from error


def _walk_files(
    repo: Repository, suffixes: set[str], ignored_dirs: set[str], *, budget: _WalkBudget,
) -> Iterable[Path]:
    yield from _walk_root(repo.scan_path, suffixes, ignored_dirs, budget=budget)


def _read_source_bytes(path: Path, *, max_bytes: int = 3_000_000) -> bytes:
    try:
        raw, _ = read_direct_file_bytes(path, max_bytes=max_bytes)
        return raw
    except ValueError as error:
        raise OSError("source path is unavailable or symbolic") from error


def _write_source_snapshot_state(settings: Settings, repositories: Iterable[Repository]) -> None:
    path = settings.state_dir / "sources.json"
    try:
        state = json.loads(read_managed_text(
            settings.state_dir, path, max_bytes=16 * 1024 * 1024,
        )) if path.is_file() else {}
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    timestamp = datetime.now(UTC).isoformat()
    for repo in repositories:
        state[repo.name] = {
            "repo": repo.name,
            "status": repo.source_status,
            "ref": repo.source_ref,
            "sha": repo.source_sha,
            "snapshot": str(repo.source_path) if repo.source_path else None,
            "fetched": repo.source_fetched,
            "warning": repo.source_warning,
            "synced_at": timestamp,
        }
    atomic_managed_text_write(settings.state_dir, path, json.dumps(state, indent=2) + "\n")


def prepare_working_tree_snapshots(
    settings: Settings, *, suffixes: set[str], ignored_dirs: set[str],
) -> None:
    """Materialize content-addressed immutable evidence snapshots for non-Git repositories."""
    from .sync import (
        MAX_GIT_REFRESH_SCAN_ITEMS, MAX_GIT_REFRESH_SCAN_SECONDS,
        _SnapshotScanBudget, _safe_component, _sealed_snapshot_is_intact,
        _snapshot_seal, _snapshot_seal_path, _windows_archive_key,
    )

    from .ops import remaining_write_capacity

    prepared: list[Repository] = []
    shared_capacity = remaining_write_capacity(settings)
    walk_budget = _WalkBudget(
        _NONGIT_SNAPSHOT_MAX_ITEMS,
        time.monotonic() + _NONGIT_SNAPSHOT_MAX_SECONDS,
        _NONGIT_SNAPSHOT_MAX_DEPTH,
    )
    snapshot_scan_budget = _SnapshotScanBudget(
        MAX_GIT_REFRESH_SCAN_ITEMS,
        time.monotonic() + MAX_GIT_REFRESH_SCAN_SECONDS,
    )
    internal_roots = {
        path.resolve() for path in (
            settings.state_dir, settings.runs_dir, settings.generated_dir, settings.knowledge_dir,
        )
    }
    for repo in settings.repositories:
        if (
            repo.source_sha not in {None, "working-tree"}
            and not str(repo.source_sha).startswith(("nongit-", "worktree-"))
        ):
            continue
        root = repo.path.resolve()
        parent = (settings.state_dir / "snapshots" / _safe_component(repo.name)).resolve()
        if not parent.is_relative_to(settings.state_dir.resolve()):
            raise ValueError("unsafe working-tree snapshot destination")
        parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix="snapshot-working-tree-", dir=parent))
        membership: list[tuple[str, str]] = []
        windows_members: set[str] = set()
        byte_budget = min(_NONGIT_SNAPSHOT_MAX_BYTES, shared_capacity)
        copied_bytes = 0
        copied_items = 0
        deadline = walk_budget.deadline
        try:
            for source in _walk_root(root, suffixes, ignored_dirs, budget=walk_budget):
                copied_items += 1
                if copied_items > _NONGIT_SNAPSHOT_MAX_ITEMS or time.monotonic() >= deadline:
                    raise RuntimeError(f"Non-Git snapshot exceeded its item or time limit for {repo.name}")
                resolved = source.resolve()
                if any(resolved == excluded or resolved.is_relative_to(excluded) for excluded in internal_roots):
                    continue
                if not resolved.is_relative_to(root) or source.is_symlink():
                    continue
                try:
                    if source.stat().st_size > 3_000_000:
                        continue
                    content = _read_source_bytes(source)
                except OSError as error:
                    raise RuntimeError(f"Could not read authoritative non-Git source {repo.name}:{source.name}") from error
                if len(content) > 3_000_000 or b"\0" in content[:8192]:
                    continue
                if (
                    copied_bytes + len(content)
                    + copied_items * _NONGIT_SNAPSHOT_METADATA_BYTES_PER_ITEM > byte_budget
                ):
                    raise OSError(f"Non-Git snapshot exceeds managed write capacity for {repo.name}")
                relative = logical_path(source.relative_to(root))
                member_key = _windows_archive_key(PurePosixPath(relative)) if os.name == "nt" else relative.casefold()
                if os.name == "nt" and member_key in windows_members:
                    raise ValueError("working tree has paths that collide on Windows")
                windows_members.add(member_key)
                destination = (temporary / Path(*Path(relative).parts)).resolve()
                if not destination.is_relative_to(temporary.resolve()):
                    raise ValueError("unsafe working-tree snapshot path")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
                destination.chmod(0o555 if source.stat().st_mode & stat.S_IXUSR else 0o444)
                membership.append((relative, _content_blob(content)))
                copied_bytes += len(content)
            digest = hashlib.sha256()
            for relative, blob in sorted(membership):
                digest.update(relative.encode("utf-8"))
                digest.update(b"\0")
                digest.update(blob.encode("ascii"))
                digest.update(b"\0")
            is_unborn_git = (repo.path / ".git").exists()
            snapshot = ("worktree-" if is_unborn_git else "nongit-") + digest.hexdigest()
            target = parent / snapshot
            seal_path = _snapshot_seal_path(parent, snapshot)
            persistent_bytes = 0
            if _sealed_snapshot_is_intact(
                target, seal_path, snapshot, scan_budget=snapshot_scan_budget,
            ):
                remove_tree(temporary, ignore_errors=True)
            else:
                seal = _snapshot_seal(
                    temporary, snapshot, scan_budget=snapshot_scan_budget,
                )
                seal_json = json.dumps(seal, sort_keys=True)
                persistent_bytes = copied_bytes + len(seal_json.encode("utf-8"))
                if persistent_bytes > shared_capacity:
                    raise OSError(f"Non-Git snapshot exceeds managed write capacity for {repo.name}")
                backup: Path | None = None
                if target.exists() or target.is_symlink():
                    backup = Path(tempfile.mkdtemp(prefix=f".{snapshot}.stale-", dir=parent))
                    backup.rmdir()
                    target.rename(backup)
                try:
                    temporary.rename(target)
                    atomic_managed_text_write(
                        settings.state_dir, seal_path, seal_json,
                    )
                except Exception:
                    if target.exists():
                        remove_tree(target, ignore_errors=True)
                    if backup is not None:
                        backup.rename(target)
                    raise
                if backup is not None:
                    remove_tree(backup, ignore_errors=True)
            shared_capacity = max(0, shared_capacity - persistent_bytes)
            repo.source_path = target
            repo.source_ref = "WORKTREE"
            repo.source_sha = snapshot
            repo.source_status = "unborn-git-snapshot" if is_unborn_git else "non-git-snapshot"
            repo.source_fetched = False
            repo.source_warning = (
                "immutable unborn-Git snapshot; no commit freshness check"
                if is_unborn_git else "immutable local snapshot; no remote freshness check"
            )
            prepared.append(repo)
        finally:
            if temporary.exists():
                remove_tree(temporary, ignore_errors=True)
    if prepared:
        _write_source_snapshot_state(settings, prepared)


def _git_manifest(repo: Repository) -> dict[str, tuple[str, str]] | None:
    if (
        not repo.source_sha
        or str(repo.source_sha).startswith(("nongit-", "worktree-"))
        or not (repo.path / ".git").exists()
    ):
        return None
    try:
        result = run_bounded_process(
            ["git", "ls-tree", "-r", "-z", repo.source_sha], repo.path,
            max_stdout_bytes=_GIT_MANIFEST_MAX_BYTES,
            timeout=_GIT_MANIFEST_TIMEOUT_SECONDS,
            binary_output=True,
        )
    except OSError as error:
        raise RuntimeError(f"Could not read authoritative Git manifest for {repo.name}") from error
    if result.returncode or getattr(result, "output_truncated", False) or getattr(result, "timed_out", False):
        raise RuntimeError(f"Authoritative Git manifest exceeded its process, time, or byte limit for {repo.name}")
    blobs: dict[str, tuple[str, str]] = {}
    output = result.stdout
    if not isinstance(output, bytes):
        raise RuntimeError(f"Authoritative Git manifest returned an invalid stream for {repo.name}")
    start = 0
    items = 0
    while start < len(output):
        end = output.find(b"\0", start)
        if end < 0:
            raise RuntimeError(f"Authoritative Git manifest is incomplete for {repo.name}")
        record = output[start:end]
        start = end + 1
        if not record or b"\t" not in record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        fields = metadata.split()
        if len(fields) == 3 and fields[1] == b"blob":
            items += 1
            if items > _GIT_MANIFEST_MAX_ITEMS:
                raise RuntimeError(f"Authoritative Git manifest exceeded its item limit for {repo.name}")
            try:
                path = raw_path.decode("utf-8", errors="strict")
                mode = fields[0].decode("ascii", errors="strict")
                blob = fields[2].decode("ascii", errors="strict")
            except UnicodeDecodeError as error:
                raise RuntimeError(f"Authoritative Git manifest contains a non-UTF-8 path for {repo.name}") from error
            if path in blobs:
                raise RuntimeError(f"Authoritative Git manifest contains a duplicate path for {repo.name}")
            blobs[path] = (mode, blob)
    return blobs


def _git_blob_contents(repo: Repository, blobs: set[str]) -> Iterable[tuple[str, bytes]]:
    """Yield changed Git objects in explicit item/byte-bounded subprocess batches."""
    if not blobs:
        return

    def check(batch: list[str]) -> Iterable[tuple[str, int]]:
        environment = {**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"}
        try:
            checked = run_bounded_process(
                ["git", "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
                repo.path,
                input_bytes=("\n".join(batch) + "\n").encode("ascii"),
                environment=environment,
                max_stdout_bytes=_GIT_BLOB_CHECK_BYTES,
                max_stderr_bytes=64 * 1024,
                timeout=30,
                binary_output=True,
            )
        except OSError as error:
            raise RuntimeError(f"Could not validate authoritative Git objects for {repo.name}") from error
        if checked.returncode or getattr(checked, "output_truncated", False) or getattr(checked, "timed_out", False):
            raise RuntimeError(f"Authoritative Git object validation failed for {repo.name}")
        lines = checked.stdout.splitlines()
        if len(lines) != len(batch):
            raise RuntimeError(f"Authoritative Git object validation is incomplete for {repo.name}")
        seen: set[str] = set()
        for line in lines:
            fields = line.split()
            try:
                if len(fields) != 3 or fields[1] != b"blob":
                    raise ValueError
                blob = fields[0].decode("ascii", errors="strict")
                size = int(fields[2])
            except (UnicodeDecodeError, ValueError) as error:
                raise RuntimeError(f"Authoritative Git object validation is malformed for {repo.name}") from error
            if blob not in batch or blob in seen:
                raise RuntimeError(f"Authoritative Git object validation changed identity for {repo.name}")
            seen.add(blob)
            if size <= 3_000_000:
                yield blob, size
        if seen != set(batch):
            raise RuntimeError(f"Authoritative Git object validation is incomplete for {repo.name}")

    def eligible_objects() -> Iterable[tuple[str, int]]:
        pending: list[str] = []
        pending_bytes = 0
        # The caller already owns the missing-object set. Avoid a second full
        # sorted copy; Git object identity makes load order irrelevant.
        for blob in blobs:
            try:
                encoded_size = len(blob.encode("ascii")) + 1
            except UnicodeEncodeError:
                continue
            if encoded_size > _GIT_BLOB_CHECK_BYTES:
                continue
            if pending and (
                len(pending) >= _GIT_BLOB_BATCH_ITEMS
                or pending_bytes + encoded_size > _GIT_BLOB_CHECK_BYTES
            ):
                yield from check(pending)
                pending, pending_bytes = [], 0
            pending.append(blob)
            pending_bytes += encoded_size
        if pending:
            yield from check(pending)

    def load(batch: list[str]) -> Iterable[tuple[str, bytes]]:
        environment = {**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"}
        try:
            loaded = run_bounded_process(
                ["git", "cat-file", "--batch"], repo.path,
                input_bytes=("\n".join(batch) + "\n").encode("ascii"),
                environment=environment,
                max_stdout_bytes=_GIT_BLOB_BATCH_BYTES + _GIT_BLOB_CHECK_BYTES,
                max_stderr_bytes=64 * 1024,
                timeout=60,
                binary_output=True,
            )
        except OSError as error:
            raise RuntimeError(f"Could not load authoritative Git objects for {repo.name}") from error
        if loaded.returncode or getattr(loaded, "output_truncated", False) or getattr(loaded, "timed_out", False):
            raise RuntimeError(f"Authoritative Git object loading failed for {repo.name}")
        position = 0
        seen: set[str] = set()
        while position < len(loaded.stdout):
            header_end = loaded.stdout.find(b"\n", position)
            if header_end < 0:
                raise RuntimeError(f"Authoritative Git object output is incomplete for {repo.name}")
            fields = loaded.stdout[position:header_end].split()
            if len(fields) != 3 or fields[1] != b"blob":
                raise RuntimeError(f"Authoritative Git object output is malformed for {repo.name}")
            try:
                blob = fields[0].decode("ascii", errors="strict")
                size = int(fields[2])
            except (UnicodeDecodeError, ValueError) as error:
                raise RuntimeError(f"Authoritative Git object output is malformed for {repo.name}") from error
            start, end = header_end + 1, header_end + 1 + size
            if end >= len(loaded.stdout) or loaded.stdout[end:end + 1] != b"\n":
                raise RuntimeError(f"Authoritative Git object output is incomplete for {repo.name}")
            if blob not in batch or blob in seen:
                raise RuntimeError(f"Authoritative Git object output changed identity for {repo.name}")
            seen.add(blob)
            yield blob, loaded.stdout[start:end]
            position = end + 1
        if seen != set(batch):
            raise RuntimeError(f"Authoritative Git object output is incomplete for {repo.name}")

    current: list[str] = []
    current_bytes = 0
    for blob, size in eligible_objects():
        if current and (
            len(current) >= _GIT_BLOB_BATCH_ITEMS or current_bytes + size > _GIT_BLOB_BATCH_BYTES
        ):
            yield from load(current)
            current, current_bytes = [], 0
        current.append(blob)
        current_bytes += size
    if current:
        yield from load(current)


def _content_blob(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _update_membership_digest(
    digest: Any, repo: str, snapshot: str, path: object, blob: object,
) -> None:
    """Add one ordered lexical membership row to its shared identity proof."""
    digest.update(f"{repo}\0{snapshot}\0{path}\0{blob}\n".encode("utf-8"))


def _membership_hash(repo: str, snapshot: str, rows: Iterable[tuple[object, object]]) -> str:
    digest = hashlib.sha256()
    for path, blob in rows:
        _update_membership_digest(digest, repo, snapshot, path, blob)
    return "sha256:" + digest.hexdigest()


def _blob_identity_valid(blob: object, content: object, size: object) -> bool:
    if not isinstance(content, str):
        return False
    encoded = content.encode("utf-8")
    try:
        if len(encoded) != int(size):
            return False
    except (TypeError, ValueError):
        return False
    identity = str(blob)
    if identity.startswith("sha256:"):
        return identity == _content_blob(encoded)
    if len(identity) in {40, 64} and all(character in "0123456789abcdef" for character in identity.casefold()):
        algorithm = hashlib.sha1 if len(identity) == 40 else hashlib.sha256
        return algorithm(b"blob " + str(len(encoded)).encode("ascii") + b"\0" + encoded).hexdigest() == identity.casefold()
    return False


def _stored_blob_valid(connection: sqlite3.Connection, blob: str) -> bool:
    row = connection.execute("SELECT content,size FROM blobs WHERE blob=?", (blob,)).fetchone()
    return bool(row and _blob_identity_valid(blob, row[0], row[1]))


def _database_artifact_identity(settings: Settings) -> tuple[tuple[object, ...], ...]:
    paths = (_database(settings), Path(str(_database(settings)) + "-wal"))
    identity: list[tuple[object, ...]] = []
    for path in paths:
        try:
            stat = path.stat()
            if path.name.endswith("-wal") and stat.st_size == 0:
                identity.append((str(path), "empty"))
                continue
            identity.append((str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        except OSError:
            identity.append((str(path), "empty" if path.name.endswith("-wal") else "missing"))
    return tuple(identity)


def _snapshot_intact(
    connection: sqlite3.Connection,
    repo: str,
    snapshot: str,
    artifact_identity: tuple[tuple[object, ...], ...] | None = None,
) -> bool:
    if artifact_identity is None:
        database_path = Path(str(connection.execute("PRAGMA database_list").fetchone()[2]))
        paths = (database_path, Path(str(database_path) + "-wal"))
        values: list[tuple[object, ...]] = []
        for path in paths:
            try:
                stat = path.stat()
                if path.name.endswith("-wal") and stat.st_size == 0:
                    values.append((str(path), "empty"))
                    continue
                values.append((str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
            except OSError:
                values.append((str(path), "empty" if path.name.endswith("-wal") else "missing"))
        artifact_identity = tuple(values)
    cache_key = (artifact_identity, repo, snapshot)
    if os.name != "nt" and cache_key in _SNAPSHOT_INTEGRITY_CACHE:
        return _SNAPSHOT_INTEGRITY_CACHE[cache_key]
    indexed = connection.execute(
        "SELECT file_count,membership_hash FROM indexed_snapshots WHERE repo=? AND snapshot_sha=?", (repo, snapshot),
    ).fetchone()
    if indexed is None:
        valid = False
        if os.name != "nt":
            if len(_SNAPSHOT_INTEGRITY_CACHE) >= 2_048:
                _SNAPSHOT_INTEGRITY_CACHE.clear()
            _SNAPSHOT_INTEGRITY_CACHE[cache_key] = valid
        return valid
    row_count = 0
    membership_digest = hashlib.sha256()
    path_digest = hashlib.sha256()
    for path, blob, content, size, fts_content in connection.execute(
        "SELECT m.path,m.blob,b.content,b.size,f.content FROM file_membership m "
        "LEFT JOIN blobs b ON b.blob=m.blob LEFT JOIN blob_fts f ON f.blob=m.blob "
        "WHERE m.repo=? AND m.snapshot_sha=? ORDER BY m.path",
        (repo, snapshot),
    ):
        row_count += 1
        if not _blob_identity_valid(blob, content, size) or content != fts_content:
            return False
        _update_membership_digest(membership_digest, repo, snapshot, path, blob)
        path_digest.update(f"{repo}\0{snapshot}\0{path}\n".encode("utf-8"))
    if (
        int(indexed[0]) != row_count
        or indexed[1] != "sha256:" + membership_digest.hexdigest()
    ):
        return False
    fts_path_digest = hashlib.sha256()
    path_count = 0
    for (path,) in connection.execute(
        "SELECT path FROM path_membership_fts "
        "WHERE repo=? AND snapshot_sha=? ORDER BY path",
        (repo, snapshot),
    ):
        path_count += 1
        fts_path_digest.update(f"{repo}\0{snapshot}\0{path}\n".encode("utf-8"))
    if path_count != row_count or fts_path_digest.digest() != path_digest.digest():
        return False
    valid = True
    if os.name != "nt":
        if len(_SNAPSHOT_INTEGRITY_CACHE) >= 2_048:
            _SNAPSHOT_INTEGRITY_CACHE.clear()
        _SNAPSHOT_INTEGRITY_CACHE[cache_key] = valid
    return valid


def build_index_generation(
    settings: Settings,
    *,
    changed_only: bool = False,
    suffixes: set[str],
    ignored_dirs: set[str],
) -> tuple[dict[str, object], list[str]]:
    """Atomically publish a blob-deduplicated SQLite search generation."""
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    connection = _connect(settings)
    updated: list[str] = []
    repaired: set[str] = set()
    state: dict[str, object] = {}
    walk_budget = _WalkBudget(
        _NONGIT_SNAPSHOT_MAX_ITEMS,
        time.monotonic() + _NONGIT_SNAPSHOT_MAX_SECONDS,
        _NONGIT_SNAPSHOT_MAX_DEPTH,
    )
    try:
        connection.execute("BEGIN IMMEDIATE")
        generation_row = connection.execute("SELECT value FROM metadata WHERE key='generation'").fetchone()
        generation = int(generation_row[0]) if generation_row else 0
        refresh_stats: dict[str, tuple[int, int]] = {}
        artifact_identity = _database_artifact_identity(settings)
        for repo in settings.repositories:
            sha = repo.source_sha
            snapshot = sha or "working-tree"
            previous = connection.execute(
                "SELECT sha FROM repositories WHERE name=?", (repo.name,)
            ).fetchone()
            same_sha = bool(sha) and bool(previous) and previous[0] == sha
            intact = _snapshot_intact(connection, repo.name, snapshot, artifact_identity) if same_sha else False
            unchanged = same_sha and intact
            if same_sha and not intact:
                repaired.add(repo.name)
            if changed_only and unchanged:
                continue

            manifest = _git_manifest(repo)
            records: list[tuple[str, str]] = []
            changed_blobs = 0
            changed_bytes = 0
            if manifest is not None:
                entries = [
                    (path, blob)
                    for path, (mode, blob) in manifest.items()
                    if mode.startswith("100") and _safe_path(path) and (Path(path).suffix.lower() in suffixes or Path(path).name in INDEXABLE_NAMES)
                ]
                missing = {
                    blob for _, blob in entries
                    if not _stored_blob_valid(connection, blob)
                }
                loaded_missing: set[str] = set()
                for blob, content in _git_blob_contents(repo, missing):
                    if b"\0" not in content[:8192]:
                        decoded = content.decode("utf-8", errors="replace")
                        connection.execute("INSERT OR REPLACE INTO blobs(blob, content, size) VALUES (?, ?, ?)", (blob, decoded, len(content)))
                        connection.execute("DELETE FROM blob_fts WHERE blob=?", (blob,))
                        connection.execute("INSERT INTO blob_fts(blob, content) VALUES (?, ?)", (blob, decoded))
                        loaded_missing.add(blob)
                        changed_blobs += 1
                        changed_bytes += len(content)
                records = [
                    (path, blob) for path, blob in entries
                    if blob not in missing or blob in loaded_missing
                ]
            else:
                for path in _walk_files(repo, suffixes, ignored_dirs, budget=walk_budget):
                    try:
                        content = _read_source_bytes(path)
                    except OSError as error:
                        raise RuntimeError(f"Could not read authoritative source {repo.name}:{path.name}") from error
                    if len(content) > 3_000_000 or b"\0" in content[:8192]:
                        continue
                    relative = logical_path(path.relative_to(repo.scan_path))
                    blob = _content_blob(content)
                    records.append((relative, blob))
                    if not _stored_blob_valid(connection, blob):
                        decoded = content.decode("utf-8", errors="replace")
                        connection.execute("INSERT OR REPLACE INTO blobs(blob, content, size) VALUES (?, ?, ?)", (blob, decoded, len(content)))
                        connection.execute("DELETE FROM blob_fts WHERE blob=?", (blob,))
                        connection.execute("INSERT INTO blob_fts(blob, content) VALUES (?, ?)", (blob, decoded))
                        changed_blobs += 1
                        changed_bytes += len(content)

            connection.execute("DELETE FROM file_membership WHERE repo=? AND snapshot_sha=?", (repo.name, snapshot))
            connection.execute("DELETE FROM path_membership_fts WHERE repo=? AND snapshot_sha=?", (repo.name, snapshot))
            # v1 tables remain a current-only compatibility projection.
            connection.execute("DELETE FROM files WHERE repo=?", (repo.name,))
            connection.execute("DELETE FROM path_fts WHERE repo=?", (repo.name,))
            for path, blob in records:
                connection.execute(
                    "INSERT INTO file_membership(repo, snapshot_sha, path, blob) VALUES (?, ?, ?, ?)",
                    (repo.name, snapshot, path, blob),
                )
                connection.execute(
                    "INSERT INTO path_membership_fts(repo, snapshot_sha, path) VALUES (?, ?, ?)",
                    (repo.name, snapshot, path),
                )
                connection.execute("INSERT INTO files(repo, path, blob) VALUES (?, ?, ?)", (repo.name, path, blob))
                connection.execute("INSERT INTO path_fts(repo, path) VALUES (?, ?)", (repo.name, path))

            indexed_at = datetime.now(UTC).isoformat()
            connection.execute(
                "INSERT OR REPLACE INTO repositories(name, sha, indexed_at, file_count) VALUES (?, ?, ?, ?)",
                (repo.name, sha, indexed_at, len(records)),
            )
            membership_hash = _membership_hash(repo.name, snapshot, sorted(records))
            connection.execute(
                "INSERT OR REPLACE INTO indexed_snapshots"
                "(repo, snapshot_sha, indexed_at, file_count, membership_hash) VALUES (?, ?, ?, ?, ?)",
                (repo.name, snapshot, indexed_at, len(records), membership_hash),
            )
            refresh_stats[repo.name] = (changed_blobs, changed_bytes)
            updated.append(repo.name)

        if updated:
            generation += 1
            connection.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES ('generation', ?)", (str(generation),))
        repair_epoch_row = connection.execute("SELECT value FROM metadata WHERE key='repair_epoch'").fetchone()
        repair_epoch = int(repair_epoch_row[0]) if repair_epoch_row else 0
        if repaired:
            repair_epoch += 1
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES ('repair_epoch', ?)", (str(repair_epoch),),
            )
        connection.execute("DELETE FROM blob_fts WHERE blob NOT IN (SELECT DISTINCT blob FROM file_membership)")
        connection.execute("DELETE FROM blobs WHERE blob NOT IN (SELECT DISTINCT blob FROM file_membership)")
        # The SQLite/FTS write amplification is backend-dependent, so validate
        # actual on-disk growth (including WAL) before making this generation
        # durable.  A quota failure rolls the transaction back below.
        from .ops import ensure_write_capacity

        ensure_write_capacity(settings)
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        final_artifact_identity = _database_artifact_identity(settings)
        if os.name != "nt":
            if len(_SNAPSHOT_INTEGRITY_CACHE) >= 2_048:
                _SNAPSHOT_INTEGRITY_CACHE.clear()
            for repo in settings.repositories:
                _SNAPSHOT_INTEGRITY_CACHE[(
                    final_artifact_identity, repo.name, repo.source_sha or "working-tree",
                )] = True

        configured_repositories = {repo.name for repo in settings.repositories}
        for name, sha, indexed_at, file_count in connection.execute(
            "SELECT name, sha, indexed_at, file_count FROM repositories ORDER BY name"
        ):
            # Retain old memberships for pinned generations/GC, but never leak
            # a repository removed from current configuration into new state.
            if name not in configured_repositories:
                continue
            state[name] = {
                "sha": sha,
                "indexed_at": indexed_at,
                "backend": "sqlite fts5 trigram",
                "generation": generation,
                "files": file_count,
                "changed_blobs": refresh_stats.get(name, (0, 0))[0],
                "bytes_indexed": refresh_stats.get(name, (0, 0))[1],
                "repair_epoch": repair_epoch,
                "repaired": name in repaired,
            }
        return state, updated
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _quoted(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _snapshot(repo: Repository, snapshot_sha: str | None) -> str:
    return snapshot_sha or repo.source_sha or "working-tree"


def _available(connection: sqlite3.Connection, repo: Repository, snapshot_sha: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM indexed_snapshots "
        "WHERE repo=? AND snapshot_sha=? AND membership_hash IS NOT NULL",
        (repo.name, snapshot_sha),
    ).fetchone()
    return bool(row)


def query_index(
    settings: Settings,
    repo: Repository,
    query: str,
    *,
    max_results: int,
    snapshot_sha: str | None = None,
) -> list[tuple[str, int, str]] | None:
    """Return exact, case-sensitive line matches, or None when fallback is required."""
    path = _database(settings)
    if not path.is_file():
        return None
    try:
        connection = _connect(settings)
        snapshot = _snapshot(repo, snapshot_sha)
        if not _available(connection, repo, snapshot):
            return None
        if len(query) >= 3:
            rows = connection.execute(
                """
                SELECT f.path, b.content, b.blob, b.size
                FROM blob_fts
                JOIN blobs b ON b.blob=blob_fts.blob
                JOIN file_membership f ON f.blob=b.blob
                WHERE blob_fts MATCH ? AND f.repo=? AND f.snapshot_sha=?
                ORDER BY f.path, b.blob
                LIMIT ?
                """,
                (_quoted(query), repo.name, snapshot, max(100, max_results * 20)),
            )
        else:
            rows = connection.execute(
                """
                SELECT f.path, b.content, b.blob, b.size
                FROM blobs b JOIN file_membership f ON f.blob=b.blob
                WHERE f.repo=? AND f.snapshot_sha=? AND instr(b.content, ?) > 0
                ORDER BY f.path, b.blob
                LIMIT ?
                """,
                (repo.name, snapshot, query, max(100, max_results * 20)),
            )
        hits: list[tuple[str, int, str]] = []
        for file_path, content, blob, size in rows:
            if not _blob_identity_valid(blob, content, size):
                return None
            for number, line in enumerate(content.splitlines(), 1):
                if query in line:
                    hits.append((file_path, number, line))
                    if len(hits) >= max_results:
                        return hits
        return hits
    except sqlite3.Error:
        return None
    finally:
        if "connection" in locals():
            connection.close()


def query_generation_indexes(
    settings: Settings,
    generation: object,
    repositories: Iterable[Repository],
    query: str,
    *,
    max_results: int,
    max_candidate_files: int,
    max_hits: int,
    max_bytes: int,
    max_seconds: float,
    stats: dict[str, object] | None = None,
) -> dict[str, list[tuple[str, int, str]]] | None:
    """Query one registered pinned lexical generation within explicit hard budgets."""
    if stats is not None:
        stats.clear()
        stats.update({
            "candidate_files": 0,
            "candidate_bytes": 0,
            "hits": 0,
            "budget_exhausted": False,
            "reason": None,
        })

    def exhausted(reason: str) -> None:
        if stats is not None:
            stats["budget_exhausted"] = True
            stats["reason"] = stats.get("reason") or reason

    selected = list(repositories)
    if not selected:
        return {}
    if (
        len(selected) > 100
        or max_results < 1
        or max_candidate_files < 1
        or max_hits < 1
        or max_bytes < 1
        or max_seconds <= 0
        or not _database(settings).is_file()
    ):
        return None
    deadline = time.monotonic() + max_seconds
    try:
        component = generation.component("lexical")  # type: ignore[attr-defined]
        snapshots = dict(generation.snapshots)  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError):
        return None
    details = component.get("details") if isinstance(component.get("details"), dict) else {}
    registered_snapshots = details.get("snapshots") if isinstance(details.get("snapshots"), dict) else {}
    repository_hashes = details.get("repository_hashes") if isinstance(details.get("repository_hashes"), dict) else {}
    repository_files = details.get("repository_files") if isinstance(details.get("repository_files"), dict) else {}
    if (
        component.get("status") != "ready"
        or str(component.get("schema_version") or "") != str(LEXICAL_COMPONENT_SCHEMA_VERSION)
        or registered_snapshots != snapshots
    ):
        return None
    names = [repo.name for repo in selected]
    if len(set(names)) != len(names) or any(name not in snapshots for name in names):
        return None

    pairs = [(name, str(snapshots[name])) for name in names]
    try:
        connection = _connect(settings)
        connection.execute("BEGIN")
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1_000)
        requested_values = ",".join("(?,?,?)" for _ in pairs)
        requested_parameters = [
            value for ordinal, (name, snapshot) in enumerate(pairs)
            for value in (ordinal, name, snapshot)
        ]
        indexed_rows = connection.execute(
            f"WITH requested(ordinal,repo,snapshot_sha) AS (VALUES {requested_values}) "
            "SELECT r.repo,r.snapshot_sha,i.membership_hash,i.file_count FROM requested r "
            "LEFT JOIN indexed_snapshots i ON i.repo=r.repo AND i.snapshot_sha=r.snapshot_sha "
            "ORDER BY r.ordinal",
            requested_parameters,
        ).fetchall()
        if len(indexed_rows) != len(pairs):
            return None
        for name, snapshot, membership_hash, file_count in indexed_rows:
            if (
                membership_hash is None
                or membership_hash != repository_hashes.get(name)
                or int(file_count) != int(repository_files.get(name, -1))
            ):
                return None

        # Fetch only bounded metadata first. Per-repository limits preserve
        # fair coverage without making SQLite sort/materialize source blobs.
        base, remainder = divmod(max_candidate_files, len(pairs))
        candidates_by_repo: list[list[tuple[int, str, str, str, int]]] = []
        for ordinal, (name, snapshot) in enumerate(pairs):
            if time.monotonic() >= deadline:
                exhausted("time")
                break
            repo_limit = base + int(ordinal < remainder)
            if repo_limit < 1:
                exhausted("candidate_files")
                candidates_by_repo.append([])
                continue
            try:
                if len(query) >= 3:
                    rows = connection.execute(
                        "SELECT f.path,b.blob,b.size FROM blob_fts "
                        "JOIN blobs b ON b.blob=blob_fts.blob "
                        "JOIN file_membership f ON f.blob=b.blob "
                        "WHERE blob_fts MATCH ? AND f.repo=? AND f.snapshot_sha=? "
                        "ORDER BY f.path,b.blob LIMIT ?",
                        (_quoted(query), name, snapshot, repo_limit + 1),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        "SELECT f.path,b.blob,b.size FROM blobs b "
                        "JOIN file_membership f ON f.blob=b.blob "
                        "WHERE f.repo=? AND f.snapshot_sha=? AND instr(b.content,?)>0 "
                        "ORDER BY f.path,b.blob LIMIT ?",
                        (name, snapshot, query, repo_limit + 1),
                    ).fetchall()
            except sqlite3.OperationalError as error:
                if "interrupted" in str(error).lower() and time.monotonic() >= deadline:
                    exhausted("time")
                    break
                raise
            if len(rows) > repo_limit:
                exhausted("candidate_files")
            repo_candidates: list[tuple[int, str, str, str, int]] = []
            for path, blob, size in rows[:repo_limit]:
                try:
                    declared_size = int(size)
                except (TypeError, ValueError, OverflowError):
                    return None
                if declared_size < 0 or declared_size > 3_000_000:
                    return None
                repo_candidates.append((ordinal, name, str(path), str(blob), declared_size))
            candidates_by_repo.append(repo_candidates)

        # Interleave routed repositories before applying the byte budget so a
        # busy early repository cannot consume every retained candidate.
        candidates = [
            rows[index]
            for index in range(max((len(rows) for rows in candidates_by_repo), default=0))
            for rows in candidates_by_repo
            if index < len(rows)
        ]

        retained: list[tuple[int, str, str, str, int]] = []
        retained_bytes = 0
        for candidate in candidates:
            if time.monotonic() >= deadline:
                exhausted("time")
                break
            if retained_bytes + candidate[4] > max_bytes:
                exhausted("bytes")
                continue
            retained.append(candidate)
            retained_bytes += candidate[4]
        if stats is not None:
            stats["candidate_files"] = len(retained)
            stats["candidate_bytes"] = retained_bytes

        hits: dict[str, list[tuple[str, int, str]]] = {name: [] for name in names}
        total_hits = 0
        # Keep VALUES below SQLite's conservative cross-platform parameter
        # ceiling while fetching content only for the retained prefix.
        for offset in range(0, len(retained), 150):
            if time.monotonic() >= deadline:
                exhausted("time")
                break
            batch = retained[offset:offset + 150]
            safe_values = ",".join("(?,?,?,?,?)" for _ in batch)
            safe_parameters = [value for row in batch for value in row]
            try:
                rows = connection.execute(
                    f"WITH safe(ordinal,repo,path,blob,size) AS (VALUES {safe_values}) "
                    "SELECT s.repo,s.path,b.content,s.blob,s.size FROM safe s "
                    "JOIN blobs b ON b.blob=s.blob ORDER BY s.ordinal",
                    safe_parameters,
                )
                for repo_name, file_path, content, blob, size in rows:
                    if time.monotonic() >= deadline:
                        exhausted("time")
                        break
                    if not _blob_identity_valid(blob, content, size):
                        return None
                    repo_hits = hits[str(repo_name)]
                    if len(repo_hits) >= max_results:
                        continue
                    for number, line in enumerate(content.splitlines(), 1):
                        if query in line:
                            repo_hits.append((str(file_path), number, line))
                            total_hits += 1
                            if total_hits >= max_hits:
                                exhausted("hits")
                                break
                            if len(repo_hits) >= max_results:
                                break
                    if total_hits >= max_hits:
                        break
            except sqlite3.OperationalError as error:
                if "interrupted" in str(error).lower() and time.monotonic() >= deadline:
                    exhausted("time")
                    break
                raise
            if time.monotonic() >= deadline:
                exhausted("time")
                break
            if total_hits >= max_hits:
                break
        if stats is not None:
            stats["hits"] = total_hits
        return hits
    except sqlite3.OperationalError as error:
        if "interrupted" in str(error).lower() and time.monotonic() >= deadline:
            exhausted("time")
            return {name: [] for name in names}
        return None
    except (TypeError, ValueError, sqlite3.Error):
        return None
    finally:
        if "connection" in locals():
            connection.close()


def query_paths(
    settings: Settings,
    repo: Repository,
    query: str,
    *,
    limit: int,
    snapshot_sha: str | None = None,
) -> list[str] | None:
    path = _database(settings)
    if not path.is_file():
        return None
    tokens = [token for token in query.lower().replace("\\", "/").split() if len(token) >= 3]
    try:
        connection = _connect(settings)
        snapshot = _snapshot(repo, snapshot_sha)
        if not _available(connection, repo, snapshot):
            return None
        if tokens:
            expression = " AND ".join(_quoted(token) for token in tokens)
            rows = connection.execute(
                "SELECT DISTINCT f.path FROM path_membership_fts p JOIN file_membership f "
                "ON f.repo=p.repo AND f.snapshot_sha=p.snapshot_sha AND f.path=p.path "
                "WHERE path_membership_fts MATCH ? AND p.repo=? AND p.snapshot_sha=? "
                "ORDER BY f.path LIMIT ?",
                (expression, repo.name, snapshot, max(100, limit * 20)),
            )
        else:
            query_value = query.lower().replace("\\", "/").strip()
            row_limit = max(1, min(2_000, max(100, limit * 20)))
            rows = connection.execute(
                "SELECT path FROM file_membership "
                "WHERE repo=? AND snapshot_sha=? AND instr(lower(path),?)>0 "
                "ORDER BY path LIMIT ?",
                (repo.name, snapshot, query_value, row_limit),
            )
        return [row[0] for row in rows]
    except sqlite3.Error:
        return None
    finally:
        if "connection" in locals():
            connection.close()


def query_generation_paths(
    settings: Settings,
    generation: object,
    repositories: Iterable[Repository],
    query: str,
    *,
    limit: int,
    max_candidate_paths: int,
    max_seconds: float,
    stats: dict[str, object] | None = None,
) -> dict[str, list[str]] | None:
    """Query generation-scoped paths within explicit item and time budgets."""
    if stats is not None:
        stats.clear()
        stats.update({"candidate_paths": 0, "budget_exhausted": False, "reason": None})

    def exhausted(reason: str) -> None:
        if stats is not None:
            stats["budget_exhausted"] = True
            stats["reason"] = stats.get("reason") or reason

    selected = list(repositories)
    if not selected:
        return {}
    if (
        len(selected) > 100 or limit < 1 or max_candidate_paths < 1 or max_seconds <= 0
        or not _database(settings).is_file()
    ):
        return None
    deadline = time.monotonic() + max_seconds
    try:
        component = generation.component("lexical")  # type: ignore[attr-defined]
        snapshots = dict(generation.snapshots)  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError):
        return None
    details = component.get("details") if isinstance(component.get("details"), dict) else {}
    registered_snapshots = details.get("snapshots") if isinstance(details.get("snapshots"), dict) else {}
    repository_hashes = details.get("repository_hashes") if isinstance(details.get("repository_hashes"), dict) else {}
    repository_files = details.get("repository_files") if isinstance(details.get("repository_files"), dict) else {}
    names = [repo.name for repo in selected]
    if (
        component.get("status") != "ready"
        or str(component.get("schema_version") or "") != str(LEXICAL_COMPONENT_SCHEMA_VERSION)
        or registered_snapshots != snapshots
        or len(set(names)) != len(names)
        or any(name not in snapshots for name in names)
    ):
        return None
    pairs = [(name, str(snapshots[name])) for name in names]
    tokens = [token for token in query.lower().replace("\\", "/").split() if len(token) >= 3]
    try:
        connection = _connect(settings)
        connection.execute("BEGIN")
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1_000)
        requested_values = ",".join("(?,?,?)" for _ in pairs)
        requested_parameters = [
            value for ordinal, (name, snapshot) in enumerate(pairs)
            for value in (ordinal, name, snapshot)
        ]
        indexed_rows = connection.execute(
            f"WITH requested(ordinal,repo,snapshot_sha) AS (VALUES {requested_values}) "
            "SELECT r.repo,r.snapshot_sha,i.membership_hash,i.file_count FROM requested r "
            "LEFT JOIN indexed_snapshots i ON i.repo=r.repo AND i.snapshot_sha=r.snapshot_sha "
            "ORDER BY r.ordinal",
            requested_parameters,
        ).fetchall()
        if len(indexed_rows) != len(pairs):
            return None
        for name, snapshot, membership_hash, file_count in indexed_rows:
            if (
                membership_hash is None
                or membership_hash != repository_hashes.get(name)
                or int(file_count) != int(repository_files.get(name, -1))
            ):
                return None
        matches: dict[str, list[str]] = {name: [] for name in names}
        base, remainder = divmod(max_candidate_paths, len(pairs))
        query_value = query.lower().replace("\\", "/").strip()
        for ordinal, (name, snapshot) in enumerate(pairs):
            if time.monotonic() >= deadline:
                exhausted("time")
                break
            repo_limit = base + int(ordinal < remainder)
            if repo_limit < 1:
                exhausted("candidate_paths")
                continue
            try:
                # Reserve bounded exact basename/stem/full-path candidates
                # before the deterministic weak contains scan. This prevents
                # alphabetically early weak paths from hiding z/target.java.
                exact_rows = connection.execute(
                    "SELECT path FROM file_membership WHERE repo=? AND snapshot_sha=? AND ("
                    "lower(path)=? OR substr(lower(path),-length(?)-1)='/'||? OR "
                    "instr(lower(path),?||'.')=1 OR instr(lower(path),'/'||?||'.')>0) "
                    "ORDER BY path LIMIT ?",
                    (
                        name, snapshot, query_value, query_value, query_value,
                        query_value, query_value, repo_limit + 1,
                    ),
                ).fetchall()
                if len(exact_rows) > repo_limit:
                    exhausted("candidate_paths")
                selected_paths = [str(row[0]) for row in exact_rows[:repo_limit]]
                remaining = repo_limit - len(selected_paths)
                weak_rows: list[tuple[object, ...]] = []
                if remaining:
                    terms = tokens or [query_value]
                    conditions = " AND ".join("instr(lower(path),?)>0" for _ in terms)
                    weak_rows = connection.execute(
                        "SELECT path FROM file_membership WHERE repo=? AND snapshot_sha=? AND "
                        f"{conditions} ORDER BY path LIMIT ?",
                        (name, snapshot, *terms, remaining + len(selected_paths) + 1),
                    ).fetchall()
                    for row in weak_rows:
                        path = str(row[0])
                        if path not in selected_paths:
                            selected_paths.append(path)
                            if len(selected_paths) >= repo_limit:
                                break
            except sqlite3.OperationalError as error:
                if "interrupted" in str(error).lower() and time.monotonic() >= deadline:
                    exhausted("time")
                    break
                raise
            if len(weak_rows) > remaining + len(exact_rows[:repo_limit]):
                exhausted("candidate_paths")
            matches[name] = selected_paths
        if stats is not None:
            stats["candidate_paths"] = sum(len(rows) for rows in matches.values())
        return matches
    except sqlite3.OperationalError as error:
        if "interrupted" in str(error).lower() and time.monotonic() >= deadline:
            exhausted("time")
            return {name: [] for name in names}
        return None
    except (TypeError, ValueError, sqlite3.Error):
        return None
    finally:
        if "connection" in locals():
            connection.close()


def read_indexed_file(
    settings: Settings,
    repo: Repository,
    file_path: str,
    *,
    snapshot_sha: str | None = None,
) -> str | None:
    path = _database(settings)
    if not path.is_file():
        return None
    try:
        connection = _connect(settings)
        snapshot = _snapshot(repo, snapshot_sha)
        if not _available(connection, repo, snapshot):
            return None
        row = connection.execute(
            "SELECT b.content,f.blob,b.size FROM file_membership f JOIN blobs b ON b.blob=f.blob "
            "WHERE f.repo=? AND f.snapshot_sha=? AND f.path=?",
            (repo.name, snapshot, file_path.replace(os.sep, "/")),
        ).fetchone()
        if not row:
            return None
        content = str(row[0])
        return content if _blob_identity_valid(row[1], content, row[2]) else None
    except sqlite3.Error:
        return None
    finally:
        if "connection" in locals():
            connection.close()


def read_generation_files(
    settings: Settings,
    generation: object,
    files: Iterable[tuple[str, str]],
    *,
    max_bytes: int,
    max_seconds: float,
) -> dict[tuple[str, str], str] | None:
    """Read a bounded set of files from one registered lexical generation."""
    requested: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for repo, path in files:
        key = str(repo), str(path).replace(os.sep, "/")
        if key not in seen:
            seen.add(key)
            requested.append(key)
    if not requested:
        return {}
    if (
        len(requested) > 256 or max_bytes < 1 or max_seconds <= 0
        or not _database(settings).is_file()
    ):
        return None
    deadline = time.monotonic() + max_seconds
    try:
        component = generation.component("lexical")  # type: ignore[attr-defined]
        snapshots = dict(generation.snapshots)  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError):
        return None
    details = component.get("details") if isinstance(component.get("details"), dict) else {}
    registered_snapshots = details.get("snapshots") if isinstance(details.get("snapshots"), dict) else {}
    repository_hashes = details.get("repository_hashes") if isinstance(details.get("repository_hashes"), dict) else {}
    repository_files = details.get("repository_files") if isinstance(details.get("repository_files"), dict) else {}
    repositories = list(dict.fromkeys(repo for repo, _ in requested))
    if (
        component.get("status") != "ready"
        or str(component.get("schema_version") or "") != str(LEXICAL_COMPONENT_SCHEMA_VERSION)
        or registered_snapshots != snapshots
        or any(repo not in snapshots for repo in repositories)
    ):
        return None
    try:
        connection = _connect(settings)
        connection.execute("BEGIN")
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1_000)
        for repo in repositories:
            if time.monotonic() >= deadline:
                return {}
            row = connection.execute(
                "SELECT membership_hash,file_count FROM indexed_snapshots WHERE repo=? AND snapshot_sha=?",
                (repo, snapshots[repo]),
            ).fetchone()
            if (
                row is None
                or row[0] != repository_hashes.get(repo)
                or int(row[1]) != int(repository_files.get(repo, -1))
            ):
                return None
        safe: list[tuple[int, str, str, str, int]] = []
        retained_bytes = 0
        for offset in range(0, len(requested), 200):
            if time.monotonic() >= deadline or retained_bytes >= max_bytes:
                break
            batch = requested[offset:offset + 200]
            requested_values = ",".join("(?,?,?,?)" for _ in batch)
            requested_parameters = [
                value
                for relative, (repo, path) in enumerate(batch)
                for value in (offset + relative, repo, snapshots[repo], path)
            ]
            metadata_rows = connection.execute(
                f"WITH requested(ordinal,repo,snapshot_sha,path) AS (VALUES {requested_values}) "
                "SELECT r.ordinal,f.repo,f.path,f.blob,b.size FROM requested r "
                "JOIN file_membership f "
                "ON f.repo=r.repo AND f.snapshot_sha=r.snapshot_sha AND f.path=r.path "
                "JOIN blobs b ON b.blob=f.blob ORDER BY r.ordinal",
                requested_parameters,
            )
            byte_limit_reached = False
            for ordinal, repo, path, blob, size in metadata_rows:
                if time.monotonic() >= deadline:
                    byte_limit_reached = True
                    break
                try:
                    declared_size = int(size)
                except (TypeError, ValueError, OverflowError):
                    return None
                if declared_size < 0 or declared_size > 3_000_000:
                    return None
                if retained_bytes + declared_size > max_bytes:
                    byte_limit_reached = True
                    break
                safe.append((int(ordinal), str(repo), str(path), str(blob), declared_size))
                retained_bytes += declared_size
            if byte_limit_reached:
                break
        if not safe:
            return {}
        result: dict[tuple[str, str], str] = {}
        verified_bytes = 0
        for offset in range(0, len(safe), 150):
            if time.monotonic() >= deadline:
                break
            batch = safe[offset:offset + 150]
            safe_values = ",".join("(?,?,?,?,?)" for _ in batch)
            safe_parameters = [value for row in batch for value in row]
            rows = connection.execute(
                f"WITH safe(ordinal,repo,path,blob,size) AS (VALUES {safe_values}) "
                "SELECT s.repo,s.path,b.content,s.blob,s.size FROM safe s "
                "JOIN blobs b ON b.blob=s.blob ORDER BY s.ordinal",
                safe_parameters,
            )
            for repo, path, content, blob, size in rows:
                if time.monotonic() >= deadline:
                    break
                source = str(content)
                if not _blob_identity_valid(blob, source, size):
                    return None
                source_bytes = len(source.encode("utf-8"))
                if verified_bytes + source_bytes > max_bytes:
                    return None
                result[(str(repo), str(path))] = source
                verified_bytes += source_bytes
        return result
    except (TypeError, ValueError, sqlite3.Error):
        return None
    finally:
        if "connection" in locals():
            connection.close()


def indexed_snapshot_contents(
    settings: Settings,
    files: dict[tuple[str, str], str],
    snapshots: dict[str, str],
) -> Iterator[tuple[tuple[str, str], str]]:
    """Stream validated lexical blobs so refresh never retains all changed source text."""
    if not files:
        return
    connection = _connect(settings)
    try:
        by_repo: dict[str, list[str]] = {}
        for repo, path in files:
            by_repo.setdefault(repo, []).append(path)
        for repo, paths in sorted(by_repo.items()):
            snapshot = snapshots.get(repo)
            if not snapshot:
                raise sqlite3.DatabaseError(f"authoritative lexical snapshot is missing for {repo}")
            ordered_paths = sorted(paths)
            for offset in range(0, len(ordered_paths), 400):
                batch = ordered_paths[offset:offset + 400]
                placeholders = ",".join("?" for _ in batch)
                found = 0
                for path, blob, content, size in connection.execute(
                    "SELECT m.path,m.blob,b.content,b.size FROM file_membership m "
                    "JOIN blobs b ON b.blob=m.blob WHERE m.repo=? AND m.snapshot_sha=? "
                    f"AND m.path IN ({placeholders}) ORDER BY m.path",
                    (repo, snapshot, *batch),
                ):
                    key = (repo, str(path))
                    if str(blob) != files.get(key) or not _blob_identity_valid(blob, content, size):
                        raise sqlite3.DatabaseError(f"authoritative lexical blob is invalid for {repo}:{path}")
                    found += 1
                    yield key, str(content)
                if found != len(batch):
                    raise sqlite3.DatabaseError("authoritative lexical file membership is incomplete")
    finally:
        connection.close()


def indexed_snapshot_documents(
    settings: Settings,
    snapshots: dict[str, str],
    suffixes: set[str],
    *,
    max_repositories: int,
    max_items: int,
    max_bytes: int,
    max_file_bytes: int,
    max_seconds: float,
) -> list[tuple[str, str, str]]:
    """Load one bounded, validated projection for refresh-time analyzers.

    Membership validation remains authoritative while suffix filtering happens
    in SQLite, so callers neither copy the complete manifest nor issue one
    content query per analyzer.
    """
    if len(snapshots) > max_repositories:
        raise sqlite3.DataError("snapshot document repository budget exceeded")
    normalized = sorted({suffix.lower() for suffix in suffixes if suffix.startswith(".")})
    if not normalized:
        return []
    deadline = time.monotonic() + max_seconds
    connection = _connect(settings)
    documents: list[tuple[str, str, str]] = []
    total_bytes = 0

    def progress() -> int:
        return int(time.monotonic() >= deadline)

    connection.set_progress_handler(progress, 10_000)
    try:
        clauses = " OR ".join("lower(m.path) LIKE ?" for _ in normalized)
        patterns = tuple(f"%{suffix}" for suffix in normalized)
        for repo, snapshot in sorted(snapshots.items()):
            if time.monotonic() >= deadline:
                raise sqlite3.DataError("snapshot document time budget exceeded")
            indexed = connection.execute(
                "SELECT file_count FROM indexed_snapshots WHERE repo=? AND snapshot_sha=?",
                (repo, snapshot),
            ).fetchone()
            actual = connection.execute(
                "SELECT COUNT(*) FROM file_membership WHERE repo=? AND snapshot_sha=?",
                (repo, snapshot),
            ).fetchone()
            if indexed is None or actual is None or int(indexed[0]) != int(actual[0]):
                raise sqlite3.DatabaseError(f"authoritative lexical membership is unavailable for {repo}")
            projection = connection.execute(
                "SELECT COUNT(*),COALESCE(SUM(b.size),0),COALESCE(MAX(b.size),0) "
                "FROM file_membership m JOIN blobs b ON b.blob=m.blob "
                f"WHERE m.repo=? AND m.snapshot_sha=? AND ({clauses})",
                (repo, snapshot, *patterns),
            ).fetchone()
            projected_items = int(projection[0] if projection else 0)
            projected_bytes = int(projection[1] if projection else 0)
            projected_file_bytes = int(projection[2] if projection else 0)
            if (
                len(documents) + projected_items > max_items
                or total_bytes + projected_bytes > max_bytes
                or projected_file_bytes > max_file_bytes
            ):
                raise sqlite3.DataError("snapshot document source budget exceeded")
            for path, blob, content, size in connection.execute(
                "SELECT m.path,m.blob,b.content,b.size FROM file_membership m "
                "JOIN blobs b ON b.blob=m.blob "
                f"WHERE m.repo=? AND m.snapshot_sha=? AND ({clauses}) ORDER BY m.path",
                (repo, snapshot, *patterns),
            ):
                if time.monotonic() >= deadline:
                    raise sqlite3.DataError("snapshot document time budget exceeded")
                if not _blob_identity_valid(blob, content, size):
                    raise sqlite3.DatabaseError(f"authoritative lexical blob is invalid for {repo}:{path}")
                documents.append((repo, str(path), str(content)))
                total_bytes += int(size)
        return documents
    except sqlite3.OperationalError as error:
        if "interrupted" in str(error).lower():
            raise sqlite3.DataError("snapshot document time budget exceeded") from error
        raise
    finally:
        connection.set_progress_handler(None, 0)
        connection.close()


def indexed_snapshot_file_manifest(
    settings: Settings,
    snapshots: dict[str, str],
) -> dict[tuple[str, str], str]:
    """Return the complete path-to-blob manifest for validated snapshot memberships."""
    connection = _connect(settings)
    try:
        files: dict[tuple[str, str], str] = {}
        for repo, snapshot in sorted(snapshots.items()):
            indexed = connection.execute(
                "SELECT file_count FROM indexed_snapshots WHERE repo=? AND snapshot_sha=?",
                (repo, snapshot),
            ).fetchone()
            rows = list(connection.execute(
                "SELECT path,blob FROM file_membership WHERE repo=? AND snapshot_sha=? ORDER BY path",
                (repo, snapshot),
            ))
            if indexed is None or int(indexed[0]) != len(rows):
                raise sqlite3.DatabaseError(f"authoritative lexical membership is unavailable for {repo}")
            for path, blob in rows:
                key = (repo, str(path))
                if key in files:
                    raise sqlite3.DatabaseError(f"duplicate authoritative lexical membership for {repo}:{path}")
                files[key] = str(blob)
        return files
    finally:
        connection.close()


def indexed_snapshot_source_projection(
    settings: Settings,
    snapshots: dict[str, str],
    *,
    max_repositories: int,
    max_items_per_repository: int,
    max_items: int,
    max_bytes_per_repository: int,
    max_bytes: int,
    max_file_bytes: int,
    max_seconds: float,
) -> dict[str, tuple[int, int, str]]:
    """Validate and size Semantic source without materializing membership rows."""
    if len(snapshots) > max_repositories:
        raise sqlite3.DataError("Semantic source repository budget exceeded")
    deadline = time.monotonic() + max_seconds
    connection = _connect(settings)
    projection: dict[str, tuple[int, int, str]] = {}
    total_items = 0
    total_bytes = 0

    def progress() -> int:
        return int(time.monotonic() >= deadline)

    connection.set_progress_handler(progress, 10_000)
    try:
        for repo, snapshot in sorted(snapshots.items()):
            if time.monotonic() >= deadline:
                raise sqlite3.DataError("Semantic source time budget exceeded")
            indexed = connection.execute(
                "SELECT file_count,membership_hash FROM indexed_snapshots "
                "WHERE repo=? AND snapshot_sha=?",
                (repo, snapshot),
            ).fetchone()
            row = connection.execute(
                "SELECT COUNT(*),COALESCE(SUM(b.size),0),COALESCE(MAX(b.size),0) "
                "FROM file_membership m JOIN blobs b ON b.blob=m.blob "
                "WHERE m.repo=? AND m.snapshot_sha=?",
                (repo, snapshot),
            ).fetchone()
            items = int(row[0] if row else 0)
            source_bytes = int(row[1] if row else 0)
            largest = int(row[2] if row else 0)
            if indexed is None or indexed[1] is None or int(indexed[0]) != items:
                raise sqlite3.DatabaseError(
                    f"authoritative lexical membership is unavailable for {repo}"
                )
            if (
                items > max_items_per_repository
                or total_items + items > max_items
                or source_bytes > max_bytes_per_repository
                or total_bytes + source_bytes > max_bytes
                or largest > max_file_bytes
            ):
                raise sqlite3.DataError("Semantic source budget exceeded")
            projection[repo] = (items, source_bytes, str(indexed[1]))
            total_items += items
            total_bytes += source_bytes
        return projection
    except sqlite3.OperationalError as error:
        if "interrupted" in str(error).lower():
            raise sqlite3.DataError("Semantic source time budget exceeded") from error
        raise
    finally:
        connection.set_progress_handler(None, 0)
        connection.close()


def indexed_snapshot_source_contents(
    settings: Settings,
    repo: str,
    snapshot: str,
    expected: tuple[int, int, str],
    *,
    max_seconds: float,
) -> Iterator[tuple[str, str, str]]:
    """Stream one preflight-bounded Semantic source projection from SQLite."""
    deadline = time.monotonic() + max_seconds
    connection = _connect(settings)
    items = 0
    source_bytes = 0

    def progress() -> int:
        return int(time.monotonic() >= deadline)

    connection.set_progress_handler(progress, 10_000)
    try:
        connection.execute("BEGIN")
        sealed = connection.execute(
            "SELECT file_count,membership_hash FROM indexed_snapshots "
            "WHERE repo=? AND snapshot_sha=?",
            (repo, snapshot),
        ).fetchone()
        if (
            sealed is None
            or int(sealed[0]) != expected[0]
            or str(sealed[1]) != expected[2]
        ):
            raise sqlite3.DatabaseError(
                f"authoritative lexical membership changed before Semantic build for {repo}"
            )
        membership_digest = hashlib.sha256()
        for path, blob, content, size in connection.execute(
            "SELECT m.path,m.blob,b.content,b.size FROM file_membership m "
            "JOIN blobs b ON b.blob=m.blob "
            "WHERE m.repo=? AND m.snapshot_sha=? ORDER BY m.path",
            (repo, snapshot),
        ):
            if time.monotonic() >= deadline:
                raise sqlite3.DataError("Semantic source time budget exceeded")
            if not _blob_identity_valid(blob, content, size):
                raise sqlite3.DatabaseError(
                    f"authoritative lexical blob is invalid for {repo}:{path}"
                )
            items += 1
            source_bytes += int(size)
            membership_digest.update(
                f"{repo}\0{snapshot}\0{path}\0{blob}\n".encode("utf-8")
            )
            if items > expected[0] or source_bytes > expected[1]:
                raise sqlite3.DatabaseError(
                    f"authoritative lexical membership changed during Semantic build for {repo}"
                )
            yield str(path), str(blob), str(content)
        identity = "sha256:" + membership_digest.hexdigest()
        current = connection.execute(
            "SELECT file_count,membership_hash FROM indexed_snapshots "
            "WHERE repo=? AND snapshot_sha=?",
            (repo, snapshot),
        ).fetchone()
        if (
            (items, source_bytes) != expected[:2]
            or identity != expected[2]
            or current is None
            or int(current[0]) != items
            or str(current[1]) != identity
        ):
            raise sqlite3.DatabaseError(
                f"authoritative lexical membership changed during Semantic build for {repo}"
            )
    except sqlite3.OperationalError as error:
        if "interrupted" in str(error).lower():
            raise sqlite3.DataError("Semantic source time budget exceeded") from error
        raise
    finally:
        connection.set_progress_handler(None, 0)
        connection.close()


def lexical_component(settings: Settings, state: dict[str, object]) -> dict[str, object]:
    """Describe and verify the exact multi-snapshot membership being published."""
    snapshots = {
        repo: str(raw.get("sha") or "working-tree")
        for repo, raw in state.items() if isinstance(raw, dict)
    }
    projection = lexical_membership_projection(settings, snapshots)
    if projection is None:
        return {
            "schema_version": str(LEXICAL_COMPONENT_SCHEMA_VERSION),
            "status": "unavailable",
            "details": {"reason": "snapshot membership validation failed"},
        }
    content_hash, file_count, repository_hashes, repository_files = projection
    return {
        "schema_version": str(LEXICAL_COMPONENT_SCHEMA_VERSION),
        "status": "ready",
        "content_hash": content_hash,
        "details": {
            "snapshots": snapshots,
            "files": file_count,
            "repository_hashes": repository_hashes,
            "repository_files": repository_files,
            "repair_epoch": max(
                (int(raw.get("repair_epoch") or 0) for raw in state.values() if isinstance(raw, dict)),
                default=0,
            ),
        },
    }


def lexical_membership_identity(
    settings: Settings, snapshots: dict[str, str],
) -> tuple[str, int] | None:
    """Recompute the immutable path-to-blob identity used by a pinned generation."""
    projection = lexical_membership_projection(settings, snapshots)
    return (projection[0], projection[1]) if projection is not None else None


def lexical_membership_projection(
    settings: Settings, snapshots: dict[str, str],
) -> tuple[str, int, dict[str, str], dict[str, int]] | None:
    """Fully validate refresh-time membership and derive per-repository serving proofs."""
    connection = _connect(settings)
    try:
        digest = hashlib.sha256()
        file_count = 0
        repository_hashes: dict[str, str] = {}
        repository_files: dict[str, int] = {}
        for repo, snapshot in sorted(snapshots.items()):
            indexed = connection.execute(
                "SELECT file_count,membership_hash FROM indexed_snapshots WHERE repo=? AND snapshot_sha=?", (repo, snapshot)
            ).fetchone()
            if not _snapshot_intact(connection, repo, snapshot):
                return None
            rows = connection.execute(
                "SELECT path,blob FROM file_membership WHERE repo=? AND snapshot_sha=? ORDER BY path",
                (repo, snapshot),
            )
            if indexed is None:
                return None
            repo_digest = hashlib.sha256()
            repo_count = 0
            for path, blob in rows:
                repo_count += 1
                row = f"{repo}\0{snapshot}\0{path}\0{blob}\n".encode("utf-8")
                digest.update(row)
                repo_digest.update(row)
            if int(indexed[0]) != repo_count:
                return None
            file_count += repo_count
            repository_hash = "sha256:" + repo_digest.hexdigest()
            if indexed[1] != repository_hash:
                return None
            repository_hashes[repo] = repository_hash
            repository_files[repo] = repo_count
        return "sha256:" + digest.hexdigest(), file_count, repository_hashes, repository_files
    except sqlite3.Error:
        return None
    finally:
        connection.close()


def lexical_repository_identity(
    settings: Settings, repo: str, snapshot: str,
) -> tuple[str, int] | None:
    """Return the refresh-sealed O(1) serving proof for one selected snapshot."""
    connection = _connect(settings)
    try:
        indexed = connection.execute(
            "SELECT membership_hash,file_count FROM indexed_snapshots WHERE repo=? AND snapshot_sha=?", (repo, snapshot),
        ).fetchone()
        if (
            indexed is None
            or not isinstance(indexed[0], str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", indexed[0])
        ):
            return None
        return indexed[0], int(indexed[1])
    except sqlite3.Error:
        return None
    finally:
        connection.close()


def lexical_generation_ready(settings: Settings, generation: object | None) -> bool:
    """Validate the registered lexical serving seal without mutating status state."""
    if generation is None:
        return False
    try:
        component = generation.component("lexical")  # type: ignore[attr-defined]
        snapshots = dict(generation.snapshots)  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError):
        return False
    details = component.get("details") if isinstance(component.get("details"), dict) else {}
    registered_snapshots = details.get("snapshots") if isinstance(details.get("snapshots"), dict) else {}
    repository_hashes = details.get("repository_hashes") if isinstance(details.get("repository_hashes"), dict) else {}
    repository_files = details.get("repository_files") if isinstance(details.get("repository_files"), dict) else {}
    if (
        component.get("status") != "ready"
        or str(component.get("schema_version") or "") != str(LEXICAL_COMPONENT_SCHEMA_VERSION)
        or registered_snapshots != snapshots
        or set(repository_hashes) != set(snapshots)
        or set(repository_files) != set(snapshots)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(component.get("content_hash") or ""))
    ):
        return False
    path = settings.state_dir / "search.sqlite3"
    try:
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            return False
        connection = sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True)
    except (OSError, sqlite3.Error, ValueError):
        return False
    try:
        if _preflight_schema_version(connection) != SCHEMA_VERSION:
            return False
        total = 0
        for repo, snapshot in sorted(snapshots.items()):
            row = connection.execute(
                "SELECT membership_hash,file_count FROM indexed_snapshots WHERE repo=? AND snapshot_sha=?",
                (repo, snapshot),
            ).fetchone()
            if row is None:
                return False
            count = int(row[1])
            if str(row[0]) != str(repository_hashes.get(repo)) or count != int(repository_files.get(repo, -1)):
                return False
            total += count
        if total != int(details.get("files") or 0):
            return False
        after = path.lstat()
        return (
            not path.is_symlink()
            and stat.S_ISREG(after.st_mode)
            and (after.st_dev, after.st_ino) == (metadata.st_dev, metadata.st_ino)
        )
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return False
    finally:
        connection.close()


def prune_memberships(settings: Settings, retain: set[tuple[str, str]]) -> list[tuple[str, str]]:
    """Remove only snapshot memberships unreachable from current, retained, or legacy pins."""
    connection = _connect(settings)
    try:
        existing = {
            (str(repo), str(snapshot))
            for repo, snapshot in connection.execute("SELECT repo, snapshot_sha FROM indexed_snapshots")
        }
        removable = sorted(existing - retain)
        connection.execute("BEGIN IMMEDIATE")
        for repo, snapshot in removable:
            connection.execute("DELETE FROM file_membership WHERE repo=? AND snapshot_sha=?", (repo, snapshot))
            connection.execute("DELETE FROM path_membership_fts WHERE repo=? AND snapshot_sha=?", (repo, snapshot))
            connection.execute("DELETE FROM indexed_snapshots WHERE repo=? AND snapshot_sha=?", (repo, snapshot))
        connection.execute("DELETE FROM blob_fts WHERE blob NOT IN (SELECT DISTINCT blob FROM file_membership)")
        connection.execute("DELETE FROM blobs WHERE blob NOT IN (SELECT DISTINCT blob FROM file_membership)")
        connection.commit()
        return removable
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def membership_snapshots(
    settings: Settings,
    *,
    consume: Callable[[int], None] | None = None,
) -> set[tuple[str, str]]:
    connection = _connect(settings)
    try:
        snapshots: set[tuple[str, str]] = set()
        for repo, snapshot in connection.execute(
            "SELECT repo, snapshot_sha FROM indexed_snapshots"
        ):
            if consume is not None:
                consume(1)
            snapshots.add((str(repo), str(snapshot)))
        return snapshots
    finally:
        connection.close()


def write_state(settings: Settings, state: dict[str, object]) -> None:
    target = settings.state_dir / "indexes.json"
    atomic_managed_text_write(settings.state_dir, target, json.dumps(state, indent=2) + "\n")

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from .core import Repository, Settings


SCHEMA_VERSION = 1
INDEXABLE_NAMES = {
    "Dockerfile", "Jenkinsfile", "Makefile", "Procfile", "build.gradle", "gradlew", "mvnw", "pom.xml",
}
SENSITIVE_FILE_NAMES = {".env", ".envrc", "credentials", "credentials.json", "service-account.json", "id_rsa", "id_ed25519", "keystore"}
SENSITIVE_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".jks"}


def _safe_path(path: str | Path) -> bool:
    value = Path(path)
    return value.name.lower() not in SENSITIVE_FILE_NAMES and value.suffix.lower() not in SENSITIVE_SUFFIXES


def _database(settings: Settings) -> Path:
    return settings.state_dir / "search.sqlite3"


def _connect(settings: Settings) -> sqlite3.Connection:
    connection = sqlite3.connect(_database(settings), timeout=30)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.executescript(
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
        """
    )
    version = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
    if version and int(version[0]) != SCHEMA_VERSION:
        raise sqlite3.DatabaseError(f"unsupported search index schema {version[0]}")
    connection.execute(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    connection.commit()
    return connection


def _walk_files(repo: Repository, suffixes: set[str], ignored_dirs: set[str]) -> Iterable[Path]:
    root = repo.scan_path
    if not root.is_dir():
        return
    for directory, dirs, names in os.walk(root):
        dirs[:] = [name for name in dirs if name not in ignored_dirs]
        base = Path(directory)
        for name in names:
            path = base / name
            if _safe_path(path) and (path.suffix.lower() in suffixes or name in INDEXABLE_NAMES):
                yield path


def _git_manifest(repo: Repository) -> dict[str, tuple[str, str]]:
    if not repo.source_sha or not (repo.path / ".git").exists():
        return {}
    try:
        result = subprocess.run(
            ["git", "ls-tree", "-r", "-z", repo.source_sha],
            cwd=repo.path,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return {}
    if result.returncode:
        return {}
    blobs: dict[str, tuple[str, str]] = {}
    for record in result.stdout.split(b"\0"):
        if not record or b"\t" not in record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        fields = metadata.split()
        if len(fields) == 3 and fields[1] == b"blob":
            blobs[raw_path.decode("utf-8", errors="surrogateescape")] = (
                fields[0].decode("ascii"), fields[2].decode("ascii")
            )
    return blobs


def _git_blob_contents(repo: Repository, blobs: set[str]) -> dict[str, bytes]:
    """Read changed Git objects in two batch processes, never one process per file."""
    if not blobs:
        return {}
    object_input = ("\n".join(sorted(blobs)) + "\n").encode("ascii")
    try:
        checked = subprocess.run(
            ["git", "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
            cwd=repo.path,
            input=object_input,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return {}
    eligible: list[str] = []
    if checked.returncode == 0:
        for line in checked.stdout.splitlines():
            fields = line.split()
            if len(fields) == 3 and fields[1] == b"blob" and int(fields[2]) <= 3_000_000:
                eligible.append(fields[0].decode("ascii"))
    if not eligible:
        return {}
    try:
        loaded = subprocess.run(
            ["git", "cat-file", "--batch"],
            cwd=repo.path,
            input=("\n".join(eligible) + "\n").encode("ascii"),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return {}
    if loaded.returncode:
        return {}
    contents: dict[str, bytes] = {}
    position = 0
    while position < len(loaded.stdout):
        header_end = loaded.stdout.find(b"\n", position)
        if header_end < 0:
            break
        fields = loaded.stdout[position:header_end].split()
        if len(fields) != 3 or fields[1] != b"blob":
            break
        size = int(fields[2])
        start, end = header_end + 1, header_end + 1 + size
        contents[fields[0].decode("ascii")] = loaded.stdout[start:end]
        position = end + 1
    return contents


def _content_blob(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


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
    state: dict[str, object] = {}
    try:
        connection.execute("BEGIN IMMEDIATE")
        generation_row = connection.execute("SELECT value FROM metadata WHERE key='generation'").fetchone()
        generation = int(generation_row[0]) if generation_row else 0
        refresh_stats: dict[str, tuple[int, int]] = {}
        for repo in settings.repositories:
            sha = repo.source_sha
            previous = connection.execute(
                "SELECT sha FROM repositories WHERE name=?", (repo.name,)
            ).fetchone()
            unchanged = bool(sha) and previous and previous[0] == sha
            if changed_only and unchanged:
                continue

            manifest = _git_manifest(repo)
            records: list[tuple[str, str]] = []
            additions: dict[str, tuple[str, int]] = {}
            if manifest:
                entries = [
                    (path, blob)
                    for path, (mode, blob) in manifest.items()
                    if mode.startswith("100") and _safe_path(path) and (Path(path).suffix.lower() in suffixes or Path(path).name in INDEXABLE_NAMES)
                ]
                missing = {
                    blob for _, blob in entries
                    if not connection.execute("SELECT 1 FROM blobs WHERE blob=?", (blob,)).fetchone()
                }
                for blob, content in _git_blob_contents(repo, missing).items():
                    if b"\0" not in content[:8192]:
                        additions[blob] = (content.decode("utf-8", errors="replace"), len(content))
                records = [
                    (path, blob) for path, blob in entries
                    if blob not in missing or blob in additions
                ]
            else:
                for path in _walk_files(repo, suffixes, ignored_dirs):
                    try:
                        content = path.read_bytes()
                    except OSError:
                        continue
                    if len(content) > 3_000_000 or b"\0" in content[:8192]:
                        continue
                    relative = str(path.relative_to(repo.scan_path)).replace(os.sep, "/")
                    blob = _content_blob(content)
                    records.append((relative, blob))
                    if not connection.execute("SELECT 1 FROM blobs WHERE blob=?", (blob,)).fetchone():
                        additions[blob] = (content.decode("utf-8", errors="replace"), len(content))

            connection.execute("DELETE FROM files WHERE repo=?", (repo.name,))
            connection.execute("DELETE FROM path_fts WHERE repo=?", (repo.name,))
            for blob, (content, size) in additions.items():
                if not connection.execute("SELECT 1 FROM blobs WHERE blob=?", (blob,)).fetchone():
                    connection.execute("INSERT INTO blobs(blob, content, size) VALUES (?, ?, ?)", (blob, content, size))
                    connection.execute("INSERT INTO blob_fts(blob, content) VALUES (?, ?)", (blob, content))
            for path, blob in records:
                connection.execute("INSERT INTO files(repo, path, blob) VALUES (?, ?, ?)", (repo.name, path, blob))
                connection.execute("INSERT INTO path_fts(repo, path) VALUES (?, ?)", (repo.name, path))

            indexed_at = datetime.now(UTC).isoformat()
            connection.execute(
                "INSERT OR REPLACE INTO repositories(name, sha, indexed_at, file_count) VALUES (?, ?, ?, ?)",
                (repo.name, sha, indexed_at, len(records)),
            )
            refresh_stats[repo.name] = (len(additions), sum(size for _, size in additions.values()))
            updated.append(repo.name)

        if updated:
            generation += 1
            connection.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES ('generation', ?)", (str(generation),))
        connection.execute("DELETE FROM blob_fts WHERE blob NOT IN (SELECT DISTINCT blob FROM files)")
        connection.execute("DELETE FROM blobs WHERE blob NOT IN (SELECT DISTINCT blob FROM files)")
        connection.commit()

        for name, sha, indexed_at, file_count in connection.execute(
            "SELECT name, sha, indexed_at, file_count FROM repositories ORDER BY name"
        ):
            state[name] = {
                "sha": sha,
                "indexed_at": indexed_at,
                "backend": "sqlite fts5 trigram",
                "generation": generation,
                "files": file_count,
                "changed_blobs": refresh_stats.get(name, (0, 0))[0],
                "bytes_indexed": refresh_stats.get(name, (0, 0))[1],
            }
        return state, updated
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _quoted(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _current(connection: sqlite3.Connection, repo: Repository) -> bool:
    if not repo.source_sha or not repo.source_path:
        return False
    row = connection.execute("SELECT sha FROM repositories WHERE name=?", (repo.name,)).fetchone()
    return bool(row and row[0] == repo.source_sha)


def query_index(
    settings: Settings,
    repo: Repository,
    query: str,
    *,
    max_results: int,
) -> list[tuple[str, int, str]] | None:
    """Return exact, case-sensitive line matches, or None when fallback is required."""
    path = _database(settings)
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(path, timeout=2)
        if not _current(connection, repo):
            return None
        if len(query) >= 3:
            rows = connection.execute(
                """
                SELECT f.path, b.content
                FROM blob_fts
                JOIN blobs b ON b.blob=blob_fts.blob
                JOIN files f ON f.blob=b.blob
                WHERE blob_fts MATCH ? AND f.repo=?
                LIMIT ?
                """,
                (_quoted(query), repo.name, max(100, max_results * 20)),
            )
        else:
            rows = connection.execute(
                """
                SELECT f.path, b.content
                FROM blobs b JOIN files f ON f.blob=b.blob
                WHERE f.repo=? AND instr(b.content, ?) > 0
                LIMIT ?
                """,
                (repo.name, query, max(100, max_results * 20)),
            )
        hits: list[tuple[str, int, str]] = []
        for file_path, content in rows:
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


def query_paths(
    settings: Settings,
    repo: Repository,
    query: str,
    *,
    limit: int,
) -> list[str] | None:
    path = _database(settings)
    if not path.is_file():
        return None
    tokens = [token for token in query.lower().replace("\\", "/").split() if len(token) >= 3]
    try:
        connection = sqlite3.connect(path, timeout=2)
        if not _current(connection, repo):
            return None
        if tokens:
            expression = " AND ".join(_quoted(token) for token in tokens)
            rows = connection.execute(
                "SELECT path FROM path_fts WHERE path_fts MATCH ? AND repo=? LIMIT ?",
                (expression, repo.name, max(100, limit * 20)),
            )
        else:
            rows = connection.execute("SELECT path FROM files WHERE repo=? ORDER BY path", (repo.name,))
        return [row[0] for row in rows]
    except sqlite3.Error:
        return None
    finally:
        if "connection" in locals():
            connection.close()


def read_indexed_file(settings: Settings, repo: Repository, file_path: str) -> str | None:
    path = _database(settings)
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(path, timeout=2)
        if not _current(connection, repo):
            return None
        row = connection.execute(
            "SELECT b.content FROM files f JOIN blobs b ON b.blob=f.blob WHERE f.repo=? AND f.path=?",
            (repo.name, file_path.replace(os.sep, "/")),
        ).fetchone()
        return str(row[0]) if row else None
    except sqlite3.Error:
        return None
    finally:
        if "connection" in locals():
            connection.close()


def write_state(settings: Settings, state: dict[str, object]) -> None:
    target = settings.state_dir / "indexes.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)

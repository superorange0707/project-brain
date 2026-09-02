from __future__ import annotations

import hashlib
from http.client import HTTPException
import json
import math
import os
import re
import sqlite3
import stat
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable
from urllib.error import URLError

from .models import (
    active_pack,
    embedding_batch_size,
    embedding_request_bytes,
    runtime_for_pack,
    valid_embedding_vector,
    verified_pack,
)
from .locks import model_lane, workspace_exclusive
from .platforms import atomic_managed_text_write, logical_path, read_managed_text

if TYPE_CHECKING:
    from .core import Repository, Settings

CHUNK_SCHEMA_VERSION = "1"
CARD_VERSION = "1"
# Atlas cards have their own input contract so an Atlas-only format change does
# not invalidate source-card embedding cache entries.
ATLAS_CARD_VERSION = "1"
DENY_NAMES = {".env", ".envrc", "id_rsa", "id_ed25519", "keystore", "credentials", "credentials.json", "service-account.json"}
DENY_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks"}
# Dependency locks are neither authored code nor useful semantic evidence. They
# can also contain a single machine-generated line too large for a local model
# context window, so keep them out of semantic cards while Core path/lexical
# retrieval remains available.
DEPENDENCY_LOCK_NAMES = {"uv.lock", "poetry.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "cargo.lock", "go.sum"}
SEMANTIC_CHILD_LINES = 80
SEMANTIC_CARD_CODE_CHARS = 2_048
SEMANTIC_IDENTIFIER_CHARS = 256
# These limits apply to the whole model input and the exact UTF-8 JSON request
# body, not merely to the source-code section of a semantic card.  They protect
# the pack-owned local runtime from an oversized request while keeping the
# autotuned item count as a separate, additional bound.
SEMANTIC_MAX_CARD_INPUT_BYTES = 8_192
SEMANTIC_MAX_REQUEST_BODY_BYTES = 24_576
# Invalidates pre-request-bounding cache/state entries without changing the
# model-pack card schema contract.
SEMANTIC_EMBEDDING_INPUT_VERSION = "3"
SEMANTIC_SHARD_MANIFEST_VERSION = "2"
MAX_SEMANTIC_STATE_BYTES = 64 * 1024 * 1024
MAX_EMBEDDING_CACHE_ROWS = 100_000
MAX_EMBEDDING_CACHE_BYTES = 512 * 1024 * 1024
MIN_SEMANTIC_SHARD_RESERVATION_BYTES = 64 * 1024 * 1024
MAX_SEMANTIC_SHARD_OVERHEAD_BYTES = 8 * 1024 * 1024
MAX_SEMANTIC_CHUNKS_PER_FILE = 4_096
MAX_SEMANTIC_CHUNKS_PER_REPOSITORY = 20_000
MAX_SEMANTIC_CHUNKS_TOTAL = 100_000
MAX_SEMANTIC_METADATA_BYTES_PER_REPOSITORY = 128 * 1024 * 1024
MAX_SEMANTIC_METADATA_BYTES_TOTAL = 512 * 1024 * 1024
MAX_SEMANTIC_SOURCE_FILES_PER_REPOSITORY = 20_000
MAX_SEMANTIC_SOURCE_FILES_TOTAL = 100_000
MAX_SEMANTIC_SOURCE_BYTES_PER_REPOSITORY = 512 * 1024 * 1024
MAX_SEMANTIC_SOURCE_BYTES_TOTAL = 2 * 1024 * 1024 * 1024
MAX_SEMANTIC_SOURCE_SCAN_SECONDS = 300.0
MAX_SEMANTIC_SOURCE_REPOSITORIES = 1_000
MAX_SEMANTIC_LEGACY_SCAN_ENTRIES = 100_000
MAX_SEMANTIC_LEGACY_SCAN_DEPTH = 128
SEMANTIC_CACHE_LOOKUP_BATCH = 500
SYMBOL = re.compile(r"(?m)^\s*(?:class|interface|record|enum|def|function|fun|func)\s+([A-Za-z_$][\w$]*)")
_SHARD_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="brain-semantic-shard")
_SERVING_STATE_CACHE: dict[tuple[object, ...], dict[str, object]] = {}
_SHARD_HASH_CACHE: dict[tuple[object, ...], str] = {}


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    blob_sha: str
    path: str
    start_line: int
    end_line: int
    kind: str
    symbol: str
    card: str
    target_id: str | None = None


class SemanticEmbeddingError(RuntimeError):
    """A sanitized semantic-indexing failure safe for CLI diagnostics."""


def _reserve_embedding_cache(
    connection: sqlite3.Connection, entries: list[tuple[str, str]],
) -> None:
    """Prune before insertion so the cache never crosses row/byte capacity."""
    incoming = {key: payload for key, payload in entries}
    incoming_bytes = sum(len(payload.encode("utf-8")) for payload in incoming.values())
    if len(incoming) > MAX_EMBEDDING_CACHE_ROWS or incoming_bytes > MAX_EMBEDDING_CACHE_BYTES:
        raise SemanticEmbeddingError("embedding cache entry batch exceeds its managed capacity")
    usage = connection.execute(
        "SELECT COUNT(*),COALESCE(SUM(length(CAST(vector_json AS BLOB))),0) FROM embedding_cache"
    ).fetchone()
    current_rows, current_bytes = int(usage[0]), int(usage[1])
    replaced_rows = 0
    replaced_bytes = 0
    keys = sorted(incoming)
    for offset in range(0, len(keys), 400):
        batch = keys[offset:offset + 400]
        placeholders = ",".join("?" for _ in batch)
        for _, size in connection.execute(
            f"SELECT cache_key,length(CAST(vector_json AS BLOB)) FROM embedding_cache "
            f"WHERE cache_key IN ({placeholders})",
            batch,
        ):
            replaced_rows += 1
            replaced_bytes += int(size)
    target_rows = current_rows - replaced_rows + len(incoming)
    target_bytes = current_bytes - replaced_bytes + incoming_bytes
    removals: list[str] = []
    if target_rows > MAX_EMBEDDING_CACHE_ROWS or target_bytes > MAX_EMBEDDING_CACHE_BYTES:
        for key, size in connection.execute(
            "SELECT cache_key,length(CAST(vector_json AS BLOB)) FROM embedding_cache "
            "ORDER BY last_used_at,cache_key"
        ):
            if str(key) in incoming:
                continue
            removals.append(str(key))
            target_rows -= 1
            target_bytes -= int(size)
            if target_rows <= MAX_EMBEDDING_CACHE_ROWS and target_bytes <= MAX_EMBEDDING_CACHE_BYTES:
                break
    if target_rows > MAX_EMBEDDING_CACHE_ROWS or target_bytes > MAX_EMBEDDING_CACHE_BYTES:
        raise SemanticEmbeddingError("embedding cache cannot satisfy its managed capacity")
    connection.executemany("DELETE FROM embedding_cache WHERE cache_key=?", ((key,) for key in removals))


SemanticProgress = Callable[[dict[str, object]], None]


_SEMANTIC_PROGRESS_LABELS = {
    "semantic_manifest": "Discovering Semantic cards",
    "semantic_embedding": "Building Semantic index",
    "semantic_shard": "Writing Semantic shards",
    "semantic_reuse": "Reused published Semantic generation",
    "semantic_publish": "Publishing Semantic generation",
}


def _excluded(path: Path, content: bytes) -> bool:
    return path.name.lower() in DENY_NAMES | DEPENDENCY_LOCK_NAMES or path.suffix.lower() in DENY_SUFFIXES or len(content) > 3_000_000 or b"\0" in content[:8192]


def _language(path: str) -> str:
    return {".py": "Python", ".java": "Java", ".kt": "Kotlin", ".ts": "TypeScript", ".js": "JavaScript", ".go": "Go", ".rs": "Rust"}.get(Path(path).suffix.lower(), "Text")


def chunk_source(repo: str, path: str, content: str, *, blob_sha: str | None = None) -> list[Chunk]:
    """Create repeatable symbol-aware cards without a generative model."""
    lines = content.splitlines()
    blob_sha = blob_sha or hashlib.sha256(content.encode("utf-8")).hexdigest()
    markers = []
    for match in SYMBOL.finditer(content):
        if len(markers) >= MAX_SEMANTIC_CHUNKS_PER_FILE:
            raise SemanticEmbeddingError("Semantic source exceeds its per-file chunk limit")
        markers.append((match.start() and content[:match.start()].count("\n") + 1 or 1, match.group(1)))
    if not markers:
        markers = [(1, "file")]
    chunks: list[Chunk] = []
    for index, (start, symbol) in enumerate(markers):
        end = markers[index + 1][0] - 1 if index + 1 < len(markers) else len(lines)
        for child_start in range(start, max(end, start) + 1, SEMANTIC_CHILD_LINES):
            if len(chunks) >= MAX_SEMANTIC_CHUNKS_PER_FILE:
                raise SemanticEmbeddingError("Semantic source exceeds its per-file chunk limit")
            child_end = min(end, child_start + SEMANTIC_CHILD_LINES - 1)
            code = "\n".join(lines[child_start - 1:child_end])
            # Structural regions remain primary. This final cap only protects a
            # pathological long source line from exceeding the local runtime's
            # context window; normal oversized symbols are split by line range.
            if len(code) > SEMANTIC_CARD_CODE_CHARS:
                code = code[:SEMANTIC_CARD_CODE_CHARS] + "\n[semantic card code capped]"
            kind = "symbol" if symbol != "file" else "file"
            identity = f"{blob_sha}\0{CHUNK_SCHEMA_VERSION}\0{child_start}\0{child_end}\0{symbol}\0{SEMANTIC_CHILD_LINES}\0{SEMANTIC_CARD_CODE_CHARS}\0{CARD_VERSION}"
            chunk_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            identifiers = " ".join(sorted(set(re.findall(r'[A-Za-z_][A-Za-z0-9_]*', code)))[:40])[:SEMANTIC_IDENTIFIER_CHARS]
            card = "\n".join([
                f"Repository: {repo}", f"Path: {path}", f"Language: {_language(path)}", f"Kind: {kind}",
                f"Symbol: {symbol}", f"Identifiers: {identifiers}",
                "Code:", code,
            ])
            chunks.append(Chunk(chunk_id, blob_sha, path, child_start, child_end, kind, symbol, card))
    return chunks


def _state_path(settings: Settings) -> Path:
    return settings.state_dir / "semantic-index.json"


def _shard_root(settings: Settings) -> Path:
    configured = settings.state_dir / "semantic-shards"
    root = configured.resolve()
    if configured.is_symlink() or root.parent != settings.state_dir.resolve():
        raise ValueError("Semantic shard root escapes managed state")
    return root


def _query_cache_path(settings: Settings) -> Path:
    return settings.state_dir / "semantic-query-cache.json"


def _files(repo: Repository, *, budget: object) -> Iterable[Path]:
    ignored = {".git", ".venv", "node_modules", "target", "build", "dist", "vendor", "generated"}
    from .index import _walk_root

    yield from _walk_root(repo.scan_path, None, ignored, budget=budget)


def _usearch() -> tuple[Any, Any] | None:
    try:
        import numpy as numpy
        from usearch.index import Index
    except ImportError:
        return None
    return Index, numpy


def _card_input_bytes(card: str, *, document_instruction: str, input_suffix: str) -> int:
    return len((document_instruction + card + input_suffix).encode("utf-8"))


def _bounded_semantic_card(
    card: str,
    *,
    document_instruction: str = "",
    input_suffix: str = "",
    dimension: int | None = None,
) -> str:
    """Keep identity metadata intact while deterministically trimming only code."""
    def fits(value: str) -> bool:
        return (
            _card_input_bytes(value, document_instruction=document_instruction, input_suffix=input_suffix) <= SEMANTIC_MAX_CARD_INPUT_BYTES
            and embedding_request_bytes([value], instruction=document_instruction, input_suffix=input_suffix, dimension=dimension) <= SEMANTIC_MAX_REQUEST_BODY_BYTES
        )

    if fits(card):
        return card
    prefix, separator, code = card.partition("\nCode:\n")
    if not separator:
        raise SemanticEmbeddingError(
            "semantic embedding card metadata exceeds the safe model/request bound: "
            f"max_card_chars={len(card)} request_bytes={embedding_request_bytes([card], instruction=document_instruction, input_suffix=input_suffix, dimension=dimension)}"
        )
    prefix += separator
    if not fits(prefix):
        # Repository/path/symbol metadata must never be silently trimmed.  A
        # malformed or pathological identity is therefore a safe hard failure.
        raise SemanticEmbeddingError(
            "semantic embedding card metadata exceeds the safe model/request bound: "
            f"max_card_chars={len(card)} request_bytes={embedding_request_bytes([prefix], instruction=document_instruction, input_suffix=input_suffix, dimension=dimension)}"
        )
    marker = "\n[semantic card code truncated]"
    lower, upper = 0, len(code)
    while lower < upper:
        middle = (lower + upper + 1) // 2
        candidate = prefix + code[:middle]
        if middle < len(code):
            candidate += marker
        if fits(candidate):
            lower = middle
        else:
            upper = middle - 1
    bounded = prefix + code[:lower]
    if lower < len(code) and fits(bounded + marker):
        bounded += marker
    return bounded


def _request_bytes(
    indexes: list[int],
    cards: list[str],
    *,
    document_instruction: str,
    input_suffix: str,
    dimension: int | None,
) -> int:
    return embedding_request_bytes(
        [cards[index] for index in indexes],
        instruction=document_instruction,
        input_suffix=input_suffix,
        dimension=dimension,
    )


def _bounded_embedding_batches(
    chunks: list[Chunk],
    indexes: list[int],
    batch_size: int,
    *,
    cards: list[str] | None = None,
    document_instruction: str = "",
    input_suffix: str = "",
    dimension: int | None = None,
) -> Iterable[list[int]]:
    """Keep requests within the tuned count and exact serialized-byte ceilings."""
    cards = cards or [chunk.card for chunk in chunks]
    cursor = 0
    while cursor < len(indexes):
        batch: list[int] = []
        while cursor < len(indexes) and len(batch) < batch_size:
            index = indexes[cursor]
            request_bytes = _request_bytes(
                [index], cards, document_instruction=document_instruction, input_suffix=input_suffix, dimension=dimension
            )
            if request_bytes > SEMANTIC_MAX_REQUEST_BODY_BYTES:
                raise SemanticEmbeddingError(
                    "semantic embedding card exceeds the safe request-body bound: "
                    f"max_card_chars={len(cards[index])} request_bytes={request_bytes}"
                )
            if batch and _request_bytes(
                [*batch, index], cards, document_instruction=document_instruction, input_suffix=input_suffix, dimension=dimension
            ) > SEMANTIC_MAX_REQUEST_BODY_BYTES:
                break
            batch.append(index)
            cursor += 1
        yield batch


def _transport_error(error: Exception) -> bool:
    return isinstance(error, (OSError, HTTPException, URLError))


def _embedding_failure(
    batch: list[int],
    cards: list[str],
    *,
    batch_size: int,
    document_instruction: str,
    input_suffix: str,
    dimension: int,
) -> SemanticEmbeddingError:
    return SemanticEmbeddingError(
        "semantic embedding failed after managed-runtime restart: "
        f"batch={batch_size} cards={len(batch)} max_card_chars={max(len(cards[index]) for index in batch)} "
        f"request_bytes={_request_bytes(batch, cards, document_instruction=document_instruction, input_suffix=input_suffix, dimension=dimension)}"
    )


def _cache_vectors(
    settings: Settings,
    pack_id: str,
    chunks: list[Chunk],
    *,
    dimension: int,
    embed: Callable[[list[str]], list[list[float]]],
    batch_size: int = 0,
    document_instruction: str = "",
    input_suffix: str = "",
    restart: Callable[[], None] | None = None,
    progress: SemanticProgress | None = None,
    pack_compatibility_identity: str | None = None,
    write_capacity: list[int] | None = None,
) -> list[list[float]]:
    """Reuse vectors by stable chunk identity without persisting query/source text."""
    from .catalog import connect

    cards = [
        _bounded_semantic_card(
            chunk.card, document_instruction=document_instruction, input_suffix=input_suffix, dimension=dimension
        )
        for chunk in chunks
    ]
    # Cache the exact, bounded document sent to the model rather than only the
    # source chunk identity.  This prevents a pre-bound card or changed pack
    # instruction from being reused as if it were current evidence.
    pack_compatibility_identity = pack_compatibility_identity or _injected_pack_identity(pack_id)
    keys = [
        hashlib.sha256(
            f"{pack_id}\0{pack_compatibility_identity}\0{dimension}\0{SEMANTIC_EMBEDDING_INPUT_VERSION}\0{document_instruction}\0{card}\0{input_suffix}".encode("utf-8")
        ).hexdigest()
        for card in cards
    ]
    vectors: list[list[float] | None] = [None] * len(chunks)
    cached = 0
    completed = 0
    completed_batches = 0

    def report(*, batch: int = 0, remaining: int | None = None) -> None:
        if progress is not None:
            progress({
                "semantic_cards_discovered": len(chunks),
                "semantic_cards_total": len(chunks),
                "cached_embeddings_reused": cached,
                "new_embeddings_completed": completed,
                "remaining_embeddings": remaining,
                "embedding_batch_size": batch,
                "embedding_batches_completed": completed_batches,
            })

    connection = connect(settings)
    try:
        from datetime import UTC, datetime

        used_at = datetime.now(UTC).isoformat()
        missing: list[int] = []
        cached_payloads: dict[str, str] = {}
        for offset in range(0, len(keys), SEMANTIC_CACHE_LOOKUP_BATCH):
            batch_keys = list(dict.fromkeys(keys[offset:offset + SEMANTIC_CACHE_LOOKUP_BATCH]))
            slots = ",".join("?" for _ in batch_keys)
            cached_payloads.update({
                str(key): str(payload)
                for key, payload in connection.execute(
                    f"SELECT cache_key,vector_json FROM embedding_cache WHERE cache_key IN ({slots})",
                    batch_keys,
                )
            })
        touched: list[tuple[str, str]] = []
        invalid: list[tuple[str]] = []
        for index, key in enumerate(keys):
            payload = cached_payloads.get(key)
            if payload is not None:
                try:
                    value = json.loads(payload)
                    normalized = valid_embedding_vector(value, dimension=dimension)
                    if normalized is not None:
                        vectors[index] = normalized
                        touched.append((used_at, key))
                        cached += 1
                        continue
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
                invalid.append((key,))
            missing.append(index)
        if touched:
            connection.executemany("UPDATE embedding_cache SET last_used_at=? WHERE cache_key=?", touched)
        if invalid:
            connection.executemany("DELETE FROM embedding_cache WHERE cache_key=?", invalid)
        report(remaining=len(missing))
        if missing:
            if write_capacity is None:
                from .ops import remaining_write_capacity

                write_capacity = [remaining_write_capacity(settings)]
            batch_size = max(1, batch_size or len(missing))
            for batch in _bounded_embedding_batches(
                chunks, missing, batch_size, cards=cards, document_instruction=document_instruction, input_suffix=input_suffix, dimension=dimension
            ):
                pending = [batch]
                while pending:
                    current = pending.pop(0)
                    report(batch=len(current), remaining=len(missing) - completed)
                    try:
                        computed = embed([cards[index] for index in current])
                    except Exception as error:
                        if not _transport_error(error):
                            raise
                        if restart is not None:
                            restart()
                        if len(current) == 1:
                            raise _embedding_failure(
                                current, cards, batch_size=batch_size, document_instruction=document_instruction,
                                input_suffix=input_suffix, dimension=dimension,
                            ) from error
                        middle = len(current) // 2
                        pending[0:0] = [current[:middle], current[middle:]]
                        continue
                    normalized_batch = [
                        valid_embedding_vector(vector, dimension=dimension) for vector in computed
                    ]
                    if len(computed) != len(current) or any(vector is None for vector in normalized_batch):
                        raise RuntimeError("embedding runtime returned an unexpected vector dimension")
                    payloads: list[tuple[int, list[float], str]] = []
                    for index, normalized in zip(current, normalized_batch, strict=True):
                        assert normalized is not None
                        payloads.append((index, normalized, json.dumps(normalized, separators=(",", ":"))))
                    payload_bytes = sum(len(payload.encode("utf-8")) for _, _, payload in payloads)
                    if payload_bytes > write_capacity[0]:
                        raise SemanticEmbeddingError("embedding cache exceeds the remaining managed write capacity")
                    _reserve_embedding_cache(
                        connection, [(keys[index], payload) for index, _, payload in payloads],
                    )
                    for index, normalized, payload in payloads:
                        vectors[index] = normalized
                        connection.execute(
                            "INSERT OR REPLACE INTO embedding_cache(cache_key, pack_id, dimension, vector_json, created_at, last_used_at) VALUES (?, ?, ?, ?, ?, ?)",
                            (keys[index], pack_id, dimension, payload, used_at, used_at),
                        )
                    completed += len(current)
                    completed_batches += 1
                    # Successful sub-batches survive a later transport failure;
                    # vector cache entries are content-addressed and are not a
                    # semantic-index publication.
                    connection.commit()
                    write_capacity[0] -= payload_bytes
                    report(remaining=len(missing) - completed)
            _reserve_embedding_cache(connection, [])
            connection.commit()
    finally:
        connection.close()
    return [vector for vector in vectors if vector is not None]


def _chunk_groups(
    repo: Repository,
    *,
    settings: Settings | None = None,
    manifest: dict[tuple[str, str], str] | None = None,
    source_projection: tuple[int, int, str] | None = None,
    legacy_walk_budget: object | None = None,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    metadata_bytes = 0

    def append_file(values: list[Chunk]) -> None:
        nonlocal metadata_bytes
        projected = sum(len(item.card.encode("utf-8")) for item in values)
        if len(chunks) + len(values) > MAX_SEMANTIC_CHUNKS_PER_REPOSITORY:
            raise SemanticEmbeddingError("Semantic repository exceeds its chunk limit")
        if metadata_bytes + projected > MAX_SEMANTIC_METADATA_BYTES_PER_REPOSITORY:
            raise SemanticEmbeddingError("Semantic repository exceeds its metadata byte limit")
        chunks.extend(values)
        metadata_bytes += projected

    if source_projection is not None:
        if settings is None:
            raise ValueError("authoritative Semantic source requires settings")
        from .index import indexed_snapshot_source_contents

        snapshot = str(repo.source_sha or "working-tree")
        for relative, blob, content in indexed_snapshot_source_contents(
            settings,
            repo.name,
            snapshot,
            source_projection,
            max_seconds=MAX_SEMANTIC_SOURCE_SCAN_SECONDS,
        ):
            raw = content.encode("utf-8")
            if _excluded(Path(relative), raw):
                continue
            append_file(chunk_source(
                repo.name, relative, content, blob_sha=blob,
            ))
        return chunks
    if manifest is not None:
        if settings is None:
            raise ValueError("authoritative Semantic source requires settings")
        from .index import indexed_snapshot_contents

        snapshot = str(repo.source_sha or "working-tree")
        selected = manifest
        if any(name != repo.name for name, _ in selected):
            raise ValueError("Semantic repository manifest is not repo-scoped")
        for (_, relative), content in indexed_snapshot_contents(
            settings, selected, {repo.name: snapshot},
        ):
            raw = content.encode("utf-8")
            if _excluded(Path(relative), raw):
                continue
            append_file(chunk_source(
                repo.name, relative, content, blob_sha=selected[(repo.name, relative)],
            ))
        return chunks
    if legacy_walk_budget is None:
        raise ValueError("legacy Semantic source requires a bounded repository walk")
    for path in _files(repo, budget=legacy_walk_budget):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            with path.open("rb") as source:
                raw = source.read(3_000_001)
        except OSError:
            continue
        if len(raw) > 3_000_000:
            continue
        if _excluded(path, raw):
            continue
        relative = logical_path(path.relative_to(repo.scan_path))
        blob = hashlib.sha256(raw).hexdigest()
        append_file(chunk_source(
            repo.name, relative, raw.decode("utf-8", errors="replace"), blob_sha=blob,
        ))
    return chunks


def _partition_semantic_inputs(
    repositories: list[Repository],
    manifest: dict[tuple[str, str], str] | None,
    atlas_cards: list[dict[str, object]] | None,
) -> tuple[
    dict[str, dict[tuple[str, str], str]] | None,
    dict[str, list[dict[str, object]]],
]:
    names = {repo.name for repo in repositories}
    manifests = {name: {} for name in names} if manifest is not None else None
    if manifests is not None and manifest is not None:
        for (name, path), blob in manifest.items():
            if name in manifests:
                manifests[name][(name, path)] = blob
    cards = {name: [] for name in names}
    for card in atlas_cards or []:
        name = str(card.get("repo") or "")
        if name in cards:
            cards[name].append(card)
    return manifests, cards


def _entries(chunks: list[Chunk]) -> list[dict[str, object]]:
    return [
        {"path": chunk.path, "line": chunk.start_line, "end_line": chunk.end_line, "chunk_id": chunk.chunk_id,
         "kind": chunk.kind, "symbol": chunk.symbol, "target_id": chunk.target_id}
        for chunk in chunks
    ]


def _published_state(settings: Settings) -> dict[str, object] | None:
    try:
        state = json.loads(read_managed_text(
            settings.state_dir, _state_path(settings), max_bytes=MAX_SEMANTIC_STATE_BYTES,
        ))
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return state if isinstance(state, dict) else None


def semantic_schema_version() -> str:
    return (
        f"{CHUNK_SCHEMA_VERSION}:{CARD_VERSION}:{SEMANTIC_EMBEDDING_INPUT_VERSION}:"
        f"{ATLAS_CARD_VERSION}:{SEMANTIC_SHARD_MANIFEST_VERSION}"
    )


def _injected_pack_identity(pack_id: str) -> str:
    return "sha256:" + hashlib.sha256(f"injected-test-embedder\0{pack_id}".encode()).hexdigest()


def _shard_sha256(path: Path) -> str:
    stat = path.stat()
    key = (
        str(path.resolve()), stat.st_dev, stat.st_ino, stat.st_size,
        stat.st_mtime_ns, stat.st_ctime_ns,
    )
    # On Windows st_ctime is creation time, so a same-size in-place rewrite
    # with restored mtime can preserve every field in this projection.
    cached = _SHARD_HASH_CACHE.get(key) if os.name != "nt" else None
    if cached is not None:
        return cached
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    value = digest.hexdigest()
    if os.name != "nt":
        if len(_SHARD_HASH_CACHE) >= 4_096:
            _SHARD_HASH_CACHE.clear()
        _SHARD_HASH_CACHE[key] = value
    return value


def _valid_shard_artifact(shard: dict[str, object], *, root: Path | None = None) -> bool:
    path = Path(str(shard.get("path") or ""))
    try:
        resolved = path.resolve()
        if root is not None and not resolved.is_relative_to(root.resolve()):
            return False
        expected_bytes = int(shard.get("artifact_bytes"))
        expected_sha256 = str(shard.get("artifact_sha256") or "").lower()
        return bool(
            path.is_file()
            and shard.get("artifact_ref") == path.name
            and path.stat().st_size == expected_bytes
            and re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
            and _shard_sha256(path) == expected_sha256
        )
    except (OSError, TypeError, ValueError):
        return False


def _valid_shard_manifest_entry(shard: dict[str, object], *, root: Path) -> bool:
    """Validate cheap shard identity and existence without hashing its payload."""
    try:
        raw_path = Path(str(shard.get("path") or ""))
        metadata = raw_path.lstat()
        path = raw_path.resolve()
        expected_bytes = int(shard.get("artifact_bytes"))
        expected_sha256 = str(shard.get("artifact_sha256") or "").lower()
        return bool(
            stat.S_ISREG(metadata.st_mode)
            and not raw_path.is_symlink()
            and path.is_relative_to(root.resolve())
            and shard.get("artifact_ref") == path.name
            and metadata.st_size == expected_bytes
            and re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
        )
    except (OSError, TypeError, ValueError):
        return False


def semantic_snapshots(state: dict[str, object]) -> dict[str, str]:
    raw_snapshots = state.get("snapshots")
    snapshots = {
        str(name): str(sha)
        for name, sha in (raw_snapshots.items() if isinstance(raw_snapshots, dict) else [])
    }
    if snapshots:
        return snapshots
    for item in [*(state.get("shards") or []), *(state.get("entries") or [])]:
        if isinstance(item, dict) and item.get("repo") and item.get("snapshot"):
            snapshots[str(item["repo"])] = str(item["snapshot"])
    return snapshots


def semantic_state_compatibility(
    settings: Settings,
    state: dict[str, object] | None,
    snapshots: dict[str, str],
    *,
    component: dict[str, object] | None = None,
    require_active_pack: bool = True,
    verify_artifacts: bool = True,
) -> tuple[bool, str | None]:
    """Validate one published Semantic artifact against its Atlas contract."""
    if not state:
        return False, "Semantic generation has not been built."
    if state.get("stale"):
        return False, str(state.get("stale_reason") or "Semantic generation is stale.")
    if (
        state.get("chunk_schema_version") != CHUNK_SCHEMA_VERSION
        or state.get("card_version") != CARD_VERSION
        or state.get("embedding_input_version") != SEMANTIC_EMBEDDING_INPUT_VERSION
        or state.get("atlas_card_version") != ATLAS_CARD_VERSION
        or state.get("shard_manifest_version") != SEMANTIC_SHARD_MANIFEST_VERSION
    ):
        return False, "Semantic generation schema is incompatible."
    if semantic_snapshots(state) != snapshots:
        return False, "Semantic generation does not match current snapshots."

    backend = str(state.get("backend") or "")
    pack_id = str(state.get("pack_id") or "")
    pack_identity = str(state.get("pack_compatibility_identity") or "")
    try:
        dimension = int(state.get("dimension") or 0)
    except (TypeError, ValueError):
        dimension = 0
    if (
        backend not in {"usearch", "exact-mock"} or not pack_id or dimension <= 0
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", pack_identity)
    ):
        return False, "Semantic generation metadata is invalid."
    if backend == "usearch" and require_active_pack:
        manifest = verified_pack(settings, pack_id, "embedding")
        if manifest is None or str(manifest.get("pack_id") or "") != pack_id:
            return False, "Semantic embedding pack does not match the active verified pack."
        try:
            active_dimension = int(manifest.get("embedding_dimension") or 0)
        except (TypeError, ValueError):
            active_dimension = 0
        if active_dimension != dimension:
            return False, "Semantic vector dimension does not match the active embedding pack."
        from .models import pack_compatibility_identity

        if pack_compatibility_identity(manifest) != pack_identity:
            return False, "Semantic embedding pack definition does not match the active verified pack."

    entries = state.get("entries")
    shards = state.get("shards")
    if not isinstance(entries, list) or not isinstance(shards, list):
        return False, "Semantic shard manifest is invalid."
    required_entry_fields = {"repo", "snapshot", "path", "chunk_id"}
    if backend == "exact-mock":
        for entry in entries:
            if not isinstance(entry, dict) or not required_entry_fields.issubset(entry):
                return False, "Semantic shard manifest is invalid."
            if snapshots.get(str(entry.get("repo"))) != str(entry.get("snapshot") or ""):
                return False, "Semantic shard manifest is invalid."
            vector = entry.get("vector")
            if not isinstance(vector, list) or len(vector) != dimension:
                return False, "Semantic vector dimension is invalid."
    else:
        seen: set[tuple[str, str]] = set()
        shard_root = _shard_root(settings).resolve()
        for shard in shards:
            if not isinstance(shard, dict) or not isinstance(shard.get("entries"), list):
                return False, "Semantic shard manifest is invalid."
            repo = str(shard.get("repo") or "")
            snapshot = str(shard.get("snapshot") or "")
            key = (repo, snapshot)
            if not repo or snapshots.get(repo) != snapshot or key in seen:
                return False, "Semantic shard manifest is invalid."
            seen.add(key)
            if not _valid_shard_manifest_entry(shard, root=shard_root):
                return False, "Semantic shard manifest is invalid."
            if verify_artifacts and not _valid_shard_artifact(shard, root=shard_root):
                return False, "Semantic shard manifest is invalid."
            for entry in shard["entries"]:
                if not isinstance(entry, dict) or not {"path", "chunk_id"}.issubset(entry):
                    return False, "Semantic shard manifest is invalid."

    if component is not None:
        details = component.get("details") if isinstance(component.get("details"), dict) else {}
        try:
            projected_dimension = int(details.get("dimension") or 0)
        except (TypeError, ValueError):
            projected_dimension = 0
        if (
            component.get("status") != "ready"
            or component.get("schema_version") != semantic_schema_version()
            or str(details.get("pack_id") or "") != pack_id
            or str(details.get("pack_compatibility_identity") or "") != pack_identity
            or projected_dimension != dimension
            or str(details.get("backend") or "") != backend
            or details.get("snapshots") != snapshots
        ):
            return False, "Semantic Atlas component metadata is incompatible."
        from .catalog import _content_hash, source_signature

        if details.get("source_signature") != source_signature(snapshots):
            return False, "Semantic Atlas component source signature is incompatible."
        if component.get("content_hash") != _content_hash(state):
            return False, "Semantic Atlas component content hash is invalid."
    return True, None


def _state_is_reusable(
    settings: Settings,
    state: dict[str, object] | None,
    groups: list[tuple[Repository, str, list[Chunk]]],
    *,
    backend: str,
    pack_id: str,
    pack_compatibility_identity: str,
    dimension: int,
) -> bool:
    if not _state_is_compatible(
        state, backend=backend, pack_id=pack_id,
        pack_compatibility_identity=pack_compatibility_identity, dimension=dimension,
    ):
        return False
    if semantic_snapshots(state or {}) != {repo.name: snapshot for repo, snapshot, _ in groups}:
        return False
    expected = {(repo.name, snapshot): _entries(chunks) for repo, snapshot, chunks in groups if chunks}
    if backend == "exact-mock":
        actual: dict[tuple[str, str], list[dict[str, object]]] = {}
        for item in state.get("entries") or []:
            if not isinstance(item, dict):
                return False
            vector = item.get("vector")
            if not isinstance(vector, list) or len(vector) != dimension:
                return False
            repo, snapshot = str(item.get("repo") or ""), str(item.get("snapshot") or "")
            entry = {key: item.get(key) for key in ("path", "line", "end_line", "chunk_id", "kind", "symbol", "target_id")}
            actual.setdefault((repo, snapshot), []).append(entry)
        return actual == expected
    actual = {}
    for shard in state.get("shards") or []:
        if not isinstance(shard, dict):
            return False
        if not _valid_shard_artifact(shard, root=_shard_root(settings)):
            return False
        actual[(str(shard.get("repo") or ""), str(shard.get("snapshot") or ""))] = shard.get("entries")
    return actual == expected


def _state_is_compatible(
    state: dict[str, object] | None,
    *,
    backend: str,
    pack_id: str,
    pack_compatibility_identity: str,
    dimension: int,
) -> bool:
    if (
        not state
        or state.get("stale")
        or state.get("chunk_schema_version") != CHUNK_SCHEMA_VERSION
        or state.get("card_version") != CARD_VERSION
        or state.get("embedding_input_version") != SEMANTIC_EMBEDDING_INPUT_VERSION
        or state.get("atlas_card_version") != ATLAS_CARD_VERSION
        or state.get("shard_manifest_version") != SEMANTIC_SHARD_MANIFEST_VERSION
    ):
        return False
    try:
        state_dimension = int(state.get("dimension") or 0)
    except (TypeError, ValueError):
        return False
    if (
        state.get("backend") != backend or state.get("pack_id") != pack_id
        or state.get("pack_compatibility_identity") != pack_compatibility_identity
        or state_dimension != dimension
    ):
        return False
    return True


def _state_result(state: dict[str, object]) -> dict[str, object]:
    return {
        "chunks": len(state.get("entries") or []) + sum(len(shard.get("entries") or []) for shard in state.get("shards") or [] if isinstance(shard, dict)),
        "backend": state.get("backend"),
        "pack_id": state.get("pack_id"),
        "stale": False,
    }


def _atomic_state_write(path: Path, state: dict[str, object]) -> None:
    """Publish one complete semantic generation without exposing a partial state."""
    atomic_managed_text_write(path.parent, path, json.dumps(state, separators=(",", ":")))


def _atomic_index_save(index: Any, shard: Path) -> None:
    """Save a native vector shard through an unpredictable sibling path."""
    if shard.parent.is_symlink():
        raise ValueError("Semantic shard parent must not be a symbolic link")
    shard.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{shard.name}.", suffix=".building", dir=shard.parent)
    os.close(descriptor)
    temporary = Path(name)
    temporary.unlink()
    try:
        index.save(str(temporary))
        if temporary.is_symlink() or not temporary.is_file():
            raise ValueError("Semantic shard builder did not create a direct regular file")
        temporary.replace(shard)
    finally:
        temporary.unlink(missing_ok=True)


def _probe_embedding_dimension(
    embed: Callable[[list[str]], list[list[float]]],
    card: str,
    *,
    document_instruction: str,
    input_suffix: str,
    restart: Callable[[], None] | None,
) -> int:
    bounded = _bounded_semantic_card(card, document_instruction=document_instruction, input_suffix=input_suffix)
    for attempt in range(2):
        try:
            probe = embed([bounded])
        except Exception as error:
            if not _transport_error(error):
                raise
            if restart is not None:
                restart()
            if attempt:
                raise SemanticEmbeddingError(
                    "semantic embedding failed after managed-runtime restart: "
                    f"batch=1 cards=1 max_card_chars={len(bounded)} "
                    f"request_bytes={_request_bytes([0], [bounded], document_instruction=document_instruction, input_suffix=input_suffix, dimension=None)}"
                ) from error
            continue
        if len(probe) != 1 or not probe[0]:
            raise RuntimeError("mock embedding returned no vector")
        return len(probe[0])
    raise AssertionError("unreachable")


@workspace_exclusive
def build_semantic_index(
    settings: Settings,
    *,
    embed: Callable[[list[str]], list[list[float]]] | None = None,
    pack_id: str | None = None,
    progress: SemanticProgress | None = None,
    atlas_cards: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Build per-repository, per-snapshot USearch shards from an approved local pack.

    An injected embedder is reserved for tests and uses an exact JSON mock index;
    production indexing refuses to silently substitute a hash embedding for a pack.
    """
    started = time.perf_counter()
    if atlas_cards is None:
        atlas_cards = getattr(settings, "atlas_cards", None)
    progress_state: dict[str, object] = {}

    def emit(phase: str, **details: object) -> None:
        progress_state.update({key: value for key, value in details.items() if value is not None})
        if progress is not None:
            progress({
                "phase": phase,
                "phase_label": _SEMANTIC_PROGRESS_LABELS[phase],
                "elapsed_ms": max(0, round((time.perf_counter() - started) * 1000)),
                **progress_state,
            })

    runtime = None
    manifest = active_pack(settings, "embedding") if embed is None else None
    if embed is None and manifest is None:
        raise RuntimeError("Semantic indexing requires a verified local embedding pack")
    if embed is None:
        runtime = runtime_for_pack(manifest)
        pack_id = str(manifest["pack_id"])
        dimension = int(manifest.get("embedding_dimension") or 0)
        if dimension <= 0:
            raise RuntimeError("embedding pack must declare embedding_dimension")
        document_instruction = str(manifest.get("document_instruction") or "")
        input_suffix = str(manifest.get("input_suffix") or "")
        from .models import pack_compatibility_identity as pack_identity

        pack_compatibility_identity = pack_identity(manifest)
        embed = lambda cards: runtime.embed(cards, instruction=document_instruction, dimension=dimension)
    else:
        pack_id = pack_id or "mock"
        pack_compatibility_identity = _injected_pack_identity(pack_id)
        dimension = 0
        document_instruction = ""
        input_suffix = ""
    try:
        backend = _usearch() if manifest is not None else None
        if backend is None and manifest is not None:
            raise RuntimeError("USearch is required for Semantic Edition; install `project-brain-context[semantic]`")

        backend_name = "usearch" if backend is not None else "exact-mock"
        groups: list[tuple[Repository, str, list[Chunk]]] = []
        snapshots = {repo.name: str(repo.source_sha or "working-tree") for repo in settings.repositories}
        try:
            from .index import indexed_snapshot_source_projection

            source_projection = indexed_snapshot_source_projection(
                settings,
                snapshots,
                max_repositories=MAX_SEMANTIC_SOURCE_REPOSITORIES,
                max_items_per_repository=MAX_SEMANTIC_SOURCE_FILES_PER_REPOSITORY,
                max_items=MAX_SEMANTIC_SOURCE_FILES_TOTAL,
                max_bytes_per_repository=MAX_SEMANTIC_SOURCE_BYTES_PER_REPOSITORY,
                max_bytes=MAX_SEMANTIC_SOURCE_BYTES_TOTAL,
                max_file_bytes=3_000_000,
                max_seconds=MAX_SEMANTIC_SOURCE_SCAN_SECONDS,
            )
        except sqlite3.DataError as error:
            raise SemanticEmbeddingError(str(error)) from error
        except sqlite3.Error as error:
            if any(snapshot != "working-tree" for snapshot in snapshots.values()):
                raise SemanticEmbeddingError(
                    "Semantic source is unavailable from the authoritative lexical snapshot"
                ) from error
            # Legacy direct Semantic builds may still operate on an explicitly
            # unpinned working tree. Published Atlas generations are always
            # content-addressed before reaching this path.
            source_projection = None
        legacy_walk_budget = None
        if source_projection is None:
            from .index import _WalkBudget

            legacy_walk_budget = _WalkBudget(
                MAX_SEMANTIC_LEGACY_SCAN_ENTRIES,
                time.monotonic() + MAX_SEMANTIC_SOURCE_SCAN_SECONDS,
                MAX_SEMANTIC_LEGACY_SCAN_DEPTH,
            )
        card_total = 0
        metadata_total = 0
        semantic_repo_total = len(settings.repositories)
        if len(atlas_cards or []) > MAX_SEMANTIC_CHUNKS_TOTAL:
            raise SemanticEmbeddingError("Semantic Atlas cards exceed the global chunk limit")
        _, cards_by_repo = _partition_semantic_inputs(
            settings.repositories, None, atlas_cards,
        )
        emit("semantic_manifest", semantic_repository_current=0, semantic_repository_total=semantic_repo_total, semantic_cards_discovered=0, generation_state="checking")
        for position, repo in enumerate(settings.repositories, start=1):
            try:
                chunks = _chunk_groups(
                    repo, settings=settings,
                    source_projection=(
                        source_projection[repo.name] if source_projection is not None else None
                    ),
                    legacy_walk_budget=legacy_walk_budget,
                )
            except SemanticEmbeddingError:
                raise
            except RuntimeError as error:
                raise SemanticEmbeddingError(
                    "Semantic legacy source scan exceeded its bounded repository contract"
                ) from error
            for card in cards_by_repo[repo.name]:
                if len(chunks) >= MAX_SEMANTIC_CHUNKS_PER_REPOSITORY:
                    raise SemanticEmbeddingError("Semantic repository exceeds its chunk limit")
                level = str(card.get("level") or "entity")
                content = str(card.get("content") or "")
                chunk_id = str(card.get("card_id") or hashlib.sha256(content.encode()).hexdigest())
                metadata = card.get("metadata") if isinstance(card.get("metadata"), dict) else {}
                line_start = max(1, int(metadata.get("line_start") or 1))
                line_end = max(line_start, int(metadata.get("line_end") or line_start))
                target_id = str(card.get("target_id") or level)
                semantic_card = "\n".join([
                    f"Repository: {repo.name}",
                    f"Path: {str(card.get('path') or '')}",
                    "Language: Text",
                    f"Kind: atlas_{level}_card",
                    f"Symbol: {target_id}",
                    "Identifiers: atlas",
                    "Code:",
                    content,
                ])
                chunks.append(Chunk(
                    chunk_id, str(card.get("content_hash") or chunk_id), str(card.get("path") or ""), line_start, line_end,
                    f"atlas_{level}_card", target_id, semantic_card,
                    target_id or None,
                ))
            repo_metadata_bytes = sum(len(chunk.card.encode("utf-8")) for chunk in chunks)
            if repo_metadata_bytes > MAX_SEMANTIC_METADATA_BYTES_PER_REPOSITORY:
                raise SemanticEmbeddingError("Semantic repository exceeds its metadata byte limit")
            if card_total + len(chunks) > MAX_SEMANTIC_CHUNKS_TOTAL:
                raise SemanticEmbeddingError("Semantic input exceeds the global chunk limit")
            if metadata_total + repo_metadata_bytes > MAX_SEMANTIC_METADATA_BYTES_TOTAL:
                raise SemanticEmbeddingError("Semantic input exceeds the global metadata byte limit")
            groups.append((repo, repo.source_sha or "working-tree", chunks))
            card_total += len(chunks)
            metadata_total += repo_metadata_bytes
            emit(
                "semantic_manifest",
                semantic_repository_current=position,
                semantic_repository_total=semantic_repo_total,
                semantic_cards_discovered=card_total,
            )
        emit("semantic_manifest", semantic_cards_total=card_total)
        published = _published_state(settings)
        if not dimension and published and published.get("pack_id") == pack_id:
            try:
                dimension = int(published.get("dimension") or 0)
            except (TypeError, ValueError):
                dimension = 0
        if dimension and _state_is_reusable(
            settings, published, groups, backend=backend_name, pack_id=pack_id,
            pack_compatibility_identity=pack_compatibility_identity, dimension=dimension,
        ):
            shard_total = sum(bool(chunks) for _, _, chunks in groups)
            emit(
                "semantic_reuse",
                semantic_cards_discovered=card_total,
                semantic_cards_total=card_total,
                cached_embeddings_reused=card_total,
                new_embeddings_completed=0,
                remaining_embeddings=0,
                embedding_batch_size=0,
                embedding_batches_completed=0,
                semantic_shards_completed=shard_total,
                semantic_shards_total=shard_total,
                semantic_shards_reused=shard_total,
                semantic_shards_rebuilt=0,
                generation_state="reused",
            )
            return _state_result(published)

        state_shards: list[dict[str, object]] = []
        all_mock_entries: list[dict[str, object]] = []
        shard_root = _shard_root(settings)
        shard_root.mkdir(parents=True, exist_ok=True)
        generation_id = hashlib.sha256(f"{pack_id}\0{time.time_ns()}\0{os.getpid()}".encode()).hexdigest()
        created_shards: list[Path] = []
        emit("semantic_embedding", generation_state="rebuilding")
        shard_total = sum(bool(chunks) for _, _, chunks in groups)
        last_semantic_position = max((position for position, (_, _, chunks) in enumerate(groups, start=1) if chunks), default=0)
        completed_shards = 0
        reused_shards = 0
        rebuilt_shards = 0
        cached_total = 0
        embedded_total = 0
        batches_total = 0
        from .ops import ensure_write_capacity, remaining_write_capacity

        semantic_cache_capacity = [remaining_write_capacity(settings)]
        prior_shards = {
            (str(shard.get("repo") or ""), str(shard.get("snapshot") or "")): shard
            for shard in (published.get("shards") or [] if _state_is_compatible(
                published, backend=backend_name, pack_id=pack_id,
                pack_compatibility_identity=pack_compatibility_identity, dimension=dimension,
            ) else [])
            if (
                isinstance(shard, dict)
                and _valid_shard_manifest_entry(shard, root=_shard_root(settings).resolve())
                and _valid_shard_artifact(shard, root=_shard_root(settings))
            )
        }
        for position, (repo, snapshot, chunks) in enumerate(groups, start=1):
            if not chunks:
                continue
            entries = _entries(chunks)
            prior_shard = prior_shards.get((repo.name, snapshot))
            if backend is not None and prior_shard and prior_shard.get("entries") == entries:
                state_shards.append(dict(prior_shard))
                cached_total += len(chunks)
                completed_shards += 1
                reused_shards += 1
                emit(
                    "semantic_shard",
                    semantic_repository_current=position,
                    semantic_repository_total=semantic_repo_total,
                    cached_embeddings_reused=cached_total,
                    new_embeddings_completed=embedded_total,
                    semantic_shards_completed=completed_shards,
                    semantic_shards_total=shard_total,
                    semantic_shards_reused=reused_shards,
                    semantic_shards_rebuilt=rebuilt_shards,
                )
                continue
            if not dimension:
                dimension = _probe_embedding_dimension(
                    embed, chunks[0].card, document_instruction=document_instruction, input_suffix=input_suffix,
                    restart=runtime.shutdown if runtime is not None else None,
                )
            repo_progress = {"cached": 0, "embedded": 0, "batches": 0}

            def cache_progress(details: dict[str, object]) -> None:
                repo_progress["cached"] = int(details.get("cached_embeddings_reused") or 0)
                repo_progress["embedded"] = int(details.get("new_embeddings_completed") or 0)
                repo_progress["batches"] = int(details.get("embedding_batches_completed") or 0)
                values: dict[str, object] = {
                    "semantic_repository_current": position,
                    "semantic_repository_total": semantic_repo_total,
                    "semantic_cards_discovered": card_total,
                    "semantic_cards_total": card_total,
                    "cached_embeddings_reused": cached_total + repo_progress["cached"],
                    "new_embeddings_completed": embedded_total + repo_progress["embedded"],
                    "embedding_batch_size": int(details.get("embedding_batch_size") or 0),
                    "embedding_batches_completed": batches_total + repo_progress["batches"],
                }
                # The cache lookup is intentionally performed by the real per-repo
                # indexing loop. The exact global remainder becomes known once the
                # final repository's cache entries have been checked.
                if position == last_semantic_position:
                    values["remaining_embeddings"] = int(details.get("remaining_embeddings") or 0)
                emit("semantic_embedding", **values)

            repo_vectors = _cache_vectors(
                settings, pack_id, chunks, dimension=dimension, embed=embed, batch_size=embedding_batch_size(settings, pack_id),
                document_instruction=document_instruction, input_suffix=input_suffix,
                restart=runtime.shutdown if runtime is not None else None,
                progress=cache_progress,
                pack_compatibility_identity=pack_compatibility_identity,
                write_capacity=semantic_cache_capacity,
            )
            cached_total += repo_progress["cached"]
            embedded_total += repo_progress["embedded"]
            batches_total += repo_progress["batches"]
            if backend is None:
                all_mock_entries.extend([{**entry, "repo": repo.name, "snapshot": snapshot, "vector": vector} for entry, vector in zip(entries, repo_vectors, strict=True)])
            else:
                Index, numpy = backend
                index = Index(ndim=dimension, metric="cos", dtype="f16")
                index.add(numpy.arange(len(repo_vectors), dtype=numpy.uint64), numpy.asarray(repo_vectors, dtype=numpy.float32))
                # A shard is never overwritten in place.  The old state continues
                # to point at its immutable generation until every new shard has
                # been built and the state pointer is atomically replaced.
                shard_identity = f"{repo.name}\0{snapshot}\0{pack_id}\0{pack_compatibility_identity}\0{generation_id}"
                shard = shard_root / f"{hashlib.sha256(shard_identity.encode()).hexdigest()}.usearch"
                entry_bytes = len(json.dumps(entries, separators=(",", ":")).encode("utf-8"))
                projected_shard_bytes = max(
                    MIN_SEMANTIC_SHARD_RESERVATION_BYTES,
                    len(repo_vectors) * dimension * 4 + entry_bytes + MAX_SEMANTIC_SHARD_OVERHEAD_BYTES,
                )
                if projected_shard_bytes > semantic_cache_capacity[0]:
                    raise SemanticEmbeddingError(
                        "Semantic shard exceeds the remaining managed write capacity"
                    )
                semantic_cache_capacity[0] -= projected_shard_bytes
                try:
                    _atomic_index_save(index, shard)
                    created_shards.append(shard)
                    actual_shard_bytes = shard.stat().st_size
                    extra = actual_shard_bytes - projected_shard_bytes
                    if extra > semantic_cache_capacity[0]:
                        raise SemanticEmbeddingError(
                            "Semantic shard exceeded its bounded capacity reservation"
                        )
                    semantic_cache_capacity[0] -= max(0, extra)
                    semantic_cache_capacity[0] += max(0, -extra)
                except Exception:
                    shard.unlink(missing_ok=True)
                    raise
                state_shards.append({
                    "repo": repo.name,
                    "snapshot": snapshot,
                    "path": str(shard),
                    "artifact_ref": shard.name,
                    "artifact_bytes": shard.stat().st_size,
                    "artifact_sha256": _shard_sha256(shard),
                    "entries": entries,
                })
            completed_shards += 1
            rebuilt_shards += 1
            emit(
                "semantic_shard",
                semantic_repository_current=position,
                semantic_repository_total=semantic_repo_total,
                semantic_shards_completed=completed_shards,
                semantic_shards_total=shard_total,
                semantic_shards_reused=reused_shards,
                semantic_shards_rebuilt=rebuilt_shards,
            )

        state = {
            "generation": generation_id,
            "chunk_schema_version": CHUNK_SCHEMA_VERSION,
            "card_version": CARD_VERSION,
            "backend": "usearch" if backend is not None else "exact-mock",
            "pack_id": pack_id,
            "pack_compatibility_identity": pack_compatibility_identity,
            "dimension": dimension,
            "embedding_input_version": SEMANTIC_EMBEDDING_INPUT_VERSION,
            "atlas_card_version": ATLAS_CARD_VERSION,
            "shard_manifest_version": SEMANTIC_SHARD_MANIFEST_VERSION,
            "stale": False,
            "snapshots": {repo.name: snapshot for repo, snapshot, _ in groups},
            "shards": state_shards,
            "entries": all_mock_entries,
        }
        projected_state_bytes = len((json.dumps(state, indent=2) + "\n").encode("utf-8"))
        if projected_state_bytes > semantic_cache_capacity[0]:
            raise SemanticEmbeddingError("Semantic state exceeds the remaining managed write capacity")
        semantic_cache_capacity[0] -= projected_state_bytes
        ensure_write_capacity(settings, projected_state_bytes)
        _atomic_state_write(_state_path(settings), state)
        emit(
            "semantic_publish",
            semantic_shards_completed=completed_shards,
            semantic_shards_total=shard_total,
            cached_embeddings_reused=cached_total,
            new_embeddings_completed=embedded_total,
            semantic_shards_reused=reused_shards,
            semantic_shards_rebuilt=rebuilt_shards,
            generation_state="rebuilt",
        )
        return _state_result(state)
    except Exception:
        for shard in locals().get("created_shards", []):
            try:
                shard.unlink(missing_ok=True)
            except OSError:
                pass
        emit("semantic_embedding", generation_state="failed")
        raise
    finally:
        if runtime is not None:
            runtime.shutdown()


def _cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=False)) / ((math.sqrt(sum(a * a for a in left)) or 1) * (math.sqrt(sum(b * b for b in right)) or 1))


def _query_vector(
    settings: Settings, query: str, *, pack_id: str, dimension: int,
    embed: Callable[[list[str]], list[list[float]]], pack_compatibility_identity: str | None = None,
) -> list[float]:
    from .catalog import connect
    from datetime import UTC, datetime

    pack_compatibility_identity = pack_compatibility_identity or _injected_pack_identity(pack_id)
    key = "query:" + hashlib.sha256(
        f"{pack_id}\0{pack_compatibility_identity}\0{dimension}\0{SEMANTIC_EMBEDDING_INPUT_VERSION}\0{query}".encode("utf-8")
    ).hexdigest()
    used_at = datetime.now(UTC).isoformat()
    connection = connect(settings)
    try:
        row = connection.execute(
            "SELECT vector_json FROM embedding_cache WHERE cache_key=? AND pack_id=? AND dimension=?",
            (key, pack_id, dimension),
        ).fetchone()
        if row:
            try:
                cached_vector = json.loads(row[0])
                vector = valid_embedding_vector(cached_vector, dimension=dimension)
                if vector is not None:
                    connection.execute(
                        "UPDATE embedding_cache SET last_used_at=? WHERE cache_key=?", (used_at, key)
                    )
                    connection.commit()
                    return vector
            except (TypeError, ValueError, json.JSONDecodeError):
                connection.execute("DELETE FROM embedding_cache WHERE cache_key=?", (key,))

    finally:
        connection.close()

    computed = embed([query])
    normalized = valid_embedding_vector(computed[0], dimension=dimension) if len(computed) == 1 else None
    if normalized is None:
        raise RuntimeError("query embedding dimension does not match the active semantic index")
    payload = json.dumps(normalized, separators=(",", ":"))
    from .ops import remaining_write_capacity

    if len(payload.encode("utf-8")) > remaining_write_capacity(settings):
        raise SemanticEmbeddingError("query embedding cache exceeds the remaining managed write capacity")
    connection = connect(settings)
    try:
        _reserve_embedding_cache(connection, [(key, payload)])
        connection.execute(
            "INSERT OR REPLACE INTO embedding_cache(cache_key,pack_id,dimension,vector_json,created_at,last_used_at) "
            "VALUES (?,?,?,?,?,?)",
            (key, pack_id, dimension, payload, used_at, used_at),
        )
        connection.execute(
            "DELETE FROM embedding_cache WHERE cache_key LIKE ? AND cache_key NOT IN "
            "(SELECT cache_key FROM embedding_cache WHERE cache_key LIKE ? ORDER BY last_used_at DESC LIMIT 256)",
            ("query:%", "query:%"),
        )
        connection.commit()
    finally:
        connection.close()
    return normalized


def _serving_state(
    settings: Settings,
    generation: Any | None,
    *,
    require_active_pack: bool = True,
) -> dict[str, object] | None:
    if generation is None:
        if getattr(settings, "atlas_generation_mode", "current") == "legacy_source_pin":
            return None
        path = _state_path(settings)
        try:
            stat = path.stat()
        except OSError:
            return None
        snapshots = {repo.name: repo.source_sha or "working-tree" for repo in settings.repositories}
        cache_key = (
            str(path.resolve()), stat.st_mtime_ns, stat.st_size, "legacy", tuple(sorted(snapshots.items())),
        )
        if cache_key in _SERVING_STATE_CACHE:
            return _SERVING_STATE_CACHE[cache_key]
        value = _published_state(settings)
        valid, _ = semantic_state_compatibility(
            settings, value, snapshots, require_active_pack=require_active_pack,
            verify_artifacts=False,
        )
        if valid and value is not None:
            if len(_SERVING_STATE_CACHE) >= 64:
                _SERVING_STATE_CACHE.clear()
            _SERVING_STATE_CACHE[cache_key] = value
            return value
        return None
    component = generation.component("semantic")
    if component.get("status") != "ready" or not component.get("artifact_ref"):
        return None
    path = settings.state_dir / str(component["artifact_ref"])
    try:
        from .catalog import generation_root

        stat = path.lstat()
        if path.is_symlink():
            return None
        cache_key = (
            str(path.absolute()), stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns,
            stat.st_size, str(generation.identity),
            str(component.get("content_hash") or ""),
        )
        if cache_key in _SERVING_STATE_CACHE:
            return _SERVING_STATE_CACHE[cache_key]
        value = json.loads(read_managed_text(
            generation_root(settings), path, max_bytes=MAX_SEMANTIC_STATE_BYTES,
        ))
        if not isinstance(value, dict):
            return None
        valid, _ = semantic_state_compatibility(
            settings,
            value,
            generation.snapshots,
            component=component,
            require_active_pack=require_active_pack,
            verify_artifacts=False,
        )
        if valid:
            if len(_SERVING_STATE_CACHE) >= 64:
                _SERVING_STATE_CACHE.clear()
            _SERVING_STATE_CACHE[cache_key] = value
            return value
        return None
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def semantic_component_available(settings: Settings, generation: Any | None) -> bool:
    """Cheaply validate that a Semantic component can enter the serving path."""
    return _serving_state(settings, generation) is not None


def search_semantic(
    settings: Settings,
    query: str,
    *,
    repos: set[str] | None = None,
    limit: int = 40,
    embed: Callable[[list[str]], list[list[float]]] | None = None,
    trace: Any | None = None,
    generation: Any | None = None,
    serving_status: dict[str, str] | None = None,
) -> list[dict[str, object]]:
    state = _serving_state(settings, generation, require_active_pack=embed is None)
    if not state:
        if serving_status is not None:
            serving_status["status"] = "unavailable"
        return []
    if serving_status is not None:
        serving_status["status"] = "validating"
    if (
        state.get("stale")
        or state.get("chunk_schema_version") != CHUNK_SCHEMA_VERSION
        or state.get("card_version") != CARD_VERSION
        or state.get("embedding_input_version") != SEMANTIC_EMBEDDING_INPUT_VERSION
        or state.get("atlas_card_version") != ATLAS_CARD_VERSION
        or state.get("shard_manifest_version") != SEMANTIC_SHARD_MANIFEST_VERSION
    ):
        if serving_status is not None:
            serving_status["status"] = "unavailable"
        return []
    pack_id = str(state.get("pack_id") or "")
    pack_compatibility_identity = str(state.get("pack_compatibility_identity") or "")
    dimension = int(state.get("dimension") or 0)
    if not pack_id or dimension <= 0 or not re.fullmatch(r"sha256:[0-9a-f]{64}", pack_compatibility_identity):
        if serving_status is not None:
            serving_status["status"] = "unavailable"
        return []
    if trace is not None and trace.physical_budget_remaining <= 0:
        trace.stop_reason = "physical_budget"
        if serving_status is not None:
            serving_status["status"] = "degraded"
        return []
    semantic_started = time.perf_counter()
    runtime = None
    owns_runtime = embed is None
    lane = None

    def close_runtime() -> None:
        nonlocal runtime, lane
        if runtime is not None:
            runtime.shutdown()
            runtime = None
        if lane is not None:
            lane.__exit__(None, None, None)
            lane = None

    try:
        manifest = verified_pack(settings, pack_id, "embedding") if embed is None else None
        if embed is None:
            try:
                active_dimension = int((manifest or {}).get("embedding_dimension") or 0)
            except (TypeError, ValueError):
                active_dimension = 0
            if manifest is None or manifest.get("pack_id") != pack_id or active_dimension != dimension:
                if serving_status is not None:
                    serving_status["status"] = "unavailable"
                return []
            from .models import pack_compatibility_identity as pack_identity

            if pack_identity(manifest) != pack_compatibility_identity:
                if serving_status is not None:
                    serving_status["status"] = "unavailable"
                return []
            query_instruction = str(manifest.get("query_instruction") or "")

            def lazy_embed(values: list[str]) -> list[list[float]]:
                nonlocal runtime, lane
                lane_started = time.perf_counter()
                lane = model_lane(settings)
                lane.__enter__()
                if trace is not None:
                    trace.add_stage("model_lane_wait_ms", (time.perf_counter() - lane_started) * 1000)
                try:
                    runtime_started = time.perf_counter()
                    runtime = runtime_for_pack(manifest)
                    if trace is not None:
                        trace.add_stage("embedding_runtime_start_ms", (time.perf_counter() - runtime_started) * 1000)
                    return runtime.embed(values, instruction=query_instruction, dimension=dimension)
                except Exception:
                    close_runtime()
                    raise

            embed = lazy_embed
        embedding_started = time.perf_counter()
        vector = _query_vector(
            settings, query, pack_id=pack_id, dimension=dimension, embed=embed,
            pack_compatibility_identity=pack_compatibility_identity,
        )
        if trace is not None:
            elapsed = (time.perf_counter() - embedding_started) * 1000
            trace.add_stage("semantic_query_embedding_ms", elapsed)
            trace.add_stage("embedding_inference_ms", elapsed)
        # Query vectors are plain data. Do not hold the cross-process model lane
        # while independent USearch shards restore and search.
        close_runtime()
        snapshots = generation.snapshots if generation is not None else {
            repo.name: repo.source_sha or "working-tree" for repo in settings.repositories
        }
        if state.get("backend") == "exact-mock":
            backend_started = time.perf_counter()
            scored = [
                (float(_cosine(vector, list(item["vector"]))), item)
                for item in state.get("entries") or []
                if isinstance(item, dict) and (not repos or item.get("repo") in repos) and snapshots.get(str(item.get("repo"))) == item.get("snapshot")
            ]
            if trace is not None:
                elapsed = (time.perf_counter() - backend_started) * 1000
                trace.add_backend("semantic-exact", elapsed, raw_hits=len(scored))
                trace.add_stage("semantic_shard_search_ms", elapsed)
            if serving_status is not None:
                serving_status["status"] = "ready"
            return [{key: value for key, value in item.items() if key != "vector"} | {"score": score} for score, item in sorted(scored, key=lambda pair: (-pair[0], str(pair[1].get("chunk_id"))))[:limit]]
        backend = _usearch()
        if backend is None:
            if serving_status is not None:
                serving_status["status"] = "unavailable"
            return []
        Index, numpy = backend
        eligible = [
            shard for shard in state.get("shards") or []
            if isinstance(shard, dict)
            and (not repos or shard.get("repo") in repos)
            and snapshots.get(str(shard.get("repo"))) == shard.get("snapshot")
        ]
        if trace is not None and len(eligible) > trace.physical_budget_remaining:
            eligible = eligible[:trace.physical_budget_remaining]
            trace.stop_reason = "physical_budget"
        if not eligible:
            if serving_status is not None:
                serving_status["status"] = "unavailable"
            return []

        def search_shard(shard: dict[str, object]) -> tuple[list[dict[str, object]], str]:
            backend_started = time.perf_counter()
            path = Path(str(shard.get("path") or ""))
            if not _valid_shard_artifact(shard, root=_shard_root(settings)):
                if trace is not None:
                    trace.add_backend("semantic-shard", (time.perf_counter() - backend_started) * 1000)
                return [], "missing"
            try:
                index = Index.restore(str(path), view=True)
                matches = index.search(numpy.asarray(vector, dtype=numpy.float32), limit)
            except Exception:
                # A corrupt optional shard cannot invalidate Core or a healthy shard
                # from another repository/snapshot.
                if trace is not None:
                    trace.add_backend("semantic-shard", (time.perf_counter() - backend_started) * 1000)
                return [], "corrupt"
            rows: list[dict[str, object]] = []
            for match in matches:
                key = int(match.key)
                entries = shard.get("entries") or []
                if 0 <= key < len(entries) and isinstance(entries[key], dict):
                    rows.append({"repo": shard["repo"], "snapshot": shard["snapshot"], **entries[key], "score": 1 - float(match.distance)})
            if trace is not None:
                trace.add_backend("semantic-shard", (time.perf_counter() - backend_started) * 1000, raw_hits=len(rows))
            return rows, "ready"

        shard_search_started = time.perf_counter()
        results: list[dict[str, object]] = []
        shard_statuses: list[str] = []
        for offset in range(0, len(eligible), settings.semantic_shard_workers):
            futures = [
                _SHARD_EXECUTOR.submit(search_shard, shard)
                for shard in eligible[offset:offset + settings.semantic_shard_workers]
            ]
            for future in futures:
                rows, shard_status = future.result()
                results.extend(rows)
                shard_statuses.append(shard_status)
        if trace is not None:
            trace.add_stage("semantic_shard_search_ms", (time.perf_counter() - shard_search_started) * 1000)
        healthy = shard_statuses.count("ready")
        if serving_status is not None:
            serving_status["status"] = (
                "ready" if healthy == len(shard_statuses)
                else "degraded" if healthy
                else "unavailable"
            )
        return sorted(results, key=lambda item: (-float(item["score"]), str(item["repo"]), str(item["chunk_id"])))[:limit]
    finally:
        close_runtime()
        if trace is not None:
            trace.add_stage("semantic_total_ms", (time.perf_counter() - semantic_started) * 1000)

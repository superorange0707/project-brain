from __future__ import annotations

import hashlib
from http.client import HTTPException
import json
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable
from urllib.error import URLError

from .models import active_pack, embedding_batch_size, embedding_request_bytes, runtime_for_pack
from .locks import MODEL_LANE, workspace_exclusive

if TYPE_CHECKING:
    from .core import Repository, Settings

CHUNK_SCHEMA_VERSION = "1"
CARD_VERSION = "1"
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
SEMANTIC_EMBEDDING_INPUT_VERSION = "2"
SYMBOL = re.compile(r"(?m)^\s*(?:class|interface|record|enum|def|function|fun|func)\s+([A-Za-z_$][\w$]*)")
_SHARD_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="brain-semantic-shard")


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


class SemanticEmbeddingError(RuntimeError):
    """A sanitized semantic-indexing failure safe for CLI diagnostics."""


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
    markers = [(match.start() and content[:match.start()].count("\n") + 1 or 1, match.group(1)) for match in SYMBOL.finditer(content)]
    if not markers:
        markers = [(1, "file")]
    chunks: list[Chunk] = []
    for index, (start, symbol) in enumerate(markers):
        end = markers[index + 1][0] - 1 if index + 1 < len(markers) else len(lines)
        for child_start in range(start, max(end, start) + 1, SEMANTIC_CHILD_LINES):
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
    return settings.state_dir / "semantic-shards"


def _query_cache_path(settings: Settings) -> Path:
    return settings.state_dir / "semantic-query-cache.json"


def _files(repo: Repository) -> Iterable[Path]:
    ignored = {".git", ".venv", "node_modules", "target", "build", "dist", "vendor", "generated"}
    for root, dirs, names in os.walk(repo.scan_path):
        dirs[:] = [name for name in dirs if name not in ignored]
        for name in names:
            yield Path(root) / name


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
    keys = [
        hashlib.sha256(
            f"{pack_id}\0{dimension}\0{SEMANTIC_EMBEDDING_INPUT_VERSION}\0{document_instruction}\0{card}\0{input_suffix}".encode("utf-8")
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
        for index, key in enumerate(keys):
            row = connection.execute("SELECT vector_json FROM embedding_cache WHERE cache_key=?", (key,)).fetchone()
            if row:
                try:
                    value = json.loads(row[0])
                    if isinstance(value, list) and len(value) == dimension:
                        vectors[index] = [float(number) for number in value]
                        connection.execute("UPDATE embedding_cache SET last_used_at=? WHERE cache_key=?", (used_at, key))
                        cached += 1
                        continue
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            missing.append(index)
        report(remaining=len(missing))
        if missing:
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
                    if len(computed) != len(current) or any(len(vector) != dimension for vector in computed):
                        raise RuntimeError("embedding runtime returned an unexpected vector dimension")
                    for index, vector in zip(current, computed, strict=True):
                        normalized = [float(number) for number in vector]
                        vectors[index] = normalized
                        connection.execute(
                            "INSERT OR REPLACE INTO embedding_cache(cache_key, pack_id, dimension, vector_json, created_at, last_used_at) VALUES (?, ?, ?, ?, ?, ?)",
                            (keys[index], pack_id, dimension, json.dumps(normalized, separators=(",", ":")), used_at, used_at),
                        )
                    completed += len(current)
                    completed_batches += 1
                    # Successful sub-batches survive a later transport failure;
                    # vector cache entries are content-addressed and are not a
                    # semantic-index publication.
                    connection.commit()
                    report(remaining=len(missing) - completed)
            connection.execute(
                "DELETE FROM embedding_cache WHERE cache_key NOT IN (SELECT cache_key FROM embedding_cache ORDER BY last_used_at DESC, cache_key DESC LIMIT 100000)"
            )
            connection.commit()
    finally:
        connection.close()
    return [vector for vector in vectors if vector is not None]


def _chunk_groups(repo: Repository) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in _files(repo):
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if _excluded(path, raw):
            continue
        relative = str(path.relative_to(repo.scan_path)).replace(os.sep, "/")
        blob = hashlib.sha256(raw).hexdigest()
        chunks.extend(chunk_source(repo.name, relative, raw.decode("utf-8", errors="replace"), blob_sha=blob))
    return chunks


def _entries(chunks: list[Chunk]) -> list[dict[str, object]]:
    return [
        {"path": chunk.path, "line": chunk.start_line, "end_line": chunk.end_line, "chunk_id": chunk.chunk_id, "kind": chunk.kind, "symbol": chunk.symbol}
        for chunk in chunks
    ]


def _published_state(settings: Settings) -> dict[str, object] | None:
    try:
        state = json.loads(_state_path(settings).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return state if isinstance(state, dict) else None


def _state_is_reusable(
    state: dict[str, object] | None,
    groups: list[tuple[Repository, str, list[Chunk]]],
    *,
    backend: str,
    pack_id: str,
    dimension: int,
) -> bool:
    if (
        not state
        or state.get("stale")
        or state.get("chunk_schema_version") != CHUNK_SCHEMA_VERSION
        or state.get("card_version") != CARD_VERSION
        or state.get("embedding_input_version") != SEMANTIC_EMBEDDING_INPUT_VERSION
    ):
        return False
    if state.get("backend") != backend or state.get("pack_id") != pack_id or int(state.get("dimension") or 0) != dimension:
        return False
    expected = {(repo.name, snapshot): _entries(chunks) for repo, snapshot, chunks in groups if chunks}
    if backend == "exact-mock":
        actual: dict[tuple[str, str], list[dict[str, object]]] = {}
        for item in state.get("entries") or []:
            if not isinstance(item, dict):
                return False
            repo, snapshot = str(item.get("repo") or ""), str(item.get("snapshot") or "")
            entry = {key: item.get(key) for key in ("path", "line", "end_line", "chunk_id", "kind", "symbol")}
            actual.setdefault((repo, snapshot), []).append(entry)
        return actual == expected
    actual = {}
    for shard in state.get("shards") or []:
        if not isinstance(shard, dict) or not Path(str(shard.get("path") or "")).is_file():
            return False
        actual[(str(shard.get("repo") or ""), str(shard.get("snapshot") or ""))] = shard.get("entries")
    return actual == expected


def _state_result(state: dict[str, object]) -> dict[str, object]:
    return {
        "chunks": len(state.get("entries") or []) + sum(len(shard.get("entries") or []) for shard in state.get("shards") or [] if isinstance(shard, dict)),
        "backend": state.get("backend"),
        "pack_id": state.get("pack_id"),
        "stale": False,
    }


def _atomic_state_write(path: Path, state: dict[str, object]) -> None:
    """Publish one complete semantic generation without exposing a partial state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.building")
    try:
        temporary.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
        temporary.replace(path)
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
) -> dict[str, object]:
    """Build per-repository, per-snapshot USearch shards from an approved local pack.

    An injected embedder is reserved for tests and uses an exact JSON mock index;
    production indexing refuses to silently substitute a hash embedding for a pack.
    """
    started = time.perf_counter()
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
        embed = lambda cards: runtime.embed(cards, instruction=document_instruction, dimension=dimension)
    else:
        pack_id = pack_id or "mock"
        dimension = 0
        document_instruction = ""
        input_suffix = ""
    try:
        backend = _usearch() if manifest is not None else None
        if backend is None and manifest is not None:
            raise RuntimeError("USearch is required for Semantic Edition; install `project-brain-context[semantic]`")

        backend_name = "usearch" if backend is not None else "exact-mock"
        groups: list[tuple[Repository, str, list[Chunk]]] = []
        card_total = 0
        semantic_repo_total = len(settings.repositories)
        emit("semantic_manifest", semantic_repository_current=0, semantic_repository_total=semantic_repo_total, semantic_cards_discovered=0, generation_state="checking")
        for position, repo in enumerate(settings.repositories, start=1):
            chunks = _chunk_groups(repo)
            groups.append((repo, repo.source_sha or "working-tree", chunks))
            card_total += len(chunks)
            emit(
                "semantic_manifest",
                semantic_repository_current=position,
                semantic_repository_total=semantic_repo_total,
                semantic_cards_discovered=card_total,
            )
        emit("semantic_manifest", semantic_cards_total=card_total)
        published = _published_state(settings)
        if not dimension and published and published.get("pack_id") == pack_id:
            dimension = int(published.get("dimension") or 0)
        if dimension and _state_is_reusable(
            published, groups, backend=backend_name, pack_id=pack_id, dimension=dimension,
        ):
            shard_total = sum(bool(chunks) for _, _, chunks in groups)
            emit(
                "semantic_reuse",
                semantic_cards_discovered=card_total,
                semantic_cards_total=card_total,
                cached_embeddings_reused=0,
                new_embeddings_completed=0,
                remaining_embeddings=0,
                embedding_batch_size=0,
                embedding_batches_completed=0,
                semantic_shards_completed=shard_total,
                semantic_shards_total=shard_total,
                generation_state="reused",
            )
            return _state_result(published)

        state_shards: list[dict[str, object]] = []
        all_mock_entries: list[dict[str, object]] = []
        shard_root = _shard_root(settings)
        shard_root.mkdir(parents=True, exist_ok=True)
        generation_id = hashlib.sha256(f"{pack_id}\0{time.time_ns()}\0{os.getpid()}".encode()).hexdigest()
        emit("semantic_embedding", generation_state="rebuilding")
        shard_total = sum(bool(chunks) for _, _, chunks in groups)
        last_semantic_position = max((position for position, (_, _, chunks) in enumerate(groups, start=1) if chunks), default=0)
        completed_shards = 0
        cached_total = 0
        embedded_total = 0
        batches_total = 0
        for position, (repo, snapshot, chunks) in enumerate(groups, start=1):
            if not chunks:
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
            )
            cached_total += repo_progress["cached"]
            embedded_total += repo_progress["embedded"]
            batches_total += repo_progress["batches"]
            entries = _entries(chunks)
            if backend is None:
                all_mock_entries.extend([{**entry, "repo": repo.name, "snapshot": snapshot, "vector": vector} for entry, vector in zip(entries, repo_vectors, strict=True)])
            else:
                Index, numpy = backend
                index = Index(ndim=dimension, metric="cos", dtype="f16")
                index.add(numpy.arange(len(repo_vectors), dtype=numpy.uint64), numpy.asarray(repo_vectors, dtype=numpy.float32))
                # A shard is never overwritten in place.  The old state continues
                # to point at its immutable generation until every new shard has
                # been built and the state pointer is atomically replaced.
                shard_identity = f"{repo.name}\0{snapshot}\0{pack_id}\0{generation_id}"
                shard = shard_root / f"{hashlib.sha256(shard_identity.encode()).hexdigest()}.usearch"
                temporary = shard.with_suffix(".building")
                index.save(str(temporary))
                temporary.replace(shard)
                state_shards.append({"repo": repo.name, "snapshot": snapshot, "path": str(shard), "entries": entries})
            completed_shards += 1
            emit(
                "semantic_shard",
                semantic_repository_current=position,
                semantic_repository_total=semantic_repo_total,
                semantic_shards_completed=completed_shards,
                semantic_shards_total=shard_total,
            )

        state = {
            "generation": generation_id,
            "chunk_schema_version": CHUNK_SCHEMA_VERSION,
            "card_version": CARD_VERSION,
            "backend": "usearch" if backend is not None else "exact-mock",
            "pack_id": pack_id,
            "dimension": dimension,
            "embedding_input_version": SEMANTIC_EMBEDDING_INPUT_VERSION,
            "stale": False,
            "shards": state_shards,
            "entries": all_mock_entries,
        }
        _atomic_state_write(_state_path(settings), state)
        emit(
            "semantic_publish",
            semantic_shards_completed=completed_shards,
            semantic_shards_total=shard_total,
            generation_state="rebuilt",
        )
        # Only the current snapshot can be queried.  Old immutable shards have no
        # session dependency after their evidence was materialized, so trim them.
        keep = {Path(str(shard["path"])).resolve() for shard in state_shards}
        for stale in shard_root.glob("*.usearch"):
            if stale.resolve() not in keep:
                stale.unlink(missing_ok=True)
        return _state_result(state)
    except Exception:
        emit("semantic_embedding", generation_state="failed")
        raise
    finally:
        if runtime is not None:
            runtime.shutdown()


def _cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=False)) / ((math.sqrt(sum(a * a for a in left)) or 1) * (math.sqrt(sum(b * b for b in right)) or 1))


def _query_vector(settings: Settings, query: str, *, pack_id: str, dimension: int, embed: Callable[[list[str]], list[list[float]]]) -> list[float]:
    key = hashlib.sha256(f"{pack_id}\0{dimension}\0{query}".encode("utf-8")).hexdigest()
    try:
        cache = json.loads(_query_cache_path(settings).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cache = {}
    cached = cache.get(key)
    cached_vector = cached.get("vector") if isinstance(cached, dict) else cached
    if isinstance(cached_vector, list) and len(cached_vector) == dimension:
        cache[key] = {"vector": cached_vector, "used_at": time.time()}
        _query_cache_path(settings).write_text(json.dumps(cache, separators=(",", ":")), encoding="utf-8")
        return [float(number) for number in cached_vector]
    vector = embed([query])[0]
    if len(vector) != dimension:
        raise RuntimeError("query embedding dimension does not match the active semantic index")
    cache[key] = {"vector": vector, "used_at": time.time()}
    retained = sorted(
        ((str(cache_key), value) for cache_key, value in cache.items() if isinstance(value, (dict, list))),
        key=lambda item: float(item[1].get("used_at", 0)) if isinstance(item[1], dict) else 0,
    )[-256:]
    _query_cache_path(settings).write_text(json.dumps(dict(retained), separators=(",", ":")), encoding="utf-8")
    return [float(number) for number in vector]


def search_semantic(
    settings: Settings,
    query: str,
    *,
    repos: set[str] | None = None,
    limit: int = 40,
    embed: Callable[[list[str]], list[list[float]]] | None = None,
    trace: Any | None = None,
) -> list[dict[str, object]]:
    try:
        state = json.loads(_state_path(settings).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if state.get("stale") or state.get("chunk_schema_version") != CHUNK_SCHEMA_VERSION or state.get("card_version") != CARD_VERSION:
        return []
    pack_id = str(state.get("pack_id") or "")
    dimension = int(state.get("dimension") or 0)
    if not pack_id or dimension <= 0:
        return []
    if trace is not None and trace.physical_budget_remaining <= 0:
        trace.stop_reason = "physical_budget"
        return []
    semantic_started = time.perf_counter()
    runtime = None
    owns_runtime = embed is None
    if owns_runtime:
        lane_started = time.perf_counter()
        MODEL_LANE.acquire()
        if trace is not None:
            trace.add_stage("model_lane_wait_ms", (time.perf_counter() - lane_started) * 1000)
    try:
        manifest = active_pack(settings, "embedding") if embed is None else None
        if embed is None:
            if manifest is None or manifest.get("pack_id") != pack_id:
                return []
            runtime_started = time.perf_counter()
            runtime = runtime_for_pack(manifest)
            if trace is not None:
                trace.add_stage("embedding_runtime_start_ms", (time.perf_counter() - runtime_started) * 1000)
            query_instruction = str(manifest.get("query_instruction") or "")
            embed = lambda values: runtime.embed(values, instruction=query_instruction, dimension=dimension)
        embedding_started = time.perf_counter()
        vector = _query_vector(settings, query, pack_id=pack_id, dimension=dimension, embed=embed)
        if trace is not None:
            elapsed = (time.perf_counter() - embedding_started) * 1000
            trace.add_stage("semantic_query_embedding_ms", elapsed)
            trace.add_stage("embedding_inference_ms", elapsed)
        snapshots = {repo.name: repo.source_sha or "working-tree" for repo in settings.repositories}
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
            return [{key: value for key, value in item.items() if key != "vector"} | {"score": score} for score, item in sorted(scored, key=lambda pair: (-pair[0], str(pair[1].get("chunk_id"))))[:limit]]
        backend = _usearch()
        if backend is None:
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

        def search_shard(shard: dict[str, object]) -> list[dict[str, object]]:
            backend_started = time.perf_counter()
            path = Path(str(shard.get("path") or ""))
            if not path.is_file():
                if trace is not None:
                    trace.add_backend("semantic-shard", (time.perf_counter() - backend_started) * 1000)
                return []
            try:
                index = Index.restore(str(path), view=True)
                matches = index.search(numpy.asarray(vector, dtype=numpy.float32), limit)
            except Exception:
                # A corrupt optional shard cannot invalidate Core or a healthy shard
                # from another repository/snapshot.
                if trace is not None:
                    trace.add_backend("semantic-shard", (time.perf_counter() - backend_started) * 1000)
                return []
            rows: list[dict[str, object]] = []
            for match in matches:
                key = int(match.key)
                entries = shard.get("entries") or []
                if 0 <= key < len(entries) and isinstance(entries[key], dict):
                    rows.append({"repo": shard["repo"], "snapshot": shard["snapshot"], **entries[key], "score": 1 - float(match.distance)})
            if trace is not None:
                trace.add_backend("semantic-shard", (time.perf_counter() - backend_started) * 1000, raw_hits=len(rows))
            return rows

        shard_search_started = time.perf_counter()
        results: list[dict[str, object]] = []
        for offset in range(0, len(eligible), settings.semantic_shard_workers):
            futures = [
                _SHARD_EXECUTOR.submit(search_shard, shard)
                for shard in eligible[offset:offset + settings.semantic_shard_workers]
            ]
            results.extend(item for future in futures for item in future.result())
        if trace is not None:
            trace.add_stage("semantic_shard_search_ms", (time.perf_counter() - shard_search_started) * 1000)
        return sorted(results, key=lambda item: (-float(item["score"]), str(item["repo"]), str(item["chunk_id"])))[:limit]
    finally:
        if runtime is not None:
            runtime.shutdown()
        if owns_runtime:
            MODEL_LANE.release()
        if trace is not None:
            trace.add_stage("semantic_total_ms", (time.perf_counter() - semantic_started) * 1000)

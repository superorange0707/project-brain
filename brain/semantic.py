from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable

from .models import active_pack, embedding_batch_size, runtime_for_pack

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
SEMANTIC_EMBEDDING_BATCH_CHARS = 4_096
SYMBOL = re.compile(r"(?m)^\s*(?:class|interface|record|enum|def|function|fun|func)\s+([A-Za-z_$][\w$]*)")


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


def _bounded_embedding_batches(chunks: list[Chunk], indexes: list[int], batch_size: int) -> Iterable[list[int]]:
    """Keep model requests bounded by both candidate count and card input size."""
    cursor = 0
    while cursor < len(indexes):
        batch: list[int] = []
        chars = 0
        while cursor < len(indexes) and len(batch) < batch_size:
            index = indexes[cursor]
            card_chars = len(chunks[index].card)
            if batch and chars + card_chars > SEMANTIC_EMBEDDING_BATCH_CHARS:
                break
            batch.append(index)
            chars += card_chars
            cursor += 1
        yield batch


def _cache_vectors(settings: Settings, pack_id: str, chunks: list[Chunk], *, dimension: int, embed: Callable[[list[str]], list[list[float]]], batch_size: int = 0) -> list[list[float]]:
    """Reuse vectors by stable chunk identity without persisting query/source text."""
    from .catalog import connect

    keys = [hashlib.sha256(f"{pack_id}\0{dimension}\0{chunk.chunk_id}".encode("utf-8")).hexdigest() for chunk in chunks]
    vectors: list[list[float] | None] = [None] * len(chunks)
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
                        continue
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            missing.append(index)
        if missing:
            batch_size = max(1, batch_size or len(missing))
            for batch in _bounded_embedding_batches(chunks, missing, batch_size):
                computed = embed([chunks[index].card for index in batch])
                if len(computed) != len(batch) or any(len(vector) != dimension for vector in computed):
                    raise RuntimeError("embedding runtime returned an unexpected vector dimension")
                for index, vector in zip(batch, computed, strict=True):
                    normalized = [float(number) for number in vector]
                    vectors[index] = normalized
                    connection.execute(
                        "INSERT OR REPLACE INTO embedding_cache(cache_key, pack_id, dimension, vector_json, created_at, last_used_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (keys[index], pack_id, dimension, json.dumps(normalized, separators=(",", ":")), used_at, used_at),
                    )
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


def build_semantic_index(
    settings: Settings,
    *,
    embed: Callable[[list[str]], list[list[float]]] | None = None,
    pack_id: str | None = None,
) -> dict[str, object]:
    """Build per-repository, per-snapshot USearch shards from an approved local pack.

    An injected embedder is reserved for tests and uses an exact JSON mock index;
    production indexing refuses to silently substitute a hash embedding for a pack.
    """
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
        embed = lambda cards: runtime.embed(cards, instruction=document_instruction, dimension=dimension)
    else:
        pack_id = pack_id or "mock"
        dimension = 0
    try:
        backend = _usearch() if manifest is not None else None
        if backend is None and manifest is not None:
            raise RuntimeError("USearch is required for Semantic Edition; install `project-brain-context[semantic]`")

        state_shards: list[dict[str, object]] = []
        all_mock_entries: list[dict[str, object]] = []
        shard_root = _shard_root(settings)
        shard_root.mkdir(parents=True, exist_ok=True)
        for repo in settings.repositories:
            snapshot = repo.source_sha or "working-tree"
            chunks = _chunk_groups(repo)
            if not chunks:
                continue
            if not dimension:
                probe = embed([chunks[0].card])
                if len(probe) != 1 or not probe[0]:
                    raise RuntimeError("mock embedding returned no vector")
                dimension = len(probe[0])
                vectors = _cache_vectors(settings, pack_id, chunks, dimension=dimension, embed=embed, batch_size=embedding_batch_size(settings, pack_id))
            else:
                vectors = _cache_vectors(settings, pack_id, chunks, dimension=dimension, embed=embed, batch_size=embedding_batch_size(settings, pack_id))
            entries = [
                {"path": chunk.path, "line": chunk.start_line, "end_line": chunk.end_line, "chunk_id": chunk.chunk_id, "kind": chunk.kind, "symbol": chunk.symbol}
                for chunk in chunks
            ]
            if backend is None:
                all_mock_entries.extend([{**entry, "repo": repo.name, "snapshot": snapshot, "vector": vector} for entry, vector in zip(entries, vectors, strict=True)])
                continue
            Index, numpy = backend
            index = Index(ndim=dimension, metric="cos", dtype="f16")
            index.add(numpy.arange(len(vectors), dtype=numpy.uint64), numpy.asarray(vectors, dtype=numpy.float32))
            shard_identity = f"{repo.name}\0{snapshot}\0{pack_id}"
            shard = shard_root / f"{hashlib.sha256(shard_identity.encode()).hexdigest()}.usearch"
            temporary = shard.with_suffix(".building")
            index.save(str(temporary))
            temporary.replace(shard)
            state_shards.append({"repo": repo.name, "snapshot": snapshot, "path": str(shard), "entries": entries})

        state = {
            "chunk_schema_version": CHUNK_SCHEMA_VERSION,
            "card_version": CARD_VERSION,
            "backend": "usearch" if backend is not None else "exact-mock",
            "pack_id": pack_id,
            "dimension": dimension,
            "stale": False,
            "shards": state_shards,
            "entries": all_mock_entries,
        }
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        _state_path(settings).write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
        # Only the current snapshot can be queried.  Old immutable shards have no
        # session dependency after their evidence was materialized, so trim them.
        keep = {Path(str(shard["path"])).resolve() for shard in state_shards}
        for stale in shard_root.glob("*.usearch"):
            if stale.resolve() not in keep:
                stale.unlink(missing_ok=True)
        return {"chunks": sum(len(shard["entries"]) for shard in state_shards) + len(all_mock_entries), "backend": state["backend"], "pack_id": pack_id, "stale": False}
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


def search_semantic(settings: Settings, query: str, *, repos: set[str] | None = None, limit: int = 40, embed: Callable[[list[str]], list[list[float]]] | None = None) -> list[dict[str, object]]:
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
    runtime = None
    manifest = active_pack(settings, "embedding") if embed is None else None
    if embed is None:
        if manifest is None or manifest.get("pack_id") != pack_id:
            return []
        runtime = runtime_for_pack(manifest)
        query_instruction = str(manifest.get("query_instruction") or "")
        embed = lambda values: runtime.embed(values, instruction=query_instruction, dimension=dimension)
    try:
        vector = _query_vector(settings, query, pack_id=pack_id, dimension=dimension, embed=embed)
        snapshots = {repo.name: repo.source_sha or "working-tree" for repo in settings.repositories}
        if state.get("backend") == "exact-mock":
            scored = [
                (float(_cosine(vector, list(item["vector"]))), item)
                for item in state.get("entries") or []
                if isinstance(item, dict) and (not repos or item.get("repo") in repos) and snapshots.get(str(item.get("repo"))) == item.get("snapshot")
            ]
            return [{key: value for key, value in item.items() if key != "vector"} | {"score": score} for score, item in sorted(scored, key=lambda pair: (-pair[0], str(pair[1].get("chunk_id"))))[:limit]]
        backend = _usearch()
        if backend is None:
            return []
        Index, numpy = backend
        results: list[dict[str, object]] = []
        for shard in state.get("shards") or []:
            if not isinstance(shard, dict) or (repos and shard.get("repo") not in repos) or snapshots.get(str(shard.get("repo"))) != shard.get("snapshot"):
                continue
            path = Path(str(shard.get("path") or ""))
            if not path.is_file():
                continue
            try:
                index = Index.restore(str(path), view=True)
                matches = index.search(numpy.asarray(vector, dtype=numpy.float32), limit)
            except Exception:
                # A corrupt optional shard cannot invalidate Core or a healthy shard
                # from another repository/snapshot.
                continue
            for match in matches:
                key = int(match.key)
                entries = shard.get("entries") or []
                if 0 <= key < len(entries) and isinstance(entries[key], dict):
                    results.append({"repo": shard["repo"], "snapshot": shard["snapshot"], **entries[key], "score": 1 - float(match.distance)})
        return sorted(results, key=lambda item: (-float(item["score"]), str(item["repo"]), str(item["chunk_id"])))[:limit]
    finally:
        if runtime is not None:
            runtime.shutdown()

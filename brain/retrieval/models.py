from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter, process_time
from typing import Any, Protocol, Sequence


@dataclass(frozen=True)
class SnapshotIdentity:
    repo: str
    ref: str | None
    commit_sha: str | None

    @property
    def key(self) -> str:
        return f"{self.repo}:{self.ref or 'working-tree'}:{self.commit_sha or 'unknown'}"


@dataclass(frozen=True)
class IndexGeneration:
    number: int
    created_at: str
    snapshots: tuple[SnapshotIdentity, ...] = ()
    backends: tuple[str, ...] = ()


@dataclass
class Candidate:
    candidate_id: str
    repo: str
    path: str
    line: int
    kind: str = "search"
    score: float = 0.0
    found_by: list[str] = field(default_factory=list)
    protected: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvidenceRegion:
    repo: str
    path: str
    line_start: int
    line_end: int
    content: str
    kind: str
    score: float
    found_by: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class QueryOperation:
    kind: str
    value: str
    repos: tuple[str, ...] = ()
    tier: int = 1
    protected: bool = False
    estimated_cost: int = 1
    reason: str = "requested"


@dataclass(frozen=True)
class QueryPlan:
    objective: str
    operations: tuple[QueryOperation, ...]
    timeout_ms: int = 10_000
    stop_reason: str = "all requested operations evaluated"


@dataclass
class RetrievalTrace:
    """Serializable operation accounting; never stores source or full queries."""

    started: float = field(default_factory=perf_counter, repr=False)
    started_cpu: float = field(default_factory=process_time, repr=False)
    operation_count: int = 0
    subprocess_count: int = 0
    bytes_scanned: int = 0
    bytes_read: int = 0
    files_visited: int = 0
    raw_hits: int = 0
    unique_candidates: int = 0
    hydrated_regions: int = 0
    context_chars: int = 0
    cache_hits: int = 0
    backend_ms: dict[str, float] = field(default_factory=dict)
    fallback_reasons: list[str] = field(default_factory=list)

    def add_backend(self, name: str, elapsed_ms: float, *, subprocesses: int = 0, bytes_scanned: int = 0, files: int = 0, raw_hits: int = 0, cache_hit: bool = False) -> None:
        self.operation_count += 1
        self.subprocess_count += subprocesses
        self.bytes_scanned += bytes_scanned
        self.files_visited += files
        self.raw_hits += raw_hits
        self.backend_ms[name] = round(self.backend_ms.get(name, 0.0) + elapsed_ms, 3)
        self.cache_hits += int(cache_hit)

    def as_dict(self) -> dict[str, Any]:
        return {
            "wall_ms": round((perf_counter() - self.started) * 1000, 3),
            "cpu_ms": round((process_time() - self.started_cpu) * 1000, 3),
            "operation_count": self.operation_count,
            "subprocess_count": self.subprocess_count,
            "bytes_scanned": self.bytes_scanned,
            "bytes_read": self.bytes_read,
            "files_visited": self.files_visited,
            "raw_hits": self.raw_hits,
            "unique_candidates": self.unique_candidates,
            "hydrated_regions": self.hydrated_regions,
            "context_chars": self.context_chars,
            "cache_hits": self.cache_hits,
            "backend_ms": self.backend_ms,
            "fallback_reasons": self.fallback_reasons,
        }


@dataclass
class BackendResult:
    candidates: list[Candidate] = field(default_factory=list)
    backend: str = "unknown"
    fallback_reason: str | None = None
    trace: RetrievalTrace | None = None


class TextSearchBackend(Protocol):
    def search(self, query: str, repos: Sequence[str], *, limit: int) -> BackendResult: ...


class PathSearchBackend(Protocol):
    def paths(self, query: str, repos: Sequence[str], *, limit: int) -> BackendResult: ...


class SymbolBackend(Protocol):
    def symbols(self, query: str, repos: Sequence[str], *, limit: int) -> BackendResult: ...


class HistoryBackend(Protocol):
    def history(self, query: str, repos: Sequence[str], *, limit: int) -> BackendResult: ...


class SemanticBackend(Protocol):
    def semantic(self, query: str, snapshots: Sequence[SnapshotIdentity], *, limit: int) -> BackendResult: ...


class RerankerBackend(Protocol):
    def rerank(self, query: str, candidates: Sequence[Candidate]) -> Sequence[Candidate]: ...


class SourceProvider(Protocol):
    def read(self, candidate: Candidate) -> EvidenceRegion: ...

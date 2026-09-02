from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
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
    includes: tuple[str, ...] = ()


@dataclass(frozen=True)
class QueryPlan:
    objective: str
    operations: tuple[QueryOperation, ...]
    timeout_ms: int = 10_000
    stop_reason: str = "all requested operations evaluated"
    protocol_version: int = 1
    requested_operations: int = 0
    deferred_operations: int = 0


@dataclass
class RetrievalTrace:
    """Serializable operation accounting; never stores source or full queries."""

    started: float = field(default_factory=perf_counter, repr=False)
    started_cpu: float = field(default_factory=process_time, repr=False)
    trace_schema_version: int = 2
    requested_operations: int = 0
    effective_operations: int = 0
    physical_backend_operations: int = 0
    max_physical_backend_operations: int = 200
    operation_count: int = 0  # v1 trace compatibility
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
    stage_ms: dict[str, float] = field(default_factory=dict)
    repo_candidates: int = 0
    initial_repo_scope: list[str] = field(default_factory=list)
    final_repo_scope: list[str] = field(default_factory=list)
    widening_rounds: int = 0
    unique_candidates_before_prune: int = 0
    candidates_after_prune: int = 0
    rerank_input_count: int = 0
    deferred_candidates: int = 0
    semantic_status: str = "not_requested"
    semantic_repo_scope: list[str] = field(default_factory=list)
    stop_reason: str = "coverage_satisfied"
    fallback_reasons: list[str] = field(default_factory=list)
    _lock: Lock = field(default_factory=Lock, repr=False)

    def add_backend(self, name: str, elapsed_ms: float, *, subprocesses: int = 0, bytes_scanned: int = 0, files: int = 0, raw_hits: int = 0, cache_hit: bool = False) -> None:
        with self._lock:
            self.physical_backend_operations += 1
            self.operation_count = self.physical_backend_operations
            self._add_backend_metrics(
                name, elapsed_ms, subprocesses=subprocesses, bytes_scanned=bytes_scanned,
                files=files, raw_hits=raw_hits, cache_hit=cache_hit,
            )

    def try_reserve_backend(self) -> bool:
        """Atomically reserve one physical operation before parallel work starts."""
        with self._lock:
            if self.physical_backend_operations >= self.max_physical_backend_operations:
                return False
            self.physical_backend_operations += 1
            self.operation_count = self.physical_backend_operations
            return True

    def complete_reserved_backend(self, name: str, elapsed_ms: float, *, subprocesses: int = 0, bytes_scanned: int = 0, files: int = 0, raw_hits: int = 0, cache_hit: bool = False) -> None:
        with self._lock:
            self._add_backend_metrics(
                name, elapsed_ms, subprocesses=subprocesses, bytes_scanned=bytes_scanned,
                files=files, raw_hits=raw_hits, cache_hit=cache_hit,
            )

    def _add_backend_metrics(self, name: str, elapsed_ms: float, *, subprocesses: int, bytes_scanned: int, files: int, raw_hits: int, cache_hit: bool) -> None:
        self.subprocess_count += subprocesses
        self.bytes_scanned += bytes_scanned
        self.files_visited += files
        self.raw_hits += raw_hits
        self.backend_ms[name] = round(self.backend_ms.get(name, 0.0) + elapsed_ms, 3)
        self.cache_hits += int(cache_hit)

    def add_cache_hit(self) -> None:
        with self._lock:
            self.cache_hits += 1

    def add_stage(self, name: str, elapsed_ms: float) -> None:
        with self._lock:
            self.stage_ms[name] = round(self.stage_ms.get(name, 0.0) + elapsed_ms, 3)

    @property
    def physical_budget_remaining(self) -> int:
        return max(0, self.max_physical_backend_operations - self.physical_backend_operations)

    def as_dict(self) -> dict[str, Any]:
        wall_ms = round((perf_counter() - self.started) * 1000, 3)
        return {
            "trace_schema_version": self.trace_schema_version,
            "wall_ms": wall_ms,
            "total_ms": wall_ms,
            "cpu_ms": round((process_time() - self.started_cpu) * 1000, 3),
            "requested_operations": self.requested_operations,
            "effective_operations": self.effective_operations,
            "physical_backend_operations": self.physical_backend_operations,
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
            **{f"{name.replace('-', '_')}_ms": elapsed for name, elapsed in self.backend_ms.items()},
            **self.stage_ms,
            "repo_candidates": self.repo_candidates,
            "initial_repo_scope": self.initial_repo_scope,
            "final_repo_scope": self.final_repo_scope,
            "widening_rounds": self.widening_rounds,
            "unique_candidates_before_prune": self.unique_candidates_before_prune,
            "candidates_after_prune": self.candidates_after_prune,
            "rerank_input_count": self.rerank_input_count,
            "deferred_candidates": self.deferred_candidates,
            "semantic_status": self.semantic_status,
            "semantic_repo_scope": self.semantic_repo_scope,
            "stop_reason": self.stop_reason,
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

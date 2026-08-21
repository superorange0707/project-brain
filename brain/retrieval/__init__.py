"""Stable retrieval contracts shared by the Core, Semantic, and Precision editions."""

from .models import (
    BackendResult,
    Candidate,
    EvidenceRegion,
    IndexGeneration,
    QueryOperation,
    QueryPlan,
    RetrievalTrace,
    SnapshotIdentity,
)
from .planner import compile_request, explain_plan

__all__ = [
    "BackendResult",
    "Candidate",
    "EvidenceRegion",
    "IndexGeneration",
    "QueryOperation",
    "QueryPlan",
    "RetrievalTrace",
    "SnapshotIdentity",
    "compile_request",
    "explain_plan",
]

from __future__ import annotations

from collections import defaultdict
from typing import Protocol, TypeVar


class Hit(Protocol):
    repo: str
    path: str
    line: int
    kind: str
    score: int | float
    found_by: list[str]


T = TypeVar("T", bound=Hit)


def fuse_and_rank(hits: list[T]) -> list[T]:
    """Deterministic reciprocal-rank fusion plus explainable source features."""
    groups: dict[tuple[str, str, int], list[T]] = defaultdict(list)
    for hit in hits:
        groups[(hit.repo, hit.path, hit.line)].append(hit)
    fused: list[T] = []
    for values in groups.values():
        primary = max(values, key=lambda item: (item.score, item.kind, item.found_by))
        channels = sorted({channel for item in values for channel in item.found_by})
        rrf = sum(1 / (60 + rank) for rank, _ in enumerate(channels, 1))
        feature = 0
        if "definition" in primary.kind:
            feature += 14
        if "requested" in primary.kind:
            feature += 30
        if "test" in primary.kind:
            feature += 8
        if "relationship" in primary.kind:
            feature += 6
        if "/generated/" in f"/{primary.path.lower()}/" or "/vendor/" in f"/{primary.path.lower()}/":
            feature -= 25
        primary.score = round(float(primary.score) + feature + rrf * 100, 3)
        primary.found_by = channels
        fused.append(primary)
    return sorted(fused, key=lambda item: (-item.score, item.repo, item.path, item.line))

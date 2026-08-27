from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable

from .models import QueryOperation, QueryPlan


DEFAULT_MAX_EFFECTIVE_OPERATIONS = 15
_OBJECTIVE_STOP_WORDS = {
    "about", "after", "before", "could", "determine", "establish", "find", "from", "into", "locate",
    "production", "repository", "responsible", "should", "tests", "that", "their", "this", "through",
    "what", "when", "where", "which", "while", "with", "would",
}


def _repos(item: dict[object, object]) -> tuple[str, ...]:
    return tuple(sorted({str(value) for value in (item.get("repos") or [])}))


def objective_terms(objective: str, *, limit: int = 4) -> list[str]:
    """Extract only deterministic, source-like objective terms for cheap discovery."""
    patterns = (
        r"['\"]([^'\"]{2,80})['\"]",
        r"\b[A-Z][a-z0-9]+(?:[A-Z][A-Za-z0-9]*)+\b",
        r"\b[A-Z][A-Z0-9_]{2,}\b",
        r"\b[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+\b",
        r"(?:/[-A-Za-z0-9_{}:.]+|[-A-Za-z0-9_]+\.(?:enabled|timeout|url|topic|queue|cache))",
    )
    values: list[str] = []
    for pattern in patterns:
        values.extend(match.group(1) if match.lastindex else match.group(0) for match in re.finditer(pattern, objective))
    if not values:
        words = re.findall(r"\b[A-Za-z][A-Za-z0-9_-]{3,}\b", objective)
        values.extend(word for word in words if word.lower() not in _OBJECTIVE_STOP_WORDS)
    if not values and objective.strip():
        values.append(objective.strip()[:500])
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))[:limit]


def requested_operation_count(request: dict[object, object]) -> int:
    return (
        len(request.get("searches") or [])
        + len(request.get("paths") or [])
        + sum(len(item.get("include") or ["definition"]) for item in request.get("symbols") or [] if isinstance(item, dict))
        + len(request.get("files") or [])
        + len(request.get("history") or [])
        + len(request.get("expand") or [])
    )


def compile_request(
    request: dict[object, object],
    *,
    timeout_ms: int = 10_000,
    max_effective_operations: int = DEFAULT_MAX_EFFECTIVE_OPERATIONS,
) -> QueryPlan:
    """Normalize, deduplicate, fuse symbol work, cost-sort, and budget a request."""
    operations: list[QueryOperation] = []
    for item in request.get("files") or []:
        if isinstance(item, dict):
            value = f"{item['repo']}:{item['path']}"
            if item.get("lines"):
                value += f":{item['lines']}"
            operations.append(QueryOperation("file", value, (str(item["repo"]),), 0, True, 0, "direct evidence"))
    symbols: dict[tuple[str, tuple[str, ...]], set[str]] = defaultdict(set)
    for item in request.get("symbols") or []:
        if not isinstance(item, dict):
            continue
        name, repos = str(item["name"]), _repos(item)
        symbols[(name, repos)].update(str(value) for value in (item.get("include") or ["definition"]))
    for (name, repos), includes in symbols.items():
        tier = 0 if "." in name else 1
        operations.append(QueryOperation("symbol", name, repos, tier, "definition" in includes, 2, "shared symbol discovery", tuple(sorted(includes))))
    for item in request.get("paths") or []:
        if isinstance(item, dict):
            operations.append(QueryOperation("path", str(item["query"]), _repos(item), 0, True, 1, "path request"))
    for item in request.get("searches") or []:
        if isinstance(item, dict):
            value = str(item["query"])
            operations.append(QueryOperation("search", value, _repos(item), 1 if value.replace("_", "").isalnum() else 4, False, 2, "literal first"))
    for item in request.get("history") or []:
        if isinstance(item, dict):
            operations.append(QueryOperation("history", str(item["query"]), _repos(item), 3, False, 4, "history expansion"))
    unique = {
        (item.kind, item.value, item.repos, item.includes): item
        for item in operations
    }
    ordered = sorted(unique.values(), key=lambda item: (item.tier, item.estimated_cost, item.kind, item.value, item.repos, item.includes))
    limit = max(1, max_effective_operations)
    effective = ordered[:limit]
    deferred = max(0, len(ordered) - len(effective))
    return QueryPlan(
        str(request.get("objective") or "").strip(),
        tuple(effective),
        timeout_ms,
        "operation_budget" if deferred else "all requested operations evaluated",
        int(request.get("version") or 1),
        requested_operation_count(request),
        deferred,
    )


def route_repositories(
    repositories: Iterable[object],
    request: dict[object, object],
    candidates: Iterable[object] = (),
    *,
    limit: int = 6,
) -> list[str]:
    """Rank repositories from explicit scope, observed hits, and catalog metadata."""
    rows = list(repositories)
    scores: dict[str, int] = {str(getattr(repo, "name")): 0 for repo in rows}
    explicit: set[str] = set()
    hints = request.get("hints") if isinstance(request.get("hints"), dict) else {}
    explicit.update(str(value) for value in (hints.get("repos") or []))
    for section in ("searches", "paths", "symbols", "history"):
        for item in request.get(section) or []:
            if isinstance(item, dict):
                explicit.update(str(value) for value in (item.get("repos") or []))
    for name in explicit:
        if name in scores:
            scores[name] += 10_000
    for candidate in candidates:
        name = str(getattr(candidate, "repo", ""))
        if name in scores:
            scores[name] += 100
    terms = {value.lower() for value in objective_terms(str(request.get("objective") or ""), limit=12)}
    for repo in rows:
        name = str(getattr(repo, "name"))
        metadata = " ".join([
            name,
            str(getattr(repo, "description", "")),
            " ".join(str(value) for value in getattr(repo, "tags", [])),
        ]).lower()
        scores[name] += sum(5 for term in terms if term in metadata)
    return sorted(scores, key=lambda name: (-scores[name], name))[: max(1, limit)]


def explain_plan(plan: QueryPlan) -> dict[str, object]:
    return {
        "requested_protocol": plan.protocol_version,
        "objective": plan.objective,
        "timeout_ms": plan.timeout_ms,
        "stop_reason": plan.stop_reason,
        "requested_operations": plan.requested_operations,
        "effective_operations": len(plan.operations),
        "deferred_operations": plan.deferred_operations,
        "operations": [
            {
                "tier": operation.tier,
                "kind": operation.kind,
                "value": operation.value,
                "repos": list(operation.repos),
                "protected": operation.protected,
                "estimated_cost": operation.estimated_cost,
                "reason": operation.reason,
                "includes": list(operation.includes),
            }
            for operation in plan.operations
        ],
    }

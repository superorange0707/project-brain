from __future__ import annotations

from .models import QueryOperation, QueryPlan


def _repos(item: dict[object, object]) -> tuple[str, ...]:
    return tuple(str(value) for value in (item.get("repos") or []))


def compile_request(request: dict[object, object], *, timeout_ms: int = 10_000) -> QueryPlan:
    """Compile the public request protocol to a deterministic, narrow-first DAG order."""
    operations: list[QueryOperation] = []
    for item in request.get("files") or []:
        if isinstance(item, dict):
            operations.append(QueryOperation("file", f"{item['repo']}:{item['path']}", (str(item["repo"]),), 0, True, 0, "direct evidence"))
    for item in request.get("symbols") or []:
        if not isinstance(item, dict):
            continue
        name, repos = str(item["name"]), _repos(item)
        tier = 0 if "." in name else 1
        for include in item.get("include") or ["definition"]:
            operations.append(QueryOperation(str(include), name, repos, tier, include == "definition", 1 if tier == 0 else 2, "symbol request"))
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
    operations.sort(key=lambda item: (item.tier, item.estimated_cost, item.kind, item.value, item.repos))
    return QueryPlan(str(request.get("objective") or "").strip(), tuple(operations), timeout_ms)


def explain_plan(plan: QueryPlan) -> dict[str, object]:
    return {
        "objective": plan.objective,
        "timeout_ms": plan.timeout_ms,
        "stop_reason": plan.stop_reason,
        "operations": [
            {
                "tier": operation.tier,
                "kind": operation.kind,
                "value": operation.value,
                "repos": list(operation.repos),
                "protected": operation.protected,
                "estimated_cost": operation.estimated_cost,
                "reason": operation.reason,
            }
            for operation in plan.operations
        ],
    }

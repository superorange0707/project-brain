from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .core import Settings


def _load(path: Path) -> dict[str, Any]:
    if path.suffix.lower() == ".json":
        value = json.loads(path.read_text(encoding="utf-8"))
    else:
        from .core import simple_yaml_load

        value = simple_yaml_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("golden evaluation suite must be a mapping")
    return value


def _files(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {str(item).strip() for item in value if str(item).strip()}


def _request(value: object) -> dict[str, Any]:
    from .core import parse_context_request

    if isinstance(value, str):
        return parse_context_request(value)
    if not isinstance(value, dict):
        raise ValueError("every golden case needs a request mapping or CONTEXT_REQUEST text")
    request = {str(key): item for key, item in value.items()}
    if not str(request.get("objective") or "").strip():
        raise ValueError("every golden request needs an objective")
    for key in ("searches", "paths", "symbols", "files", "history"):
        request.setdefault(key, [])
    request.setdefault("expand", [])
    request.setdefault("version", 2)
    return request


def _ranking(bundle: Any) -> list[str]:
    return list(dict.fromkeys(_raw_ranking(bundle)))


def _raw_ranking(bundle: Any) -> list[str]:
    ranked = [f"{item.repo}:{item.path}" for item in bundle.evidence if item.repo not in {"external", "knowledge"}]
    ranked.extend(f"{item.repo}:{item.path}" for item in bundle.additional_candidates)
    return ranked


def _recall(found: set[str], expected: set[str]) -> float | None:
    return len(found & expected) / len(expected) if expected else None


def _mrr(ranking: list[str], relevant: set[str], limit: int) -> float | None:
    for index, item in enumerate(ranking[:limit], 1):
        if item in relevant:
            return 1 / index
    return 0.0 if relevant else None


def _ndcg(ranking: list[str], relevant: set[str], limit: int) -> float | None:
    if not relevant:
        return None
    gained = sum(1 / math.log2(index + 1) for index, item in enumerate(ranking[:limit], 1) if item in relevant)
    ideal = sum(1 / math.log2(index + 1) for index in range(1, min(limit, len(relevant)) + 1))
    return gained / ideal if ideal else 0.0


def _precision(ranking: list[str], relevant: set[str], limit: int) -> float | None:
    selected = ranking[:limit]
    return len(set(selected) & relevant) / len(selected) if selected else None


def _peak_rss_mb() -> float | None:
    try:
        import os
        import resource

        scale = 1 if os.uname().sysname == "Darwin" else 1_024
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale / 1_000_000, 3)
    except (AttributeError, ImportError, OSError):
        return None


def evaluate_golden(settings: Settings, suite_path: str | Path, *, split: str | None = None, limit: int = 20) -> dict[str, Any]:
    """Replay local, hand-labelled tickets without leaking request text to metrics."""
    path = Path(suite_path).expanduser().resolve()
    suite = _load(path)
    cases = suite.get("cases") or suite.get("tickets") or []
    if not isinstance(cases, list) or not cases:
        raise ValueError("golden evaluation suite needs a non-empty cases list")
    if split and split not in {"calibration", "validation", "holdout"}:
        raise ValueError("split must be calibration, validation, or holdout")
    selected: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for position, item in enumerate(cases, 1):
        if not isinstance(item, dict):
            raise ValueError(f"golden case {position} must be a mapping")
        identifier = str(item.get("id") or f"case-{position}")
        if identifier in identifiers:
            raise ValueError(f"duplicate golden case id: {identifier}")
        identifiers.add(identifier)
        case_split = str(item.get("split") or "holdout")
        if case_split not in {"calibration", "validation", "holdout"}:
            raise ValueError(f"golden case {identifier} has invalid split")
        if split and case_split != split:
            continue
        selected.append({"id": identifier, "split": case_split, "request": _request(item.get("request")), "expect": item.get("expect") or item})
    if not selected:
        raise ValueError("no golden cases matched the requested split")

    from .core import retrieve_context

    reports: list[dict[str, Any]] = []
    for case in selected:
        bundle = retrieve_context(settings, case["request"])
        ranking = _ranking(bundle)
        raw_ranking = _raw_ranking(bundle)
        expect = case["expect"] if isinstance(case["expect"], dict) else {}
        production = _files(expect.get("production_files") or expect.get("required_production_files"))
        tests = _files(expect.get("test_config_files") or expect.get("required_test_config_files"))
        required = production | tests | _files(expect.get("required_files"))
        excluded = _files(expect.get("false_positive_files") or expect.get("excluded_files"))
        top = ranking[:limit]
        semantic_only = [
            f"{item.repo}:{item.path}"
            for item in [*bundle.evidence, *bundle.additional_candidates]
            if item.repo not in {"external", "knowledge"} and set(item.found_by) == {"local semantic index"}
        ]
        reports.append({
            "id": case["id"], "split": case["split"], "repo_recall_at_10": _recall({item.split(":", 1)[0] for item in ranking[:10]}, {item.split(":", 1)[0] for item in required}),
            "repo_recall_at_5": _recall({item.split(":", 1)[0] for item in ranking[:5]}, {item.split(":", 1)[0] for item in required}),
            "file_recall_at_limit": _recall(set(top), required),
            "file_recall_at_5": _recall(set(ranking[:5]), required),
            "file_recall_at_10": _recall(set(ranking[:10]), required),
            "file_recall_at_20": _recall(set(ranking[:20]), required),
            "test_config_recall_at_limit": _recall(set(top), tests),
            "test_recall_at_10": _recall(set(ranking[:10]), tests),
            "mrr_at_10": _mrr(ranking, required, 10),
            "ndcg_at_10": _ndcg(ranking, required, 10),
            "precision_at_5": _precision(ranking, required, 5),
            "precision_at_10": _precision(ranking, required, 10),
            "false_positives_at_10": sum(item in excluded for item in ranking[:10]),
            "context_chars": sum(len(item.content) for item in bundle.evidence),
            "duplicate_ratio": 1 - len(ranking) / len(raw_ranking) if raw_ranking else 0.0,
            "semantic_only_candidates": len(semantic_only),
            "semantic_only_useful_hit_rate": _precision(semantic_only, required, len(semantic_only)),
            "candidate_ms": bundle.metrics.get("candidate_ms"),
            "hydrate_ms": bundle.metrics.get("hydrate_ms"),
            "total_ms": bundle.metrics.get("total_ms"),
            "process_peak_rss_mb": _peak_rss_mb(),
            "stale_warning": any("stale" in warning.lower() for warning in bundle.warnings),
        })

    def average(key: str) -> float | None:
        values = [float(item[key]) for item in reports if isinstance(item.get(key), (float, int))]
        return round(sum(values) / len(values), 6) if values else None

    raw = path.read_bytes()
    report = {
        "suite_hash": hashlib.sha256(raw).hexdigest(),
        "suite_name": str(suite.get("name") or path.name),
        "limit": limit,
        "cases": reports,
        "summary": {
            "evaluated_cases": len(reports),
            "repo_recall_at_10": average("repo_recall_at_10"),
            "repo_recall_at_5": average("repo_recall_at_5"),
            "file_recall_at_limit": average("file_recall_at_limit"),
            "file_recall_at_5": average("file_recall_at_5"),
            "file_recall_at_10": average("file_recall_at_10"),
            "file_recall_at_20": average("file_recall_at_20"),
            "test_config_recall_at_limit": average("test_config_recall_at_limit"),
            "test_recall_at_10": average("test_recall_at_10"),
            "mrr_at_10": average("mrr_at_10"),
            "ndcg_at_10": average("ndcg_at_10"),
            "precision_at_5": average("precision_at_5"),
            "precision_at_10": average("precision_at_10"),
            "false_positives_at_10": average("false_positives_at_10"),
            "context_chars": average("context_chars"),
            "duplicate_ratio": average("duplicate_ratio"),
            "semantic_only_useful_hit_rate": average("semantic_only_useful_hit_rate"),
            "candidate_ms": average("candidate_ms"),
            "hydrate_ms": average("hydrate_ms"),
            "total_ms": average("total_ms"),
            "process_peak_rss_mb": average("process_peak_rss_mb"),
            "stale_cases": sum(bool(item["stale_warning"]) for item in reports),
        },
    }
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    (settings.state_dir / "golden-eval.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    from .catalog import record_metric_run

    record_metric_run(settings, "golden_evaluation", {"suite_hash": report["suite_hash"], "summary": report["summary"]})
    return report

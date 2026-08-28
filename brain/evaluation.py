from __future__ import annotations

import hashlib
import json
import math
import time
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


def _values(value: object) -> set[str]:
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


def _ranked_label_recall(ranking: list[set[str]], expected: set[str], limit: int) -> float | None:
    if not expected:
        return None
    found = {value.casefold() for labels in ranking[:limit] for value in labels}
    relevant = {value.casefold() for value in expected}
    return len(found & relevant) / len(relevant)


def _atlas_ranking_labels(settings: Settings, bundle: Any, module_ids: list[str], entity_ids: list[str]) -> tuple[list[set[str]], list[set[str]]]:
    generation = getattr(bundle.atlas_generation, "generation", None)
    if generation is None:
        return [], []
    from .catalog import connect

    connection = connect(settings)
    try:
        modules: dict[str, set[str]] = {}
        if module_ids:
            placeholders = ",".join("?" for _ in module_ids)
            for module_id, repo, path, name in connection.execute(
                f"SELECT m.module_id,m.repo,m.path,m.name FROM generation_modules g "
                f"JOIN atlas_modules m ON m.module_id=g.module_id "
                f"WHERE g.generation=? AND m.module_id IN ({placeholders})",
                (generation, *module_ids),
            ):
                modules[str(module_id)] = {str(module_id), str(name), str(path), f"{repo}:{path}"}
        entities: dict[str, set[str]] = {}
        if entity_ids:
            placeholders = ",".join("?" for _ in entity_ids)
            for entity_id, repo, path, simple, qualified in connection.execute(
                f"SELECT e.entity_id,e.repo,e.path,e.simple_name,e.qualified_name FROM generation_entities g "
                f"JOIN atlas_entities e ON e.entity_id=g.entity_id "
                f"WHERE g.generation=? AND e.entity_id IN ({placeholders})",
                (generation, *entity_ids),
            ):
                entities[str(entity_id)] = {
                    str(entity_id), str(simple), str(qualified), str(path), f"{repo}:{path}",
                }
        return ([modules.get(value, {value}) for value in module_ids], [entities.get(value, {value}) for value in entity_ids])
    finally:
        connection.close()


def _peak_rss_mb() -> float | None:
    try:
        import os
        import resource

        scale = 1 if os.uname().sysname == "Darwin" else 1_024
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale / 1_000_000, 3)
    except (AttributeError, ImportError, OSError):
        return None


def evaluate_golden(
    settings: Settings,
    suite_path: str | Path,
    *,
    split: str | None = None,
    limit: int = 20,
    evaluation_ablation: set[str] | None = None,
) -> dict[str, Any]:
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

    from .core import pack_context, retrieve_context

    reports: list[dict[str, Any]] = []
    for case in selected:
        request = json.loads(json.dumps(case["request"]))
        if evaluation_ablation:
            request["_evaluation_ablation"] = sorted(evaluation_ablation)
        bundle = retrieve_context(settings, request)
        pack_started = time.perf_counter()
        full_context = pack_context(settings, f"EVAL-{case['id']}", 1, bundle)
        context_pack_ms = (time.perf_counter() - pack_started) * 1000
        ranking = _ranking(bundle)
        raw_ranking = _raw_ranking(bundle)
        expect = case["expect"] if isinstance(case["expect"], dict) else {}
        production = _files(expect.get("production_files") or expect.get("required_production_files"))
        tests = _files(expect.get("test_config_files") or expect.get("required_test_config_files"))
        required = production | tests | _files(expect.get("required_files"))
        required_repos = {item.split(":", 1)[0] for item in required}
        required_modules = _values(expect.get("required_modules"))
        required_entities = _values(expect.get("required_entities"))
        required_edges = _values(expect.get("required_edges") or expect.get("graph_edges"))
        excluded = _files(expect.get("false_positive_files") or expect.get("excluded_files"))
        top = ranking[:limit]
        semantic_only = [
            f"{item.repo}:{item.path}"
            for item in [*bundle.evidence, *bundle.additional_candidates]
            if item.repo not in {"external", "knowledge"} and set(item.found_by) == {"local semantic index"}
        ]
        atlas_route = bundle.trace.get("atlas_route") or {}
        routed_repos = list(atlas_route.get("repositories") or [])
        routed_modules = set(atlas_route.get("modules") or [])
        routed_entities = set(atlas_route.get("entity_ids") or [])
        module_labels, entity_labels = _atlas_ranking_labels(
            settings, bundle, list(atlas_route.get("modules") or []), list(atlas_route.get("entity_ids") or []),
        )
        relationship_text = "\n".join(bundle.relationships)
        repo_order = list(dict.fromkeys([*routed_repos, *(item.split(":", 1)[0] for item in ranking)]))
        cache_hit = bool(atlas_route.get("cache_hit"))
        prefetch_reused = int(atlas_route.get("prefetch_reused") or 0)
        reports.append({
            "id": case["id"], "split": case["split"], "repo_recall_at_10": _recall(set(repo_order[:10]), required_repos),
            "repo_recall_at_5": _recall(set(repo_order[:5]), required_repos),
            "repo_recall_at_4": _recall(set(repo_order[:4]), required_repos),
            "repo_recall_at_6": _recall(set(repo_order[:6]), required_repos),
            "repo_recall_at_8": _recall(set(repo_order[:8]), required_repos),
            "repo_recall_at_16": _recall(set(repo_order[:16]), required_repos),
            "module_recall_at_5": _ranked_label_recall(module_labels, required_modules, 5),
            "module_recall_at_10": _ranked_label_recall(module_labels, required_modules, 10),
            "module_recall_at_20": _ranked_label_recall(module_labels, required_modules, 20),
            "entity_recall_at_10": _ranked_label_recall(entity_labels, required_entities, 10),
            "entity_recall_at_20": _ranked_label_recall(entity_labels, required_entities, 20),
            "entity_recall_at_40": _ranked_label_recall(entity_labels, required_entities, 40),
            "entity_recall_at_50": _ranked_label_recall(entity_labels, required_entities, 50),
            "graph_edge_recall": _recall({edge for edge in required_edges if edge in relationship_text}, required_edges),
            "evidence_recall_at_18": _recall(set(ranking[:18]), required),
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
            "context_pack_ms": round(context_pack_ms, 3),
            "total_context_ms": round(float(bundle.metrics.get("total_ms") or 0) + context_pack_ms, 3),
            "time_to_first_likely_repo_ms": bundle.metrics.get("repo_routing_ms"),
            "time_to_first_useful_entity_ms": bundle.metrics.get("candidate_ms"),
            "time_to_first_repo_ms": bundle.metrics.get("time_to_first_repo_ms"),
            "time_to_first_entity_ms": bundle.metrics.get("time_to_first_entity_ms"),
            "time_to_first_verified_evidence_ms": bundle.metrics.get("time_to_first_verified_evidence_ms"),
            "requested_operations": bundle.metrics.get("requested_operations"),
            "effective_operations": bundle.metrics.get("effective_operations"),
            "physical_backend_operations": bundle.metrics.get("physical_backend_operations"),
            "physical_operations": bundle.metrics.get("physical_backend_operations"),
            "raw_candidates": bundle.metrics.get("raw_candidates"),
            "late_candidates": bundle.metrics.get("late_candidates"),
            "relevant_late_candidates": sum(item in required for item in ranking[18:]),
            "rerank_input_count": bundle.metrics.get("rerank_input_count"),
            "hydrated_regions": bundle.metrics.get("hydrated_regions"),
            "repo_route_cache_hit_rate": 1.0 if cache_hit and repo_order else 0.0,
            "entity_cache_hit_rate": 1.0 if cache_hit and routed_entities else 0.0,
            "graph_cache_hit_rate": 1.0 if cache_hit and int(atlas_route.get("graph_edges") or 0) else 0.0,
            "similar_ticket_hit_rate": 1.0 if int(atlas_route.get("investigation_reused") or 0) else 0.0,
            "prefetch_hit_rate": 1.0 if prefetch_reused else 0.0,
            "cache_hit_rate": 1.0 if cache_hit else 0.0,
            "prefetch_usefulness": 1.0 if (cache_hit or prefetch_reused) and bool(set(ranking[:18]) & required) else 0.0,
            "full_context_chars": len(full_context),
            "delta_context_chars": None,
            "delta_reduction_percent": None,
            "delta_context_reduction": None,
            "brain_rounds_until_ready": None,
            "no_progress_rounds": None,
            "focused_followup_count": None,
            "next_best_evidence_usefulness": None,
            "rounds_until_final_solution": None,
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
        "evaluation_ablation": sorted(evaluation_ablation or set()),
        "limit": limit,
        "cases": reports,
        "summary": {
            "evaluated_cases": len(reports),
            "repo_recall_at_10": average("repo_recall_at_10"),
            "repo_recall_at_5": average("repo_recall_at_5"),
            "repo_recall_at_4": average("repo_recall_at_4"),
            "repo_recall_at_6": average("repo_recall_at_6"),
            "repo_recall_at_8": average("repo_recall_at_8"),
            "repo_recall_at_16": average("repo_recall_at_16"),
            "module_recall_at_5": average("module_recall_at_5"),
            "module_recall_at_10": average("module_recall_at_10"),
            "module_recall_at_20": average("module_recall_at_20"),
            "entity_recall_at_10": average("entity_recall_at_10"),
            "entity_recall_at_20": average("entity_recall_at_20"),
            "entity_recall_at_40": average("entity_recall_at_40"),
            "entity_recall_at_50": average("entity_recall_at_50"),
            "graph_edge_recall": average("graph_edge_recall"),
            "evidence_recall_at_18": average("evidence_recall_at_18"),
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
            "context_pack_ms": average("context_pack_ms"),
            "total_context_ms": average("total_context_ms"),
            "time_to_first_likely_repo_ms": average("time_to_first_likely_repo_ms"),
            "time_to_first_useful_entity_ms": average("time_to_first_useful_entity_ms"),
            "time_to_first_repo_ms": average("time_to_first_repo_ms"),
            "time_to_first_entity_ms": average("time_to_first_entity_ms"),
            "time_to_first_verified_evidence_ms": average("time_to_first_verified_evidence_ms"),
            "requested_operations": average("requested_operations"),
            "effective_operations": average("effective_operations"),
            "physical_backend_operations": average("physical_backend_operations"),
            "physical_operations": average("physical_operations"),
            "raw_candidates": average("raw_candidates"),
            "rerank_input_count": average("rerank_input_count"),
            "hydrated_regions": average("hydrated_regions"),
            "repo_route_cache_hit_rate": average("repo_route_cache_hit_rate"),
            "entity_cache_hit_rate": average("entity_cache_hit_rate"),
            "graph_cache_hit_rate": average("graph_cache_hit_rate"),
            "similar_ticket_hit_rate": average("similar_ticket_hit_rate"),
            "prefetch_hit_rate": average("prefetch_hit_rate"),
            "cache_hit_rate": average("cache_hit_rate"),
            "late_candidates": average("late_candidates"),
            "relevant_late_candidates": average("relevant_late_candidates"),
            "prefetch_usefulness": average("prefetch_usefulness"),
            "full_context_chars": average("full_context_chars"),
            "delta_context_chars": average("delta_context_chars"),
            "delta_reduction_percent": average("delta_reduction_percent"),
            "delta_context_reduction": average("delta_context_reduction"),
            "brain_rounds_until_ready": average("brain_rounds_until_ready"),
            "no_progress_rounds": average("no_progress_rounds"),
            "focused_followup_count": average("focused_followup_count"),
            "next_best_evidence_usefulness": average("next_best_evidence_usefulness"),
            "rounds_until_final_solution": average("rounds_until_final_solution"),
            "process_peak_rss_mb": average("process_peak_rss_mb"),
            "stale_cases": sum(bool(item["stale_warning"]) for item in reports),
        },
    }
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    (settings.state_dir / "golden-eval.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    from .catalog import record_metric_run

    record_metric_run(settings, "golden_evaluation", {"suite_hash": report["suite_hash"], "summary": report["summary"]})
    return report

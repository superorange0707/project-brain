from __future__ import annotations

import hashlib
import json
import math
import time
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from .locks import workspace_exclusive
from .platforms import atomic_managed_text_write, read_direct_file_bytes

if TYPE_CHECKING:
    from .core import Settings


V1_EVALUATION_ABLATIONS = frozenset({
    "anchors", "graph_flow", "program_slice", "historical_prior", "prefetch",
    "generation_cache", "multi_wave",
})
MAX_EVALUATION_SUITE_BYTES = 8 * 1024 * 1024
MAX_EVALUATION_CASES = 512
MAX_EVALUATION_WAVES_PER_CASE = 4
MAX_EVALUATION_REQUEST_BYTES = 100_000


def _load(path: Path) -> tuple[dict[str, Any], bytes]:
    raw, exceeded = read_direct_file_bytes(path, max_bytes=MAX_EVALUATION_SUITE_BYTES)
    if exceeded:
        raise ValueError("golden evaluation suite exceeds its byte limit")
    text = raw.decode("utf-8")
    if path.suffix.lower() == ".json":
        value = json.loads(text)
    else:
        from .core import simple_yaml_load

        value = simple_yaml_load(text)
    if not isinstance(value, dict):
        raise ValueError("golden evaluation suite must be a mapping")
    return value, raw


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
        request = parse_context_request(value)
        if len(value.encode("utf-8")) > MAX_EVALUATION_REQUEST_BYTES:
            raise ValueError("golden evaluation request exceeds its byte limit")
        return request
    if not isinstance(value, dict):
        raise ValueError("every golden case needs a request mapping or CONTEXT_REQUEST text")
    request = {str(key): item for key, item in value.items()}
    if not str(request.get("objective") or "").strip():
        raise ValueError("every golden request needs an objective")
    for key in ("searches", "paths", "symbols", "files", "history"):
        request.setdefault(key, [])
    request.setdefault("expand", [])
    request.setdefault("version", 2)
    if len(json.dumps(request, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > MAX_EVALUATION_REQUEST_BYTES:
        raise ValueError("golden evaluation request exceeds its byte limit")
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


def _runtime_metrics(runtime: dict[str, Any], expect: dict[str, Any]) -> dict[str, float | None]:
    anchors = (runtime.get("anchors") or {}).get("candidates") or []
    expected_anchors = {str(value).casefold() for value in expect.get("required_anchors") or []}
    anchor_values = [str(item.get("value") or "").casefold() for item in anchors]
    expected_execution = {str(value) for value in expect.get("execution_steps") or []}
    execution = runtime.get("execution_flow") or {}
    execution_values = [str(item.get("edge_type") or item.get("target") or "") for item in execution.get("steps") or []]
    expected_integration_repos = {str(value) for value in expect.get("integration_repositories") or []}
    integration_repos = set((runtime.get("integration_flow") or {}).get("repositories") or [])
    expected_surfaces = _files(expect.get("required_surfaces"))
    surfaces = runtime.get("surfaces") or {}
    found_surfaces = {
        f"{name}:{item.get('repo')}:{item.get('path')}"
        for name in ("implementation", "test", "impact", "contract", "config_data")
        for item in surfaces.get(name) or []
    }
    expected_order = [str(value) for value in expect.get("execution_steps") or []]
    return {
        "anchor_top1_accuracy": (
            1.0 if expected_anchors and anchor_values and anchor_values[0] in expected_anchors else 0.0
        ) if expected_anchors else None,
        "anchor_recall_at_5": _recall(set(anchor_values[:5]), expected_anchors),
        "execution_flow_step_recall": _recall(set(execution_values), expected_execution),
        "execution_flow_order_accuracy": (
            sum(
                index < len(execution_values) and execution_values[index] == expected
                for index, expected in enumerate(expected_order)
            ) / len(expected_order)
            if expected_order else None
        ),
        "integration_repo_recall": _recall(integration_repos, expected_integration_repos),
        "surface_recall": _recall(found_surfaces, expected_surfaces),
        "program_slice_statement_count": float(len((runtime.get("program_slice") or {}).get("statements") or [])),
        "hypothesis_supported_rate": (
            sum(item.get("status") == "supported" for item in (runtime.get("hypothesis_ledger") or {}).get("items") or [])
            / len((runtime.get("hypothesis_ledger") or {}).get("items") or [])
        ) if (runtime.get("hypothesis_ledger") or {}).get("items") else None,
        "frontier_blocker_count": float(len((runtime.get("evidence_frontier") or {}).get("items") or [])),
        "first_useful_checkpoint_rate": 1.0 if runtime.get("first_useful_checkpoint") else 0.0,
    }


def evaluate_m365_response(response: str, required_evidence_ids: Iterable[str] = ()) -> dict[str, Any]:
    """Deterministically score an attached M365 response without invoking a model."""
    required = {str(value) for value in required_evidence_ids}
    present = {value for value in required if value in response}
    final_sections = (
        "Ticket interpretation", "Verified current behavior", "Root cause", "Exact repository",
        "Tests", "Validation", "Edge cases", "Implementation order",
    )
    return {
        "final_solution": "FINAL_SOLUTION" in response,
        "evidence_id_recall": len(present) / len(required) if required else None,
        "final_contract_coverage": sum(section.casefold() in response.casefold() for section in final_sections) / len(final_sections),
        "repeated_retrieval_requests": max(0, response.count("INVESTIGATION_REQUEST") - 1),
        "unsupported_authority_claims": sum(
            token in response for token in ("Program Slice proves", "Atlas card proves", "historical ticket proves")
        ),
    }


def _evaluate_public_v5_waves(
    settings: Settings,
    ticket: str,
    requests: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, float | int | None]]:
    """Drive the public ticket/context protocol; never invoke a hosted model."""
    from .core import create_context, session_state, start_session

    start_session(settings, ticket, str(requests[0].get("objective") or ticket))
    contents: list[str] = []
    ready_round: int | None = None
    nbe_useful = 0
    previous_context: str | None = None
    selected_requests = requests[:1] if "multi_wave" in settings.evaluation_ablations else requests
    for index, raw in enumerate(selected_requests, 1):
        allowed = {
            "version", "mode", "objective", "runtime_facts", "hypotheses", "required", "resolve",
            "anchors", "base_context_id", "checkpoint", "wave",
        }
        request = {key: value for key, value in json.loads(json.dumps(raw)).items() if key in allowed}
        request["version"] = 5
        request["wave"] = index
        if previous_context:
            request["base_context_id"] = previous_context
        else:
            request.pop("base_context_id", None)
        content, _, _ = create_context(
            settings,
            ticket,
            json.dumps({"INVESTIGATION_REQUEST": request}, ensure_ascii=False),
        )
        contents.append(content)
        state = session_state(settings, ticket)
        runtime = dict(state.get("investigation_runtime") or {})
        previous_context = str(state.get("last_context_id") or "") or None
        history = state.get("request_history") or []
        current_new_evidence = int((history[-1] if history else {}).get("new_evidence") or 0)
        if index > 1 and current_new_evidence > 0:
            nbe_useful += 1
        if ready_round is None and runtime.get("stop_reason") == "coverage_satisfied":
            ready_round = index
        if runtime.get("stop_reason") in {"coverage_satisfied", "no_progress"}:
            break
    final_state = session_state(settings, ticket)
    runtime = dict(final_state.get("investigation_runtime") or {})
    full_bytes = len(contents[0].encode("utf-8")) if contents else 0
    delta_values = [len(value.encode("utf-8")) for value in contents[1:] if value.startswith("# PROJECT BRAIN CONTEXT DELTA")]
    delta_bytes = sum(delta_values) / len(delta_values) if delta_values else None
    return runtime, {
        "full_context_chars": float(full_bytes),
        "delta_context_chars": float(delta_bytes) if delta_bytes is not None else None,
        "delta_reduction_percent": (
            max(0.0, (1.0 - float(delta_bytes) / full_bytes) * 100.0)
            if delta_bytes is not None and full_bytes else None
        ),
        "delta_context_reduction": (
            max(0.0, 1.0 - float(delta_bytes) / full_bytes)
            if delta_bytes is not None and full_bytes else None
        ),
        "brain_rounds_until_ready": float(ready_round) if ready_round is not None else None,
        "no_progress_rounds": float(final_state.get("no_progress_rounds") or 0),
        "focused_followup_count": float(max(0, len(contents) - 1)),
        "next_best_evidence_usefulness": (
            nbe_useful / max(1, len(contents) - 1) if len(contents) > 1 else None
        ),
        "rounds_until_final_solution": None,
    }


@workspace_exclusive
def evaluate_golden(
    settings: Settings,
    suite_path: str | Path,
    *,
    split: str | None = None,
    limit: int = 20,
    evaluation_ablation: set[str] | None = None,
) -> dict[str, Any]:
    """Replay local, hand-labelled tickets without leaking request text to metrics."""
    unknown_ablations = set(evaluation_ablation or ()) - V1_EVALUATION_ABLATIONS
    if unknown_ablations:
        raise ValueError(f"unknown v1 evaluation ablation(s): {', '.join(sorted(unknown_ablations))}")
    path = Path(suite_path).expanduser().resolve()
    suite, raw = _load(path)
    cases = suite.get("cases") or suite.get("tickets") or []
    if not isinstance(cases, list) or not cases:
        raise ValueError("golden evaluation suite needs a non-empty cases list")
    if len(cases) > MAX_EVALUATION_CASES:
        raise ValueError("golden evaluation suite exceeds its case limit")
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
        request = _request(item.get("request"))
        raw_waves = item.get("waves") or [request]
        if not isinstance(raw_waves, list) or not raw_waves:
            raise ValueError(f"golden case {identifier} waves must be a non-empty list")
        if len(raw_waves) > MAX_EVALUATION_WAVES_PER_CASE:
            raise ValueError(f"golden case {identifier} exceeds its wave limit")
        waves = [_request(value) for value in raw_waves]
        selected.append({
            "id": identifier, "split": case_split, "request": request, "waves": waves,
            "expect": item.get("expect") or item,
        })
    if not selected:
        raise ValueError("no golden cases matched the requested split")

    from .core import pack_context, retrieve_context

    reports: list[dict[str, Any]] = []
    ablations = frozenset(evaluation_ablation or ())
    for case in selected:
        request = json.loads(json.dumps(case["request"]))
        ranking_settings = replace(settings, evaluation_ablations=ablations)
        if ablations:
            request["_evaluation_ablation"] = sorted(ablations)
        bundle = retrieve_context(ranking_settings, request)
        pack_started = time.perf_counter()
        full_context = pack_context(ranking_settings, f"EVAL-{case['id']}", 1, bundle)
        context_pack_ms = (time.perf_counter() - pack_started) * 1000
        ranking = _ranking(bundle)
        raw_ranking = _raw_ranking(bundle)
        expect = case["expect"] if isinstance(case["expect"], dict) else {}
        runtime: dict[str, Any] = {}
        protocol_metrics: dict[str, float | int | None] = {
            "full_context_chars": float(len(full_context.encode("utf-8"))),
            "delta_context_chars": None, "delta_reduction_percent": None,
            "delta_context_reduction": None, "brain_rounds_until_ready": None,
            "no_progress_rounds": None, "focused_followup_count": None,
            "next_best_evidence_usefulness": None, "rounds_until_final_solution": None,
        }
        if int(request.get("version") or 1) == 5 and bundle.atlas_generation is not None:
            with tempfile.TemporaryDirectory(prefix="brain-v1-evaluation-") as temporary:
                evaluation_root = Path(temporary).resolve()
                evaluation_runs = evaluation_root / "runs"
                evaluation_generated = evaluation_root / "generated"
                evaluation_runs.mkdir(mode=0o700)
                evaluation_generated.mkdir(mode=0o700)
                evaluation_settings = replace(
                    ranking_settings,
                    runs_dir=evaluation_runs,
                    generated_dir=evaluation_generated,
                    evaluation_ablations=ablations,
                    persist_investigation_records=False,
                )
                runtime, protocol_metrics = _evaluate_public_v5_waves(
                    evaluation_settings,
                    f"EVAL-{hashlib.sha256(str(case['id']).encode()).hexdigest()[:12]}",
                    case["waves"],
                )
        runtime_metrics = _runtime_metrics(runtime, expect) if runtime else {}
        m365_metrics = evaluate_m365_response(
            str(expect.get("m365_response") or ""), expect.get("required_evidence_ids") or [],
        ) if expect.get("m365_response") else {}
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
            **protocol_metrics,
            "m365_evaluation_mode": "attached-response contract only; live M365 behavior is an external gate",
            "evaluation_execution_model": (
                "ranking retrieval and protocol-v5 replay are separate measured executions; "
                "total_ms is ranking retrieval latency, not end-to-end protocol latency"
            ),
            "process_peak_rss_mb": _peak_rss_mb(),
            "stale_warning": any("stale" in warning.lower() for warning in bundle.warnings),
            **runtime_metrics,
            **{f"m365_{key}": value for key, value in m365_metrics.items()},
        })

    def average(key: str) -> float | None:
        values = [float(item[key]) for item in reports if isinstance(item.get(key), (float, int))]
        return round(sum(values) / len(values), 6) if values else None

    report = {
        "suite_hash": hashlib.sha256(raw).hexdigest(),
        "suite_name": str(suite.get("name") or path.name),
        "evaluation_ablation": sorted(evaluation_ablation or set()),
        "evaluation_execution_model": (
            "ranking retrieval and protocol-v5 replay are separate measured executions; latencies are not combined"
        ),
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
            "anchor_top1_accuracy": average("anchor_top1_accuracy"),
            "anchor_recall_at_5": average("anchor_recall_at_5"),
            "execution_flow_step_recall": average("execution_flow_step_recall"),
            "execution_flow_order_accuracy": average("execution_flow_order_accuracy"),
            "integration_repo_recall": average("integration_repo_recall"),
            "surface_recall": average("surface_recall"),
            "program_slice_statement_count": average("program_slice_statement_count"),
            "hypothesis_supported_rate": average("hypothesis_supported_rate"),
            "frontier_blocker_count": average("frontier_blocker_count"),
            "first_useful_checkpoint_rate": average("first_useful_checkpoint_rate"),
            "m365_evidence_id_recall": average("m365_evidence_id_recall"),
            "m365_final_contract_coverage": average("m365_final_contract_coverage"),
            "m365_repeated_retrieval_requests": average("m365_repeated_retrieval_requests"),
            "m365_unsupported_authority_claims": average("m365_unsupported_authority_claims"),
        },
    }
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    atomic_managed_text_write(
        settings.state_dir,
        settings.state_dir / "golden-eval.json",
        json.dumps(report, indent=2) + "\n",
    )
    from .catalog import record_metric_run

    record_metric_run(settings, "golden_evaluation", {"suite_hash": report["suite_hash"], "summary": report["summary"]})
    return report

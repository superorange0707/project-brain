from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from brain.atlas import (
    MAX_CHANGE_GIT_OPERATIONS,
    MAX_CHANGE_PATH_CHARS,
    _change_rows,
    _retain_change_rows,
    _route_cache_identity,
    atlas_components,
    build_atlas,
    initial_coverage_map,
    initial_investigation_memory,
    record_investigation,
    route,
    similar_investigations,
    update_investigation,
)
from brain.agent import create_m365_agent_kit
from brain.catalog import current_generation_ref
from brain.core import (
    Evidence,
    create_context,
    load_settings,
    parse_context_request,
    retrieve_context,
    session_state,
    snapshot_indexes,
    start_session,
)
from brain.locks import model_lane
from brain.evaluation import evaluate_golden
from brain.investigation import (
    PREFETCH_SCHEMA_VERSION,
    _prefetch_compatibility_identity,
    _valid_prefetch_envelope,
)


class AtlasV09Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        repo = self.root / "service"
        (repo / "src").mkdir(parents=True)
        (repo / "src/service.py").write_text(
            "class EligibilityService:\n"
            "    def recalculate(self, customer):\n"
            "        return policy(customer)\n\n"
            "def policy(customer):\n"
            "    return customer.active\n",
            encoding="utf-8",
        )
        (repo / "tests").mkdir()
        (repo / "tests/test_service.py").write_text(
            "from src.service import EligibilityService\n\n"
            "def test_recalculate():\n"
            "    assert EligibilityService().recalculate(type('C', (), {'active': True})())\n",
            encoding="utf-8",
        )
        (repo / "config.yml").write_text("red_team_checkpoint_marker: true\n", encoding="utf-8")
        config = self.root / "brain.toml"
        config.write_text(
            "[project]\nname='atlas-test'\nstate_dir='state'\nruns_dir='.runs'\ngenerated_dir='generated'\n"
            "[knowledge]\npath='knowledge'\n"
            "[[repositories]]\nname='service'\npath='service'\ndescription='eligibility service'\ntags=['eligibility']\n",
            encoding="utf-8",
        )
        self.settings = load_settings(config)
        self.state, _ = snapshot_indexes(self.settings, changed_only=False)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_hierarchy_graph_cards_cache_and_incremental_reuse_are_generation_scoped(self) -> None:
        generation = current_generation_ref(self.settings)
        self.assertIsNotNone(generation)
        self.assertEqual("ready", generation.component("hierarchy")["status"])
        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            card_count = connection.execute(
                "SELECT COUNT(*) FROM generation_cards WHERE generation=?", (generation.generation,)
            ).fetchone()[0]
            card_index = connection.execute(
                "SELECT schema_version,card_count,term_count FROM generation_card_indexes WHERE generation=?",
                (generation.generation,),
            ).fetchone()
            self.assertEqual(card_count, card_index[1])
            self.assertGreater(card_index[2], 0)
            self.assertGreater(connection.execute(
                "SELECT COUNT(*) FROM generation_entities WHERE generation=?", (generation.generation,)
            ).fetchone()[0], 2)
            self.assertGreater(connection.execute(
                "SELECT COUNT(*) FROM generation_edges WHERE generation=?", (generation.generation,)
            ).fetchone()[0], 1)
            self.assertEqual({"repo", "module", "entity"}, {
                row[0] for row in connection.execute(
                    "SELECT DISTINCT c.level FROM generation_cards g JOIN atlas_cards c ON c.card_id=g.card_id WHERE g.generation=?",
                    (generation.generation,),
                )
            })
        finally:
            connection.close()
        request = {"version": 4, "objective": "EligibilityService recalculate", "searches": [], "paths": [],
                   "symbols": [], "files": [], "history": [], "expand": []}
        first = route(self.settings, request["objective"], request, generation)
        second = route(self.settings, request["objective"], request, generation)
        self.assertFalse(first["cache_hit"])
        self.assertEqual("precomputed", first["routing_index"])
        self.assertLessEqual(first["cards_considered"], card_count)
        self.assertTrue(second["cache_hit"])
        self.assertTrue(any(item["path"] == "src/service.py" for item in second["entities"]))
        from brain import atlas as atlas_module

        atlas_module._ROUTE_CACHE_SEALS.clear()
        cold_process = route(self.settings, request["objective"], request, generation)
        self.assertTrue(cold_process["cache_hit"])
        self.assertEqual(second["entities"], cold_process["entities"])
        for tamper in ("score", "subset", "reorder"):
            connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
            try:
                cache_key, raw = connection.execute(
                    "SELECT cache_key,payload_json FROM atlas_retrieval_cache WHERE generation=? LIMIT 1",
                    (generation.generation,),
                ).fetchone()
                poisoned = json.loads(raw)
                if tamper == "score":
                    poisoned["entities"][0]["score"] = float(poisoned["entities"][0]["score"]) + 123.0
                    poisoned["cache_identity"] = _route_cache_identity(poisoned)
                elif tamper == "subset":
                    poisoned["entities"] = poisoned["entities"][:-1]
                    poisoned["candidates"] = poisoned["entities"]
                else:
                    poisoned["entities"] = list(reversed(poisoned["entities"]))
                    poisoned["candidates"] = poisoned["entities"]
                connection.execute(
                    "UPDATE atlas_retrieval_cache SET payload_json=? WHERE generation=? AND cache_key=?",
                    (json.dumps(poisoned, sort_keys=True), generation.generation, cache_key),
                )
                connection.commit()
            finally:
                connection.close()
            recovered = route(self.settings, request["objective"], request, generation)
            self.assertFalse(recovered["cache_hit"], tamper)
            self.assertTrue(any(item["path"] == "src/service.py" for item in recovered["entities"]))
        canonical_entities = route(self.settings, request["objective"], request, generation)["entities"]
        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            from brain.atlas import _hash

            cache_key, raw = connection.execute(
                "SELECT cache_key,payload_json FROM atlas_retrieval_cache WHERE generation=? LIMIT 1",
                (generation.generation,),
            ).fetchone()
            poisoned = json.loads(raw)
            poisoned["entities"] = list(reversed(poisoned["entities"]))
            poisoned["entities"][0]["score"] = 999999.0
            poisoned["candidates"] = poisoned["entities"]
            poisoned["cache_identity"] = _route_cache_identity(poisoned)
            poisoned_json = json.dumps(poisoned, sort_keys=True)
            connection.execute(
                "UPDATE atlas_retrieval_cache SET payload_json=?,payload_hash=? WHERE generation=? AND cache_key=?",
                (
                    poisoned_json,
                    _hash("route-cache-row", generation.generation, cache_key, poisoned_json),
                    generation.generation,
                    cache_key,
                ),
            )
            connection.commit()
        finally:
            connection.close()
        rederived = route(self.settings, request["objective"], request, generation)
        self.assertFalse(rederived["cache_hit"])
        self.assertEqual(canonical_entities, rederived["entities"])
        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            from brain import atlas as atlas_module
            from brain.atlas import _hash

            cache_key, raw = connection.execute(
                "SELECT cache_key,payload_json FROM atlas_retrieval_cache WHERE generation=? LIMIT 1",
                (generation.generation,),
            ).fetchone()
            poisoned = json.loads(raw)
            poisoned["entities"] = poisoned["entities"][:-1]
            poisoned["candidates"] = poisoned["entities"]
            poisoned["cache_identity"] = _route_cache_identity(poisoned)
            poisoned_json = json.dumps(poisoned, sort_keys=True)
            poisoned_hash = _hash("route-cache-row", generation.generation, cache_key, poisoned_json)
            connection.execute(
                "UPDATE atlas_retrieval_cache SET payload_json=?,payload_hash=? WHERE generation=? AND cache_key=?",
                (poisoned_json, poisoned_hash, generation.generation, cache_key),
            )
            connection.execute(
                "UPDATE atlas_retrieval_cache_registrations SET payload_hash=? WHERE generation=? AND cache_key=?",
                (poisoned_hash, generation.generation, cache_key),
            )
            connection.commit()
            atlas_module._ROUTE_CACHE_SEALS.clear()
        finally:
            connection.close()
        cold_rederived = route(self.settings, request["objective"], request, generation)
        self.assertFalse(cold_rederived["cache_hit"])
        self.assertEqual(canonical_entities, cold_rederived["entities"])
        uncached_request = {**request, "_evaluation_ablation": ["generation_cache"]}
        self.assertFalse(route(self.settings, request["objective"], uncached_request, generation)["cache_hit"])
        self.assertFalse(route(self.settings, request["objective"], uncached_request, generation)["cache_hit"])
        graphless = route(self.settings, request["objective"], {**request, "_evaluation_ablation": ["graph"]}, generation)
        self.assertEqual([], graphless["graph_edges"])
        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            connection.execute("DELETE FROM generation_card_indexes WHERE generation=?", (generation.generation,))
            connection.commit()
        finally:
            connection.close()
        unavailable = route(self.settings, request["objective"], uncached_request, generation)
        self.assertEqual("unavailable", unavailable["routing_index"])
        self.assertEqual([], unavailable["entities"])
        flat = route(self.settings, request["objective"], {**request, "_evaluation_ablation": ["flat"]}, generation)
        self.assertEqual([], flat["entities"])
        payload = build_atlas(self.settings, self.state)
        self.assertEqual(0, payload["delta"]["parsed_files"])
        self.assertGreaterEqual(payload["delta"]["reused_files"], 2)
        source = self.root / "service/src/service.py"
        source.write_text(source.read_text(encoding="utf-8") + "\ndef invalidate():\n    return True\n", encoding="utf-8")
        self.state, _ = snapshot_indexes(self.settings, changed_only=True)
        generation_two = current_generation_ref(self.settings)
        self.assertNotEqual(generation.identity, generation_two.identity)
        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            delta = json.loads(connection.execute(
                "SELECT payload_json FROM atlas_refresh_deltas WHERE generation=?", (generation_two.generation,)
            ).fetchone()[0])
        finally:
            connection.close()
        self.assertEqual([{"path": "src/service.py", "repo": "service"}], delta["modified"])
        self.assertEqual(1, delta["files_changed"])
        self.assertGreater(delta["entities_reused"], 0)
        self.assertGreater(delta["entities_rebuilt"], 0)
        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM generation_edges g JOIN atlas_edges e ON e.edge_id=g.edge_id "
                "LEFT JOIN generation_entities target ON target.generation=g.generation AND target.entity_id=e.target_id "
                "WHERE g.generation=? AND json_extract(e.metadata_json, '$.resolved')=1 AND target.entity_id IS NULL",
                (generation_two.generation,),
            ).fetchone()[0])
        finally:
            connection.close()
        isolated = route(self.settings, request["objective"], request, generation_two)
        self.assertFalse(isolated["cache_hit"])
        no_change = build_atlas(self.settings, self.state)
        self.assertEqual(0, no_change["delta"]["files_changed"])
        self.assertEqual(no_change["delta"]["entities"], no_change["delta"]["entities_reused"])
        self.state, _ = snapshot_indexes(self.settings, changed_only=True)
        self.assertEqual(generation_two.identity, current_generation_ref(self.settings).identity)

    def test_equal_count_term_substitution_fails_closed_for_cards_and_changes(self) -> None:
        from brain import atlas as atlas_module
        from brain.catalog import _term_projection_hash

        generation = current_generation_ref(self.settings)
        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            card_id, original_term = connection.execute(
                "SELECT t.card_id,t.term FROM atlas_card_terms t JOIN generation_cards g "
                "ON g.card_id=t.card_id WHERE g.generation=? ORDER BY t.card_id,t.term LIMIT 1",
                (generation.generation,),
            ).fetchone()
            connection.execute(
                "UPDATE atlas_card_terms SET term='forgeduniqueterm' WHERE card_id=? AND term=?",
                (card_id, original_term),
            )

            file_entity = connection.execute(
                "SELECT e.entity_id,e.path FROM generation_entities g JOIN atlas_entities e "
                "ON e.entity_id=g.entity_id WHERE g.generation=? AND e.kind='file' LIMIT 1",
                (generation.generation,),
            ).fetchone()
            connection.execute(
                "INSERT INTO atlas_changes(change_id,repo,commit_sha,committed_at,ticket,path,old_path,status,"
                "additions,deletions,metadata_json) VALUES ('change-test','service','sha-test',NULL,'ABC-1',?,NULL,'M',1,0,'{}')",
                (file_entity[1],),
            )
            connection.execute(
                "INSERT INTO generation_changes(generation,change_id,snapshot_sha) VALUES (?,?,?)",
                (generation.generation, "change-test", generation.snapshots["service"]),
            )
            connection.execute(
                "INSERT INTO atlas_change_terms(change_id,schema_version,term) VALUES "
                "('change-test','atlas-change-terms-v1','customer')",
            )
            valid_change_hash = _term_projection_hash(
                connection, generation.generation, kind="change", schema_version="atlas-change-terms-v1",
            )
            connection.execute(
                "UPDATE generation_change_indexes SET change_count=1,term_count=1,projection_hash=? "
                "WHERE generation=?",
                (valid_change_hash, generation.generation),
            )
            connection.execute(
                "UPDATE atlas_change_terms SET term='forgedchangeuniqueterm' WHERE change_id='change-test'",
            )
            connection.commit()
        finally:
            connection.close()
        atlas_module._TERM_INDEX_VALIDATION_CACHE.clear()

        request = {
            "version": 4, "objective": "forgeduniqueterm forgedchangeuniqueterm",
            "searches": [], "paths": [], "symbols": [], "files": [], "history": [], "expand": [],
            "_evaluation_ablation": ["generation_cache"],
        }
        result = route(self.settings, request["objective"], request, generation)
        self.assertEqual("unavailable", result["routing_index"])
        self.assertEqual("unavailable", result["change_routing_index"])
        self.assertEqual([], result["entities"])

    def test_windows_term_indexes_use_invalidation_markers_without_full_card_scan(self) -> None:
        generation = current_generation_ref(self.settings)
        request = {
            "version": 4, "objective": "EligibilityService recalculate",
            "searches": [], "paths": [], "symbols": [], "files": [], "history": [], "expand": [],
            "_evaluation_ablation": ["generation_cache"],
        }
        with mock.patch("brain.atlas.os.name", "nt"), mock.patch(
            "brain.catalog._term_projection_hash", side_effect=AssertionError("full term scan")
        ):
            healthy = route(self.settings, request["objective"], request, generation)
        self.assertEqual("precomputed", healthy["routing_index"])

        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            card_id, original_term = connection.execute(
                "SELECT t.card_id,t.term FROM atlas_card_terms t JOIN generation_cards g "
                "ON g.card_id=t.card_id WHERE g.generation=? ORDER BY t.card_id,t.term LIMIT 1",
                (generation.generation,),
            ).fetchone()
            connection.execute(
                "UPDATE atlas_card_terms SET term='windowsforgedterm' WHERE card_id=? AND term=?",
                (card_id, original_term),
            )
            connection.commit()
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM generation_card_indexes WHERE generation=?", (generation.generation,),
            ).fetchone())
        finally:
            connection.close()
        with mock.patch("brain.atlas.os.name", "nt"), mock.patch(
            "brain.catalog._term_projection_hash", side_effect=AssertionError("full term scan")
        ):
            poisoned = route(self.settings, request["objective"], request, generation)
        self.assertEqual("unavailable", poisoned["routing_index"])

    def test_warm_route_cache_revalidates_generation_membership(self) -> None:
        generation = current_generation_ref(self.settings)
        request = {
            "version": 4, "objective": "EligibilityService recalculate",
            "searches": [], "paths": [], "symbols": [], "files": [], "history": [], "expand": [],
        }
        first = route(self.settings, request["objective"], request, generation)
        warm = route(self.settings, request["objective"], request, generation)
        self.assertTrue(warm["cache_hit"])
        poisoned_id = str(first["entities"][0]["entity_id"])
        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            connection.execute(
                "DELETE FROM generation_entities WHERE generation=? AND entity_id=?",
                (generation.generation, poisoned_id),
            )
            connection.commit()
        finally:
            connection.close()

        revalidated = route(self.settings, request["objective"], request, generation)
        self.assertFalse(revalidated["cache_hit"])
        self.assertNotIn(poisoned_id, {
            str(item["entity_id"]) for item in revalidated["entities"]
        })

    def test_query_plan_time_budget_stops_later_stages(self) -> None:
        from brain.retrieval.models import QueryOperation, QueryPlan

        request = {
            "version": 1, "objective": "EligibilityService", "searches": [{"query": "never-run", "repos": []}],
            "paths": [], "symbols": [], "files": [], "history": [], "expand": [],
        }
        plan = QueryPlan(
            request["objective"], (QueryOperation("search", "never-run"),), timeout_ms=1,
            requested_operations=1,
        )

        def slow_route(*args, **kwargs):
            time.sleep(.01)
            return {"schema": "test", "repos": [], "modules": [], "entities": [], "candidates": [],
                    "graph_edges": [], "cache_hit": False}

        with (
            mock.patch("brain.retrieval.compile_request", return_value=plan),
            mock.patch("brain.atlas.route", side_effect=slow_route),
            mock.patch("brain.core.search") as lexical,
        ):
            bundle = retrieve_context(self.settings, request)
        lexical.assert_not_called()
        self.assertEqual("time_budget", bundle.trace["stop_reason"])
        self.assertLessEqual(len(bundle.trace["final_repo_scope"]), self.settings.widen_repo_limit)

    def test_time_budget_never_reports_an_unchecked_semantic_component_ready(self) -> None:
        from brain.retrieval.models import QueryOperation, QueryPlan

        (self.settings.state_dir / "edition.json").write_text(
            json.dumps({"edition": "semantic"}), encoding="utf-8",
        )
        request = {
            "version": 1, "objective": "EligibilityService", "searches": [{"query": "never-run", "repos": []}],
            "paths": [], "symbols": [], "files": [], "history": [], "expand": [],
        }
        plan = QueryPlan(
            request["objective"], (QueryOperation("search", "never-run"),), timeout_ms=1,
            requested_operations=1,
        )

        def slow_route(*args, **kwargs):
            time.sleep(.01)
            return {"schema": "test", "repos": [], "modules": [], "entities": [], "candidates": [],
                    "graph_edges": [], "cache_hit": False}

        with (
            mock.patch("brain.retrieval.compile_request", return_value=plan),
            mock.patch("brain.atlas.route", side_effect=slow_route),
            mock.patch("brain.core.search") as lexical,
        ):
            bundle = retrieve_context(self.settings, request)
        lexical.assert_not_called()
        self.assertEqual("time_budget", bundle.trace["stop_reason"])
        self.assertEqual("unavailable", bundle.trace["semantic_status"])
        self.assertTrue(any("Semantic" in warning for warning in bundle.warnings))

    def test_expired_optional_stages_still_hydrate_one_exact_source(self) -> None:
        from brain.retrieval.models import QueryPlan

        request = {
            "version": 1, "objective": "EligibilityService", "searches": [],
            "paths": [], "symbols": [], "files": [], "history": [], "expand": [],
        }
        plan = QueryPlan(request["objective"], (), timeout_ms=1, requested_operations=0)

        def slow_route(*args, **kwargs):
            time.sleep(.01)
            return {
                "schema": "test", "repos": ["service"], "modules": [], "entities": [],
                "candidates": [{
                    "repo": "service", "path": "src/service.py", "line": 1,
                    "kind": "entity", "score": 100, "found_by": ["Atlas hierarchical router"],
                }],
                "graph_edges": [], "cache_hit": False,
            }

        with (
            mock.patch("brain.retrieval.compile_request", return_value=plan),
            mock.patch("brain.atlas.route", side_effect=slow_route),
        ):
            bundle = retrieve_context(self.settings, request)
        self.assertEqual("time_budget", bundle.trace["stop_reason"])
        self.assertEqual(["src/service.py"], [item.path for item in bundle.evidence])
        self.assertIsNotNone(bundle.metrics["time_to_first_verified_evidence_ms"])

    def test_incremental_refresh_records_rename_delete_and_add_together(self) -> None:
        repository = self.root / "service"
        (repository / "config.yml").rename(repository / "renamed-config.yml")
        (repository / "tests/test_service.py").unlink()
        (repository / "src/added.py").write_text("def added():\n    return True\n", encoding="utf-8")

        self.state, _ = snapshot_indexes(self.settings, changed_only=True)
        generation = current_generation_ref(self.settings)
        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            delta = json.loads(connection.execute(
                "SELECT payload_json FROM atlas_refresh_deltas WHERE generation=?", (generation.generation,)
            ).fetchone()[0])
        finally:
            connection.close()

        self.assertIn({"repo": "service", "old_path": "config.yml", "path": "renamed-config.yml"}, delta["renamed"])
        self.assertIn({"repo": "service", "path": "tests/test_service.py"}, delta["deleted"])
        self.assertIn({"repo": "service", "path": "src/added.py"}, delta["added"])

    def test_route_cache_isolated_by_runtime_facts_and_investigation_priors(self) -> None:
        generation = current_generation_ref(self.settings)
        fact_one = {"version": 4, "objective": "Runtime routing probe", "runtime_facts": ["EligibilityService"],
                    "searches": [], "paths": [], "symbols": [], "files": [], "history": []}
        fact_two = {**fact_one, "runtime_facts": ["test_recalculate"]}
        first = route(self.settings, fact_one["objective"], fact_one, generation)
        second = route(self.settings, fact_two["objective"], fact_two, generation)
        self.assertFalse(first["cache_hit"])
        self.assertFalse(second["cache_hit"])
        self.assertNotEqual(
            [item["entity_id"] for item in first["entities"]],
            [item["entity_id"] for item in second["entities"]],
        )

        entity_id = first["entities"][0]["entity_id"]
        objective = "Prior isolation probe"
        with_prior = {"version": 4, "objective": objective, "searches": [], "paths": [], "symbols": [],
                      "files": [], "history": [], "_prior_entity_ids": [entity_id]}
        without_prior = {key: value for key, value in with_prior.items() if key != "_prior_entity_ids"}
        self.assertTrue(route(self.settings, objective, with_prior, generation)["entities"])
        isolated = route(self.settings, objective, without_prior, generation)
        self.assertFalse(isolated["cache_hit"])
        self.assertEqual([], isolated["entities"])
        wrong_prefetch = route(
            self.settings,
            objective,
            {**without_prior, "_prefetch": {"candidate_ids": [entity_id]}},
            generation,
        )
        self.assertTrue(wrong_prefetch["cache_hit"])
        self.assertEqual([], wrong_prefetch["entities"])
        self.assertEqual(0, wrong_prefetch["prefetch_reused"])

        padding = [f"missing-{index:03d}" for index in range(200)]
        bounded_objective = "Bounded prior isolation probe"
        valid_in_bound = route(self.settings, bounded_objective, {
            **without_prior, "objective": bounded_objective, "_prior_entity_ids": [entity_id, *padding],
        }, generation)
        valid_out_of_bound = route(self.settings, bounded_objective, {
            **without_prior, "objective": bounded_objective, "_prior_entity_ids": [*padding, entity_id],
        }, generation)
        self.assertTrue(valid_in_bound["entities"])
        self.assertFalse(valid_out_of_bound["cache_hit"])
        self.assertEqual([], valid_out_of_bound["entities"])

    def test_prefetch_is_generation_validated_causal_and_never_wins_weak_overlap(self) -> None:
        generation = current_generation_ref(self.settings)
        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            entity_id = str(connection.execute(
                "SELECT entity_id FROM generation_entities WHERE generation=? ORDER BY entity_id LIMIT 1",
                (generation.generation,),
            ).fetchone()[0])
        finally:
            connection.close()

        def prefetch(objective: str) -> dict[str, object]:
            result: dict[str, object] = {
                "status": "ready", "generation": generation.generation,
                "atlas_generation_id": generation.identity, "objective": objective,
                "repos": [], "modules": [], "candidate_ids": [entity_id], "anchor_ids": [],
                "anchor_status": "ready", "schema_version": PREFETCH_SCHEMA_VERSION,
            }
            result["compatibility_identity"] = _prefetch_compatibility_identity(generation, result)
            return result

        objective = "alphawidget betagateway"
        request = {"version": 4, "objective": objective, "searches": [], "paths": [], "symbols": [],
                   "files": [], "history": [], "_prefetch": prefetch(objective),
                   "_evaluation_ablation": ["generation_cache"]}
        routed = route(self.settings, objective, request, generation)
        without = route(
            self.settings, objective,
            {**request, "_evaluation_ablation": ["generation_cache", "prefetch"]}, generation,
        )
        self.assertIn(entity_id, {item["entity_id"] for item in routed["entities"]})
        self.assertNotIn(entity_id, {item["entity_id"] for item in without["entities"]})
        weak = prefetch("trace customer failure")
        self.assertFalse(_valid_prefetch_envelope(generation, weak, "trace unrelated billing"))
        canonical = prefetch(objective)
        poisoned = []
        for key, replacement in (
            ("candidate_ids", ["sha256:replacement"]),
            ("candidate_ids", [entity_id, "sha256:appended"]),
            ("repos", ["service", "other"]),
            ("modules", ["module-b", "module-a"]),
            ("anchor_ids", ["anchor-b", "anchor-a"]),
        ):
            value = {**canonical, key: replacement}
            poisoned.append(value)
        poisoned.extend((
            {**canonical, "candidate_ids": [entity_id, entity_id]},
            {**canonical, "candidate_ids": [f"candidate-{index}" for index in range(501)]},
            {**canonical, "objective": "different objective"},
        ))
        for value in poisoned:
            self.assertFalse(
                _valid_prefetch_envelope(generation, value, objective),
                f"accepted prefetch mutation: {value}",
            )

    def test_hierarchical_route_promotes_low_rank_repo_from_compound_entity_name(self) -> None:
        root = self.root / "low-rank-probe"
        root.mkdir()
        lines = [
            "[project]", "name='low-rank-probe'", "state_dir='state'", "runs_dir='.runs'",
            "generated_dir='generated'", "[graph]", "enabled=false", "[experience]", "enabled=false",
            "[retrieval]", "initial_repo_limit=6", "widen_repo_limit=16",
        ]
        for index in range(50):
            name = f"repo{index:02d}"
            repository = root / name
            repository.mkdir()
            functions = "\n".join(f"def TARGET_decoy_{item}(): return {item}" for item in range(4))
            if index == 49:
                functions += "\ndef TARGET_authoritative(): return 'ONLY_RELEVANT_EVIDENCE'"
            (repository / "service.py").write_text(functions + "\n", encoding="utf-8")
            lines.extend(["[[repositories]]", f"name='{name}'", f"path='{name}'", "description='TARGET service'"])
        config = root / "brain.toml"
        config.write_text("\n".join(lines) + "\n", encoding="utf-8")
        settings = load_settings(config)
        snapshot_indexes(settings, changed_only=False)

        (settings.state_dir / "edition.json").write_text(json.dumps({"edition": "semantic"}), encoding="utf-8")
        with mock.patch("brain.semantic.search_semantic", return_value=[]) as semantic_search:
            bundle = retrieve_context(settings, {
                "version": 4,
                "objective": "TARGET authoritative evidence",
                "searches": [{"query": "TARGET", "repos": []}],
                "paths": [], "symbols": [], "files": [], "history": [], "expand": [],
            })

        self.assertIn("repo49", bundle.trace["initial_repo_scope"])
        semantic_scope = semantic_search.call_args.kwargs["repos"]
        self.assertIn("repo49", semantic_scope)
        self.assertLessEqual(len(semantic_scope), settings.widen_repo_limit)
        self.assertEqual(sorted(semantic_scope), sorted(bundle.trace["semantic_repo_scope"]))
        self.assertTrue(any(
            item.repo == "repo49" and "ONLY_RELEVANT_EVIDENCE" in item.content
            for item in bundle.evidence
        ))

    def test_corrupt_graph_cache_target_is_recomputed(self) -> None:
        generation = current_generation_ref(self.settings)
        request = {"version": 4, "objective": "recalculate policy", "searches": [], "paths": [],
                   "symbols": [], "files": [], "history": []}
        first = route(self.settings, request["objective"], request, generation)
        self.assertTrue(first["graph_edges"])
        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            rowid, raw = connection.execute(
                "SELECT rowid,payload_json FROM atlas_retrieval_cache WHERE generation=?",
                (generation.generation,),
            ).fetchone()
            payload = json.loads(raw)
            payload["graph_edges"][0]["target_id"] = "sha256:deleted-entity"
            connection.execute(
                "UPDATE atlas_retrieval_cache SET payload_json=? WHERE rowid=?",
                (json.dumps(payload), rowid),
            )
            connection.commit()
        finally:
            connection.close()
        rebuilt = route(self.settings, request["objective"], request, generation)
        self.assertFalse(rebuilt["cache_hit"])
        self.assertNotEqual("sha256:deleted-entity", rebuilt["graph_edges"][0]["target_id"])
        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            rowid, raw = connection.execute(
                "SELECT rowid,payload_json FROM atlas_retrieval_cache WHERE generation=?",
                (generation.generation,),
            ).fetchone()
            payload = json.loads(raw)
            payload["entities"][0]["score"] = "not-a-number"
            payload["candidates"] = payload["entities"]
            connection.execute(
                "UPDATE atlas_retrieval_cache SET payload_json=? WHERE rowid=?",
                (json.dumps(payload), rowid),
            )
            connection.commit()
        finally:
            connection.close()
        rebuilt_again = route(self.settings, request["objective"], request, generation)
        self.assertFalse(rebuilt_again["cache_hit"])
        self.assertIsInstance(rebuilt_again["entities"][0]["score"], float)

    def test_change_intelligence_does_not_cross_join_every_entity(self) -> None:
        generation = current_generation_ref(self.settings)
        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        snapshot = generation.snapshots["service"]
        try:
            entities = []
            memberships = []
            for index in range(2_101):
                entity_id = f"noise-{index:04d}"
                path = f"noise/{index}.py"
                entities.append((entity_id, "service", "noise-module", path, 1, 1, path, path, "", "python",
                                 "file", None, f"blob-{index}", "test", "1", f"fingerprint-{index}", "{}"))
                memberships.append((generation.generation, entity_id, snapshot))
            connection.executemany(
                "INSERT INTO atlas_entities(entity_id,repo,module_id,path,line_start,line_end,qualified_name,simple_name,"
                "signature,language,kind,parent_entity_id,blob_sha,extractor,extractor_version,fingerprint,metadata_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                entities,
            )
            connection.executemany(
                "INSERT INTO generation_entities(generation,entity_id,snapshot_sha) VALUES (?,?,?)",
                memberships,
            )
            changes = [
                ("a-change", "service", "commit-a", "2026-01-01T00:00:00+00:00", None, "noise/0.py", None,
                 "modified", 1, 1, json.dumps({"subject": "irrelevant"})),
                ("b-change", "service", "commit-b", "2026-01-02T00:00:00+00:00", "LOWRANKCHANGE",
                 "src/service.py", None, "modified", 1, 1, json.dumps({"subject": "LOWRANKCHANGE"})),
            ]
            connection.executemany(
                "INSERT INTO atlas_changes(change_id,repo,commit_sha,committed_at,ticket,path,old_path,status,additions,"
                "deletions,metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                changes,
            )
            connection.executemany(
                "INSERT INTO generation_changes(generation,change_id,snapshot_sha) VALUES (?,?,?)",
                [(generation.generation, "a-change", snapshot), (generation.generation, "b-change", snapshot)],
            )
            connection.execute(
                "INSERT INTO atlas_change_terms(change_id,schema_version,term) VALUES (?,?,?)",
                ("b-change", "atlas-change-terms-v1", "lowrankchange"),
            )
            from brain.catalog import _term_projection_hash

            projection_hash = _term_projection_hash(
                connection, generation.generation, kind="change", schema_version="atlas-change-terms-v1",
            )
            connection.execute(
                "INSERT INTO generation_change_indexes"
                "(generation,schema_version,change_count,term_count,projection_hash) VALUES (?,?,?,?,?)",
                (generation.generation, "atlas-change-terms-v1", 2, 1, projection_hash),
            )
            connection.commit()
        finally:
            connection.close()
        generation = replace(
            generation,
            components={
                **generation.components,
                "change_intelligence": {
                    **generation.component("change_intelligence"),
                    "status": "ready",
                    "schema_version": "3",
                },
            },
        )
        request = {"version": 4, "objective": "LOWRANKCHANGE", "searches": [], "paths": [], "symbols": [],
                   "files": [], "history": []}
        routed = route(self.settings, request["objective"], request, generation)
        self.assertTrue(any(
            item["path"] == "src/service.py" and "Atlas change intelligence" in item["found_by"]
            for item in routed["entities"]
        ), routed)

    def test_same_repo_relationship_does_not_claim_cross_repo_coverage(self) -> None:
        memory = initial_investigation_memory("flow")
        coverage = initial_coverage_map()
        bundle = SimpleNamespace(
            evidence=[Evidence("service", "src/service.py", 1, 2, "class Service: pass", "code", 90, ["test"])],
            relationships=["service:a CALLS service:b"], history=[], unresolved=[],
            trace={"cross_repo_relationships": False},
        )
        update_investigation(memory, coverage, bundle, "ctx-one")
        self.assertEqual("candidate", coverage["main_execution_flow"])
        self.assertNotEqual("verified", coverage["cross_repo_integration"])
        bundle.trace["cross_repo_relationships"] = True
        update_investigation(memory, coverage, bundle, "ctx-two")
        self.assertEqual("candidate", coverage["cross_repo_integration"])

    def test_local_diff_is_not_promoted_to_verified_investigation_memory(self) -> None:
        memory = initial_investigation_memory("diff")
        coverage = initial_coverage_map()
        bundle = SimpleNamespace(
            evidence=[Evidence("service", "(working tree diff)", 1, 2, "+uncommitted", "local diff", 90, ["local diff"])],
            relationships=[], history=[], unresolved=[], trace={},
        )
        update_investigation(memory, coverage, bundle, "ctx-one")
        self.assertEqual([], memory["verified_facts"])
        self.assertEqual([], memory["implementation_surface"])
        self.assertEqual("not_requested", coverage["production_entry_point"])

    def test_history_navigation_is_not_promoted_to_verified_coverage(self) -> None:
        memory = initial_investigation_memory("history")
        coverage = initial_coverage_map()
        bundle = SimpleNamespace(
            evidence=[], relationships=[], history=["commit abc changed Service"], unresolved=[], trace={},
        )
        update_investigation(memory, coverage, bundle, "ctx-one")
        self.assertEqual("candidate", coverage["history"])

    def test_investigation_evidence_id_preserves_null_delimited_content_identity(self) -> None:
        memory = initial_investigation_memory("identity")
        coverage = initial_coverage_map()
        content = "class Service:\n    pass\n"
        bundle = SimpleNamespace(
            evidence=[Evidence("service", "src/service.py", 1, 2, content, "code", 90, ["test"])],
            relationships=[], history=[], unresolved=[], trace={},
        )
        update_investigation(memory, coverage, bundle, "ctx-one")
        identity = "\0".join(("service", "src/service.py", "1", "2", content))
        expected = "E-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        self.assertEqual(expected, memory["verified_facts"][0]["evidence_id"])
        update_investigation(memory, coverage, bundle, "ctx-two")
        self.assertEqual(1, len(memory["verified_facts"]))

    def test_investigation_memory_retains_failed_searches_and_unresolved_blockers(self) -> None:
        memory = initial_investigation_memory("flow")
        coverage = initial_coverage_map()
        bundle = SimpleNamespace(
            evidence=[Evidence("service", "src/service.py", 1, 2, "class Service: pass", "code", 90, ["test"])],
            relationships=[], history=[], unresolved=["Definition for `MissingService` was not found"], trace={},
        )
        update_investigation(memory, coverage, bundle, "ctx-one")
        bundle.unresolved = []
        update_investigation(memory, coverage, bundle, "ctx-two")
        self.assertIn("Definition for `MissingService` was not found", memory["blocking_unknowns"])
        self.assertTrue(any("MissingService" in item for item in memory["rejected_areas"]))

    def test_cross_ticket_investigation_records_are_byte_item_and_retention_bounded(self) -> None:
        from brain import atlas as atlas_module

        state = {
            "generation": 1,
            "status": "completed",
            "investigation_memory": {
                "objective": "eligibility needle " + ("é" * 100_000),
            },
            "atlas_entity_ids": [f"entity-{index}" for index in range(1_000)],
            "evidence_manifest": [
                {"evidence_id": f"E-{index}", "reference": "service:src/service.py:1-2"}
                for index in range(1_000)
            ],
        }
        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            connection.executemany(
                "INSERT INTO investigation_records "
                "(record_id,ticket,generation,objective,entity_ids_json,evidence_json,outcome,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                [
                    (f"old-{index}", f"OLD-{index}", 1, "old", "[]", "[]", "completed", f"2000-01-0{index + 1}T00:00:00+00:00")
                    for index in range(3)
                ],
            )
            connection.commit()
        finally:
            connection.close()
        with mock.patch.object(atlas_module, "MAX_INVESTIGATION_RECORDS", 3):
            record_investigation(self.settings, "BOUNDED", state)

        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            objective, entities, evidence = connection.execute(
                "SELECT objective,entity_ids_json,evidence_json FROM investigation_records WHERE ticket='BOUNDED'"
            ).fetchone()
            self.assertLessEqual(len(objective.encode("utf-8")), atlas_module.MAX_INVESTIGATION_OBJECTIVE_BYTES)
            self.assertLessEqual(len(entities.encode("utf-8")), atlas_module.MAX_INVESTIGATION_ENTITY_BYTES)
            self.assertLessEqual(len(evidence.encode("utf-8")), atlas_module.MAX_INVESTIGATION_EVIDENCE_BYTES)
            self.assertLessEqual(len(json.loads(entities)), atlas_module.MAX_INVESTIGATION_ENTITY_IDS)
            self.assertLessEqual(len(json.loads(evidence)), atlas_module.MAX_INVESTIGATION_EVIDENCE_ROWS)
            self.assertEqual(3, connection.execute("SELECT COUNT(*) FROM investigation_records").fetchone()[0])
            connection.execute(
                "INSERT INTO investigation_records "
                "(record_id,ticket,generation,objective,entity_ids_json,evidence_json,outcome,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("oversized", "OVERSIZED", 1, "eligibility needle " + ("x" * 40_000), "[]", "[]",
                 "completed", "9999-01-01T00:00:00+00:00"),
            )
            connection.commit()
        finally:
            connection.close()
        matches = similar_investigations(self.settings, "eligibility needle", limit=5)
        self.assertTrue(any(item["ticket"] == "BOUNDED" for item in matches))
        self.assertFalse(any(item["ticket"] == "OVERSIZED" for item in matches))

    def test_stale_delta_base_full_checkpoint_restores_prior_evidence_and_memory(self) -> None:
        start_session(self.settings, "ATLAS-RECOVERY", "Recover all verified evidence after attachment loss.")
        first, _, _ = create_context(self.settings, "ATLAS-RECOVERY", """INVESTIGATION_REQUEST:
  version: 4
  objective: Locate red_team_checkpoint_marker configuration.
  resolve: [red_team_checkpoint_marker]
""")
        self.assertIn("red_team_checkpoint_marker", first)
        recovered, _, _ = create_context(self.settings, "ATLAS-RECOVERY", """INVESTIGATION_REQUEST:
  version: 4
  objective: Locate EligibilityService implementation.
  resolve: [EligibilityService]
  base_context_id: ctx-stale
""")
        self.assertTrue(recovered.startswith("# PROJECT BRAIN CONTEXT\n"))
        self.assertIn("red_team_checkpoint_marker", recovered)
        self.assertIn("## Investigation Memory", recovered)
        state = session_state(self.settings, "ATLAS-RECOVERY")
        self.assertEqual("base_mismatch", state["request_history"][-1]["retrieval"]["checkpoint_reason"])
        self.assertGreaterEqual(len(state["evidence_records"]), 2)

    def test_protocol_v4_prefetch_memory_and_delta_lineage(self) -> None:
        parsed = parse_context_request("""INVESTIGATION_REQUEST:
  version: 4
  objective: Establish the EligibilityService execution flow.
  runtime_facts: [The issue occurs after customer changes.]
  hypotheses: [Policy evaluation may be skipped.]
  required: [main execution flow, tests]
  resolve: [Where is recalculate implemented?]
""")
        self.assertEqual(4, parsed["version"])
        self.assertTrue(parsed["searches"])
        start_session(self.settings, "ATLAS-1", "Establish EligibilityService behavior and tests.")
        state_path = self.settings.runs_dir / "ATLAS-1/session.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual("ready", state["prefetch"]["status"])
        first, _, _ = create_context(self.settings, "ATLAS-1", """INVESTIGATION_REQUEST:
  version: 4
  objective: Establish the EligibilityService execution flow.
  required: [main execution flow]
  resolve: [EligibilityService]
""")
        self.assertTrue(first.startswith("# PROJECT BRAIN CONTEXT\n"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertGreater(state["request_history"][-1]["retrieval"]["trace"]["atlas_route"]["prefetch_reused"], 0)
        context_id = state["last_context_id"]
        second, _, _ = create_context(self.settings, "ATLAS-1", f"""INVESTIGATION_REQUEST:
  version: 4
  objective: Establish tests for EligibilityService.
  required: [tests]
  resolve: [test_recalculate]
  base_context_id: {context_id}
""")
        self.assertTrue(second.startswith("# PROJECT BRAIN CONTEXT DELTA\n"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(2, len(state["context_lineage"]))
        self.assertEqual("candidate", state["coverage_map"]["production_entry_point"])
        self.assertTrue(state["investigation_memory"]["verified_references"])
        recovery, _, _ = create_context(self.settings, "ATLAS-1", """INVESTIGATION_REQUEST:
  version: 4
  objective: Verify recovery from a stale delta base.
  resolve: [policy]
  base_context_id: ctx-stale
""")
        self.assertTrue(recovery.startswith("# PROJECT BRAIN CONTEXT\n"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual("base_mismatch", state["request_history"][-1]["retrieval"]["checkpoint_reason"])

        objective = "Locate EligibilityService."
        start_session(self.settings, "ATLAS-PREFETCH", objective)
        create_context(self.settings, "ATLAS-PREFETCH", f"""INVESTIGATION_REQUEST:
  version: 4
  objective: {objective}
""")
        prefetched = session_state(self.settings, "ATLAS-PREFETCH")
        atlas_trace = prefetched["request_history"][-1]["retrieval"]["trace"]["atlas_route"]
        self.assertTrue(atlas_trace["cache_hit"])
        self.assertGreater(atlas_trace["prefetch_reused"], 0)

    def test_change_intelligence_has_stage_budgets_exact_ticket_patterns_and_bounded_rows(self) -> None:
        repositories = []
        for index in range(100):
            path = self.root / f"change-repo-{index:03d}"
            (path / ".git").mkdir(parents=True)
            repositories.append(SimpleNamespace(name=f"change-{index:03d}", path=path))
        original_repositories = self.settings.repositories
        self.settings.repositories = repositories
        snapshots = {repo.name: f"current-{index}" for index, repo in enumerate(repositories)}
        parents = {repo.name: f"parent-{index}" for index, repo in enumerate(repositories)}
        completed = subprocess.CompletedProcess(["git"], 0, "", "")
        try:
            with mock.patch("brain.atlas.run_bounded_process", return_value=completed) as run_git:
                changes, build = _change_rows(self.settings, snapshots, parents, [])
        finally:
            self.settings.repositories = original_repositories
        self.assertEqual([], changes)
        self.assertLessEqual(run_git.call_count, MAX_CHANGE_GIT_OPERATIONS)
        self.assertEqual(run_git.call_count, build["operations"])

        (self.root / "service/.git").mkdir()
        self.settings.ticket_pattern = r"ABC-\d+"
        valid_path = "src/valid.py"
        oversized_path = "src/" + "x" * MAX_CHANGE_PATH_CHARS
        shown_text = (
            "\x1ecommit\x1f2026-01-01T00:00:00Z\x1fABC-123 bounded subject\n"
            f"M\t{valid_path}\nM\t{oversized_path}\n"
        )
        counted_text = (
            "\x1ecommit\x1f2026-01-01T00:00:00Z\x1fABC-123 bounded subject\n"
            f"1\t1\t{valid_path}\n1\t1\t{oversized_path}\n"
        )
        with mock.patch(
            "brain.atlas.run_bounded_process",
            side_effect=[
                subprocess.CompletedProcess(["git"], 0, shown_text, ""),
                subprocess.CompletedProcess(["git"], 0, counted_text, ""),
            ],
        ):
            changes, build = _change_rows(self.settings, {"service": "current"}, {}, [])
        self.assertEqual(["ABC-123"], [item["ticket"] for item in changes])
        self.assertEqual([valid_path], [item["path"] for item in changes])
        self.assertEqual(1, build["oversized_paths"])

        self.settings.ticket_pattern = r"(NOPE-\d+)|(XYZ-\d+)"
        with mock.patch(
            "brain.atlas.run_bounded_process",
            side_effect=[
                subprocess.CompletedProcess(["git"], 0, shown_text.replace("ABC-123", "XYZ-9"), ""),
                subprocess.CompletedProcess(["git"], 0, counted_text.replace("ABC-123", "XYZ-9"), ""),
            ],
        ):
            changes, _ = _change_rows(self.settings, {"service": "current"}, {}, [])
        self.assertEqual("XYZ-9", changes[0]["ticket"])

        self.settings.ticket_pattern = r"LATE-\d+"
        late_subject = "x" * 600 + " LATE-1"
        late_shown = f"\x1ecommit\x1f2026-01-01T00:00:00Z\x1f{late_subject}\nM\t{valid_path}\n"
        late_counted = f"\x1ecommit\x1f2026-01-01T00:00:00Z\x1f{late_subject}\n1\t1\t{valid_path}\n"
        with mock.patch(
            "brain.atlas.run_bounded_process",
            side_effect=[
                subprocess.CompletedProcess(["git"], 0, late_shown, ""),
                subprocess.CompletedProcess(["git"], 0, late_counted, ""),
            ],
        ):
            changes, _ = _change_rows(self.settings, {"service": "current"}, {}, [])
        self.assertIsNone(changes[0]["ticket"])
        self.assertEqual(500, len(changes[0]["metadata"]["subject"]))

    def test_incomplete_change_build_is_registered_degraded_and_never_routes_prior_rows(self) -> None:
        build = {
            "git_failures": 1, "oversized_paths": 0, "row_limit_reached": 0,
            "budget_exhausted_repos": 0, "operations": 1, "output_bytes": 0,
        }
        manifest = atlas_components({
            "modules": [], "entities": [], "regions": [], "edges": [], "cards": [], "changes": [],
            "delta": {"change_intelligence_build": build},
        })["change_intelligence"]
        self.assertEqual("degraded", manifest["status"])
        self.assertIn("incomplete", manifest["details"]["reason"])

        generation = current_generation_ref(self.settings)
        self.assertIsNotNone(generation)
        degraded = replace(
            generation,
            components={**generation.components, "change_intelligence": manifest},
        )
        result = route(
            self.settings, "service change history",
            {"objective": "service change history", "history": [{"query": "service"}]},
            degraded,
        )
        self.assertEqual("unavailable", result["change_routing_index"])
        self.assertFalse(any(
            "change intelligence" in str(found).casefold()
            for item in result["entities"] for found in item.get("found_by") or []
        ))

    def test_change_retention_partitions_hundred_repositories_once(self) -> None:
        class CountingRows(list[dict[str, object]]):
            visits = 0

            def __iter__(self):  # type: ignore[override]
                for value in super().__iter__():
                    self.visits += 1
                    yield value

        values = CountingRows([
            {
                "repo": f"repo-{index:03d}", "commit_sha": f"commit-{index}",
                "committed_at": "2026-01-01", "change_id": f"change-{index}",
            }
            for index in range(100)
        ])
        retained = _retain_change_rows(values)
        self.assertEqual(100, values.visits)
        self.assertEqual(100, len(retained))

    def test_model_lane_serializes_a_second_process(self) -> None:
        marker = self.root / "child-acquired"
        script = (
            "from pathlib import Path\n"
            "from types import SimpleNamespace\n"
            "from brain.locks import model_lane\n"
            f"settings=SimpleNamespace(state_dir=Path({str(self.settings.state_dir)!r}))\n"
            "with model_lane(settings):\n"
            f"    Path({str(marker)!r}).write_text('acquired')\n"
        )
        with model_lane(SimpleNamespace(state_dir=self.settings.state_dir)):
            process = subprocess.Popen([sys.executable, "-c", script], cwd=Path(__file__).parents[1])
            time.sleep(.2)
            self.assertFalse(marker.exists())
        process.wait(timeout=5)
        self.assertEqual(0, process.returncode)
        self.assertTrue(marker.exists())

    def test_m365_v3_kit_and_atlas_evaluation_metrics(self) -> None:
        kit = create_m365_agent_kit(self.settings)
        self.assertEqual(4, kit["manifest"]["agent_kit_version"])
        self.assertEqual(5, kit["manifest"]["context_request_protocol"])
        self.assertIn("base_context_id", kit["instructions"])
        suite = self.root / "golden.json"
        suite.write_text(json.dumps({
            "name": "atlas-local",
            "cases": [{
                "id": "route-1", "split": "holdout",
                "request": {"version": 1, "objective": "EligibilityService", "searches": [{"query": "EligibilityService"}]},
                "expect": {"required_files": ["service:src/service.py"]},
            }],
        }), encoding="utf-8")
        report = evaluate_golden(self.settings, suite)
        for key in (
            "repo_recall_at_4", "repo_recall_at_6", "repo_recall_at_8", "repo_recall_at_16",
            "module_recall_at_5", "module_recall_at_10", "module_recall_at_20",
            "entity_recall_at_10", "entity_recall_at_20", "entity_recall_at_50",
            "graph_edge_recall", "evidence_recall_at_18", "time_to_first_repo_ms",
            "time_to_first_entity_ms", "time_to_first_verified_evidence_ms", "total_context_ms",
            "requested_operations", "effective_operations", "physical_backend_operations", "raw_candidates",
            "late_candidates", "rerank_input_count", "hydrated_regions", "repo_route_cache_hit_rate",
            "entity_cache_hit_rate", "graph_cache_hit_rate", "similar_ticket_hit_rate", "prefetch_hit_rate",
            "full_context_chars", "delta_context_chars", "delta_reduction_percent",
        ):
            self.assertIn(key, report["summary"])


if __name__ == "__main__":
    unittest.main()

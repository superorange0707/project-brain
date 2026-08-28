from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from brain.atlas import build_atlas, initial_coverage_map, initial_investigation_memory, route, update_investigation
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
        self.assertTrue(second["cache_hit"])
        self.assertTrue(any(item["path"] == "src/service.py" for item in second["entities"]))
        uncached_request = {**request, "_evaluation_ablation": ["generation_cache"]}
        self.assertFalse(route(self.settings, request["objective"], uncached_request, generation)["cache_hit"])
        self.assertFalse(route(self.settings, request["objective"], uncached_request, generation)["cache_hit"])
        graphless = route(self.settings, request["objective"], {**request, "_evaluation_ablation": ["graph"]}, generation)
        self.assertEqual([], graphless["graph_edges"])
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

        bundle = retrieve_context(settings, {
            "version": 4,
            "objective": "TARGET authoritative evidence",
            "searches": [{"query": "TARGET", "repos": []}],
            "paths": [], "symbols": [], "files": [], "history": [], "expand": [],
        })

        self.assertIn("repo49", bundle.trace["initial_repo_scope"])
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
            entities.append(("change-target", "service", "change-module", "src/change_target.py", 1, 1,
                             "service:src/change_target.py", "change_target.py", "", "python", "file", None,
                             "blob-target", "test", "1", "fingerprint-target", "{}"))
            memberships.append((generation.generation, "change-target", snapshot))
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
                 "src/change_target.py", None, "modified", 1, 1, json.dumps({"subject": "LOWRANKCHANGE"})),
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
            connection.commit()
        finally:
            connection.close()
        request = {"version": 4, "objective": "LOWRANKCHANGE", "searches": [], "paths": [], "symbols": [],
                   "files": [], "history": []}
        routed = route(self.settings, request["objective"], request, generation)
        self.assertTrue(any(
            item["path"] == "src/change_target.py" and "Atlas change intelligence" in item["found_by"]
            for item in routed["entities"]
        ))

    def test_same_repo_relationship_does_not_claim_cross_repo_coverage(self) -> None:
        memory = initial_investigation_memory("flow")
        coverage = initial_coverage_map()
        bundle = SimpleNamespace(
            evidence=[Evidence("service", "src/service.py", 1, 2, "class Service: pass", "code", 90, ["test"])],
            relationships=["service:a CALLS service:b"], history=[], unresolved=[],
            trace={"cross_repo_relationships": False},
        )
        update_investigation(memory, coverage, bundle, "ctx-one")
        self.assertEqual("verified", coverage["main_execution_flow"])
        self.assertNotEqual("verified", coverage["cross_repo_integration"])
        bundle.trace["cross_repo_relationships"] = True
        update_investigation(memory, coverage, bundle, "ctx-two")
        self.assertEqual("verified", coverage["cross_repo_integration"])

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
        self.assertEqual("verified", state["coverage_map"]["production_entry_point"])
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
        self.assertEqual(3, kit["manifest"]["agent_kit_version"])
        self.assertEqual(4, kit["manifest"]["context_request_protocol"])
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

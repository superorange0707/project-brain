from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from brain import core, investigation
from brain.catalog import current_generation_ref


class AnchorAlignmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "repo").mkdir()
        self.source = (
            "class Payments {\n"
            "  void handlePayment() {\n    validatePayment();\n  }\n"
            "  void validatePayment() {\n    checkAccount();\n  }\n"
            "  void checkAccount() {}\n"
            "}\n"
        )
        self.path = self.root / "repo/Payments.java"
        self.path.write_text(self.source, encoding="utf-8")
        config = self.root / "brain.toml"
        config.write_text("[project]\nname='anchor-alignment'\n[graph]\nenabled=false\n"
                          "[experience]\nenabled=false\n[[repositories]]\nname='repo'\npath='repo'\n", encoding="utf-8")
        self.settings = core.load_settings(config)
        core.snapshot_indexes(self.settings)
        self.generation = current_generation_ref(self.settings)

    def test_v5_method_anchor_produces_source_verified_execution_flow(self):
        core.start_session(self.settings, "ANCHOR-1", "Trace handlePayment")
        request = {"INVESTIGATION_REQUEST": {"version": 5, "mode": "flow_trace", "objective": "Trace handlePayment",
                   "anchors": [{"kind": "symbol", "value": "handlePayment"}]}}
        content, _, _ = core.create_context(self.settings, "ANCHOR-1", json.dumps(request))
        runtime = core.session_state(self.settings, "ANCHOR-1")["investigation_runtime"]
        flow = runtime["execution_flow"]
        self.assertEqual("ready", flow["status"], flow.get("reason"))
        self.assertEqual(["validatePayment", "checkAccount"], [item["target"] for item in flow["steps"]])
        self.assertTrue(all(item["state"] == "verified" and item["evidence_ids"] for item in flow["steps"]))
        self.assertTrue(flow["paths"])
        self.assertEqual("verified", runtime["coverage"]["main_execution_flow"])
        self.assertFalse(any(item.get("coverage_key") == "main_execution_flow"
                             for item in runtime["evidence_frontier"]["items"]))
        self.assertNotIn("no anchored entities", content)

    def test_existing_casefolded_projection_resolves_camel_case_method(self):
        connection = investigation.connect(self.settings)
        try:
            terms = {row[0] for row in connection.execute("SELECT DISTINCT term FROM atlas_runtime_anchor_terms")}
        finally:
            connection.close()
        self.assertIn("handlepayment", terms)
        self.assertNotIn("handle", terms)
        result = investigation.resolve_runtime_anchors(self.settings, self.generation,
                                                       [{"kind": "symbol", "value": "handlePayment"}])
        self.assertTrue(any(item["entity_id"] and item["line"] == 2 for item in result["candidates"]))

    def test_structure_edges_cannot_consume_the_execution_path_depth(self):
        edges = [("DEFINES", "file", "class"), ("DEFINES", "class", "entry"),
                 ("CALLS", "entry", "validate"), ("CALLS", "validate", "check")]
        steps = [{"identity": str(number), "edge_type": kind, "source_id": source,
                  "target_id": target, "state": "verified"}
                 for number, (kind, source, target) in enumerate(edges)]
        paths = investigation._execution_paths(steps)
        self.assertTrue(any(item["step_ids"] == ["2", "3"] for item in paths))
        self.assertTrue(all(step_id not in {"0", "1"} for item in paths for step_id in item["step_ids"]))
        self.assertEqual(4, len(steps), "structure edges remain available as graph navigation")

    def test_legacy_negative_cache_does_not_hide_the_repaired_query(self):
        compound = investigation._compound_terms

        def legacy_terms(value):
            # Reproduce the pre-fix term list and its valid cache identity,
            # without changing the already-published casefolded projection.
            return compound("handlePayment") if value == "handlepayment" else compound(value)

        query = [{"kind": "symbol", "value": "handlePayment"}]
        with mock.patch.object(investigation, "_compound_terms", side_effect=legacy_terms):
            old = investigation.resolve_runtime_anchors(self.settings, self.generation, query)
        self.assertEqual([], old["candidates"])
        new = investigation.resolve_runtime_anchors(self.settings, self.generation, query)
        self.assertFalse(new["cache_hit"])
        self.assertTrue(new["candidates"])
        cached = investigation.resolve_runtime_anchors(self.settings, self.generation, query)
        self.assertTrue(cached["cache_hit"])
        self.assertEqual(new["candidates"], cached["candidates"])
        self.assertEqual(self.generation.identity, current_generation_ref(self.settings).identity)

    def test_old_ticket_flow_and_anchor_cache_remain_on_the_old_generation(self):
        request = {"INVESTIGATION_REQUEST": {"version": 5, "mode": "flow_trace", "objective": "Trace handlePayment",
                   "anchors": [{"kind": "symbol", "value": "handlePayment"}]}}
        core.start_session(self.settings, "OLD-1", "Trace handlePayment")
        core.create_context(self.settings, "OLD-1", json.dumps(request))
        old_state = core.session_state(self.settings, "OLD-1")
        old_ids = [item["identity"] for item in old_state["investigation_runtime"]["execution_flow"]["steps"]]
        self.path.write_text(self.source.replace("validatePayment", "newValidation"), encoding="utf-8")
        core.snapshot_indexes(self.settings)
        new_generation = current_generation_ref(self.settings)
        self.assertNotEqual(self.generation.identity, new_generation.identity)
        followup = {"INVESTIGATION_REQUEST": {**request["INVESTIGATION_REQUEST"],
                    "objective": "Confirm the handlePayment call path", "base_context_id": "CTX-001"}}
        core.create_context(self.settings, "OLD-1", json.dumps(followup))
        old_runtime = core.session_state(self.settings, "OLD-1")["investigation_runtime"]
        self.assertEqual(self.generation.generation, old_runtime["generation"])
        self.assertEqual(old_ids, [item["identity"] for item in old_runtime["execution_flow"]["steps"]])
        self.assertEqual(["validatePayment", "checkAccount"], [item["target"] for item in old_runtime["execution_flow"]["steps"]])
        core.start_session(self.settings, "NEW-1", "Trace handlePayment")
        core.create_context(self.settings, "NEW-1", json.dumps(request))
        new_runtime = core.session_state(self.settings, "NEW-1")["investigation_runtime"]
        self.assertEqual(new_generation.generation, new_runtime["generation"])
        self.assertEqual(["newValidation", "checkAccount"], [item["target"] for item in new_runtime["execution_flow"]["steps"]])

    def test_identifier_variants_reuse_existing_projection_without_parsing(self):
        names = ("processHTTPResponse", "evaluate_policy", "URLParser", "handlePayment")
        self.path.write_text("class Payments {\n" + "".join(f" void {name}() {{}}\n" for name in names) + "}\n", encoding="utf-8")
        core.snapshot_indexes(self.settings)
        generation = current_generation_ref(self.settings)
        for name in names:
            with self.subTest(name=name), mock.patch.object(investigation, "_java_file_intelligence", side_effect=AssertionError("query-time parse")):
                result = investigation.resolve_runtime_anchors(self.settings, generation, [{"kind": "symbol", "value": name}])
                self.assertTrue(any(item["entity_id"] and item["value"].endswith(":" + name) for item in result["candidates"]))
                self.assertLessEqual(result["bounds"]["compound_terms"], investigation.MAX_COMPOUND_ANCHOR_QUERIES)
        wrong_kind = investigation.resolve_runtime_anchors(self.settings, generation, [{"kind": "endpoint", "value": "handlePayment"}])
        self.assertEqual([], wrong_kind["candidates"])

    def test_corrupt_projection_is_explicitly_degraded_not_bypassed(self):
        query = [{"kind": "symbol", "value": "handlePayment"}]
        self.assertTrue(investigation.resolve_runtime_anchors(self.settings, self.generation, query)["candidates"])
        connection = investigation.connect(self.settings)
        try:
            connection.execute("DELETE FROM atlas_runtime_anchor_terms WHERE term='handlepayment'")
            connection.commit()
        finally:
            connection.close()
        result = investigation.resolve_runtime_anchors(self.settings, self.generation, query)
        self.assertEqual("degraded", result["status"])
        self.assertEqual([], result["candidates"])
        self.assertIn("projection", result["reason"])

    def test_method_resolution_keeps_query_count_bounded_at_repository_scale(self):
        config = self.settings.config_path.read_text(encoding="utf-8")
        added = 1
        operation_counts = []
        for count in (10, 50, 100):
            with self.subTest(repositories=count):
                for number in range(added, count):
                    repo = self.root / f"other{number}"
                    repo.mkdir()
                    (repo / "Other.java").write_text("class Other {\n" + "".join(
                        f" void unrelatedAction{item}() {{ external.work(); }}\n" for item in range(20)) + "}\n", encoding="utf-8")
                    config += f"[[repositories]]\nname='other{number}'\npath='other{number}'\n"
                added = count
                self.settings.config_path.write_text(config, encoding="utf-8")
                self.settings = core.load_settings(self.settings.config_path)
                core.snapshot_indexes(self.settings)
                generation = current_generation_ref(self.settings)
                with mock.patch.object(investigation, "_java_file_intelligence", side_effect=AssertionError("query-time parse")):
                    result = investigation.resolve_runtime_anchors(self.settings, generation,
                                                                  [{"kind": "symbol", "value": "handlePayment"}], use_cache=False)
                self.assertEqual("ready", result["status"])
                self.assertEqual(["repo"], [item["repo"] for item in result["candidates"]])
                self.assertTrue(result["candidates"][0]["entity_id"])
                self.assertLessEqual(result["database_operations"], 16)
                operation_counts.append(result["database_operations"])
        self.assertEqual(1, len(set(operation_counts)))


if __name__ == "__main__":
    unittest.main()

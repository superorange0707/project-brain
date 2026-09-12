from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from brain import atlas, core
from brain.catalog import connect, current_generation_ref
from brain.retrieval.models import RetrievalTrace


class SymbolTraceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "repo").mkdir()
        self.source = (
            'class Payments {\n'
            '  void handlePayment() {\n'
            '    validatePayment();\n'
            '    // fakeComment();\n'
            '    String text = "fakeString()";\n'
            '  }\n'
            '  void unrelatedMaintenance() { dangerousWrite(); }\n'
            '  void validatePayment() { readRecord(); }\n'
            '}\n'
        )
        self.path = self.root / "repo/Payments.java"
        self.path.write_text(self.source, encoding="utf-8")
        config = self.root / "brain.toml"
        config.write_text("[project]\nname='trace-regression'\n[graph]\nenabled=false\n"
                          "[experience]\nenabled=false\n[[repositories]]\nname='repo'\npath='repo'\n", encoding="utf-8")
        self.settings = core.load_settings(config)

    def publish(self):
        core.snapshot_indexes(self.settings)
        generation = current_generation_ref(self.settings)
        return replace(self.settings, atlas_generation=generation, atlas_generation_mode="pinned",
                       repositories=[replace(repo, source_sha=generation.snapshots[repo.name])
                                     for repo in self.settings.repositories])

    def assert_direct_calls(self, relationships, expected="validatePayment"):
        outgoing = [item for item in relationships if item.startswith("handlePayment  CALLS  ")]
        self.assertEqual([f"handlePayment  CALLS  {expected}"], outgoing)

    def test_pinned_trace_reuses_graph_and_delivers_the_actual_callee(self):
        serving = self.publish()
        with mock.patch.object(core, "read_source", side_effect=AssertionError("query-time source read")), \
                mock.patch.object(atlas, "_file_intelligence", side_effect=AssertionError("query-time parse")):
            hits, relationships = core.trace_symbol(serving, "handlePayment")
        self.assert_direct_calls(relationships)
        self.assertTrue(any(item.kind == "callee candidate" and item.line == 8 for item in hits))
        core.start_session(self.settings, "TRACE-1", "Trace handlePayment")
        request = {"CONTEXT_REQUEST": {"version": 3, "objective": "Trace handlePayment",
                   "hints": {"symbols": ["handlePayment"], "repos": ["repo"]}}}
        content, _, _ = core.create_context(self.settings, "TRACE-1", json.dumps(request))
        self.assertIn("void validatePayment() { readRecord(); }", content)
        self.assertIn("handlePayment  CALLS  validatePayment", content)
        self.assertNotIn("handlePayment  CALLS  dangerousWrite", content)

    def test_legacy_fallback_is_method_scoped_and_parses_once_per_file(self):
        (self.root / "repo/Usage.java").write_text("class Usage { void run() { handlePayment(); } }\n", encoding="utf-8")
        with mock.patch.object(core, "read_source", wraps=core.read_source) as reads, \
                mock.patch.object(atlas, "_file_intelligence", wraps=atlas._file_intelligence) as parsed:
            _, relationships = core.trace_symbol(self.settings, "handlePayment")
        self.assert_direct_calls(relationships)
        self.assertEqual(1, reads.call_count)
        self.assertEqual(1, parsed.call_count)

    def test_python_control_flow_is_a_caller_not_a_definition(self):
        (self.root / "repo/policy.py").write_text(
            "def evaluate_policy():\n    return validate_policy()\n"
            "def unrelated():\n    dangerous_write()\n"
            "def validate_policy():\n    return []\n"
            "for rule in evaluate_policy():\n    inspect_rule(rule)\n", encoding="utf-8")
        serving = self.publish()
        hits, relationships = core.trace_symbol(serving, "evaluate_policy")
        self.assertEqual([1], [item.line for item in hits if item.kind == "definition"])
        self.assertTrue(any(item.line == 7 and item.kind == "caller" for item in hits))
        self.assertEqual(["evaluate_policy  CALLS  validate_policy"],
                         [item for item in relationships if item.startswith("evaluate_policy  CALLS")])

    def test_corrupt_graph_falls_back_to_exact_old_source_never_new_generation(self):
        old = self.publish()
        self.path.write_text(self.source.replace("validatePayment", "newValidation"), encoding="utf-8")
        new = self.publish()
        self.assert_direct_calls(core.trace_symbol(old, "handlePayment")[1])
        self.assert_direct_calls(core.trace_symbol(new, "handlePayment")[1], "newValidation")
        connection = connect(self.settings)
        try:
            connection.execute("UPDATE atlas_edges SET metadata_json='{}' WHERE edge_id IN "
                               "(SELECT edge_id FROM generation_edges WHERE generation=?) AND edge_type='CALLS'",
                               (old.atlas_generation.generation,))
            connection.commit()
        finally:
            connection.close()
        self.assertIsNone(atlas.symbol_call_edges(old, old.atlas_generation, [("repo", "Payments.java", 2)], "handlePayment"))
        trace = RetrievalTrace()
        token = core._ACTIVE_RETRIEVAL_TRACE.set(trace)
        try:
            self.assert_direct_calls(core.trace_symbol(old, "handlePayment")[1])
        finally:
            core._ACTIVE_RETRIEVAL_TRACE.reset(token)
        self.assertIn("symbol_trace_exact_source_fallback", trace.fallback_reasons)
        self.assertIn("symbol_trace_source", trace.backend_ms)
        with mock.patch.object(core, "read_source", side_effect=core.BrainError("Pinned source unavailable")):
            self.assertFalse(any(item.startswith("handlePayment  CALLS") for item in core.trace_symbol(old, "handlePayment")[1]))

    def test_fallback_reads_respect_the_physical_budget(self):
        hit = core.SearchHit("repo", "Payments.java", 2, "void handlePayment() {")
        for budget, expected_reads in ((0, 0), (1, 0), (2, 1)):
            with self.subTest(budget=budget):
                trace = RetrievalTrace(max_physical_backend_operations=budget)
                token = core._ACTIVE_RETRIEVAL_TRACE.set(trace)
                try:
                    with mock.patch.object(core, "search", return_value=[replace(hit)]), \
                            mock.patch.object(core, "read_source", wraps=core.read_source) as reads:
                        core.trace_symbol(replace(self.settings, atlas_generation_mode="legacy_source_pin"), "handlePayment")
                    self.assertEqual(expected_reads, reads.call_count)
                    self.assertLessEqual(trace.physical_backend_operations, budget)
                    if not expected_reads:
                        self.assertIn("symbol_trace_physical_budget", trace.fallback_reasons)
                finally:
                    core._ACTIVE_RETRIEVAL_TRACE.reset(token)

    def test_graph_has_bounded_results_and_closes_failed_queries(self):
        self.path.write_text("class Payments {\n void handlePayment() {\n" +
                             "".join(f"    call{number}();\n" for number in range(100)) + " }\n}\n", encoding="utf-8")
        serving = self.publish()
        result = atlas.symbol_call_edges(serving, serving.atlas_generation, [("repo", "Payments.java", 2)], "handlePayment")
        self.assertTrue(result["truncated"])
        self.assertEqual(16, len(result["edges"]))
        self.assertEqual(list(range(3, 19)), [item["line_start"] for item in result["edges"]])
        connection = connect(serving)
        with mock.patch.object(atlas, "connect", return_value=connection), \
                mock.patch.object(atlas, "_valid_generation_edges", side_effect=sqlite3.OperationalError("interrupted")):
            self.assertIsNone(atlas.symbol_call_edges(serving, serving.atlas_generation, [("repo", "Payments.java", 2)], "handlePayment"))
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        with mock.patch.object(atlas, "connect", side_effect=sqlite3.OperationalError("unavailable")):
            self.assertIsNone(atlas.symbol_call_edges(serving, serving.atlas_generation, [("repo", "Payments.java", 2)], "handlePayment"))

    def test_missing_component_and_oversized_fallback_do_not_claim_callees(self):
        serving = self.publish()
        missing = replace(serving.atlas_generation, components={})
        self.assertIsNone(atlas.symbol_call_edges(serving, missing, [("repo", "Payments.java", 2)], "handlePayment"))
        with mock.patch.object(atlas, "symbol_call_edges", return_value=None), \
                mock.patch("brain.investigation.MAX_REFRESH_FILE_BYTES", 16), \
                mock.patch.object(atlas, "_file_intelligence", side_effect=AssertionError("oversized parser input")):
            self.assertFalse(any(item.startswith("handlePayment  CALLS") for item in core.trace_symbol(serving, "handlePayment")[1]))

    def test_graph_read_work_is_constant_at_ten_fifty_and_one_hundred_repositories(self):
        config = self.settings.config_path.read_text(encoding="utf-8")
        statements_at_scale = []
        added = 1
        for count in (10, 50, 100):
            with self.subTest(repositories=count):
                for number in range(added, count):
                    repo = self.root / f"other{number}"
                    repo.mkdir()
                    (repo / "Other.java").write_text("class Other {\n" + "".join(
                        f" void action{item}() {{ external.work(); }}\n" for item in range(20)) + "}\n", encoding="utf-8")
                    config += f"[[repositories]]\nname='other{number}'\npath='other{number}'\n"
                added = count
                self.settings.config_path.write_text(config, encoding="utf-8")
                self.settings = core.load_settings(self.settings.config_path)
                serving = self.publish()
                statements = []
                connection = connect(serving)
                connection.set_trace_callback(statements.append)
                with mock.patch.object(atlas, "connect", return_value=connection) as opened:
                    result = atlas.symbol_call_edges(serving, serving.atlas_generation, [("repo", "Payments.java", 2)], "handlePayment")
                self.assertEqual(1, opened.call_count)
                self.assertEqual(1, len(result["edges"]))
                self.assertEqual("validatePayment", result["edges"][0]["metadata"]["target_name"])
                self.assertLessEqual(len(statements), 8)
                statements_at_scale.append(len(statements))
        self.assertEqual(1, len(set(statements_at_scale)))


if __name__ == "__main__":
    unittest.main()

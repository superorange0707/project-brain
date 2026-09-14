from __future__ import annotations

import hashlib
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
            '  void readRecord() {}\n'
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

    def test_multi_symbol_fallback_parses_once_per_request_on_each_ticket_pin(self):
        old = self.publish()
        core.start_session(self.settings, "MULTI-OLD", "Trace both original methods")
        updated = self.source.replace("validatePayment", "newValidation")
        self.path.write_text(updated, encoding="utf-8")
        new = self.publish()
        core.start_session(self.settings, "MULTI-NEW", "Trace both updated methods")
        outer_cache = {}
        token = core._ACTIVE_RETRIEVAL_CACHE.set(outer_cache)
        self.addCleanup(core._ACTIVE_RETRIEVAL_CACHE.reset, token)
        for ticket, serving, name, source in (
            ("MULTI-OLD", old, "validatePayment", self.source),
            ("MULTI-NEW", new, "newValidation", updated),
        ):
            request = {"CONTEXT_REQUEST": {"version": 3, "objective": "Trace both methods",
                       "hints": {"symbols": ["handlePayment", name], "repos": ["repo"]}}}
            with self.subTest(ticket=ticket), mock.patch.object(atlas, "symbol_call_edges", return_value=None), \
                    mock.patch.object(atlas, "_file_intelligence", wraps=atlas._file_intelligence) as parsed, \
                    mock.patch.object(core, "read_source", wraps=core.read_source) as reads:
                content, _, _ = core.create_context(self.settings, ticket, json.dumps(request))
                self.assertIn(f"handlePayment  CALLS  {name}", content)
                self.assertIn(f"{name}  CALLS  readRecord", content)
                self.assertIn(f"void {name}() {{ readRecord(); }}", content)
                self.assertNotIn("void " + ("newValidation" if name == "validatePayment" else "validatePayment"), content)
                self.assertEqual(serving.atlas_generation.identity, core.session_state(self.settings, ticket)["atlas_generation_id"])
                # Each fallback still obtains and validates its pinned source.
                self.assertEqual(2, sum(bool(call.kwargs.get("full")) for call in reads.call_args_list))
                self.assertEqual([source], [call.args[3] for call in parsed.call_args_list])
                self.assertIs(outer_cache, core._ACTIVE_RETRIEVAL_CACHE.get())
                self.assertEqual({}, outer_cache)

    def test_parser_cache_never_masks_a_later_authoritative_source_failure(self):
        serving = self.publish()
        cache = core._ACTIVE_RETRIEVAL_CACHE.set({})
        self.addCleanup(core._ACTIVE_RETRIEVAL_CACHE.reset, cache)
        trace = RetrievalTrace()
        token = core._ACTIVE_RETRIEVAL_TRACE.set(trace)
        self.addCleanup(core._ACTIVE_RETRIEVAL_TRACE.reset, token)
        with mock.patch.object(atlas, "symbol_call_edges", return_value=None), \
                mock.patch.object(atlas, "_file_intelligence", wraps=atlas._file_intelligence) as parsed:
            self.assert_direct_calls(core.trace_symbol(serving, "handlePayment")[1])
            with mock.patch.object(core, "read_source", side_effect=core.BrainError("Pinned source unavailable")):
                _, relationships = core.trace_symbol(serving, "validatePayment")
            self.assertFalse(any(item.startswith("validatePayment  CALLS") for item in relationships))
            self.assertEqual(1, parsed.call_count)
            self.assertIn("symbol_trace_source_unavailable", trace.fallback_reasons)
            _, relationships = core.trace_symbol(serving, "validatePayment")
            self.assertIn("validatePayment  CALLS  readRecord", relationships)
            self.assertEqual(1, parsed.call_count)
            self.assertGreaterEqual(trace.cache_hits, 1)

    def test_parser_cache_identity_capacity_and_failure_are_request_local(self):
        serving = self.publish()
        cache = {}
        token = core._ACTIVE_RETRIEVAL_CACHE.set(cache)
        self.addCleanup(core._ACTIVE_RETRIEVAL_CACHE.reset, token)

        def parse(settings=serving, repo="repo", path="Payments.java", source=self.source):
            return core._symbol_trace_file_intelligence(settings, repo, path,
                                                         hashlib.sha256(source.encode()).hexdigest(), source)

        with mock.patch.object(atlas, "_file_intelligence", wraps=atlas._file_intelligence) as parsed:
            self.assertFalse(parse()[2])
            self.assertTrue(parse()[2])
            self.assertFalse(parse(replace(serving, atlas_generation=replace(serving.atlas_generation, identity="other-generation")))[2])
            self.assertFalse(parse(repo="other")[2])
            self.assertFalse(parse(path="Other.java")[2])
            self.assertFalse(parse(source=self.source.replace("validatePayment", "newValidation"))[2])
            with mock.patch.object(atlas, "EXTRACTOR_VERSION", str(atlas.EXTRACTOR_VERSION) + "-next"):
                self.assertFalse(parse()[2])
            with mock.patch.object(atlas, "ATLAS_SCHEMA_VERSION", str(atlas.ATLAS_SCHEMA_VERSION) + "-next"):
                self.assertFalse(parse()[2])
            self.assertEqual(7, parsed.call_count)
        cache.clear()
        with mock.patch.object(atlas, "_file_intelligence", side_effect=atlas.AtlasCapacityError("fixture")):
            with self.assertRaises(atlas.AtlasCapacityError):
                parse()
        self.assertEqual({}, cache)
        projection = ({}, [{"metadata": {"unicode": "证据" * 80}}], [], [])
        with mock.patch.object(atlas, "_file_intelligence", return_value=projection) as parsed, \
                mock.patch.object(core, "MAX_SYMBOL_TRACE_CACHE_BYTES", 128):
            self.assertFalse(parse()[2])
            self.assertFalse(parse()[2])
            self.assertEqual(2, parsed.call_count)
            self.assertEqual({}, cache)
        with mock.patch.object(atlas, "_file_intelligence", return_value=projection):
            for number in range(core.MAX_SYMBOL_TRACE_CACHED_FILES + 2):
                parse(path=f"File{number}.java")
            self.assertEqual(core.MAX_SYMBOL_TRACE_CACHED_FILES, len(cache))
            self.assertLessEqual(sum(value[0] for value in cache.values()), core.MAX_SYMBOL_TRACE_CACHE_BYTES)
            for key, (size, entities, edges) in cache.items():
                self.assertEqual(len(json.dumps((key, entities, edges), ensure_ascii=True,
                                                separators=(",", ":")).encode("ascii")), size)

    def test_legacy_fallback_is_method_scoped_and_parses_once_per_file(self):
        (self.root / "repo/Usage.java").write_text("class Usage { void run() { handlePayment(); } }\n", encoding="utf-8")
        with mock.patch.object(core, "read_source", wraps=core.read_source) as reads, \
                mock.patch.object(atlas, "_file_intelligence", wraps=atlas._file_intelligence) as parsed:
            _, relationships = core.trace_symbol(self.settings, "handlePayment")
        self.assert_direct_calls(relationships)
        # Legacy declaration verification and tracing share this one source.
        self.assertEqual(1, reads.call_count)
        self.assertEqual(1, parsed.call_count)

    def test_lexical_call_shapes_are_navigation_not_static_relationships(self):
        from brain.retrieval.ranker import fuse_and_rank

        (self.root / "repo/Usage.java").write_text(
            "class Usage { void run() { handlePayment(); } }\n", encoding="utf-8")
        (self.root / "repo/Noise.java").write_text(
            'class Noise {\n String example = "handlePayment()";\n'
            ' /*\n handlePayment();\n */\n}\n', encoding="utf-8")
        for pinned in (False, True):
            serving = self.publish() if pinned else self.settings
            channels = {(hit.repo, hit.path, hit.line): list(hit.found_by)
                        for hit in core.search(serving, "handlePayment", fixed=True)}
            with self.subTest(pinned=pinned), mock.patch.object(core, "read_source", wraps=core.read_source) as reads:
                hits, relationships = core.trace_symbol(serving, "handlePayment")
                self.assert_direct_calls(relationships)
                self.assertFalse(any("  CALLS  handlePayment" in item for item in relationships))
                references = [hit for hit in hits if hit.path in {"Usage.java", "Noise.java"}]
                self.assertTrue(any(hit.path == "Usage.java" for hit in references))
                self.assertTrue(references)
                self.assertTrue(all(hit.found_by == channels[(hit.repo, hit.path, hit.line)] for hit in references))
                self.assertTrue(all(hit.kind == "lexical reference candidate (call and dispatch not established)"
                                    for hit in references))
                original = [replace(hit, kind="caller", found_by=channels[(hit.repo, hit.path, hit.line)]) for hit in references]
                self.assertEqual([(hit.path, hit.line, hit.score) for hit in fuse_and_rank(original)],
                                 [(hit.path, hit.line, hit.score) for hit in fuse_and_rank(references)])
                # Pinned classification uses one batched lexical query; legacy
                # classification shares its source read with this same trace.
                self.assertEqual(0 if pinned else 1, reads.call_count)

    def test_legacy_trace_keeps_generic_method_reference_as_navigation(self):
        (self.root / 'repo/GenericUsage.java').write_text(
            'class GenericUsage { java.util.function.Function<String,String> reference = Payments::<String>handlePayment; }\n',
            encoding='utf-8')
        for pinned in (False, True):
            with self.subTest(pinned=pinned):
                serving = self.publish() if pinned else self.settings
                hits, relationships = core.trace_symbol(serving, 'handlePayment')
                self.assertTrue(any(hit.path == 'GenericUsage.java' and 'lexical reference candidate' in hit.kind for hit in hits))
                self.assertFalse(any('  CALLS  handlePayment' in value for value in relationships))
                self.assert_direct_calls(relationships)

    def test_public_legacy_trace_reports_missing_source_despite_readable_noise(self):
        (self.root / "repo/Noise.java").write_text(
            'class Noise { String note = "handlePayment()"; }\n', encoding="utf-8")
        serving = self.publish()
        read = core.read_source

        def source(settings, hit, **kwargs):
            if hit.path == "Payments.java":
                raise core.BrainError("Pinned source unavailable")
            return read(settings, hit, **kwargs)

        for version in (2, 3):
            body = {"version": version, "objective": "Trace handlePayment"}
            if version == 2:
                body["symbols"] = [{"name": "handlePayment", "repos": ["repo"], "include": ["callers", "callees"]}]
            else:
                body["hints"] = {"symbols": ["handlePayment"], "repos": ["repo"]}
            request = core.parse_context_request(json.dumps({"CONTEXT_REQUEST": body}))
            for graph_available in (False, True):
                graph = (mock.patch.object(atlas, "symbol_call_edges", wraps=atlas.symbol_call_edges)
                         if graph_available else mock.patch.object(atlas, "symbol_call_edges", return_value=None))
                # Both graph entry points fail together; lexical state remains usable.
                route = (mock.patch.object(atlas, "route", wraps=atlas.route) if graph_available
                         else mock.patch.object(atlas, "route", return_value={}))
                with self.subTest(version=version, graph_available=graph_available), graph, route, \
                        mock.patch.object(core, "read_source", side_effect=source):
                    bundle = core.retrieve_context(serving, request)
                    self.assertTrue(any(item.path == "Noise.java" for item in bundle.evidence))
                    self.assertFalse(any(item.path == "Payments.java" for item in bundle.evidence))
                    self.assertFalse(any("Noise.java" in item for item in bundle.relationships))
                    self.assertTrue(any("Source availability is unknown" in item and "repo:Payments.java" in item
                                        for item in bundle.unresolved))
                    if not graph_available:
                        self.assertFalse(bundle.relationships)
                        self.assertFalse(core._coverage(bundle)["relationships"])
                        self.assertTrue(any("No static call evidence" in item for item in bundle.unresolved))
                    else:
                        self.assert_direct_calls(bundle.relationships)

    def test_python_control_flow_is_a_caller_not_a_definition(self):
        (self.root / "repo/policy.py").write_text(
            "def evaluate_policy():\n    return validate_policy()\n"
            "def unrelated():\n    dangerous_write()\n"
            "def validate_policy():\n    return []\n"
            "for rule in evaluate_policy():\n    inspect_rule(rule)\n", encoding="utf-8")
        serving = self.publish()
        hits, relationships = core.trace_symbol(serving, "evaluate_policy")
        self.assertEqual([1], [item.line for item in hits if item.kind == "definition"])
        self.assertTrue(any(item.line == 7 and item.kind.startswith("lexical reference candidate") for item in hits))
        self.assertEqual(["evaluate_policy  CALLS  validate_policy"],
                         [item for item in relationships if item.startswith("evaluate_policy  CALLS")])

    def test_unresolved_java_receiver_is_a_candidate_in_pinned_and_legacy_trace(self):
        self.path.write_text('class Payments {\n void handlePayment() {\n external.doThing();\n local();\n }\n'
                             ' void local() {}\n void doThing() {}\n}\n', encoding='utf-8')
        serving = self.publish()
        for legacy in (False, True):
            with self.subTest(legacy=legacy), mock.patch.object(atlas, 'symbol_call_edges',
                **({'return_value': None} if legacy else {'wraps': atlas.symbol_call_edges})):
                hits, relationships = core.trace_symbol(serving, 'handlePayment')
                self.assert_direct_calls(relationships, 'local')
                self.assertIn('handlePayment  CALL CANDIDATE (binding unavailable)  external.doThing', relationships)
                self.assertFalse(any(hit.kind == 'callee candidate' and 'doThing' in hit.text for hit in hits))

    def test_java_definition_noise_does_not_stop_public_repository_widening(self):
        noise = self.root / "noise"
        noise.mkdir()
        (noise / "Noise.java").write_text(
            'class Noise {\n /* public void handlePayment() { fake(); } */\n'
            ' String documentation = "public void handlePayment() { fake(); }";\n'
            ' boolean choose() { return ready ? handlePayment() : fallback(); }\n'
            ' void route() { if (ready) other(); else handlePayment(); }\n}\n', encoding="utf-8")
        config = self.settings.config_path
        config.write_text(config.read_text(encoding="utf-8") +
                          "[[repositories]]\nname='noise'\npath='noise'\n", encoding="utf-8")
        self.settings = core.load_settings(config)
        serving = replace(self.publish(), candidate_limit=2, hydrate_limit=1,
                          initial_repo_limit=1, widen_repo_limit=2)
        for version in (2, 3):
            body = {"version": version, "objective": "Find the payment implementation"}
            if version == 2:
                body["symbols"] = [{"name": "handlePayment", "include": ["definition"]}]
            else:
                body["hints"] = {"symbols": ["handlePayment"]}
                body["coverage"] = {"production": "required", "tests": "omit", "relationships": "omit"}
            request = core.parse_context_request(json.dumps({"CONTEXT_REQUEST": body}))
            with self.subTest(version=version), mock.patch.object(atlas, "route", return_value={"repos": ["noise", "repo"]}), \
                    mock.patch("brain.backends.zoekt.search", return_value=None):
                bundle = core.retrieve_context(serving, request)
                self.assertEqual([("repo", "Payments.java")], [(item.repo, item.path) for item in bundle.evidence])
                self.assertIn("void handlePayment()", bundle.evidence[0].content)
                self.assertEqual(["noise", "repo"], bundle.trace["final_repo_scope"])
                self.assertGreaterEqual(bundle.trace["widening_rounds"], 1)
                self.assertFalse(any(item.repo == "noise" and "definition" in item.kind
                                     for item in [*bundle.evidence, *bundle.additional_candidates]))
                if version == 2:
                    body["symbols"][0]["repos"] = ["noise"]
                else:
                    body["hints"]["repos"] = ["noise"]
                scoped = core.retrieve_context(serving, core.parse_context_request(json.dumps({"CONTEXT_REQUEST": body})))
                self.assertEqual({"noise"}, {item.repo for item in scoped.evidence})
                self.assertTrue(any("Definition for `handlePayment` is not verified" in item for item in scoped.unresolved))
                self.assertFalse(any("definition" in item.kind for item in scoped.evidence))
        for pinned in (False, True):
            selected = serving if pinned else replace(self.settings, atlas_generation_mode="legacy_source_pin")
            with self.subTest(pinned=pinned):
                hits = core.symbol_hits(selected, "handlePayment", ["noise"])
                self.assertFalse(any("definition" in hit.kind for hit in hits))
                hits, relationships = core.trace_symbol(selected, "handlePayment", ["noise"])
                self.assertFalse(any("definition" in hit.kind for hit in hits))
                self.assertEqual([], relationships)

    def test_symbol_reference_fallback_needs_no_optional_backend_and_keeps_word_boundaries(self):
        self.path.write_text(
            'class Payments {\n void route() { handlePayment(); }\n'
            ' void unrelated() { handlePaymentExtra(); }\n}\n', encoding='utf-8')
        serving = self.publish()
        with mock.patch('brain.backends.zoekt.search', return_value=None) as optional:
            hits = core.symbol_hits(serving, 'handlePayment', ['repo'])
        self.assertEqual([('repo', 'Payments.java', 2)], [(hit.repo, hit.path, hit.line) for hit in hits])
        self.assertEqual({'symbol reference'}, {hit.kind for hit in hits})
        optional.assert_not_called()

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
        for budget, expected_reads in ((0, 0), (1, 1), (2, 1)):
            with self.subTest(budget=budget):
                trace = RetrievalTrace(max_physical_backend_operations=budget)
                token = core._ACTIVE_RETRIEVAL_TRACE.set(trace)
                try:
                    with mock.patch.object(core, "search", return_value=[replace(hit)]), \
                            mock.patch.object(core, "read_source", wraps=core.read_source) as reads:
                        _, relationships = core.trace_symbol(replace(self.settings, atlas_generation_mode="legacy_source_pin"), "handlePayment")
                    self.assertEqual(expected_reads, reads.call_count)
                    self.assertLessEqual(trace.physical_backend_operations, budget)
                    if budget < 2:
                        self.assertFalse(any(item.startswith("handlePayment  CALLS") for item in relationships))
                        self.assertTrue(any("physical" in reason for reason in trace.fallback_reasons))
                    else:
                        self.assert_direct_calls(relationships)
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

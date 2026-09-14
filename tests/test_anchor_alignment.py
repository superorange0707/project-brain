from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from brain import atlas, core, investigation
from brain.catalog import current_generation_ref


class AnchorAlignmentTests(unittest.TestCase):
    deep_path = "src/main/java/com/example/banking/payments/settlement/application/adapters/outbound/validation/Payments.java"

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

    def _publish_deep_source(self):
        target = self.root / "repo" / self.deep_path
        target.parent.mkdir(parents=True)
        self.path.rename(target)
        self.path = target
        core.snapshot_indexes(self.settings)
        return current_generation_ref(self.settings)

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

    def test_v5_explicit_callers_reach_inbound_source_on_the_ticket_generation(self):
        source = (
            "package com.example;\nclass Payments {\n  void validatePayment() {}\n"
            + "\n" * 220 + "  void oldCaller() { validatePayment(); }\n}\n"
        )
        self.path.write_text(source, encoding="utf-8")
        (self.root / "repo/Decoy.java").write_text(
            "package com.other;\nclass Payments { void validatePayment() {} }\n", encoding="utf-8",
        )
        core.snapshot_indexes(self.settings)
        core.start_session(self.settings, "CALLERS-A", "Find the requested callers")
        request = {"INVESTIGATION_REQUEST": {
            "version": 5, "mode": "flow_trace", "objective": "com.example.Payments.validatePayment",
            "anchors": [{"kind": "symbol", "value": "com.example.Payments.validatePayment"}],
            "required": ["callers"],
        }}
        parsed = core.parse_context_request(json.dumps(request))
        from brain.retrieval.planner import compile_request
        self.assertTrue(any(op.kind == "symbol" and "callers" in op.includes for op in compile_request(parsed).operations))
        first, _, _ = core.create_context(self.settings, "CALLERS-A", json.dumps(request))
        self.assertIn("void oldCaller()", first)
        self.assertNotIn("package com.other", first)
        runtime = core.session_state(self.settings, "CALLERS-A")["investigation_runtime"]
        self.assertTrue(any(step["edge_type"] == "CALLS" and step["state"] == "verified"
                            and step["line"] > 200 for step in runtime["execution_flow"]["steps"]))
        self.path.write_text(source.replace("oldCaller", "newCaller"), encoding="utf-8")
        core.snapshot_indexes(self.settings)
        core.start_session(self.settings, "CALLERS-B", "Find the requested callers")
        request["INVESTIGATION_REQUEST"]["objective"] += " again"
        old, _, _ = core.create_context(self.settings, "CALLERS-A", json.dumps(request))
        new, _, _ = core.create_context(self.settings, "CALLERS-B", json.dumps(request))
        self.assertIn("void oldCaller()", old)
        self.assertNotIn("void newCaller()", old)
        self.assertIn("void newCaller()", new)
        self.assertNotIn("void oldCaller()", new)

    def test_v5_explicit_implementations_use_inbound_typed_edges(self):
        self.path.write_text("package com.example;\ninterface Payments {}\n", encoding="utf-8")
        (self.root / "repo/CardPayments.java").write_text(
            "package com.example;\nclass CardPayments implements Payments {}\n", encoding="utf-8",
        )
        core.snapshot_indexes(self.settings)
        core.start_session(self.settings, "IMPLEMENTATIONS", "Find implementations")
        request = {"INVESTIGATION_REQUEST": {
            "version": 5, "mode": "implementation_plan", "objective": "com.example.Payments",
            "anchors": [{"kind": "symbol", "value": "com.example.Payments"}],
            "required": ["implementations"],
        }}
        content, _, _ = core.create_context(self.settings, "IMPLEMENTATIONS", json.dumps(request))
        self.assertIn("class CardPayments implements Payments", content)
        runtime = core.session_state(self.settings, "IMPLEMENTATIONS")["investigation_runtime"]
        self.assertTrue(any(step["edge_type"] == "IMPLEMENTS" and step["state"] == "verified"
                            for step in runtime["execution_flow"]["steps"]), runtime["execution_flow"])
        self.assertTrue(any(step["edge_type"] == "IMPLEMENTS" and step["state"] == "candidate"
                            for step in runtime["execution_flow"]["steps"]), "unresolved type leaves are not verified")

    def test_v5_relations_reject_ambiguous_symbols_and_corrupt_pinned_edges(self):
        from brain.retrieval.planner import compile_request

        self.path.write_text("class Payments {\n void validate() {}\n void caller() { validate(); }\n}\n", encoding="utf-8")
        (self.root / "repo/Other.java").write_text("class Other {\n void validate() {}\n void caller() { validate(); }\n}\n", encoding="utf-8")
        core.snapshot_indexes(self.settings)
        generation = current_generation_ref(self.settings)
        pinned = replace(self.settings, atlas_generation=generation, atlas_generation_mode="pinned")
        body = {"version": 5, "mode": "flow_trace", "objective": "validate",
                "anchors": [{"kind": "symbol", "value": "validate"}], "required": ["caller"]}
        request = core.parse_context_request(json.dumps({"INVESTIGATION_REQUEST": body}))
        bundle = core.retrieve_context(pinned, request)
        self.assertTrue(any("ambiguous" in value for value in bundle.unresolved), bundle.unresolved)
        self.assertIn("symbol_relation", bundle.trace["backend_ms"])
        self.assertFalse(any("requested symbol relationship" in item.kind for item in bundle.evidence))
        without_intent = core.parse_context_request(json.dumps({"INVESTIGATION_REQUEST": {**body, "required": []}}))
        self.assertFalse(any(op.kind == "symbol" for op in compile_request(without_intent).operations))
        body.update(objective="Payments.validate", anchors=[{"kind": "symbol", "value": "Payments.validate"}])
        request = core.parse_context_request(json.dumps({"INVESTIGATION_REQUEST": body}))
        healthy = core.retrieve_context(pinned, request)
        self.assertTrue(any("requested symbol relationship" in item.kind for item in healthy.evidence))
        connection = investigation.connect(self.settings)
        try:
            connection.execute("UPDATE atlas_edges SET confidence=.123 WHERE edge_type='CALLS'")
            connection.commit()
        finally:
            connection.close()
        corrupted = core.retrieve_context(pinned, request)
        self.assertTrue(any("content identity" in value for value in corrupted.unresolved), corrupted.unresolved)
        self.assertFalse(any("requested symbol relationship" in item.kind for item in corrupted.evidence))
        self.assertIn("symbol_relation", corrupted.trace["backend_ms"])

    def test_v5_relation_batch_preserves_each_independently_qualified_symbol(self):
        self.path.write_text("package com.example;\n" + self.source, encoding="utf-8")
        core.snapshot_indexes(self.settings)
        core.start_session(self.settings, "RELATION-BATCH", "Trace two known methods together")
        request = {"INVESTIGATION_REQUEST": {
            "version": 5, "mode": "flow_trace", "objective": "Trace the requested relationships",
            "required": ["callers", "callees"], "anchors": [
                {"kind": "symbol", "value": "com.example.Payments.validatePayment"},
                {"kind": "symbol", "value": "com.example.Payments.checkAccount"},
            ],
        }}
        core.create_context(self.settings, "RELATION-BATCH", json.dumps(request))
        runtime = core.session_state(self.settings, "RELATION-BATCH")["investigation_runtime"]
        flow = runtime["execution_flow"]
        self.assertEqual("ready", flow["status"], flow)
        self.assertEqual({"validatePayment", "checkAccount"}, {step["target"] for step in flow["steps"]})
        self.assertTrue(all(step["state"] == "verified" for step in flow["steps"]))
        self.assertLessEqual(runtime["bounds"]["database_operations"], investigation.MAX_RUNTIME_DB_OPERATIONS)

    def test_incoming_and_outgoing_flow_keeps_direction_and_bounded_branching(self):
        source = "class Payments {\n void target() { sink(); }\n void sink() {}\n" + "".join(
            f" void caller{i}() {{ target(); }}\n" for i in range(100)
        ) + "}\n"
        self.path.write_text(source, encoding="utf-8")
        core.snapshot_indexes(self.settings)
        generation = current_generation_ref(self.settings)
        resolved = investigation.resolve_runtime_anchors(self.settings, generation, [{"kind": "symbol", "value": "Payments.target"}])
        seeds = [item["entity_id"] for item in resolved["candidates"] if item.get("method") == "entity_name"]
        evidence = core.Evidence("repo", "Payments.java", 1, len(source.splitlines()), source, "code", 100, verification_content=source)
        bundle = core.ContextBundle("target relations", evidence=[evidence], atlas_generation=generation)
        flow = investigation._execution_flow(self.settings, generation, seeds, bundle,
                                             incoming_types=("CALLS",), outgoing_types=("CALLS",))
        self.assertEqual("ready", flow["status"], flow)
        self.assertTrue(flow["truncated"])
        self.assertLessEqual(len(flow["steps"]), investigation.MAX_FLOW_STEPS)
        self.assertLessEqual(flow["database_operations"], investigation.MAX_FLOW_DB_QUERIES)
        self.assertEqual(investigation.MAX_FLOW_BRANCH, sum(step["target_id"] == seeds[0] for step in flow["steps"]))
        by_id = {step["identity"]: step for step in flow["steps"]}
        self.assertTrue(flow["paths"])
        for path in flow["paths"]:
            ordered = [by_id[identifier] for identifier in path["step_ids"]]
            for first, second in zip(ordered, ordered[1:]):
                self.assertEqual(first["target_id"], second["source_id"], "inbound traversal never reverses a call")
        with mock.patch.object(investigation, "MAX_FLOW_SECONDS", -1):
            failed = investigation._execution_flow(self.settings, generation, seeds, bundle, incoming_types=("CALLS",))
        self.assertEqual("degraded", failed["status"])
        self.assertEqual([], failed["steps"])
        expired = False
        original = investigation._valid_generation_entities

        def validate_then_expire(*args, **kwargs):
            nonlocal expired
            result = original(*args, **kwargs)
            expired = True
            return result

        with mock.patch.object(investigation, "_valid_generation_entities", side_effect=validate_then_expire), \
                mock.patch.object(investigation.time, "monotonic", side_effect=lambda: 100 if expired else 0):
            partial = investigation._execution_flow(self.settings, generation, seeds, bundle,
                                                    incoming_types=("CALLS",), outgoing_types=("CALLS",))
        self.assertEqual("degraded", partial["status"], partial)
        self.assertTrue(partial["truncated"])
        self.assertTrue(partial["steps"], "a later budget timeout cannot erase completed, validated depths")
        self.assertTrue(partial["paths"])
        self.assertTrue(all(step["state"] == "verified" for step in partial["steps"]))
        self.assertTrue({step["identity"] for step in partial["steps"]}.issubset(by_id))

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

    def test_deep_directory_method_anchor_reuses_the_published_entity(self):
        generation = self._publish_deep_source()
        connection = investigation.connect(self.settings)
        try:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM generation_runtime_anchors g JOIN atlas_runtime_anchor_terms t ON t.anchor_id=g.anchor_id "
                "WHERE g.generation=? AND t.term='handlepayment'", (generation.generation,),
            ).fetchone(), "fixture exercises a method outside the legacy term projection")
        finally:
            connection.close()
        with mock.patch.object(investigation, "_java_file_intelligence", side_effect=AssertionError("query-time parse")):
            result = investigation.resolve_runtime_anchors(self.settings, generation,
                                                          [{"kind": "symbol", "value": "handlePayment"}])
        self.assertTrue(any(item["entity_id"] and item["path"] == self.deep_path and item["line"] == 2
                            for item in result["candidates"]), result)
        self.assertEqual(generation.identity, current_generation_ref(self.settings).identity)

    def test_qualified_method_anchor_selects_owner_and_declared_package(self):
        self._publish_deep_source()
        self.path.write_text("package com.example.payments;\n" + self.source, encoding="utf-8")
        for number in range(12):
            (self.root / "repo" / f"Decoy{number}.java").write_text(
                f"class Decoy{number} {{ void handlePayment() {{}} }}\n", encoding="utf-8",
            )
        # A matching directory is not evidence of the Java package declaration.
        wrong = self.root / "repo/com/example/payments/Payments.java"
        wrong.parent.mkdir(parents=True)
        wrong.write_text("package com.other;\n" + self.source, encoding="utf-8")
        (self.root / "repo/Nested.java").write_text(
            "package com.example.payments;\nclass Outer {\n" + self.source + "}\n", encoding="utf-8",
        )
        core.snapshot_indexes(self.settings)
        generation = current_generation_ref(self.settings)
        for query in ("Payments.handlePayment", "Payments#handlePayment",
                      "com.example.payments.Payments.handlePayment"):
            with self.subTest(query=query):
                result = investigation.resolve_runtime_anchors(
                    self.settings, generation, [{"kind": "symbol", "value": query}],
                )
                exact = [item for item in result["candidates"] if item["method"] == "entity_name"]
                self.assertTrue(any(item["path"] == self.deep_path and item["line"] == 3
                                    for item in exact), result)
                self.assertTrue(all(item["value"].endswith(":handlePayment") for item in exact))
                if query.startswith("com.example"):
                    self.assertEqual([self.deep_path], [item["path"] for item in exact])
                cached = investigation.resolve_runtime_anchors(
                    self.settings, generation, [{"kind": "symbol", "value": query}],
                )
                self.assertTrue(cached["cache_hit"])
                self.assertEqual(result["candidates"], cached["candidates"])
        for query in ("Missing.handlePayment", "com.missing.Payments.handlePayment"):
            result = investigation.resolve_runtime_anchors(
                self.settings, generation, [{"kind": "symbol", "value": query}],
            )
            self.assertEqual([], result["candidates"], "qualified misses must not become unrelated methods")
        for _ in range(2):
            result = investigation.resolve_runtime_anchors(self.settings, generation, [
                {"kind": "symbol", "value": "com.example.payments.Payments"},
            ])
            self.assertEqual([(self.deep_path, 2)], [(item["path"], item["line"]) for item in result["candidates"]])
        core.start_session(self.settings, "QUALIFIED-1", "Trace the specified method")
        request = {"INVESTIGATION_REQUEST": {"version": 5, "mode": "flow_trace",
                   "objective": "Trace com.example.payments.Payments.handlePayment",
                   "anchors": [{"kind": "symbol", "value": "com.example.payments.Payments.handlePayment"}]}}
        with mock.patch("brain.editions.current_edition", return_value="precision"), \
                mock.patch("brain.semantic.search_semantic", side_effect=AssertionError("unnecessary model search")), \
                mock.patch("brain.models.rerank_candidates", side_effect=AssertionError("unnecessary model rerank")):
            content, _, _ = core.create_context(self.settings, "QUALIFIED-1", json.dumps(request))
        runtime = core.session_state(self.settings, "QUALIFIED-1")["investigation_runtime"]
        flow = runtime["execution_flow"]
        self.assertEqual(["validatePayment", "checkAccount"], [item["target"] for item in flow["steps"]])
        self.assertTrue(all(item["state"] == "verified" and item["evidence_ids"] for item in flow["steps"]))
        self.assertTrue(all(item["path"] == self.deep_path for item in flow["steps"]))
        self.assertNotIn("class Decoy", content, "unrelated same-name declarations must not fill the source handoff")
        self.assertNotIn("package com.other;", content, "a requested package must not resolve to another package")
        self.assertNotIn("returned no code matches", content)
        self.assertEqual("Core", core.session_state(self.settings, "QUALIFIED-1")["request_history"][-1]["retrieval"]["effective_edition"])

    def test_qualified_anchor_cache_revalidates_owner_and_package(self):
        self.path.write_text("package com.example;\n" + self.source, encoding="utf-8")
        core.snapshot_indexes(self.settings)
        generation = current_generation_ref(self.settings)
        query = [{"kind": "symbol", "value": "com.example.Payments.handlePayment"}]
        # Package mutation invalidates the published anchor projection, so test it last.
        for target in ("owner", "owner_membership", "package"):
            with self.subTest(target=target):
                self.assertTrue(investigation.resolve_runtime_anchors(self.settings, generation, query)["candidates"])
                connection = investigation.connect(self.settings)
                try:
                    owner = connection.execute(
                        "SELECT e.entity_id,e.fingerprint FROM generation_entities g JOIN atlas_entities e "
                        "ON e.entity_id=g.entity_id WHERE g.generation=? AND e.simple_name='Payments'",
                        (generation.generation,),
                    ).fetchone()
                    package = connection.execute("SELECT anchor_id,fingerprint FROM atlas_runtime_anchors WHERE kind='package'").fetchone()
                    if target == "owner_membership":
                        member = connection.execute("SELECT * FROM generation_entities WHERE generation=? AND entity_id=?",
                                                    (generation.generation, owner[0])).fetchone()
                        connection.execute("DELETE FROM generation_entities WHERE generation=? AND entity_id=?",
                                           (generation.generation, owner[0]))
                    else:
                        table, key, row = ("atlas_entities", "entity_id", owner) if target == "owner" else (
                            "atlas_runtime_anchors", "anchor_id", package,
                        )
                        connection.execute(f"UPDATE {table} SET fingerprint='corrupt' WHERE {key}=?", (row[0],))
                    connection.commit()
                    result = investigation.resolve_runtime_anchors(self.settings, generation, query)
                    self.assertEqual("degraded", result["status"])
                    self.assertEqual([], result["candidates"])
                    if target == "package":
                        rows, _, poisoned = investigation._entity_name_anchor_rows(connection, generation, [query[0]["value"]])
                        self.assertTrue(poisoned)
                        self.assertEqual([], rows)
                    if target == "owner_membership":
                        connection.execute("INSERT INTO generation_entities VALUES (?,?,?)", member)
                    else:
                        connection.execute(f"UPDATE {table} SET fingerprint=? WHERE {key}=?", (row[1], row[0]))
                    connection.commit()
                finally:
                    connection.close()

    def test_qualified_route_cache_preserves_scope_and_revalidates_ownership(self):
        self.path.write_text("package com.example;\n" + self.source, encoding="utf-8")
        (self.root / "repo/Other.java").write_text("class Other { void handlePayment() {} }\n", encoding="utf-8")
        core.snapshot_indexes(self.settings)
        generation = current_generation_ref(self.settings)
        body = {"version": 5, "mode": "flow_trace", "objective": "Trace com.example.Payments.handlePayment",
                "anchors": [{"kind": "symbol", "value": "com.example.Payments.handlePayment"}]}
        request = core.parse_context_request(json.dumps({"INVESTIGATION_REQUEST": body}))
        self.assertEqual(["com.example.Payments.handlePayment"], [item["query"] for item in request["searches"]])
        lower_objective = core.parse_context_request(json.dumps({"INVESTIGATION_REQUEST": {
            **body, "objective": body["objective"].lower(),
        }}))
        self.assertEqual(request["searches"], lower_objective["searches"])
        old = atlas.route(self.settings, request["objective"], {**request, "anchors": []}, generation)
        self.assertTrue(any(item["path"] == "Other.java" for item in old["candidates"]))
        for warm in (False, True):
            route = atlas.route(self.settings, request["objective"], request, generation)
            self.assertEqual(warm, route["cache_hit"])
            self.assertTrue(route["qualified_symbols_only"])
            self.assertEqual({"Payments.java"}, {item["path"] for item in route["candidates"]})
        for extra in ({"resolve": ["Other"]}, {"files": [{"repo": "repo", "path": "Other.java"}]}):
            mixed = core.parse_context_request(json.dumps({"INVESTIGATION_REQUEST": {**body, **extra}}))
            bundle = core.retrieve_context(self.settings, mixed)
            self.assertIn("Other.java", {item.path for item in bundle.evidence}, "independent requested evidence must survive")
        connection = investigation.connect(self.settings)
        try:
            connection.execute("UPDATE atlas_entities SET fingerprint='corrupt' WHERE simple_name='Payments'")
            connection.commit()
        finally:
            connection.close()
        failed = atlas.route(self.settings, request["objective"], request, generation)
        self.assertEqual([], failed["candidates"], "a sealed route cache must not hide corrupt symbol ownership")
        bundle = core.retrieve_context(self.settings, request)
        self.assertEqual([], bundle.evidence)
        self.assertTrue(any("Qualified symbol resolution is unavailable" in item for item in bundle.warnings))

    def test_python_module_qualified_symbols_deliver_the_requested_definition(self):
        path = self.root / "repo/src/ledger/payments.py"
        path.parent.mkdir(parents=True)
        source = (
            "def route():\n    return 'MODULE_ROUTE'\n\n"
            "class Payments:\n    def handle_payment(self):\n        return 'CLASS_METHOD'\n\n"
            "def outer():\n    def route():\n        return 'NESTED_NOT_MODULE'\n    return route()\n"
        )
        path.write_text(source, encoding="utf-8")
        wrong = self.root / "repo/src/other/payments.py"
        wrong.parent.mkdir(parents=True)
        wrong.write_text(source.replace("MODULE_ROUTE", "WRONG_MODULE"), encoding="utf-8")
        core.snapshot_indexes(self.settings)
        generation = current_generation_ref(self.settings)
        for number, (query, line, marker) in enumerate((
            ("ledger.payments.route", 1, "MODULE_ROUTE"),
            ("ledger.payments.Payments.handle_payment", 5, "CLASS_METHOD"),
        ), 1):
            with self.subTest(query=query):
                result = investigation.resolve_runtime_anchors(self.settings, generation, [{"kind": "symbol", "value": query}])
                self.assertEqual([("src/ledger/payments.py", line)], [(item["path"], item["line"]) for item in result["candidates"]])
                core.start_session(self.settings, f"PYTHON-{number}", f"Inspect {query}")
                request = {"INVESTIGATION_REQUEST": {"version": 5, "mode": "implementation_plan",
                           "objective": f"Inspect {query}", "anchors": [{"kind": "symbol", "value": query}]}}
                content, _, _ = core.create_context(self.settings, f"PYTHON-{number}", json.dumps(request))
                self.assertIn(marker, content)
                self.assertNotIn("WRONG_MODULE", content)
                self.assertNotIn("returned no code matches", content)

    def test_v5_test_surface_delivers_test_source_for_a_qualified_method(self):
        test_path = self.root / "repo/src/test/java/PaymentsTest.java"
        test_path.parent.mkdir(parents=True)
        test_path.write_text(
            "class PaymentsTest {\n"
            "  @Test void validatesPayment() {\n"
            "    Payments payments = new Payments();\n"
            "    payments.handlePayment();\n"
            "    assertTrue(paymentAccepted());\n"
            "  }\n}\n", encoding="utf-8",
        )
        core.snapshot_indexes(self.settings)
        for number, (mode, required) in enumerate((("test_surface", []), ("implementation_plan", ["tests"])), 1):
            with self.subTest(mode=mode):
                ticket = f"TEST-SURFACE-{number}"
                core.start_session(self.settings, ticket, "Inspect Payments.handlePayment")
                request = {"INVESTIGATION_REQUEST": {
                    "version": 5, "mode": mode, "objective": "Inspect Payments.handlePayment",
                    "anchors": [{"kind": "symbol", "value": "Payments.handlePayment"}], "required": required,
                }}
                content, _, _ = core.create_context(self.settings, ticket, json.dumps(request))
                self.assertIn("assertTrue(paymentAccepted());", content)
                runtime = core.session_state(self.settings, ticket)["investigation_runtime"]
                self.assertEqual("verified", runtime["coverage"]["tests"])
                self.assertFalse(any(item.get("coverage_key") == "tests" for item in runtime["evidence_frontier"]["items"]))

    def test_requested_test_planning_keeps_explicit_anchor_priority_and_bounds(self):
        from brain.retrieval.planner import compile_request

        request = core.parse_context_request(json.dumps({"INVESTIGATION_REQUEST": {
            "version": 5, "mode": "test_surface", "objective": "Inspect Payments.handlePayment",
            "anchors": [{"kind": "symbol", "value": "Payments.handlePayment"}],
        }}))
        limited = compile_request(request, max_effective_operations=1)
        self.assertEqual([("symbol", ("definition", "tests"))],
                         [(item.kind, item.includes) for item in limited.operations])
        self.assertTrue(limited.operations[0].protected)
        self.assertEqual(0, limited.deferred_operations)
        complete = compile_request(request, max_effective_operations=2)
        self.assertEqual([("symbol", ("definition", "tests"))],
                         [(item.kind, item.includes) for item in complete.operations])
        request["symbols"] = [{"name": "Payments.handlePayment", "include": ["tests"]}]
        self.assertEqual(1, len(compile_request(request).operations), "merge the definition and duplicate test requests")

    def test_required_test_discovery_reaches_remote_test_repo_at_scale(self):
        from brain import index

        test = self.root / "zz-tests/src/test/java/PaymentsTest.java"
        test.parent.mkdir(parents=True)
        test.write_text("class PaymentsTest {\n @Test void acceptsPayment() {\n"
                        "  new Payments().handlePayment();\n  assertTrue(accepted());\n }\n}\n", encoding="utf-8")
        base = self.settings.config_path.read_text(encoding="utf-8")
        added = 0
        request = core.parse_context_request(json.dumps({"INVESTIGATION_REQUEST": {
            "version": 5, "mode": "test_surface", "objective": "Inspect Payments.handlePayment",
            "anchors": [{"kind": "symbol", "value": "Payments.handlePayment"}],
        }}))
        for count in (10, 50, 100):
            with self.subTest(repositories=count):
                for number in range(added, count - 2):
                    repo = self.root / f"other{number:03}"
                    repo.mkdir()
                    (repo / "Usage.java").write_text("class Usage {\n void work() {\n" +
                        "  dependency.handlePayment();\n" * 40 + " }\n}\n", encoding="utf-8")
                added = count - 2
                config = base + "".join(f"[[repositories]]\nname='other{n:03}'\npath='other{n:03}'\n" for n in range(added))
                config += "[[repositories]]\nname='zz-tests'\npath='zz-tests'\n"
                self.settings.config_path.write_text(config, encoding="utf-8")
                self.settings = core.load_settings(self.settings.config_path)
                core.snapshot_indexes(self.settings)
                generation = current_generation_ref(self.settings)
                pinned = replace(self.settings, atlas_generation=generation, atlas_generation_mode="pinned",
                    initial_repo_limit=1, widen_repo_limit=2,
                    repositories=[replace(repo, source_sha=generation.snapshots[repo.name]) for repo in self.settings.repositories])
                with mock.patch.object(index, "query_generation_indexes", wraps=index.query_generation_indexes) as queries, \
                        mock.patch.object(index, "read_generation_files", wraps=index.read_generation_files) as sources, \
                        mock.patch("brain.editions.current_edition", return_value="precision"), \
                        mock.patch("brain.semantic.search_semantic", side_effect=AssertionError("optional model search")), \
                        mock.patch("brain.models.rerank_candidates", side_effect=AssertionError("optional model rerank")):
                    bundle = core.retrieve_context(pinned, request)
                    packed = core.pack_context(pinned, f"TEST-SCALE-{count}", 1, bundle)
                self.assertEqual({("repo", "Payments.java"), ("zz-tests", "src/test/java/PaymentsTest.java")},
                                 {(item.repo, item.path) for item in bundle.evidence})
                self.assertIn("assertTrue(accepted());", packed)
                self.assertNotIn("dependency.handlePayment", packed)
                self.assertEqual(1, queries.call_count)
                self.assertTrue(queries.call_args.kwargs["test_only"])
                self.assertEqual(count, len(queries.call_args.args[2]))
                self.assertEqual(1, sources.call_count, "source hydration stays one batch at every repository scale")
                self.assertIn("source-hydration", bundle.trace["backend_ms"])
                # Resolver, global test lookup, source batch, and exact ranges.
                self.assertEqual(4, bundle.metrics["physical_backend_operations"])
                self.assertLessEqual(bundle.metrics["bytes_read"], 2_000)
                if count == 10:
                    self.assertEqual([], core.test_hits(pinned, "Payments.handlePayment", ["repo"]))
                    self.assertEqual({"zz-tests"}, {hit.repo for hit in core.test_hits(pinned, "Payments.handlePayment", ["zz-tests"])})

    def test_test_lookup_cache_and_failure_keep_pinned_generation_authority(self):
        from brain import index
        from brain.retrieval.models import RetrievalTrace

        path = self.root / "repo/tests/test_payment.py"
        path.parent.mkdir()
        path.write_text("def test_payment():\n    assert handlePayment('OLD')\n", encoding="utf-8")
        core.snapshot_indexes(self.settings)
        old = current_generation_ref(self.settings)
        path.write_text("def test_payment():\n    assert handlePayment('NEW')\n", encoding="utf-8")
        core.snapshot_indexes(self.settings)
        new = current_generation_ref(self.settings)
        cache = core._ACTIVE_RETRIEVAL_CACHE.set({})
        try:
            for generation, marker in ((old, "OLD"), (new, "NEW"), (old, "OLD")):
                pinned = replace(self.settings, atlas_generation=generation, atlas_generation_mode="pinned",
                                 repositories=[replace(repo, source_sha=generation.snapshots[repo.name]) for repo in self.settings.repositories])
                hits = core.test_hits(pinned, "Payments.handlePayment")
                self.assertEqual(1, len(hits))
                self.assertIn(marker, hits[0].text)
                self.assertEqual([], core.test_hits(pinned, "Payments.missing"))
            core._ACTIVE_RETRIEVAL_CACHE.get().clear()
            trace = RetrievalTrace(max_physical_backend_operations=0)
            token = core._ACTIVE_RETRIEVAL_TRACE.set(trace)
            try:
                with mock.patch.object(index, "query_generation_indexes", side_effect=AssertionError("budget bypass")):
                    self.assertEqual([], core.test_hits(pinned, "Payments.handlePayment"))
                self.assertEqual(0, trace.physical_backend_operations)
            finally:
                core._ACTIVE_RETRIEVAL_TRACE.reset(token)
        finally:
            core._ACTIVE_RETRIEVAL_CACHE.reset(cache)

    def test_test_index_scope_filter_budgets_and_corruption(self):
        from brain import index
        from brain.retrieval.models import RetrievalTrace

        path = self.root / "repo/tests/test_payment.py"
        path.parent.mkdir()
        path.write_text("def test_payment():\n    assert handlePayment('go')\n", encoding="utf-8")
        core.snapshot_indexes(self.settings)
        generation = current_generation_ref(self.settings)
        pinned = replace(self.settings, atlas_generation=generation, atlas_generation_mode="pinned",
                         repositories=[replace(repo, source_sha=generation.snapshots[repo.name]) for repo in self.settings.repositories])
        budget = dict(max_results=5, max_candidate_files=1, max_hits=5, max_bytes=2_000, max_seconds=5)
        for query in ("handlePayment", "go"):
            stats = {}
            hits = index.query_generation_indexes(pinned, generation, pinned.repositories, query,
                                                  **budget, test_only=True, stats=stats)
            self.assertEqual(["tests/test_payment.py"], [row[0] for row in hits["repo"]])
            self.assertFalse(stats["budget_exhausted"])
        cache = core._ACTIVE_RETRIEVAL_CACHE.set({})
        trace = RetrievalTrace()
        token = core._ACTIVE_RETRIEVAL_TRACE.set(trace)
        try:
            self.assertIn("Payments.java", {hit.path for hit in core.search(pinned, "handlePayment", fixed=True)})
            with mock.patch.object(core, "MAX_PINNED_QUERY_BYTES", 1):
                self.assertEqual([], core.test_hits(pinned, "Payments.handlePayment", ["repo"]))
            self.assertEqual("lexical_batch_budget", trace.stop_reason)
            self.assertIn("test_search_budget:bytes", trace.fallback_reasons)
            self.assertEqual(["tests/test_payment.py"], [hit.path for hit in core.test_hits(pinned, "Payments.handlePayment", ["repo"])],
                             "a partial empty result must not poison the test cache")
        finally:
            core._ACTIVE_RETRIEVAL_TRACE.reset(token)
            core._ACTIVE_RETRIEVAL_CACHE.reset(cache)
        connection = index._connect(pinned)
        try:
            connection.execute("UPDATE blobs SET content='corrupt' WHERE blob IN "
                               "(SELECT blob FROM file_membership WHERE path='tests/test_payment.py')")
            connection.commit()
        finally:
            connection.close()
        self.assertIsNone(index.query_generation_indexes(pinned, generation, pinned.repositories, "handlePayment",
                                                        **budget, test_only=True), "corrupt indexed content is not evidence")

    def test_test_surface_tickets_retain_old_test_source_after_refresh(self):
        path = self.root / "repo/tests/test_payment.py"
        path.parent.mkdir()
        path.write_text("def test_payment():\n    assert handlePayment('OLD')\n", encoding="utf-8")
        core.snapshot_indexes(self.settings)
        core.start_session(self.settings, "TEST-OLD", "Inspect Payments.handlePayment")
        request = json.dumps({"INVESTIGATION_REQUEST": {
            "version": 5, "mode": "test_surface", "objective": "Inspect Payments.handlePayment",
            "anchors": [{"kind": "symbol", "value": "Payments.handlePayment"}],
        }})
        path.write_text("def test_payment():\n    assert handlePayment('NEW')\n", encoding="utf-8")
        core.snapshot_indexes(self.settings)
        core.start_session(self.settings, "TEST-NEW", "Inspect Payments.handlePayment")
        for ticket, expected, forbidden in (("TEST-OLD", "OLD", "NEW"), ("TEST-NEW", "NEW", "OLD")):
            content, _, _ = core.create_context(self.settings, ticket, request)
            self.assertIn(f"handlePayment('{expected}')", content)
            self.assertNotIn(f"handlePayment('{forbidden}')", content)

    def test_python_module_layouts_scope_and_pinned_cache_validation(self):
        source = (
            "def route():\n    return 'OLD'\n\n"
            "class Service:\n    class Nested:\n        def execute(self):\n            return 'NESTED'\n\n"
            "def outer():\n    def hidden():\n        return 'LOCAL_ONLY'\n    return hidden()\n"
        )
        paths = ("ledger/payments.py", "src/ledger/handlers.py", "ledger/entry/__init__.py")
        for relative in paths:
            path = self.root / "repo" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
        core.snapshot_indexes(self.settings)
        old = current_generation_ref(self.settings)
        query = [{"kind": "symbol", "value": "ledger.payments.route"}]
        original = investigation.resolve_runtime_anchors(self.settings, old, query)
        for module, relative in zip(("ledger.payments", "ledger.handlers", "ledger.entry"), paths):
            for suffix, line in (("route", 1), ("Service.Nested.execute", 6)):
                with self.subTest(module=module, suffix=suffix):
                    anchors = [{"kind": "symbol", "value": module + "." + suffix}]
                    result = investigation.resolve_runtime_anchors(self.settings, old, anchors)
                    self.assertEqual([(relative, line)], [(item["path"], item["line"]) for item in result["candidates"]])
                    cached = investigation.resolve_runtime_anchors(self.settings, old, anchors)
                    self.assertTrue(cached["cache_hit"])
                    self.assertEqual(result["candidates"], cached["candidates"])
        for missing in ("ledger.payments.hidden", "ledger.payments.outer.hidden", "missing.payments.route",
                        "ledger.payments.Service.execute", "Ledger.payments.route"):
            result = investigation.resolve_runtime_anchors(self.settings, old, [{"kind": "symbol", "value": missing}])
            self.assertEqual([], result["candidates"], missing)
        (self.root / "repo" / paths[0]).write_text(source.replace("OLD", "NEW"), encoding="utf-8")
        core.snapshot_indexes(self.settings)
        current = current_generation_ref(self.settings)
        self.assertEqual(original["candidates"], investigation.resolve_runtime_anchors(self.settings, old, query)["candidates"])
        new = investigation.resolve_runtime_anchors(self.settings, current, query)
        self.assertNotEqual(original["candidates"][0]["entity_id"], new["candidates"][0]["entity_id"])
        connection = investigation.connect(self.settings)
        try:
            connection.execute("UPDATE atlas_entities SET fingerprint='corrupt' WHERE entity_id IN "
                               "(SELECT e.entity_id FROM atlas_entities e JOIN generation_entities g ON g.entity_id=e.entity_id "
                               "WHERE g.generation=? AND e.path=? AND e.kind='file')", (old.generation, paths[0]))
            connection.commit()
        finally:
            connection.close()
        failed = investigation.resolve_runtime_anchors(self.settings, old, query)
        self.assertEqual("degraded", failed["status"])
        self.assertEqual([], failed["candidates"])
        self.assertEqual(new["candidates"], investigation.resolve_runtime_anchors(self.settings, current, query)["candidates"])

    def test_deep_anchor_cache_preserves_name_identity_and_rejects_corrupt_entities(self):
        generation = self._publish_deep_source()
        correct = [{"kind": "symbol", "value": "handlePayment"}]
        wrong_case = [{"kind": "symbol", "value": "handlepayment"}]
        self.assertEqual([], investigation.resolve_runtime_anchors(self.settings, generation, wrong_case)["candidates"])
        result = investigation.resolve_runtime_anchors(self.settings, generation, correct)
        self.assertFalse(result["cache_hit"])
        self.assertEqual(["entity_name"], [item["method"] for item in result["candidates"]])
        cached = investigation.resolve_runtime_anchors(self.settings, generation, correct)
        self.assertTrue(cached["cache_hit"])
        self.assertEqual(result["candidates"], cached["candidates"])
        connection = investigation.connect(self.settings)
        try:
            connection.execute("UPDATE atlas_entities SET fingerprint='corrupt' WHERE entity_id=?",
                               (result["candidates"][0]["entity_id"],))
            connection.commit()
        finally:
            connection.close()
        failed = investigation.resolve_runtime_anchors(self.settings, generation, correct)
        self.assertEqual("degraded", failed["status"])
        self.assertEqual([], failed["candidates"])

    def test_pre_entity_lookup_negative_cache_does_not_hide_deep_anchor(self):
        generation = self._publish_deep_source()
        query = [{"kind": "symbol", "value": "handlePayment"}]
        original_hash = investigation._hash

        def legacy_hash(*values):
            return original_hash(*(values[:-1] if values[0] == "runtime-anchors" else values))

        with mock.patch.object(investigation, "_hash", side_effect=legacy_hash), mock.patch.object(
            investigation, "_entity_name_anchor_rows", return_value=([], 0, False),
        ):
            old = investigation.resolve_runtime_anchors(self.settings, generation, query)
        self.assertEqual([], old["candidates"])
        result = investigation.resolve_runtime_anchors(self.settings, generation, query)
        self.assertFalse(result["cache_hit"])
        self.assertEqual(1, len(result["candidates"]))

    def test_deep_anchor_requires_registered_hierarchy_even_with_a_warm_cache(self):
        generation = self._publish_deep_source()
        query = [{"kind": "symbol", "value": "handlePayment"}]
        for change in ({"status": "unavailable"}, {"schema_version": "incompatible"}):
            with self.subTest(change=change):
                self.assertTrue(investigation.resolve_runtime_anchors(self.settings, generation, query)["candidates"])
                broken = replace(generation, components={**generation.components,
                    "hierarchy": {**generation.component("hierarchy"), **change}})
                result = investigation.resolve_runtime_anchors(self.settings, broken, query)
                self.assertEqual("degraded", result["status"])
                self.assertEqual([], result["candidates"])

    def test_deep_cached_anchor_cannot_hide_missing_entity_membership(self):
        generation = self._publish_deep_source()
        query = [{"kind": "symbol", "value": "handlePayment"}]
        result = investigation.resolve_runtime_anchors(self.settings, generation, query)
        connection = investigation.connect(self.settings)
        try:
            connection.execute("DELETE FROM generation_entities WHERE generation=? AND entity_id=?",
                               (generation.generation, result["candidates"][0]["entity_id"]))
            connection.commit()
        finally:
            connection.close()
        result = investigation.resolve_runtime_anchors(self.settings, generation, query)
        self.assertEqual("degraded", result["status"])
        self.assertEqual([], result["candidates"])

    def test_deep_v5_flow_keeps_the_old_ticket_generation(self):
        generation = self._publish_deep_source()
        request = {"INVESTIGATION_REQUEST": {"version": 5, "mode": "flow_trace", "objective": "Trace handlePayment",
                   "anchors": [{"kind": "symbol", "value": "Payments.handlePayment"}]}}
        core.start_session(self.settings, "DEEP-OLD", "Trace handlePayment")
        core.create_context(self.settings, "DEEP-OLD", json.dumps(request))
        self.path.write_text(self.source.replace("validatePayment", "newValidation"), encoding="utf-8")
        core.snapshot_indexes(self.settings)
        current = current_generation_ref(self.settings)
        followup = {"INVESTIGATION_REQUEST": {**request["INVESTIGATION_REQUEST"],
                    "objective": "Confirm the handlePayment call path", "base_context_id": "CTX-001"}}
        core.create_context(self.settings, "DEEP-OLD", json.dumps(followup))
        core.start_session(self.settings, "DEEP-NEW", "Trace handlePayment")
        core.create_context(self.settings, "DEEP-NEW", json.dumps(request))
        for ticket, expected_generation, target in (("DEEP-OLD", generation, "validatePayment"),
                                                   ("DEEP-NEW", current, "newValidation")):
            runtime = core.session_state(self.settings, ticket)["investigation_runtime"]
            self.assertEqual(expected_generation.generation, runtime["generation"])
            self.assertEqual([target, "checkAccount"], [item["target"] for item in runtime["execution_flow"]["steps"]])
            self.assertTrue(all(item["state"] == "verified" and item["evidence_ids"]
                                for item in runtime["execution_flow"]["steps"]))
            self.assertEqual("verified", runtime["coverage"]["main_execution_flow"])

    def test_legacy_negative_cache_does_not_hide_the_repaired_query(self):
        compound = investigation._compound_terms

        def legacy_terms(value):
            # Reproduce the pre-fix term list and its valid cache identity,
            # without changing the already-published casefolded projection.
            return compound("handlePayment") if value == "handlepayment" else compound(value)

        query = [{"kind": "symbol", "value": "handlePayment"}]
        with mock.patch.object(investigation, "_compound_terms", side_effect=legacy_terms), mock.patch.object(
            investigation, "_entity_name_anchor_rows", return_value=([], 0, False),
        ):
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
        self._publish_deep_source()
        config = self.settings.config_path.read_text(encoding="utf-8")
        added = 1
        operation_counts = []
        lookup_vm_steps = []
        lookup = investigation._entity_name_anchor_rows

        def measured_lookup(connection, generation, names):
            ticks = []
            connection.set_progress_handler(lambda: ticks.append(1) or 0, 1)
            try:
                return lookup(connection, generation, names)
            finally:
                connection.set_progress_handler(None, 0)
                lookup_vm_steps.append(len(ticks))

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
                with mock.patch.object(investigation, "_java_file_intelligence", side_effect=AssertionError("query-time parse")), \
                        mock.patch.object(investigation, "_entity_name_anchor_rows", side_effect=measured_lookup):
                    result = investigation.resolve_runtime_anchors(self.settings, generation,
                                                                  [{"kind": "symbol", "value": "handlePayment"}], use_cache=False)
                self.assertEqual("ready", result["status"])
                self.assertEqual(["repo"], [item["repo"] for item in result["candidates"]])
                self.assertEqual(self.deep_path, result["candidates"][0]["path"])
                self.assertEqual("entity_name", result["candidates"][0]["method"])
                self.assertTrue(result["candidates"][0]["entity_id"])
                self.assertLessEqual(result["database_operations"], 16)
                operation_counts.append(result["database_operations"])
        self.assertEqual(1, len(set(operation_counts)))
        self.assertEqual(3, len(lookup_vm_steps))
        self.assertGreater(min(lookup_vm_steps), 0, "the SQLite progress counter must observe executed work")
        self.assertLessEqual(max(lookup_vm_steps), 2_000, "entity-name fallback must seek indexes, not scan all entities")

    def test_qualified_resolution_seeks_scope_at_repository_scale(self):
        self._publish_deep_source()
        self.path.write_text("package com.example;\n" + self.source, encoding="utf-8")
        config = self.settings.config_path.read_text(encoding="utf-8")
        added = 1
        measurements = {query: [] for query in ("Payments.handlePayment", "com.example.Payments.handlePayment")}
        lookup = investigation._entity_name_anchor_rows

        for count in (10, 50, 100):
            for number in range(added, count):
                repo = self.root / f"other{number}"
                repo.mkdir()
                (repo / "Other.java").write_text(f"package unrelated.repo{number};\n" + "".join(
                    f"class Other{item} {{ void handlePayment() {{}} }}\n" for item in range(20)
                ), encoding="utf-8")
                config += f"[[repositories]]\nname='other{number}'\npath='other{number}'\n"
            added = count
            self.settings.config_path.write_text(config, encoding="utf-8")
            self.settings = core.load_settings(self.settings.config_path)
            core.snapshot_indexes(self.settings)
            generation = current_generation_ref(self.settings)
            for query, observations in measurements.items():
                with self.subTest(repositories=count, query=query):
                    ticks = []

                    def measured_lookup(connection, pinned, names):
                        connection.set_progress_handler(lambda: ticks.append(1) or 0, 1)
                        try:
                            return lookup(connection, pinned, names)
                        finally:
                            connection.set_progress_handler(None, 0)

                    with mock.patch.object(investigation, "_entity_name_anchor_rows", side_effect=measured_lookup), \
                            mock.patch.object(investigation, "_java_file_intelligence", side_effect=AssertionError("query-time parse")):
                        result = investigation.resolve_runtime_anchors(
                            self.settings, generation, [{"kind": "symbol", "value": query}], use_cache=False,
                        )
                    self.assertEqual("ready", result["status"])
                    self.assertEqual([(self.deep_path, 3)], [(item["path"], item["line"]) for item in result["candidates"]])
                    self.assertLessEqual(result["database_operations"], 12)
                    self.assertGreater(len(ticks), 0)
                    self.assertLessEqual(len(ticks), 3_000, "includes canonical owner and package validation")
                    observations.append(len(ticks))
            request = core.parse_context_request(json.dumps({"INVESTIGATION_REQUEST": {
                "version": 5, "mode": "flow_trace", "objective": "Trace com.example.Payments.handlePayment",
                "anchors": [{"kind": "symbol", "value": "com.example.Payments.handlePayment"}],
            }}))
            with mock.patch("brain.editions.current_edition", return_value="precision"), \
                    mock.patch("brain.semantic.search_semantic", side_effect=AssertionError("unnecessary model search")), \
                    mock.patch("brain.models.rerank_candidates", side_effect=AssertionError("unnecessary model rerank")):
                bundle = core.retrieve_context(self.settings, request)
                packed = core.pack_context(self.settings, f"SCALE-{count}", 1, bundle)
            self.assertEqual({("repo", self.deep_path)}, {(item.repo, item.path) for item in bundle.evidence})
            self.assertEqual([], bundle.unresolved)
            self.assertNotIn("lexical_references_only", bundle.trace["fallback_reasons"])
            self.assertNotIn("class Other", packed)
            self.assertLessEqual(bundle.metrics["physical_backend_operations"], 2)
            self.assertLessEqual(bundle.metrics["candidates"], 4)
        for observations in measurements.values():
            self.assertEqual(3, len(observations))
            self.assertLessEqual(max(observations), min(observations) + 100,
                                 "qualified lookup must not enumerate same-named methods in unrelated repositories")


if __name__ == "__main__":
    unittest.main()

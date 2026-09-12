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

from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest import mock

from brain import mps_index
from brain.catalog import canonical_atlas_identity, collect_generation_components, current_generation_ref, publish_current_components, publish_generation, resolve_generation
from brain.core import (BrainError, ContextBundle, SearchHit, _restore_checkpoint_evidence, create_context, load_settings,
                        parse_context_request, read_source, session_state, snapshot_indexes, start_session)
from brain.investigation import _validated_prior_evidence_ids
from brain.index import _connect, lexical_membership_identity
from brain.ops import gc, refresh_brain
from brain.platforms import native_command
from test_mps import model


class MpsRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / "service"
        self.repository.mkdir()
        self.config = self.root / "brain.toml"
        self.config.write_text("[project]\nname='mps-retrieval'\n[graph]\nenabled=false\n"
                               "[experience]\nenabled=false\n[[repositories]]\nname='service'\npath='service'\n")
        self.settings = load_settings(self.config)
        self.write_models()
        (self.repository / "code.py").write_text("def unchanged_code():\n    return 1\n")

    def write_models(self, label: str = "FlowA") -> None:
        documents = {
            "a.mps": model("f:a", f'<node concept="c" id="a"><property role="p" value="{label}"/>'
                           '<node concept="c" id="call" role="h"><ref role="r" to="b:inside"/></node></node>',
                           '<import index="b" ref="f:b"/>'),
            "b.mps": model("f:b", '<node concept="c" id="b"><property role="p" value="FlowB"/>'
                           '<node concept="c" id="inside" role="h"><ref role="r" to="c:c"/></node></node>',
                           '<import index="c" ref="f:c"/>'),
            "c.mps": model("f:c", '<node concept="c" id="c"><property role="p" value="FlowC"/>'
                           '<ref role="r" to="a:a"/></node>', '<import index="a" ref="f:a"/>'),
        }
        for path, source in documents.items():
            (self.repository / path).write_bytes(source.replace(b'language-id', b'26b3d6d5-b99a-4ed6-83be-d2ea6f3627a1')
                .replace(b'concept-id', b'101').replace(b'reference-id', b'104').replace(b'><', b'>\n<'))

    def git(self, *args: str) -> str:
        result = subprocess.run([native_command("git"), *args], cwd=self.repository, capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def commit_fixture(self) -> str:
        if not (self.repository / ".git").exists():
            self.git("init")
            self.git("config", "user.name", "MPS test")
            self.git("config", "user.email", "mps-test@example.invalid")
        self.git("add", ".")
        self.git("commit", "-m", "MPS fixture")
        sha = self.git("rev-parse", "HEAD")
        self.settings.repo("service").source_sha = sha
        return sha

    @staticmethod
    def request(**values) -> str:
        return json.dumps({"INVESTIGATION_REQUEST": {"version": 5, "mode": "flow_trace",
            "objective": "Trace the exact authoring models", **values}})

    def test_existing_git_sha_gets_optional_component_without_rewriting_lexical_membership(self) -> None:
        sha = self.commit_fixture()
        with mock.patch("brain.mps_index.build_component", return_value={"schema_version": mps_index.SCHEMA_VERSION, "status": "unavailable"}):
            snapshot_indexes(self.settings)
        old = current_generation_ref(self.settings)
        old_identity = canonical_atlas_identity(old.manifest)
        lexical = lexical_membership_identity(self.settings, {"service": sha})
        state, updated = snapshot_indexes(self.settings, changed_only=True)
        generation = current_generation_ref(self.settings)
        self.assertEqual([], updated)
        self.assertGreater(generation.generation, old.generation)
        self.assertEqual("ready", generation.component("mps_models")["status"])
        self.assertEqual(lexical, lexical_membership_identity(self.settings, {"service": sha}))
        self.assertEqual(old_identity, canonical_atlas_identity(resolve_generation(self.settings, generation=old.generation).manifest))
        with closing(_connect(self.settings)) as connection:
            paths = [row[0] for row in connection.execute("SELECT path FROM file_membership WHERE repo='service' AND snapshot_sha=?", (sha,))]
        self.assertNotIn("a.mps", paths)
        self.assertEqual(sha, state["service"]["sha"])
        snapshot_indexes(self.settings, changed_only=True)
        self.assertEqual(generation.generation, current_generation_ref(self.settings).generation)

    def test_full_refresh_builds_and_publishes_mps_sources_once(self) -> None:
        with mock.patch("brain.mps_index.build_component", wraps=mps_index.build_component) as built:
            refresh_brain(self.settings, fetch=False, discover=False)
        self.assertEqual(1, built.call_count)
        generation = current_generation_ref(self.settings)
        self.assertEqual("ready", generation.component("mps_models")["status"])
        self.assertIn("FlowA", mps_index.read_source(self.settings, "service", "a.mps", generation))

    def test_v5_model_routes_direct_files_and_old_ticket_pin_survive_refresh_and_gc(self) -> None:
        self.commit_fixture()
        snapshot_indexes(self.settings)
        original = current_generation_ref(self.settings)
        start_session(self.settings, "MPS-DELIVERY", "Change FlowA's internal jump with exact evidence")
        self.write_models("NEW_FLOW_A")
        self.commit_fixture()
        snapshot_indexes(self.settings, changed_only=True)
        with mock.patch("brain.atlas.route", side_effect=AssertionError("MPS requests must avoid generic discovery")):
            content, _, _ = create_context(self.settings, "MPS-DELIVERY", self.request(
                anchors=[{"kind": "file_hint", "value": "service/a.mps#id:a"}]))
        self.assertIn("MPS structural navigation", content)
        self.assertIn('"node_id": "inside"', content)
        self.assertIn("FlowA", content)
        self.assertNotIn("NEW_FLOW_A", content)
        state = session_state(self.settings, "MPS-DELIVERY")
        trace = state["request_history"][-1]["retrieval"]["trace"]
        self.assertTrue(trace["mps_only"])
        self.assertEqual(1, trace["physical_backend_operations"])
        self.assertEqual(3, len(trace["mps_navigation"]["steps"]))
        self.assertFalse(any(step.get("verified") for step in state["investigation_runtime"].get("execution_flow", {}).get("steps", [])))
        with mock.patch("brain.atlas.route", side_effect=AssertionError("exact files must avoid discovery")):
            page, _, _ = create_context(self.settings, "MPS-DELIVERY", self.request(
                mode="implementation_plan", base_context_id=state["last_context_id"], files=[{"repo": "service", "path": "a.mps", "lines": "1-8"}]))
        self.assertIn('<model ref="f:a">', page)
        self.assertNotIn("NEW_FLOW_A", page)
        state = session_state(self.settings, "MPS-DELIVERY")
        self.assertEqual(original.identity, state["atlas_generation_id"])
        pinned = replace(self.settings, atlas_generation=original, atlas_generation_mode="pinned")
        prior = next(item for item in state["evidence_records"] if item["path"] == "a.mps" and item.get("public_id"))
        state["investigation_runtime"]["hypothesis_ledger"] = {"items": [{"candidate_evidence": [prior["public_id"]]}]}
        with mock.patch("brain.index.read_generation_files", side_effect=AssertionError("MPS proofs must not use lexical membership")):
            valid = _validated_prior_evidence_ids(pinned, original, state, ContextBundle("revalidate", atlas_generation=original), state["stable_identities"])
        self.assertIn(prior["public_id"], valid)
        with mock.patch("brain.atlas.route", side_effect=AssertionError("exact MPS jump must avoid generic discovery")):
            delta, _, _ = create_context(self.settings, "MPS-DELIVERY", self.request(
                mode="impact_analysis", base_context_id=state["last_context_id"],
                anchors=[{"kind": "file_hint", "value": "service/b.mps#id:inside"}]), continue_investigation=True)
        self.assertIn("MPS structural navigation", delta)
        self.assertIn('"node_id": "call"', delta)
        self.assertNotIn("NEW_FLOW_A", delta)
        records = [item for item in state["evidence_records"] if item["path"].endswith(".mps")]
        restored, missed = _restore_checkpoint_evidence(pinned, records)
        self.assertEqual(0, missed)
        self.assertTrue(restored)
        gc(self.settings, dry_run=False, keep_recent=0)
        self.assertIn("FlowA", mps_index.read_source(pinned, "service", "a.mps"))

    def test_nongit_model_only_change_seals_new_source_and_preserves_old_models(self) -> None:
        snapshot_indexes(self.settings)
        old = current_generation_ref(self.settings)
        old_sha = old.snapshots["service"]
        lexical = lexical_membership_identity(self.settings, old.snapshots)
        self.write_models("UPDATED_NONGIT")
        snapshot_indexes(self.settings, changed_only=True)
        new = current_generation_ref(self.settings)
        self.assertNotEqual(old_sha, new.snapshots["service"])
        self.assertEqual(lexical, lexical_membership_identity(self.settings, old.snapshots))
        self.assertIn("FlowA", mps_index.read_source(self.settings, "service", "a.mps", old))
        self.assertIn("UPDATED_NONGIT", mps_index.read_source(self.settings, "service", "a.mps", new))

    def test_three_repository_nested_flow_impact_and_duplicate_identity_keep_ticket_pin(self) -> None:
        orchestration = self.root / "orchestration"
        connector = self.root / "connector"
        orchestration.mkdir()
        connector.mkdir()
        (self.repository / "b.mps").rename(orchestration / "b.mps")
        (self.repository / "c.mps").rename(connector / "c.mps")
        inner = (orchestration / "b.mps").read_bytes()
        (orchestration / "b.mps").write_bytes(inner.replace(b'</node>\n</node>',
            b'</node>\n<node concept="c" id="unrelated" role="h"><ref role="r" to="c:missing"/></node>\n</node>'))
        caller = model("f:shared-caller", '<node concept="c" id="shared"><node concept="c" id="shared-call" role="h">'
            '<ref role="r" to="b:inside"/></node></node>', '<import index="b" ref="f:b"/>')
        (self.repository / "shared.mps").write_bytes(caller.replace(b'><', b'>\n<'))
        with self.config.open("a") as stream:
            stream.write("[[repositories]]\nname='orchestration'\npath='orchestration'\n"
                         "[[repositories]]\nname='connector'\npath='connector'\n")
        self.settings = load_settings(self.config)
        snapshot_indexes(self.settings)
        old = current_generation_ref(self.settings)
        pinned = replace(self.settings, atlas_generation=old, atlas_generation_mode="pinned")
        navigation, evidence = mps_index.navigation(pinned, {"symbols": ["FlowA"], "repos": ["service"]})
        self.assertEqual(3, len(navigation["steps"]))
        self.assertEqual({"service", "orchestration", "connector"}, {item.repo for item in evidence})
        self.assertEqual("orchestration/b.mps#id:inside", navigation["steps"][0]["target_node"]["anchor"])
        self.assertTrue(navigation["steps"][-1]["revisited_node"])
        self.assertFalse(any(step["source_node"]["node_id"] == "unrelated" for step in navigation["steps"]))
        impact, _ = mps_index.navigation(pinned, {"mode": "impact_analysis", "anchors": [
            {"kind": "file_hint", "value": "orchestration/b.mps#id:inside"}]})
        self.assertEqual({"call", "shared-call"}, {step["source_node"]["node_id"] for step in impact["steps"] if step["depth"] == 0})
        start_session(self.settings, "MPS-CROSS-REPO", "Change the shared subflow")
        (orchestration / "b.mps").write_bytes(inner.replace(
            b'<ref role="r"', b'<property role="p" value="UPDATED_INNER"/>\n<ref role="r"'))
        (connector / "b.mps").write_bytes(inner)
        snapshot_indexes(self.settings, changed_only=True)
        current = current_generation_ref(self.settings)
        newer, _ = mps_index.navigation(replace(pinned, atlas_generation=current), {"symbols": ["FlowA"]})
        self.assertEqual("ambiguous", newer["steps"][0]["status"])
        self.assertIsNone(newer["steps"][0]["target_node"])
        with mock.patch("brain.atlas.route", side_effect=AssertionError("exact cross-repo models avoid generic discovery")):
            content, _, _ = create_context(self.settings, "MPS-CROSS-REPO", self.request(
                anchors=[{"kind": "file_hint", "value": "service/a.mps#id:a"}]))
        self.assertIn("orchestration/b.mps#id:inside", content)
        self.assertNotIn("UPDATED_INNER", content)
        state = session_state(self.settings, "MPS-CROSS-REPO")
        self.assertEqual(old.identity, state["atlas_generation_id"])
        self.assertEqual(3, len(state["request_history"][-1]["retrieval"]["trace"]["mps_navigation"]["steps"]))

    def test_repository_context_byte_trim_resumes_exact_connections(self) -> None:
        source = model("f:many", '<node concept="c" id="root">' + ''.join(
            f'<node concept="c" id="call-{number}" role="h"><ref role="r" node="leaf"/></node>'
            for number in range(20)) + '</node><node concept="c" id="leaf"/>').replace(b'><', b'>\n<')
        (self.repository / "many.mps").write_bytes(source)
        snapshot_indexes(self.settings)
        generation = current_generation_ref(self.settings)
        pinned = replace(self.settings, atlas_generation=generation, atlas_generation_mode="pinned", hard_context_chars=24_000)
        first, _ = mps_index.navigation(pinned, {"anchors": [{"kind": "file_hint", "value": "service/many.mps#id:root"}]})
        self.assertTrue(first["steps"])
        self.assertIn("next_step_anchor", first)
        self.assertEqual("outgoing", first["next_step_direction"])
        second, _ = mps_index.navigation(pinned, {"anchors": [{"kind": "file_hint", "value": first["next_step_anchor"]}]})
        self.assertTrue(second["steps"])
        self.assertEqual(first["next_step_anchor"], second["steps"][0]["resume_anchor"])
        self.assertFalse({step["identity"] for step in first["steps"]} & {step["identity"] for step in second["steps"]})

    def test_component_poisoning_rejected_and_same_sha_repaired_without_breaking_old_identity(self) -> None:
        self.commit_fixture()
        snapshot_indexes(self.settings)
        old = current_generation_ref(self.settings)
        pinned = replace(self.settings, atlas_generation=old, atlas_generation_mode="pinned")
        path = self.settings.state_dir / old.component("mps_models")["artifact_ref"]
        payload = json.loads(path.read_text())
        payload["files"][0]["source"] = "POISONED_SOURCE"
        path.write_text(json.dumps(payload))
        (self.repository / "a.mps").write_text("WORKING_TREE_FALLBACK_MUST_NOT_APPEAR")
        with self.assertRaisesRegex(BrainError, "Pinned MPS source"):
            read_source(pinned, SearchHit("service", "a.mps", 1, "", "model", 100))
        snapshot_indexes(self.settings, changed_only=True)
        repaired = current_generation_ref(self.settings)
        self.assertGreater(repaired.generation, old.generation)
        self.assertIn("FlowA", mps_index.read_source(self.settings, "service", "a.mps", repaired))
        self.assertIsNone(mps_index.read_source(self.settings, "service", "a.mps", old))

    def test_project_flow_mapping_is_exact_pinned_and_not_runtime_proof(self) -> None:
        declaration = {"language_id": "26b3d6d5-b99a-4ed6-83be-d2ea6f3627a1", "concept_id": "101", "role_id": "104",
                       "kind": "flow_call", "evidence": {"path": "flow-semantics.md", "line_start": 1, "line_end": 1}}
        (self.repository / "flow-semantics.md").write_text("Role 104 is a declared flow-call reference in this example DSL.\n")
        (self.repository / mps_index.MAPPING_PATH).write_text(json.dumps({"version": 1, "mappings": [declaration]}))
        self.commit_fixture()
        snapshot_indexes(self.settings)
        generation = current_generation_ref(self.settings)
        pinned = replace(self.settings, atlas_generation=generation, atlas_generation_mode="pinned")
        navigation, evidence = mps_index.navigation(pinned, {"symbols": ["FlowA"]})
        self.assertTrue(evidence)
        self.assertEqual("project_declared_with_source", navigation["steps"][0]["flow_mapping"]["status"])
        self.assertIn("not observed execution", navigation["steps"][0]["flow_mapping"]["semantics"])
        self.assertEqual("delivered", navigation["steps"][0]["flow_mapping"]["source_status"])
        self.assertIn(mps_index.MAPPING_PATH, {item.path for item in evidence})
        self.assertIn("flow-semantics.md", {item.path for item in evidence})
        (self.repository / mps_index.MAPPING_PATH).write_text(json.dumps({"version": 1, "mappings": [declaration, declaration]}))
        self.commit_fixture()
        snapshot_indexes(self.settings, changed_only=True)
        new = current_generation_ref(self.settings)
        ambiguous, _ = mps_index.navigation(replace(pinned, atlas_generation=new), {"symbols": ["FlowA"]})
        self.assertEqual("ambiguous", ambiguous["steps"][0]["flow_mapping"]["status"])
        self.assertEqual("project_declared_with_source", mps_index.navigation(pinned, {"symbols": ["FlowA"]})[0]["steps"][0]["flow_mapping"]["status"])

    def test_parser_upgrade_republishes_and_failed_optional_refresh_retains_aligned_component(self) -> None:
        self.commit_fixture()
        with mock.patch.object(mps_index, "PARSER_VERSION", "mps-v9-v1"):
            snapshot_indexes(self.settings)
        legacy = current_generation_ref(self.settings)
        snapshot_indexes(self.settings)
        old = current_generation_ref(self.settings)
        self.assertGreater(old.generation, legacy.generation)
        self.assertEqual("mps-v9-v2", old.component("mps_models")["details"]["parser_version"])
        self.assertIsNotNone(mps_index.load_component(self.settings, legacy))
        with mock.patch.object(mps_index, "PARSER_VERSION", "mps-v9-v3"):
            snapshot_indexes(self.settings, changed_only=True)
            new = current_generation_ref(self.settings)
            self.assertGreater(new.generation, old.generation)
            self.assertIsNotNone(mps_index.load_component(self.settings, old))
            with mock.patch("brain.mps_index.build_component", return_value={"status": "unavailable"}):
                state, _ = snapshot_indexes(self.settings, changed_only=True)
            self.assertEqual(new.generation, current_generation_ref(self.settings).generation)
            self.assertIn("retaining", state["service"]["mps_warning"])

    def test_all_publication_paths_retain_component_on_same_snapshot_optional_failure(self) -> None:
        self.commit_fixture()
        snapshot_indexes(self.settings)
        old = current_generation_ref(self.settings)
        with mock.patch("brain.mps_index.build_component", return_value={"status": "unavailable"}):
            publish_current_components(self.settings)
        current = current_generation_ref(self.settings)
        self.assertEqual(old.component("mps_models")["content_hash"], current.component("mps_models")["content_hash"])
        self.assertIsNotNone(mps_index.load_component(self.settings, current))

    def test_publication_rejects_missing_artifact_and_forged_git_membership(self) -> None:
        self.commit_fixture()
        state, _ = snapshot_indexes(self.settings)
        components = collect_generation_components(self.settings, state)
        components["mps_models"].pop("_artifact_source")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "requires an immutable source artifact"):
            publish_generation(self.settings, state, components=components)
        components = collect_generation_components(self.settings, state)
        payload = json.loads(Path(components["mps_models"]["_artifact_source"]).read_text())
        payload["files"][0]["path"] = "forged.mps"
        payload["source_manifest_hash"] = mps_index._hash([{key: item[key] for key in ("repo", "snapshot", "path", "blob", "sha256")} for item in payload["files"]])
        components["mps_models"]["details"]["source_manifest_hash"] = payload["source_manifest_hash"]
        components["mps_models"]["content_hash"] = mps_index._hash(payload)
        Path(components["mps_models"]["_artifact_source"]).write_text(json.dumps(payload))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "path/blob membership"):
            publish_generation(self.settings, state, components=components)

    def test_source_bounds_are_applied_before_git_load_and_nongit_omissions_are_explicit(self) -> None:
        (self.repository / "large.mps").write_bytes(b"x" * 3_000_001)
        (self.repository / "binary.mps").write_bytes(b"\0binary-persistence")
        state, _ = snapshot_indexes(self.settings)
        component = current_generation_ref(self.settings).component("mps_models")
        self.assertTrue(any("excluded from the immutable source seal" in item["reason"] for item in component["details"]["limitations"]))
        self.commit_fixture()
        from brain.index import _git_blob_contents

        with mock.patch.object(mps_index.mps, "MAX_PROJECT_BYTES", 300), mock.patch("brain.index._git_blob_contents", wraps=_git_blob_contents) as loaded:
            candidate = mps_index.build_component(self.settings, {"service": {"sha": self.settings.repo("service").source_sha}})
        self.assertLessEqual(candidate["details"]["source_bytes"], 300)
        self.assertTrue(candidate["details"]["limitations"])
        self.assertTrue(all(call.kwargs.get("max_total_bytes", 0) <= 300 for call in loaded.call_args_list))

    def test_navigation_is_bounded_scoped_and_stops_at_expired_deadline(self) -> None:
        snapshot_indexes(self.settings)
        generation = current_generation_ref(self.settings)
        pinned = replace(self.settings, atlas_generation=generation, atlas_generation_mode="pinned", hard_context_chars=3000)
        navigation, evidence = mps_index.navigation(pinned, {"anchors": [{"kind": "file_hint", "value": "a.mps#id:a"}]})
        self.assertTrue(evidence)
        self.assertLessEqual(len(json.dumps(navigation, indent=2, ensure_ascii=True).encode()), 1024)
        self.assertTrue(navigation["truncated"])
        self.assertEqual((None, []), mps_index.navigation(pinned, {"symbols": ["FlowA"], "repos": ["other-repo"]}))
        with self.assertRaisesRegex(mps_index.mps.MpsError, "time budget"):
            mps_index.navigation(pinned, {"symbols": ["FlowA"]}, deadline=0)

    def test_legacy_dictionary_queries_find_names_paths_and_respect_repository_scope(self) -> None:
        snapshot_indexes(self.settings)
        generation = current_generation_ref(self.settings)
        pinned = replace(self.settings, atlas_generation=generation, atlas_generation_mode="pinned")
        for values in ({"symbols": [{"name": "FlowA", "repos": ["service"]}]},
                       {"paths": [{"query": "a.mps", "repos": ["service"]}]}):
            with self.subTest(values=values):
                request = parse_context_request(json.dumps({"CONTEXT_REQUEST": {"version": 2, "objective": "Inspect exact FlowA", **values}}))
                self.assertTrue(mps_index.may_match(generation, request))
                navigation, evidence = mps_index.navigation(pinned, request)
                self.assertEqual(3, len(navigation["steps"]))
                self.assertTrue(evidence)
        self.assertEqual((None, []), mps_index.navigation(pinned, {"symbols": [{"name": "FlowA", "repos": ["other"]}]}))
        start_session(self.settings, "MPS-LEGACY", "Inspect FlowA")
        content, _, _ = create_context(self.settings, "MPS-LEGACY", json.dumps({"CONTEXT_REQUEST": {
            "version": 2, "objective": "Inspect exact FlowA", "symbols": [{"name": "FlowA", "repos": ["service"]}]}}))
        self.assertIn("MPS structural navigation", content)

    def test_objective_model_name_activates_navigation_without_explicit_anchors(self) -> None:
        snapshot_indexes(self.settings)
        generation = current_generation_ref(self.settings)
        ordinary = parse_context_request(self.request(objective="Inspect unchanged_code callers"))
        self.assertFalse(mps_index.may_match(generation, ordinary))
        start_session(self.settings, "MPS-OBJECTIVE", "Understand the collaborating model roots")
        content, _, _ = create_context(self.settings, "MPS-OBJECTIVE", self.request(
            objective="Trace FlowA downstream authoring dependencies"))
        self.assertIn("MPS structural navigation", content)
        trace = session_state(self.settings, "MPS-OBJECTIVE")["request_history"][-1]["retrieval"]["trace"]
        self.assertEqual(3, len(trace["mps_navigation"]["steps"]))
        self.assertIn('"node_id": "inside"', content)

    def test_ordinary_mixed_code_and_model_query_returns_both_in_one_context(self) -> None:
        (self.repository / "code.py").write_text("def unchanged_code():\n    return 1\n\ndef FlowA():\n    return 2\n")
        snapshot_indexes(self.settings)
        start_session(self.settings, "MPS-MIXED", "Trace code and model together")
        with mock.patch("brain.mps.parse_project", wraps=mps_index.mps.parse_project) as parsed:
            content, _, _ = create_context(self.settings, "MPS-MIXED", self.request(
                objective="Trace FlowA alongside unchanged_code", anchors=[{"kind": "symbol", "value": "unchanged_code"}]))
        self.assertEqual(1, parsed.call_count)
        self.assertIn("MPS structural navigation", content)
        self.assertIn("def unchanged_code()", content)
        state = session_state(self.settings, "MPS-MIXED")
        self.assertEqual({"a.mps", "b.mps", "c.mps", "code.py"}, {item["path"] for item in state["evidence_records"]})
        trace = state["request_history"][-1]["retrieval"]["trace"]
        self.assertFalse(trace.get("mps_only", False))
        self.assertEqual(3, len(trace["mps_navigation"]["steps"]))
        generation = current_generation_ref(self.settings)
        pinned = replace(self.settings, atlas_generation=generation, atlas_generation_mode="pinned")
        navigation, _ = mps_index.navigation(pinned, {"anchors": [
            {"kind": "symbol", "value": f"ordinary_code_{index}"} for index in range(20)], "searches": ["FlowA"]})
        self.assertEqual(3, len(navigation["steps"]))
        self.assertEqual("a", navigation["matched_nodes"][0]["node_id"])
        self.assertTrue(navigation["truncated"])
        start_session(self.settings, "MPS-MIXED-EXACT", "Read a model node and code with the same name")
        content, _, _ = create_context(self.settings, "MPS-MIXED-EXACT", self.request(
            objective="Inspect FlowA", anchors=[{"kind": "file_hint", "value": "service/a.mps#id:a"},
                                                {"kind": "symbol", "value": "FlowA"}]))
        self.assertIn("def FlowA()", content)
        self.assertIn("MPS structural navigation", content)
        trace = session_state(self.settings, "MPS-MIXED-EXACT")["request_history"][-1]["retrieval"]["trace"]
        self.assertFalse(trace.get("mps_only", False))

    def test_ordinary_business_query_matches_property_and_unrelated_code_skips_models(self) -> None:
        source = (self.repository / "a.mps").read_bytes().replace(b'name="name"', b'name="businessRule"').replace(b'FlowA', b'ApprovePayment')
        source = source.replace(b'<node concept="c" id="call" role="h">',
                                b'<node concept="c" id="call" role="h"><property role="p" value="PE"/>')
        (self.repository / "a.mps").write_bytes(source)
        snapshot_indexes(self.settings)
        generation = current_generation_ref(self.settings)
        pinned = replace(self.settings, atlas_generation=generation, atlas_generation_mode="pinned")
        for query in ("ApprovePayment", "approve payment", "approve-payment", "approve_payment"):
            with self.subTest(query=query):
                request = parse_context_request(self.request(objective=f'Inspect "{query}"'))
                self.assertTrue(mps_index.may_match(generation, request))
                navigation, evidence = mps_index.navigation(pinned, request)
                self.assertEqual("a", navigation["matched_nodes"][0]["node_id"])
                self.assertEqual(3, len(navigation["steps"]))
                self.assertTrue(evidence)
        start_session(self.settings, "SHORT-DOMAIN", "Inspect a short domain identifier")
        content, _, _ = create_context(self.settings, "SHORT-DOMAIN", self.request(objective="Inspect PE"))
        self.assertIn("MPS structural navigation", content)
        trace = session_state(self.settings, "SHORT-DOMAIN")["request_history"][-1]["retrieval"]["trace"]
        self.assertEqual(["call"], [node["node_id"] for node in trace["mps_navigation"]["matched_nodes"]])
        start_session(self.settings, "CODE-ONLY", "Inspect unrelated ordinary code")
        with mock.patch("brain.mps.parse_project", side_effect=AssertionError("unrelated code must skip model parsing")):
            content, _, _ = create_context(self.settings, "CODE-ONLY", self.request(
                objective="Inspect unchanged_code callers", anchors=[{"kind": "symbol", "value": "unchanged_code"}]))
        self.assertIn("def unchanged_code()", content)
        self.assertNotIn("MPS structural navigation", content)
        # Explicit model/node follow-ups remain focused despite broader objectives.
        navigation, _ = mps_index.navigation(pinned, {"anchors": [{"kind": "file_hint", "value": "service/a.mps#id:call"}],
                                                   "searches": ["FlowB"]})
        self.assertEqual(["call"], [node["node_id"] for node in navigation["matched_nodes"]])

    def test_malformed_import_does_not_abort_component_and_invalid_discovery_is_rejected(self) -> None:
        source = (self.repository / "a.mps").read_bytes().replace(b'ref="f:b"', b'ref="f:b%"')
        (self.repository / "a.mps").write_bytes(source)
        snapshot_indexes(self.settings)
        generation = current_generation_ref(self.settings)
        payload = mps_index.load_component(self.settings, generation)
        self.assertIsNotNone(payload)
        self.assertEqual("ready", generation.component("mps_models")["status"])
        parsed = mps_index.mps.parse_project({"a.mps": source})
        self.assertEqual("unsupported", parsed["files"][0]["kind"])
        for key, value in (("discovery_terms", ["x" * 257]), ("discovery_complete", "yes"), ("limitations", "invalid")):
            with self.subTest(key=key):
                candidate = {**payload, key: value}
                component = {**generation.component("mps_models"), "content_hash": mps_index._hash(candidate),
                             "details": {**generation.component("mps_models")["details"], key: value}}
                self.assertFalse(mps_index.validate_payload(candidate, generation.snapshots, component))

    def test_exact_descriptor_and_unsupported_files_return_source_only_status(self) -> None:
        for path, source in {"language.mpl": '<language namespace="example"><models/></language>',
                             "broken.mps": '<model', "split.model": '<model content="header"/>'}.items():
            (self.repository / path).write_text(source)
        snapshot_indexes(self.settings)
        generation = current_generation_ref(self.settings)
        pinned = replace(self.settings, atlas_generation=generation, atlas_generation_mode="pinned")
        for path, kind in (("language.mpl", "descriptor"), ("broken.mps", "unsupported"), ("split.model", "unsupported")):
            for suffix in ("", "#lines:1-1", "#id:missing"):
                with self.subTest(path=path, suffix=suffix):
                    value = f"service/{path}{suffix}"
                    navigation, evidence = mps_index.navigation(pinned, {"anchors": [{"kind": "file_hint", "value": value}]})
                    self.assertEqual("source_only", navigation["route_status"])
                    self.assertEqual(kind, navigation["file_statuses"][0]["kind"])
                    self.assertEqual([], navigation["steps"])
                    self.assertTrue(any(item.path == path for item in evidence))
                    self.assertTrue(all(item.kind == "MPS source" for item in evidence))
                    self.assertEqual([value] if suffix.startswith("#id:") else [], navigation["unmatched_queries"])


if __name__ == "__main__":
    unittest.main()

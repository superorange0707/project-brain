from __future__ import annotations

import gc as garbage_collector
import hashlib
import json
import multiprocessing
import os
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import time
import unittest
import warnings
from dataclasses import replace
from pathlib import Path
from unittest import mock

from brain.atlas import _card, build_atlas
from brain.catalog import (
    canonical_atlas_identity,
    collect_generation_components,
    current_generation_ref,
    publish_generation,
    resolve_generation,
)
from brain.core import (
    BrainError, SearchHit, create_context, generate_map, load_settings, path_hits, read_source, search, session_state, snapshot_indexes,
    start_session,
)
from brain.experience import build_experience_index, similar_cases
from brain.graph import graph_symbol_hits
from brain.index import _connect as connect_search, _git_blob_contents, _initialize_connection, membership_snapshots, query_generation_indexes, query_generation_paths, query_index, query_paths, read_generation_files, write_state
from brain.investigation import _anchor
from brain.ops import _component_state, gc, semantic_status
from brain.platforms import native_command
from brain.relations import _relationship_payload_hash, generate_relationship_map, related_relationships
from brain.semantic import build_semantic_index, search_semantic
from brain.sync import (
    _export_snapshot, _git_archive_to_path, _sealed_snapshot_is_intact,
    _snapshot_metadata, _snapshot_seal, _snapshot_seal_path,
)


def _catalog_upgrade_worker(config: str) -> str:
    from brain.catalog import connect
    from brain.core import load_settings

    connection = connect(load_settings(Path(config)))
    try:
        return str(connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'",
        ).fetchone()[0])
    finally:
        connection.close()


def _search_upgrade_worker(config: str) -> int:
    from brain.core import load_settings
    from brain.index import query_index

    settings = load_settings(Path(config))
    return len(query_index(
        settings, settings.repo("service"), "LEGACY", max_results=10, snapshot_sha="sha-v1",
    ) or [])


class AtlasGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "service"
        self.repository.mkdir()
        self.config = self.root / "brain.toml"
        self.config.write_text(
            "[project]\nname='atlas-test'\n[graph]\nenabled=false\n"
            "[experience]\nenabled=false\n"
            "[[repositories]]\nname='service'\npath='service'\n",
            encoding="utf-8",
        )
        self.settings = load_settings(self.config)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def embed(values: list[str]) -> list[list[float]]:
        return [
            [float("G1_MARKER" in value), float("G2_MARKER" in value), 1.0]
            for value in values
        ]

    def publish(self, sha: str, path: str, marker: str) -> object:
        snapshot = self.settings.state_dir / "snapshots" / "service" / sha
        snapshot.mkdir(parents=True)
        (snapshot / path).write_text(f"def evidence():\n    return '{marker}'\n", encoding="utf-8")
        repo = self.settings.repo("service")
        repo.source_path = snapshot
        repo.source_sha = sha
        repo.source_ref = "refs/heads/main"

        state, _ = snapshot_indexes(self.settings, changed_only=True, publish=False)
        generate_relationship_map(self.settings)
        relationship_path = self.settings.state_dir / "relationships.json"
        relationships = json.loads(relationship_path.read_text(encoding="utf-8"))
        relationships["relationships"] = [{
            "source": "service",
            "target": "service",
            "kind": "TEST_EDGE",
            "key": marker,
            "source_evidence": f"service:{path}:1",
            "target_evidence": f"service:{path}:1",
            "confidence": "high",
        }]
        relationships["payload_hash"] = _relationship_payload_hash(relationships)
        relationship_path.write_text(json.dumps(relationships), encoding="utf-8")

        experience = build_experience_index(self.settings, changed_only=False)
        experience["cases"] = [{
            "ticket": marker,
            "latest_date": "2026-01-01",
            "repos": ["service"],
            "paths": [f"service:{path}"],
            "test_paths": [],
            "config_paths": [],
            "subjects": [marker],
            "ticket_excerpt": "",
            "knowledge_excerpt": "",
            "terms": ["generationone" if marker == "G1_MARKER" else "generationtwo"],
            "commits": [],
        }]
        (self.settings.state_dir / "ticket-history.json").write_text(json.dumps(experience), encoding="utf-8")
        build_semantic_index(self.settings, embed=self.embed, pack_id="atlas-test-pack")

        components = collect_generation_components(self.settings, state)
        manifest = publish_generation(self.settings, state, components=components)
        for value in state.values():
            if isinstance(value, dict):
                value["generation"] = manifest["generation"]
        write_state(self.settings, state)
        generation = resolve_generation(self.settings, generation=int(manifest["generation"]))
        self.assertIsNotNone(generation)
        return generation

    def test_canonical_identity_is_stable_and_invalidates_on_serving_change(self) -> None:
        manifest = {
            "created_at": "first",
            "snapshots": [{"repo": "b", "sha": "2"}, {"repo": "a", "sha": "1"}],
            "components": {"lexical": {"status": "ready", "content_hash": "sha256:x", "artifact_ref": "/private/one"}},
        }
        reordered = {
            "components": {"lexical": {"artifact_ref": "/private/two", "content_hash": "sha256:x", "status": "ready"}},
            "snapshots": [{"sha": "1", "repo": "a"}, {"sha": "2", "repo": "b"}],
            "created_at": "second",
        }
        self.assertEqual(canonical_atlas_identity(manifest), canonical_atlas_identity(reordered))
        changed = json.loads(json.dumps(reordered))
        changed["components"]["lexical"]["content_hash"] = "sha256:y"
        self.assertNotEqual(canonical_atlas_identity(manifest), canonical_atlas_identity(changed))

        first_graph = json.loads(json.dumps(manifest))
        second_graph = json.loads(json.dumps(manifest))
        first_graph["components"]["structural"] = {
            "status": "ready", "content_hash": "sha256:graph", "project": "project-brain-root-one-api",
        }
        second_graph["components"]["structural"] = {
            "status": "ready", "content_hash": "sha256:graph", "project": "project-brain-root-two-api",
        }
        self.assertEqual(canonical_atlas_identity(first_graph), canonical_atlas_identity(second_graph))

        first_change = json.loads(json.dumps(manifest))
        second_change = json.loads(json.dumps(manifest))
        first_change["components"]["change_intelligence"] = {
            "status": "ready", "content_hash": "sha256:changes",
            "details": {"build": {"operations": 9, "output_bytes": 1024}},
        }
        second_change["components"]["change_intelligence"] = {
            "status": "ready", "content_hash": "sha256:changes",
            "details": {"build": {"operations": 1, "output_bytes": 0}},
        }
        self.assertEqual(canonical_atlas_identity(first_change), canonical_atlas_identity(second_change))

    def test_full_noop_refresh_reuses_the_current_generation_identity(self) -> None:
        from brain.ops import refresh_brain

        (self.repository / "service.py").write_text("VALUE = 'STABLE'\n", encoding="utf-8")
        subprocess.run([native_command("git"), "init", "-q", "-b", "main"], cwd=self.repository, check=True)
        subprocess.run([native_command("git"), "config", "user.name", "Atlas test"], cwd=self.repository, check=True)
        subprocess.run([native_command("git"), "config", "user.email", "atlas@example.invalid"], cwd=self.repository, check=True)
        subprocess.run([native_command("git"), "add", "service.py"], cwd=self.repository, check=True)
        subprocess.run([native_command("git"), "commit", "-qm", "stable source"], cwd=self.repository, check=True)
        settings = load_settings(self.config)
        refresh_brain(settings, fetch=False, discover=False)
        first = current_generation_ref(settings)
        self.assertIsNotNone(first)
        refresh_brain(settings, fetch=False, discover=False)
        second = current_generation_ref(settings)
        self.assertIsNotNone(second)
        self.assertEqual(first.identity, second.identity)
        self.assertEqual(first.generation, second.generation)

    def test_current_projection_failure_cannot_commit_a_missing_generation(self) -> None:
        generation_one = self.publish("sha-projection-g1", "g1.py", "G1_MARKER")
        with mock.patch(
            "brain.catalog._write_current_projection",
            side_effect=OSError("injected current projection failure"),
        ), self.assertRaisesRegex(OSError, "projection failure"):
            self.publish("sha-projection-g2", "g2.py", "G2_MARKER")
        current = current_generation_ref(self.settings)
        self.assertIsNotNone(current)
        self.assertEqual(generation_one.generation, current.generation)
        self.assertTrue(
            (self.settings.state_dir / "generations" / f"generation-{generation_one.generation:06d}" / "manifest.json").is_file(),
        )
        self.assertFalse((self.settings.state_dir / "generations" / "generation-000002").exists())

    def test_publish_allocates_above_crash_orphan_generation_directory(self) -> None:
        first = self.publish("sha-orphan-g1", "orphan-g1.py", "G1_MARKER")
        self.assertEqual(1, first.generation)
        root = self.settings.state_dir / "generations"
        orphan = root / "generation-000002"
        orphan.mkdir()
        marker = orphan / "crash-orphan.txt"
        marker.write_text("must not be overwritten", encoding="utf-8")

        recovered = self.publish("sha-orphan-g2", "orphan-g2.py", "G2_MARKER")
        self.assertEqual(3, recovered.generation)
        self.assertEqual("must not be overwritten", marker.read_text(encoding="utf-8"))
        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            self.assertEqual(
                [(1,), (3,)],
                connection.execute("SELECT generation FROM index_generations ORDER BY generation").fetchall(),
            )
        finally:
            connection.close()

    def test_generation_manifest_projection_cannot_override_catalog_authority(self) -> None:
        generation = self.publish("sha-manifest-authority", "authority.py", "G1_MARKER")
        manifest_path = (
            self.settings.state_dir / "generations"
            / f"generation-{generation.generation:06d}" / "manifest.json"
        )
        manifest_path.write_text(json.dumps({
            "generation": 999,
            "identity": "sha256:" + "f" * 64,
            "source_signature": "sha256:" + "e" * 64,
            "snapshots": [{"repo": "service", "sha": "poisoned"}],
            "components": {"lexical": {"status": "unavailable"}},
        }), encoding="utf-8")

        resolved = current_generation_ref(self.settings)
        self.assertIsNotNone(resolved)
        self.assertEqual(generation.generation, resolved.generation)
        self.assertEqual(generation.identity, resolved.identity)
        self.assertEqual({"service": "sha-manifest-authority"}, resolved.snapshots)
        self.assertEqual("ready", resolved.component("lexical")["status"])
        self.assertEqual(generation.generation, resolved.manifest["generation"])

        outside = self.root / "outside-manifest.json"
        outside.write_text('{"generation": 1000}\n', encoding="utf-8")
        manifest_path.unlink()
        try:
            manifest_path.symlink_to(outside)
        except OSError as error:
            self.skipTest(f"file symlinks unavailable: {error}")
        resolved = current_generation_ref(self.settings)
        self.assertIsNotNone(resolved)
        self.assertEqual(generation.generation, resolved.generation)
        self.assertEqual('{"generation": 1000}\n', outside.read_text(encoding="utf-8"))

    def test_old_ticket_remains_on_g1_across_every_pinned_component(self) -> None:
        generation_one = self.publish("sha-g1", "old.py", "G1_MARKER")
        start_session(self.settings, "TICKET-A", "Keep G1 pinned.")
        generation_two = self.publish("sha-g2", "new.py", "G2_MARKER")
        start_session(self.settings, "TICKET-B", "Use G2.")
        self.publish("sha-g3", "third.py", "G3_MARKER")
        generation_four = self.publish("sha-g4", "fourth.py", "G4_MARKER")

        request_g1 = """CONTEXT_REQUEST:
  version: 1
  objective: Locate G1_MARKER.
  searches:
    - query: G1_MARKER
      repos: [service]
  paths: []
  symbols: []
  files: []
  history: []
"""
        old_context, _, _ = create_context(self.settings, "TICKET-A", request_g1)
        new_context, _, _ = create_context(self.settings, "TICKET-B", request_g1.replace("G1_MARKER", "G2_MARKER"))
        self.assertIn("old.py", old_context)
        self.assertNotIn("new.py", old_context)
        self.assertIn(f"Generation: `{generation_one.generation}`", old_context)
        self.assertIn(generation_one.identity, old_context)
        self.assertIn("new.py", new_context)
        self.assertIn(f"Generation: `{generation_two.generation}`", new_context)

        pinned_settings = replace(
            self.settings,
            atlas_generation=generation_one,
            atlas_generation_mode="pinned",
        )
        semantic = search_semantic(
            pinned_settings,
            "G2_MARKER",
            embed=self.embed,
            generation=generation_one,
        )
        self.assertEqual({"old.py"}, {str(item["path"]) for item in semantic})
        old_relationships = related_relationships(
            pinned_settings,
            ["G2_MARKER"],
            set(),
            generation=generation_one,
        )
        self.assertEqual([], old_relationships)
        self.assertEqual([], similar_cases(pinned_settings, "generationtwo", generation=generation_one))
        self.assertEqual(
            "G2_MARKER",
            similar_cases(self.settings, "generationtwo", generation=generation_two)[0]["ticket"],
        )
        self.assertEqual("degraded", generation_one.component("structural")["status"])
        with mock.patch("brain.graph._invoke") as invoke:
            self.assertEqual([], graph_symbol_hits(pinned_settings, "evidence", ["service"]))
        invoke.assert_not_called()

        self.assertEqual(
            {("service", "sha-g1"), ("service", "sha-g2"), ("service", "sha-g3"), ("service", "sha-g4")},
            membership_snapshots(self.settings),
        )
        report = gc(self.settings, dry_run=True, keep_recent=1)
        self.assertNotIn(
            str(self.settings.state_dir / "generations" / f"generation-{generation_one.generation:06d}"),
            [item["path"] for item in report["remove"]],
        )
        self.assertEqual(generation_one.generation, session_state(self.settings, "TICKET-A")["generation"])
        self.assertEqual(generation_four.generation, current_generation_ref(self.settings).generation)

        (self.settings.runs_dir / "TICKET-A" / "session.json").unlink()
        unpinned = gc(self.settings, dry_run=True, keep_recent=1)
        self.assertIn(
            str(self.settings.state_dir / "generations" / f"generation-{generation_one.generation:06d}"),
            [item["path"] for item in unpinned["remove"]],
        )

    def test_pinned_commit_without_exported_snapshot_hydrates_from_immutable_lexical_membership(self) -> None:
        source = self.repository / "service.py"
        source.write_text("VALUE = 'G1_ONLY'\n", encoding="utf-8")
        repo = self.settings.repo("service")
        repo.source_sha = "sha-g1"
        repo.source_ref = "refs/heads/main"
        snapshot_indexes(self.settings, changed_only=False)
        start_session(self.settings, "PIN-INDEXED", "Keep indexed G1 content.")

        source.write_text("VALUE = 'G2_ONLY'\n", encoding="utf-8")
        repo.source_sha = "sha-g2"
        snapshot_indexes(self.settings, changed_only=True)
        request = """CONTEXT_REQUEST:
  version: 1
  objective: Locate G1_ONLY.
  searches:
    - query: G1_ONLY
      repos: [service]
  paths: []
  symbols: []
  files: []
  history: []
"""
        context, _, _ = create_context(self.settings, "PIN-INDEXED", request)
        self.assertIn("G1_ONLY", context)
        self.assertNotIn("G2_ONLY", context)

    def test_pinned_hydration_ignores_mutated_export_and_fails_closed_on_blob_corruption(self) -> None:
        original = "VALUE = 'IMMUTABLE_G1'\n"
        self.repository.joinpath("service.py").write_text(original, encoding="utf-8")
        repo = self.settings.repo("service")
        repo.source_sha = "sha-g1"
        snapshot_indexes(self.settings, changed_only=False)
        exported = self.settings.state_dir / "snapshots" / "service" / "sha-g1"
        exported.mkdir(parents=True, exist_ok=True)
        exported.joinpath("service.py").write_text("VALUE = 'MUTATED_EXPORT'\n", encoding="utf-8")
        pinned_repo = replace(repo, source_path=exported, source_sha="sha-g1")
        pinned = replace(
            self.settings, repositories=[pinned_repo], atlas_generation_mode="pinned",
        )
        hit = SearchHit("service", "service.py", 1, "", "test", 100, ["test"])
        self.assertEqual("VALUE = 'IMMUTABLE_G1'", read_source(pinned, hit, full=True).content)

        connection = sqlite3.connect(self.settings.state_dir / "search.sqlite3")
        try:
            blob = connection.execute(
                "SELECT blob FROM file_membership WHERE repo=? AND snapshot_sha=? AND path=?",
                ("service", "sha-g1", "service.py"),
            ).fetchone()[0]
            connection.execute(
                "UPDATE blobs SET content=? WHERE blob=?", ("X" * len(original.encode("utf-8")), blob),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(BrainError, "Pinned indexed source is unavailable"):
            read_source(pinned, hit, full=True)

    def test_mandatory_component_failure_does_not_move_current(self) -> None:
        generation = self.publish("sha-g1", "old.py", "G1_MARKER")
        state = json.loads((self.settings.state_dir / "indexes.json").read_text(encoding="utf-8"))
        with self.assertRaises(sqlite3.IntegrityError):
            publish_generation(
                self.settings,
                state,
                components={"lexical": {"schema_version": "2", "status": "unavailable"}},
            )
        components = collect_generation_components(self.settings, state)
        components["lexical"]["schema_version"] = "future-incompatible"
        with self.assertRaisesRegex(sqlite3.IntegrityError, "aligned lexical component"):
            publish_generation(self.settings, state, components=components)
        self.assertEqual(generation.generation, current_generation_ref(self.settings).generation)

    def test_lexical_serving_rejects_incompatible_component_schema(self) -> None:
        generation = self.publish("sha-g1", "old.py", "G1_MARKER")
        components = json.loads(json.dumps(generation.components))
        components["lexical"]["schema_version"] = "future-incompatible"
        poisoned = replace(generation, components=components)
        pinned = replace(
            self.settings, atlas_generation=poisoned, atlas_generation_mode="pinned",
        )
        from brain.core import _lexical_generation_ready

        self.assertFalse(_lexical_generation_ready(pinned, pinned.repo("service")))

    def test_lexical_serving_uses_the_refresh_sealed_o1_membership_proof(self) -> None:
        generation = self.publish("sha-g1", "old.py", "G1_MARKER")
        pinned = replace(
            self.settings, atlas_generation=generation, atlas_generation_mode="pinned",
        )
        from brain import index as search_index
        from brain.core import _lexical_generation_ready

        connection = search_index._connect(self.settings)

        class GuardedConnection:
            def execute(self, statement: str, parameters: object = ()) -> object:
                self_test.assertNotIn("FROM file_membership", statement)
                return connection.execute(statement, parameters)

            def close(self) -> None:
                connection.close()

        self_test = self
        with mock.patch("brain.index._connect", return_value=GuardedConnection()):
            self.assertTrue(_lexical_generation_ready(pinned, pinned.repo("service")))

    def test_snapshot_index_does_not_report_success_when_atlas_publication_fails(self) -> None:
        generation = self.publish("sha-g1", "old.py", "G1_MARKER")
        with mock.patch("brain.catalog.publish_generation", side_effect=sqlite3.OperationalError("publish failed")):
            with self.assertRaises(sqlite3.OperationalError):
                snapshot_indexes(self.settings, changed_only=False)
        self.assertEqual(generation.generation, current_generation_ref(self.settings).generation)

    def test_refresh_retry_after_lexical_commit_rebuilds_catalog_before_atlas_publish(self) -> None:
        source = self.repository / "service.py"
        source.write_text("def first_evidence():\n    return 'G1'\n", encoding="utf-8")
        snapshot_indexes(self.settings, changed_only=False)
        first = current_generation_ref(self.settings)
        self.assertIsNotNone(first)

        source.write_text("def second_evidence():\n    return 'G2'\n", encoding="utf-8")
        with mock.patch("brain.catalog.record_index_catalog", side_effect=OSError("simulated crash window")):
            with self.assertRaisesRegex(OSError, "simulated crash window"):
                snapshot_indexes(self.settings, changed_only=True)
        self.assertEqual(first.generation, current_generation_ref(self.settings).generation)

        state, updated = snapshot_indexes(self.settings, changed_only=True)
        self.assertEqual([], updated)
        current = current_generation_ref(self.settings)
        self.assertIsNotNone(current)
        self.assertGreater(current.generation, first.generation)
        current_sha = state["service"]["sha"]
        self.assertEqual(current_sha, current.snapshots["service"])
        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM snapshot_files WHERE repo='service' AND sha=?",
                (current_sha,),
            ).fetchone()[0])
            self.assertGreater(connection.execute(
                "SELECT COUNT(*) FROM generation_entities WHERE generation=?",
                (current.generation,),
            ).fetchone()[0], 0)
        finally:
            connection.close()

    def test_future_lexical_schema_never_mutates_catalog_during_refresh_recovery(self) -> None:
        source = self.repository / "service.py"
        source.write_text("VALUE = 'CURRENT_SCHEMA'\n", encoding="utf-8")
        snapshot_indexes(self.settings, changed_only=False)
        current = current_generation_ref(self.settings)
        self.assertIsNotNone(current)
        catalog_path = self.settings.state_dir / "catalog.sqlite3"
        before_bytes = catalog_path.read_bytes()
        connection = sqlite3.connect(catalog_path)
        try:
            before_rows = connection.execute(
                "SELECT repo,sha,path,blob_sha FROM snapshot_files ORDER BY repo,sha,path"
            ).fetchall()
        finally:
            connection.close()

        search = sqlite3.connect(self.settings.state_dir / "search.sqlite3")
        try:
            search.execute("UPDATE metadata SET value='999' WHERE key='schema_version'")
            search.commit()
        finally:
            search.close()
        source.write_text("VALUE = 'FUTURE_SCHEMA_MUST_NOT_PUBLISH'\n", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "incompatible with Atlas publication"):
            snapshot_indexes(self.settings, changed_only=True)
        self.assertEqual(before_bytes, catalog_path.read_bytes())
        connection = sqlite3.connect(catalog_path)
        try:
            after_rows = connection.execute(
                "SELECT repo,sha,path,blob_sha FROM snapshot_files ORDER BY repo,sha,path"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(before_rows, after_rows)
        self.assertEqual(current.generation, current_generation_ref(self.settings).generation)

    def test_publish_rederives_new_atlas_row_identity_before_registration(self) -> None:
        source = self.repository / "Existing.java"
        source.write_text("class Existing {}\n", encoding="utf-8")
        repo = self.settings.repo("service")
        repo.source_sha = "sha-g1"
        snapshot_indexes(self.settings, changed_only=False)
        previous = current_generation_ref(self.settings)
        self.assertIsNotNone(previous)

        (self.repository / "NewOnly.java").write_text("class NewOnly {}\n", encoding="utf-8")
        repo.source_sha = "sha-g2"
        state, _ = snapshot_indexes(self.settings, changed_only=True, publish=False)
        payload = build_atlas(self.settings, state)
        poisoned = next(item for item in payload["entities"] if item["simple_name"] == "NewOnly")
        poisoned["simple_name"] = "POISONED"
        components = collect_generation_components(self.settings, state, atlas_payload=payload)

        with self.assertRaisesRegex(sqlite3.IntegrityError, "independent publication validation"):
            publish_generation(
                self.settings, state, components=components, atlas_payload=payload,
            )
        self.assertEqual(previous.generation, current_generation_ref(self.settings).generation)

    def test_publish_rejects_rehashed_card_and_anchor_with_false_membership(self) -> None:
        source = self.repository / "Authority.java"
        source.write_text("class Authority { void run() {} }\n", encoding="utf-8")
        repo = self.settings.repo("service")
        repo.source_sha = "sha-authority"
        snapshot_indexes(self.settings, changed_only=False)
        previous = current_generation_ref(self.settings)
        state, _ = snapshot_indexes(self.settings, changed_only=True, publish=False)
        clean = build_atlas(self.settings, state)

        poisoned_card_payload = json.loads(json.dumps(clean))
        original_card = next(item for item in poisoned_card_payload["cards"] if item["level"] == "entity")
        poisoned_card_payload["cards"].remove(original_card)
        poisoned_card_payload["cards"].append(_card(
            "entity", original_card["target_id"], original_card["repo"], original_card["content"],
            module_id=original_card["module_id"], entity_id=original_card["entity_id"],
            path="src/Fabricated.java", metadata=original_card["metadata"],
        ))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "independent publication validation"):
            publish_generation(
                self.settings, state,
                components=collect_generation_components(self.settings, state, atlas_payload=poisoned_card_payload),
                atlas_payload=poisoned_card_payload,
            )

        poisoned_anchor_payload = json.loads(json.dumps(clean))
        fabricated = _anchor(
            kind="symbol", value="Fabricated", repo="service", path="src/Fabricated.java", line=1,
            blob_sha="sha256:fabricated", module_id=clean["modules"][0]["module_id"],
        )
        self.assertIsNotNone(fabricated)
        poisoned_anchor_payload["runtime_anchors"].append(fabricated)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "independent publication validation"):
            publish_generation(
                self.settings, state,
                components=collect_generation_components(self.settings, state, atlas_payload=poisoned_anchor_payload),
                atlas_payload=poisoned_anchor_payload,
            )
        self.assertEqual(previous.generation, current_generation_ref(self.settings).generation)

    def test_component_artifact_mutation_between_collect_and_copy_rolls_back(self) -> None:
        generation = self.publish("sha-g1", "old.py", "G1_MARKER")
        state = json.loads((self.settings.state_dir / "indexes.json").read_text(encoding="utf-8"))
        payload = build_atlas(self.settings, state)

        for component_name in ("relationships", "experience", "semantic"):
            with self.subTest(component=component_name):
                components = collect_generation_components(self.settings, state, atlas_payload=payload)
                self.assertEqual("ready", components[component_name]["status"])
                components["lexical"]["details"]["toctou_nonce"] = component_name
                path = Path(str(components[component_name]["_artifact_source"]))
                original = path.read_bytes()
                mutated = json.loads(original)
                mutated["publication_race_poison"] = component_name
                path.write_text(json.dumps(mutated), encoding="utf-8")
                try:
                    with self.assertRaisesRegex(sqlite3.IntegrityError, "content identity changed"):
                        publish_generation(
                            self.settings, state, components=components, atlas_payload=payload,
                        )
                finally:
                    path.write_bytes(original)
                self.assertEqual(generation.generation, current_generation_ref(self.settings).generation)

    def test_serving_rejects_tampered_relationship_and_experience_artifacts(self) -> None:
        generation = self.publish("sha-g1", "old.py", "G1_MARKER")
        self.assertTrue(related_relationships(
            self.settings, ["G1_MARKER"], set(), generation=generation,
        ))
        self.assertTrue(similar_cases(
            self.settings, "generationone", generation=generation,
        ))

        relationship_path = self.settings.state_dir / str(generation.component("relationships")["artifact_ref"])
        relationships = json.loads(relationship_path.read_text(encoding="utf-8"))
        relationships["relationships"][0]["key"] = "POISONED_RELATIONSHIP"
        relationship_path.write_text(json.dumps(relationships), encoding="utf-8")
        self.assertEqual([], related_relationships(
            self.settings, ["POISONED_RELATIONSHIP"], set(), generation=generation,
        ))

        experience_path = self.settings.state_dir / str(generation.component("experience")["artifact_ref"])
        experience = json.loads(experience_path.read_text(encoding="utf-8"))
        experience["cases"][0]["ticket"] = "POISONED_HISTORY"
        experience_path.write_text(json.dumps(experience), encoding="utf-8")
        self.assertEqual([], similar_cases(
            self.settings, "generationone", generation=generation,
        ))

    def test_semantic_serving_and_gc_never_read_symlinked_or_oversized_component_state(self) -> None:
        generation = self.publish("sha-semantic-managed", "semantic.py", "G1_MARKER")
        artifact = self.settings.state_dir / str(generation.component("semantic")["artifact_ref"])
        outside = self.root / "outside-semantic.json"
        outside.write_bytes(artifact.read_bytes())
        artifact.unlink()
        try:
            artifact.symlink_to(outside)
        except OSError as error:
            self.skipTest(f"file symlinks unavailable: {error}")

        self.assertEqual([], search_semantic(
            self.settings, "G1_MARKER", generation=generation, embed=self.embed,
        ))
        self.assertFalse(semantic_status(self.settings)["aligned"])
        self.assertEqual({}, _component_state(self.settings, generation, "semantic"))
        preserved = outside.read_bytes()
        self.assertTrue(preserved)

        artifact.unlink()
        artifact.write_text('{"oversized":"' + ("x" * 128) + '"}', encoding="utf-8")
        with mock.patch("brain.semantic.MAX_SEMANTIC_STATE_BYTES", 32):
            self.assertEqual([], search_semantic(
                self.settings, "G1_MARKER", generation=generation, embed=self.embed,
            ))
        self.assertEqual(preserved, outside.read_bytes())

    def test_stale_relationship_projection_is_not_imported_into_new_atlas(self) -> None:
        self.publish("sha-g1", "old.py", "STALE_RELATIONSHIP_KEY")
        repo = self.settings.repo("service")
        next_snapshot = self.settings.state_dir / "snapshots" / "service" / "sha-g2"
        next_snapshot.mkdir(parents=True)
        (next_snapshot / "new.py").write_text("def current():\n    return 'G2'\n", encoding="utf-8")
        repo.source_path = next_snapshot
        repo.source_sha = "sha-g2"
        state, _ = snapshot_indexes(self.settings, changed_only=True, publish=False)

        payload = build_atlas(self.settings, state)
        self.assertFalse(any(
            (item.get("metadata") or {}).get("key") == "STALE_RELATIONSHIP_KEY"
            for item in payload["edges"]
        ))

    def test_atlas_derives_only_from_authoritative_lexical_blob_not_mutated_export(self) -> None:
        source = self.repository / "A.java"
        source.write_text("class Good {}\n", encoding="utf-8")
        git = native_command("git")
        for command in (
            [git, "init"], [git, "config", "user.email", "brain@example.invalid"],
            [git, "config", "user.name", "Project Brain Test"], [git, "add", "A.java"],
            [git, "commit", "-m", "authoritative source"],
        ):
            subprocess.run(command, cwd=self.repository, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        commit = subprocess.run(
            [git, "rev-parse", "HEAD"], cwd=self.repository, check=True,
            text=True, stdout=subprocess.PIPE,
        ).stdout.strip()
        exported = self.settings.state_dir / "snapshots" / "service" / commit
        exported.mkdir(parents=True)
        (exported / "A.java").write_text("class Poisoned {}\n", encoding="utf-8")
        repo = self.settings.repo("service")
        repo.source_path = exported
        repo.source_sha = commit
        repo.source_ref = "refs/heads/main"

        state, _ = snapshot_indexes(self.settings, changed_only=False, publish=False)
        payload = build_atlas(self.settings, state)
        names = {str(item["simple_name"]) for item in payload["entities"]}
        self.assertIn("Good", names)
        self.assertNotIn("Poisoned", names)
        self.assertNotIn("Poisoned", "\n".join(str(item["content"]) for item in payload["cards"]))

        repaired = _export_snapshot(repo, "HEAD", commit, self.settings.state_dir)
        self.assertEqual(exported, repaired)
        self.assertIn("class Good", (exported / "A.java").read_text(encoding="utf-8"))
        self.assertTrue((exported.parent / f".{commit}.brain-snapshot.json").is_file())
        sealed_source = exported / "A.java"
        sealed_metadata = sealed_source.stat()
        sealed_source.chmod(stat.S_IMODE(sealed_metadata.st_mode) | stat.S_IWUSR)
        sealed_source.write_text("class Evil {}\n", encoding="utf-8")
        os.utime(sealed_source, ns=(sealed_metadata.st_atime_ns, sealed_metadata.st_mtime_ns))
        sealed_source.chmod(stat.S_IMODE(sealed_metadata.st_mode))
        self.assertEqual(exported, _export_snapshot(repo, "HEAD", commit, self.settings.state_dir))
        self.assertEqual("class Good {}\n", sealed_source.read_text(encoding="utf-8"))
        captured: list[str] = []

        def embed(cards: list[str]) -> list[list[float]]:
            captured.extend(cards)
            return [[1.0, 0.0] for _ in cards]

        build_semantic_index(self.settings, embed=embed, pack_id="sealed-export-pack")
        generate_relationship_map(self.settings)
        self.assertIn("Good", "\n".join(captured))
        self.assertNotIn("Poisoned", "\n".join(captured))
        self.assertNotIn(
            "Poisoned",
            (self.settings.state_dir / "relationships.json").read_text(encoding="utf-8"),
        )

    def test_snapshot_export_uses_resolved_commit_when_selected_ref_moves(self) -> None:
        source = self.repository / "value.txt"
        git = native_command("git")
        for command in (
            [git, "init"], [git, "config", "user.email", "brain@example.invalid"],
            [git, "config", "user.name", "Project Brain Test"],
        ):
            subprocess.run(
                command, cwd=self.repository, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        source.write_text("G1\n", encoding="utf-8")
        subprocess.run([git, "add", "value.txt"], cwd=self.repository, check=True)
        subprocess.run([git, "commit", "-m", "G1"], cwd=self.repository, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        generation_one = subprocess.run(
            [git, "rev-parse", "HEAD"], cwd=self.repository, check=True,
            text=True, stdout=subprocess.PIPE,
        ).stdout.strip()
        source.write_text("G2\n", encoding="utf-8")
        subprocess.run([git, "add", "value.txt"], cwd=self.repository, check=True)
        subprocess.run([git, "commit", "-m", "G2"], cwd=self.repository, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        generation_two = subprocess.run(
            [git, "rev-parse", "HEAD"], cwd=self.repository, check=True,
            text=True, stdout=subprocess.PIPE,
        ).stdout.strip()
        subprocess.run(
            [git, "update-ref", "refs/heads/moving", generation_one],
            cwd=self.repository, check=True,
        )
        repo = self.settings.repo("service")
        original_archive = _git_archive_to_path

        def move_ref_then_archive(repo_value, selected, archive_path, *, max_bytes=None):
            subprocess.run(
                [git, "update-ref", "refs/heads/moving", generation_two],
                cwd=self.repository, check=True,
            )
            return original_archive(repo_value, selected, archive_path, max_bytes=max_bytes)

        with mock.patch("brain.sync._git_archive_to_path", side_effect=move_ref_then_archive):
            exported = _export_snapshot(
                repo, "refs/heads/moving", generation_one, self.settings.state_dir,
            )
        self.assertIsNotNone(exported)
        self.assertEqual("G1\n", (exported / "value.txt").read_text(encoding="utf-8"))
        self.assertEqual(generation_one, exported.name)

    def test_windows_snapshot_validation_rechecks_content_not_creation_metadata(self) -> None:
        parent = self.settings.state_dir / "snapshots" / "service"
        snapshot = "a" * 40
        target = parent / snapshot
        target.mkdir(parents=True)
        source = target / "A.java"
        source.write_text("class Good {}\n", encoding="utf-8")
        seal_path = _snapshot_seal_path(parent, snapshot)
        seal_path.write_text(json.dumps(_snapshot_seal(target, snapshot)), encoding="utf-8")
        with mock.patch("brain.sync.os.name", "nt"):
            self.assertTrue(_sealed_snapshot_is_intact(target, seal_path, snapshot))
            metadata = source.stat()
            source.write_text("class Evil {}\n", encoding="utf-8")
            os.utime(source, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
            self.assertFalse(_sealed_snapshot_is_intact(target, seal_path, snapshot))

    def test_semantic_and_relationships_use_git_objects_when_snapshot_export_is_missing(self) -> None:
        producer = self.repository
        consumer = self.root / "consumer"
        consumer.mkdir()
        self.config.write_text(
            "[project]\nname='atlas-test'\n[graph]\nenabled=false\n[experience]\nenabled=false\n"
            "[[repositories]]\nname='service'\npath='service'\n"
            "[[repositories]]\nname='consumer'\npath='consumer'\n",
            encoding="utf-8",
        )
        git = native_command("git")

        def commit(repository: Path, name: str, content: str) -> str:
            (repository / "App.java").write_text(content, encoding="utf-8")
            for command in (
                [git, "init"], [git, "config", "user.email", "brain@example.invalid"],
                [git, "config", "user.name", "Project Brain Test"], [git, "add", "App.java"],
                [git, "commit", "-m", "authoritative source"],
            ):
                subprocess.run(
                    command, cwd=repository, check=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            return subprocess.run(
                [git, "rev-parse", "HEAD"], cwd=repository, check=True,
                text=True, stdout=subprocess.PIPE,
            ).stdout.strip()

        commits = {
            "service": commit(producer, "service", "class Producer { String COMMIT_SOURCE = \"safe\"; }\n"),
            "consumer": commit(consumer, "consumer", "class Consumer { String COMMIT_SOURCE = \"safe\"; }\n"),
        }
        (producer / "App.java").write_text(
            'class Producer { void send() { kafkaTemplate.send("live.poison"); } }\n',
            encoding="utf-8",
        )
        (consumer / "App.java").write_text(
            'class Consumer { @KafkaListener(topics="live.poison") void receive() {} }\n',
            encoding="utf-8",
        )
        settings = load_settings(self.config)
        for repo in settings.repositories:
            repo.source_sha = commits[repo.name]
            repo.source_ref = "refs/heads/main"
            repo.source_path = None

        state, _ = snapshot_indexes(settings, changed_only=False, publish=False)
        atlas = build_atlas(settings, state)
        captured: list[str] = []

        def embed(cards: list[str]) -> list[list[float]]:
            captured.extend(cards)
            return [[1.0, 0.0] for _ in cards]

        build_semantic_index(
            settings, embed=embed, pack_id="git-object-source-pack", atlas_cards=atlas["cards"],
        )
        facts = generate_map(settings)
        generate_relationship_map(settings)
        semantic_input = "\n".join(captured)
        relationship_input = (settings.state_dir / "relationships.json").read_text(encoding="utf-8")
        self.assertIn("COMMIT_SOURCE", semantic_input)
        self.assertNotIn("live.poison", semantic_input)
        self.assertNotIn("live.poison", facts)
        self.assertNotIn("live.poison", relationship_input)
        self.assertEqual(
            commits,
            {item[0]: item[2] for item in json.loads(relationship_input)["sources"]},
        )

    def test_normal_refresh_repairs_corrupt_same_snapshot_and_keeps_old_pin_usable(self) -> None:
        source = self.repository / "service.py"
        source.write_text("VALUE = 'REPAIR_ME'\n", encoding="utf-8")
        repo = self.settings.repo("service")
        repo.source_sha = "sha-repair"
        snapshot_indexes(self.settings, changed_only=False)
        old_generation = current_generation_ref(self.settings)
        self.assertIsNotNone(old_generation)
        start_session(self.settings, "REPAIR-PIN", "Keep the original generation usable.")

        database = self.settings.state_dir / "search.sqlite3"
        connection = sqlite3.connect(database)
        try:
            blob = connection.execute(
                "SELECT blob FROM file_membership WHERE repo='service' AND snapshot_sha='sha-repair' AND path='service.py'"
            ).fetchone()[0]
            connection.execute("UPDATE blobs SET content=? WHERE blob=?", ("X" * len(source.read_bytes()), blob))
            connection.commit()
        finally:
            connection.close()

        state, updated = snapshot_indexes(self.settings, changed_only=True)
        self.assertEqual(["service"], updated)
        self.assertTrue(state["service"]["repaired"])
        current = current_generation_ref(self.settings)
        self.assertIsNotNone(current)
        self.assertGreater(current.generation, old_generation.generation)
        self.assertGreater(int(current.component("lexical")["details"]["repair_epoch"]), 0)
        pinned = replace(self.settings, atlas_generation=old_generation, atlas_generation_mode="pinned")
        self.assertEqual(
            [("service.py", 1, "VALUE = 'REPAIR_ME'")],
            query_index(pinned, pinned.repo("service"), "REPAIR_ME", max_results=10, snapshot_sha="sha-repair"),
        )

    def test_noop_refresh_after_query_does_not_rehash_all_blob_content(self) -> None:
        source = self.repository / "service.py"
        source.write_text("VALUE = 'NOOP'\n", encoding="utf-8")
        repo = self.settings.repo("service")
        repo.source_sha = "sha-noop"
        snapshot_indexes(self.settings, changed_only=False, publish=False)
        self.assertTrue(query_index(self.settings, repo, "NOOP", max_results=5))

        from brain import index as search_index

        with mock.patch("brain.index._blob_identity_valid", wraps=search_index._blob_identity_valid) as validated:
            _, updated = snapshot_indexes(self.settings, changed_only=True, publish=False)
        self.assertEqual([], updated)
        self.assertEqual(1 if os.name == "nt" else 0, validated.call_count)

    def test_pinned_multi_repo_lexical_query_is_one_bounded_operation_and_fail_closed(self) -> None:
        scale_root = self.root / "scale"
        config_rows = [
            "[project]", "name='lexical-scale'", "[graph]", "enabled=false",
            "[experience]", "enabled=false",
        ]
        for index in range(100):
            name = f"repo-{index:03d}"
            repository = scale_root / name
            repository.mkdir(parents=True)
            (repository / "Marker.java").write_text(
                f"final class Marker{index:03d} {{ String value = \"NEEDLE_{index + 1:03d}\"; }}\n",
                encoding="utf-8",
            )
            if index == 0:
                for path_index in range(300):
                    target = repository / "a" / f"target-copy-{path_index:02d}.java"
                    target.parent.mkdir(exist_ok=True)
                    target.write_text("final class WeakTarget {}\n", encoding="utf-8")
                exact = repository / "z" / "target.java"
                exact.parent.mkdir(exist_ok=True)
                exact.write_text("final class ExactTarget {}\n", encoding="utf-8")
            config_rows.extend(["[[repositories]]", f"name='{name}'", f"path='scale/{name}'"])
        config = self.root / "lexical-scale.toml"
        config.write_text("\n".join(config_rows) + "\n", encoding="utf-8")
        settings = load_settings(config)
        for index, repo in enumerate(settings.repositories):
            repo.source_sha = f"sha-{index:03d}"
        snapshot_indexes(settings, changed_only=False)
        generation = current_generation_ref(settings)
        self.assertIsNotNone(generation)
        pinned = replace(settings, atlas_generation=generation, atlas_generation_mode="pinned")

        from brain import core as core_module
        from brain import index as search_index
        from brain.retrieval.models import RetrievalTrace

        self.assertIsNotNone(
            query_generation_indexes(
                pinned, generation, pinned.repositories[:10], "NEEDLE_010",
                max_results=10, max_candidate_files=100, max_hits=100,
                max_bytes=8 * 1024 * 1024, max_seconds=2.0,
            ),
            json.dumps(generation.component("lexical"), sort_keys=True),
        )

        with mock.patch("brain.core._zoekt_manifest_hash", return_value=None):
            for count in (10, 50, 100):
                trace = RetrievalTrace(max_physical_backend_operations=1)
                trace_token = core_module._ACTIVE_RETRIEVAL_TRACE.set(trace)
                cache_token = core_module._ACTIVE_RETRIEVAL_CACHE.set({})
                try:
                    with mock.patch("brain.index._connect", wraps=search_index._connect) as opened:
                        hits = search(
                            pinned, f"NEEDLE_{count:03d}",
                            [f"repo-{index:03d}" for index in range(count)], fixed=True,
                        )
                finally:
                    core_module._ACTIVE_RETRIEVAL_CACHE.reset(cache_token)
                    core_module._ACTIVE_RETRIEVAL_TRACE.reset(trace_token)
                self.assertEqual([f"repo-{count - 1:03d}"], [hit.repo for hit in hits])
                self.assertEqual(1, opened.call_count)
                self.assertEqual(1, trace.physical_backend_operations)

            for count in (10, 50, 100):
                trace = RetrievalTrace(max_physical_backend_operations=1)
                trace_token = core_module._ACTIVE_RETRIEVAL_TRACE.set(trace)
                cache_token = core_module._ACTIVE_RETRIEVAL_CACHE.set({})
                try:
                    with mock.patch("brain.index._connect", wraps=search_index._connect) as opened:
                        hits = path_hits(
                            pinned, "Marker.java",
                            [f"repo-{index:03d}" for index in range(count)],
                        )
                finally:
                    core_module._ACTIVE_RETRIEVAL_CACHE.reset(cache_token)
                    core_module._ACTIVE_RETRIEVAL_TRACE.reset(trace_token)
                self.assertEqual(count, len(hits))
                self.assertEqual(1, opened.call_count)
                self.assertEqual(1, trace.physical_backend_operations)

            # Fresh user-facing search/path settings capture the current
            # generation once instead of opening SQLite per repository.
            fresh_current = load_settings(config)
            trace = RetrievalTrace(max_physical_backend_operations=1)
            trace_token = core_module._ACTIVE_RETRIEVAL_TRACE.set(trace)
            cache_token = core_module._ACTIVE_RETRIEVAL_CACHE.set({})
            try:
                with mock.patch("brain.core._zoekt_manifest_hash", return_value=None), mock.patch(
                    "brain.index._connect", wraps=search_index._connect,
                ) as opened:
                    direct_hits = search(fresh_current, "NEEDLE_100", fixed=True)
            finally:
                core_module._ACTIVE_RETRIEVAL_CACHE.reset(cache_token)
                core_module._ACTIVE_RETRIEVAL_TRACE.reset(trace_token)
            self.assertEqual(["repo-099"], [hit.repo for hit in direct_hits])
            self.assertEqual(1, opened.call_count)
            self.assertEqual(1, trace.physical_backend_operations)
            trace = RetrievalTrace(max_physical_backend_operations=1)
            trace_token = core_module._ACTIVE_RETRIEVAL_TRACE.set(trace)
            cache_token = core_module._ACTIVE_RETRIEVAL_CACHE.set({})
            try:
                with mock.patch("brain.index._connect", wraps=search_index._connect) as opened:
                    direct_paths = path_hits(fresh_current, "Marker.java")
            finally:
                core_module._ACTIVE_RETRIEVAL_CACHE.reset(cache_token)
                core_module._ACTIVE_RETRIEVAL_TRACE.reset(trace_token)
            self.assertEqual(100, len(direct_paths))
            self.assertEqual(1, opened.call_count)
            self.assertEqual(1, trace.physical_backend_operations)

            # A direct current-mode request captures G1 once. Publishing G2
            # afterwards must not turn the captured request back into 100
            # per-repository queries or substitute G2 lexical evidence.
            newest = scale_root / "repo-099" / "Marker.java"
            newest.write_text("final class MarkerG2 { String value = \"G2_ONLY\"; }\n", encoding="utf-8")
            settings.repo("repo-099").source_sha = "sha-099-g2"
            snapshot_indexes(settings, changed_only=True)
            captured_current = replace(
                settings, atlas_generation=generation, atlas_generation_mode="current",
            )
            trace = RetrievalTrace(max_physical_backend_operations=1)
            trace_token = core_module._ACTIVE_RETRIEVAL_TRACE.set(trace)
            cache_token = core_module._ACTIVE_RETRIEVAL_CACHE.set({})
            try:
                with mock.patch("brain.core._zoekt_manifest_hash", return_value=None), mock.patch(
                    "brain.index._connect", wraps=search_index._connect,
                ) as opened:
                    captured_hits = search(captured_current, "NEEDLE_100", fixed=True)
            finally:
                core_module._ACTIVE_RETRIEVAL_CACHE.reset(cache_token)
                core_module._ACTIVE_RETRIEVAL_TRACE.reset(trace_token)
            self.assertEqual(["repo-099"], [hit.repo for hit in captured_hits])
            self.assertIn("NEEDLE_100", captured_hits[0].text)
            self.assertNotIn("G2_ONLY", captured_hits[0].text)
            self.assertEqual(1, opened.call_count)
            self.assertEqual(1, trace.physical_backend_operations)
            trace = RetrievalTrace(max_physical_backend_operations=1)
            trace_token = core_module._ACTIVE_RETRIEVAL_TRACE.set(trace)
            cache_token = core_module._ACTIVE_RETRIEVAL_CACHE.set({})
            try:
                with mock.patch("brain.index._connect", wraps=search_index._connect) as opened:
                    captured_paths = path_hits(captured_current, "Marker.java")
            finally:
                core_module._ACTIVE_RETRIEVAL_CACHE.reset(cache_token)
                core_module._ACTIVE_RETRIEVAL_TRACE.reset(trace_token)
            self.assertEqual(100, len(captured_paths))
            self.assertEqual(1, opened.call_count)
            self.assertEqual(1, trace.physical_backend_operations)

            ranked_paths = path_hits(pinned, "target", ["repo-000"])
            self.assertEqual(settings.path_result_limit, len(ranked_paths))
            self.assertEqual("z/target.java", ranked_paths[0].path)
            self.assertEqual(100, ranked_paths[0].score)

            connection = sqlite3.connect(settings.state_dir / "search.sqlite3")
            try:
                connection.execute(
                    "INSERT INTO path_membership_fts(repo,snapshot_sha,path) VALUES (?,?,?)",
                    ("repo-000", "sha-000", "z/orphan-target.java"),
                )
                connection.commit()
                self.assertEqual([], query_generation_paths(
                    pinned, generation, pinned.repositories[:1], "orphan-target",
                    limit=10, max_candidate_paths=100, max_seconds=2.0,
                )["repo-000"])
                connection.execute(
                    "UPDATE indexed_snapshots SET membership_hash=? WHERE repo=? AND snapshot_sha=?",
                    ("sha256:" + "0" * 64, "repo-099", "sha-099"),
                )
                connection.commit()
            finally:
                connection.close()
            self.assertIsNone(query_generation_indexes(
                pinned, generation, pinned.repositories, "NEEDLE_100",
                max_results=10, max_candidate_files=200, max_hits=100,
                max_bytes=8 * 1024 * 1024, max_seconds=2.0,
            ))
            self.assertIsNone(query_generation_paths(
                pinned, generation, pinned.repositories, "Marker.java",
                limit=10, max_candidate_paths=1_000, max_seconds=2.0,
            ))

    def test_pinned_batch_hydration_preserves_priority_and_total_byte_bound(self) -> None:
        marker = "PINNED_BATCH_OLD"
        paths = ["a.py", "b.py", "c.py", "z.py"]
        for path in paths:
            (self.repository / path).write_text(
                f"{marker}_{path}\n" + "x" * 700_000,
                encoding="utf-8",
            )
        repo = self.settings.repo("service")
        repo.source_sha = "sha-batch-hydration"
        snapshot_indexes(self.settings, changed_only=False)
        generation = current_generation_ref(self.settings)
        self.assertIsNotNone(generation)
        pinned = replace(self.settings, atlas_generation=generation, atlas_generation_mode="pinned")
        (self.repository / "z.py").write_text("LIVE_NEWER_CONTENT\n", encoding="utf-8")

        from brain import index as index_module

        requested = [("service", "z.py"), ("service", "a.py"), ("service", "b.py"), ("service", "c.py")]
        statements: list[tuple[str, tuple[object, ...]]] = []
        real_connect = index_module._connect

        class RecordingConnection:
            def __init__(self, connection: sqlite3.Connection) -> None:
                self.connection = connection

            def execute(self, sql: str, parameters: object = ()) -> object:
                values = tuple(parameters) if parameters else ()
                statements.append((sql, values))
                return self.connection.execute(sql, values)

            def __getattr__(self, name: str) -> object:
                return getattr(self.connection, name)

        def recording_connect(settings: object) -> RecordingConnection:
            return RecordingConnection(real_connect(settings))

        with mock.patch("brain.index._connect", side_effect=recording_connect) as opened:
            loaded = read_generation_files(
                pinned, generation, requested, max_bytes=900_000, max_seconds=2.0,
            )
        self.assertIsNotNone(loaded)
        self.assertEqual([("service", "z.py")], list(loaded))
        self.assertLessEqual(sum(len(value.encode("utf-8")) for value in loaded.values()), 900_000)
        self.assertIn("PINNED_BATCH_OLD_z.py", loaded[("service", "z.py")])
        self.assertNotIn("LIVE_NEWER_CONTENT", loaded[("service", "z.py")])
        self.assertEqual(1, opened.call_count)
        content_fetches = [row for row in statements if "WITH safe(" in row[0]]
        self.assertEqual(1, len(content_fetches))
        self.assertEqual(5, len(content_fetches[0][1]))

    def test_pinned_lexical_batch_bounds_metadata_content_and_repo_fairness(self) -> None:
        scale_root = self.root / "bounded-lexical"
        config_rows = [
            "[project]", "name='bounded-lexical'", "[graph]", "enabled=false",
            "[experience]", "enabled=false",
        ]
        for index in range(10):
            name = f"repo-{index:02d}"
            repository = scale_root / name
            repository.mkdir(parents=True)
            content = (
                "COMMON_TOKEN EXACT_USEFUL\n"
                if index == 9
                else f"COMMON_TOKEN irrelevant_{index}\n" + "x" * 700_000
            )
            (repository / "Evidence.java").write_text(content, encoding="utf-8")
            common = repository / "common"
            common.mkdir()
            for path_index in range(30):
                (common / f"common-path-{path_index:02d}.java").write_text(
                    f"final class Path{index}_{path_index} {{}}\n", encoding="utf-8",
                )
            config_rows.extend(["[[repositories]]", f"name='{name}'", f"path='bounded-lexical/{name}'"])
        config = self.root / "bounded-lexical.toml"
        config.write_text("\n".join(config_rows) + "\n", encoding="utf-8")
        settings = load_settings(config)
        for index, repo in enumerate(settings.repositories):
            repo.source_sha = f"bounded-sha-{index:02d}"
        snapshot_indexes(settings, changed_only=False)
        generation = current_generation_ref(settings)
        self.assertIsNotNone(generation)
        pinned = replace(settings, atlas_generation=generation, atlas_generation_mode="pinned")

        from brain import index as index_module

        statements: list[tuple[str, tuple[object, ...]]] = []
        real_connect = index_module._connect

        class RecordingConnection:
            def __init__(self, connection: sqlite3.Connection) -> None:
                self.connection = connection

            def execute(self, sql: str, parameters: object = ()) -> object:
                values = tuple(parameters) if parameters else ()
                statements.append((sql, values))
                return self.connection.execute(sql, values)

            def __getattr__(self, name: str) -> object:
                return getattr(self.connection, name)

        def recording_connect(value: object) -> RecordingConnection:
            return RecordingConnection(real_connect(value))

        stats: dict[str, object] = {}
        with mock.patch("brain.index._connect", side_effect=recording_connect) as opened:
            hits = query_generation_indexes(
                pinned, generation, pinned.repositories, "COMMON_TOKEN",
                max_results=10, max_candidate_files=10, max_hits=20,
                max_bytes=900_000, max_seconds=2.0, stats=stats,
            )
        self.assertIsNotNone(hits)
        self.assertTrue(any("EXACT_USEFUL" in row[2] for row in hits["repo-09"]))
        self.assertLessEqual(sum(len(rows) for rows in hits.values()), 20)
        self.assertEqual(1, opened.call_count)
        self.assertTrue(stats["budget_exhausted"])
        self.assertEqual("bytes", stats["reason"])
        self.assertLessEqual(int(stats["candidate_bytes"]), 900_000)
        metadata = [sql for sql, _ in statements if "FROM blob_fts" in sql]
        self.assertEqual(10, len(metadata))
        self.assertTrue(all("b.content" not in sql for sql in metadata))
        content_fetches = [row for row in statements if "WITH safe(" in row[0]]
        self.assertEqual(1, len(content_fetches))
        self.assertEqual(10, len(content_fetches[0][1]))

        from brain import core as core_module
        from brain.retrieval.models import RetrievalTrace

        trace = RetrievalTrace(max_physical_backend_operations=1)
        cache: dict[tuple[object, ...], object] = {}
        trace_token = core_module._ACTIVE_RETRIEVAL_TRACE.set(trace)
        cache_token = core_module._ACTIVE_RETRIEVAL_CACHE.set(cache)
        try:
            with mock.patch("brain.core._zoekt_manifest_hash", return_value=None), mock.patch(
                "brain.core.MAX_PINNED_QUERY_BYTES", 900_000,
            ):
                search_hits = search(pinned, "COMMON_TOKEN", fixed=True)
        finally:
            core_module._ACTIVE_RETRIEVAL_CACHE.reset(cache_token)
            core_module._ACTIVE_RETRIEVAL_TRACE.reset(trace_token)
        self.assertTrue(any(hit.repo == "repo-09" and "EXACT_USEFUL" in hit.text for hit in search_hits))
        self.assertIn("lexical_batch_budget:bytes", trace.fallback_reasons)
        self.assertEqual("lexical_batch_budget", trace.stop_reason)
        self.assertEqual({}, cache)

        statements.clear()
        path_stats: dict[str, object] = {}
        with mock.patch("brain.index._connect", side_effect=recording_connect) as opened:
            paths = query_generation_paths(
                pinned, generation, pinned.repositories, "common",
                limit=10, max_candidate_paths=50, max_seconds=2.0, stats=path_stats,
            )
        self.assertIsNotNone(paths)
        self.assertEqual([5] * 10, [len(paths[repo.name]) for repo in pinned.repositories])
        self.assertEqual(1, opened.call_count)
        self.assertEqual(50, path_stats["candidate_paths"])
        self.assertTrue(path_stats["budget_exhausted"])
        self.assertEqual("candidate_paths", path_stats["reason"])
        path_queries = [sql for sql, _ in statements if "SELECT path FROM file_membership" in sql]
        self.assertEqual(20, len(path_queries))
        self.assertTrue(all("ROW_NUMBER" not in sql for sql in path_queries))

        trace = RetrievalTrace(max_physical_backend_operations=1)
        cache = {}
        trace_token = core_module._ACTIVE_RETRIEVAL_TRACE.set(trace)
        cache_token = core_module._ACTIVE_RETRIEVAL_CACHE.set(cache)
        try:
            with mock.patch("brain.core.MAX_PINNED_PATH_CANDIDATES", 50):
                path_hits(pinned, "common")
        finally:
            core_module._ACTIVE_RETRIEVAL_CACHE.reset(cache_token)
            core_module._ACTIVE_RETRIEVAL_TRACE.reset(trace_token)
        self.assertIn("path_batch_budget:candidate_paths", trace.fallback_reasons)
        self.assertEqual("path_batch_budget", trace.stop_reason)
        self.assertEqual({}, cache)

    def test_windows_lexical_cache_revalidates_a_stable_artifact_projection(self) -> None:
        source = self.repository / "service.py"
        original = "VALUE = 'WINDOWS_CACHE'\n"
        source.write_text(original, encoding="utf-8")
        repo = self.settings.repo("service")
        repo.source_sha = "sha-windows-cache"
        snapshot_indexes(self.settings, changed_only=False, publish=False)

        from brain import index as search_index

        stable_identity = search_index._database_artifact_identity(self.settings)
        connection = sqlite3.connect(self.settings.state_dir / "search.sqlite3")
        try:
            poisoned = ("VALUE = 'POISONED'\n" + "X" * len(original))[:len(original)]
            connection.execute(
                "UPDATE blobs SET content=? WHERE blob IN "
                "(SELECT blob FROM file_membership WHERE repo='service' AND snapshot_sha='sha-windows-cache')",
                (poisoned,),
            )
            connection.execute(
                "UPDATE blob_fts SET content=? WHERE blob IN "
                "(SELECT blob FROM file_membership WHERE repo='service' AND snapshot_sha='sha-windows-cache')",
                (poisoned,),
            )
            connection.commit()
        finally:
            connection.close()

        search_index._SNAPSHOT_INTEGRITY_CACHE[(
            stable_identity, "service", "sha-windows-cache",
        )] = True
        connection = sqlite3.connect(self.settings.state_dir / "search.sqlite3")
        try:
            with mock.patch("brain.index.os.name", "nt"):
                self.assertFalse(search_index._snapshot_intact(
                    connection, "service", "sha-windows-cache", stable_identity,
                ))
        finally:
            connection.close()
        state, updated = snapshot_indexes(self.settings, changed_only=True, publish=False)
        self.assertEqual(["service"], updated)
        self.assertTrue(state["service"]["repaired"])
        self.assertEqual(
            [("service.py", 1, "VALUE = 'WINDOWS_CACHE'")],
            query_index(self.settings, repo, "WINDOWS_CACHE", max_results=5),
        )

    def test_refresh_repairs_equal_count_path_fts_substitution(self) -> None:
        source = self.repository / "service.py"
        source.write_text("VALUE = 'PATH_PROOF'\n", encoding="utf-8")
        repo = self.settings.repo("service")
        repo.source_sha = "sha-path-proof"
        snapshot_indexes(self.settings, changed_only=False, publish=False)

        connection = sqlite3.connect(self.settings.state_dir / "search.sqlite3")
        try:
            connection.execute(
                "DELETE FROM path_membership_fts WHERE repo='service' AND snapshot_sha='sha-path-proof'"
            )
            connection.execute(
                "INSERT INTO path_membership_fts(repo,snapshot_sha,path) VALUES ('service','sha-path-proof','bogus.py')"
            )
            connection.commit()
        finally:
            connection.close()
        from brain import index as search_index

        search_index._SNAPSHOT_INTEGRITY_CACHE.clear()
        state, updated = snapshot_indexes(self.settings, changed_only=True, publish=False)
        self.assertEqual(["service"], updated)
        self.assertTrue(state["service"]["repaired"])
        self.assertEqual(["service.py"], query_paths(
            self.settings, repo, "service", limit=5, snapshot_sha="sha-path-proof",
        ))

    def test_short_path_query_never_materializes_the_complete_membership(self) -> None:
        self.publish("sha-short-path", "a.py", "SHORT_PATH")
        connection = sqlite3.connect(self.settings.state_dir / "search.sqlite3")
        try:
            blob = connection.execute(
                "SELECT blob FROM file_membership WHERE repo='service' AND snapshot_sha='sha-short-path' LIMIT 1"
            ).fetchone()[0]
            connection.executemany(
                "INSERT INTO file_membership(repo,snapshot_sha,path,blob) VALUES (?,?,?,?)",
                [
                    ("service", "sha-short-path", f"a-noise/{number:04d}.txt", blob)
                    for number in range(500)
                ],
            )
            connection.commit()
        finally:
            connection.close()
        repo = self.settings.repo("service")
        with mock.patch("brain.index._available", return_value=True):
            rows = query_paths(
                self.settings, repo, "a", limit=1, snapshot_sha="sha-short-path",
            )
        self.assertIsNotNone(rows)
        self.assertEqual(100, len(rows))

    def test_repository_walker_counts_nonindexable_entries_before_filtering(self) -> None:
        from brain.index import _WalkBudget, _walk_root

        noise = self.root / "nonindexable-noise"
        noise.mkdir()
        for number in range(5):
            (noise / f"ignored-{number}.bin").write_bytes(b"noise")
        budget = _WalkBudget(3, time.monotonic() + 10, 10)
        with self.assertRaisesRegex(RuntimeError, "item or time limit"):
            list(_walk_root(noise, {".py"}, set(), budget=budget))

    def test_fresh_process_noop_revalidates_content_and_corruption_forces_repair(self) -> None:
        source = self.repository / "service.py"
        source.write_text("VALUE = 'SEALED_NOOP'\n", encoding="utf-8")
        repo = self.settings.repo("service")
        repo.source_sha = "sha-sealed-noop"
        snapshot_indexes(self.settings, changed_only=False, publish=False)

        from brain import index as search_index

        search_index._SNAPSHOT_INTEGRITY_CACHE.clear()
        with mock.patch("brain.index._blob_identity_valid", wraps=search_index._blob_identity_valid) as validated:
            _, updated = snapshot_indexes(self.settings, changed_only=True, publish=False)
        self.assertEqual([], updated)
        self.assertGreater(validated.call_count, 0)

        database = self.settings.state_dir / "search.sqlite3"
        connection = sqlite3.connect(database)
        try:
            original = source.read_text(encoding="utf-8")
            poison = ("VALUE = 'POISONED'\n" + "X" * len(original))[:len(original)]
            connection.execute(
                "UPDATE blobs SET content=? WHERE blob IN "
                "(SELECT blob FROM file_membership WHERE repo='service' AND snapshot_sha='sha-sealed-noop')",
                (poison,),
            )
            connection.execute(
                "UPDATE blob_fts SET content=? WHERE blob IN "
                "(SELECT blob FROM file_membership WHERE repo='service' AND snapshot_sha='sha-sealed-noop')",
                (poison,),
            )
            connection.commit()
        finally:
            connection.close()
        forged_identity = search_index._database_artifact_identity(self.settings)
        (self.settings.state_dir / "search-integrity.json").write_text(json.dumps({
            "version": 1,
            "artifact_identity": json.loads(json.dumps(forged_identity)),
            "snapshots": {"service": {"sha-sealed-noop": 1}},
        }), encoding="utf-8")
        search_index._SNAPSHOT_INTEGRITY_CACHE.clear()
        state, updated = snapshot_indexes(self.settings, changed_only=True, publish=False)
        self.assertEqual(["service"], updated)
        self.assertTrue(state["service"]["repaired"])
        self.assertEqual(
            [("service.py", 1, "VALUE = 'SEALED_NOOP'")],
            query_index(self.settings, repo, "SEALED_NOOP", max_results=5),
        )

    def test_git_blob_loader_enforces_item_and_byte_batch_bounds(self) -> None:
        repo = self.settings.repo("service")
        blobs = {f"{index:040x}" for index in range(7)}
        checked_batches: list[list[str]] = []
        loaded_batches: list[list[str]] = []

        def run(command: list[str], _cwd: Path, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            requested = bytes(kwargs.get("input_bytes") or b"").decode("ascii").splitlines()
            if any("--batch-check" in value for value in command):
                checked_batches.append(requested)
                output = b"".join(f"{blob} blob 3\n".encode("ascii") for blob in requested)
            else:
                loaded_batches.append(requested)
                output = b"".join(f"{blob} blob 3\nxxx\n".encode("ascii") for blob in requested)
            return subprocess.CompletedProcess(command, 0, stdout=output)

        with (
            mock.patch("brain.index.run_bounded_process", side_effect=run),
            mock.patch("brain.index._GIT_BLOB_BATCH_ITEMS", 3),
            mock.patch("brain.index._GIT_BLOB_BATCH_BYTES", 7),
            mock.patch("brain.index._GIT_BLOB_CHECK_BYTES", 100),
        ):
            loaded = list(_git_blob_contents(repo, blobs))
        self.assertEqual(7, len(loaded))
        self.assertEqual([2, 2, 2, 1], [len(batch) for batch in loaded_batches])
        self.assertTrue(all(len(batch) <= 3 for batch in checked_batches))
        self.assertTrue(all(len(("\n".join(batch) + "\n").encode("ascii")) <= 100 for batch in checked_batches))
        self.assertTrue(all(len(batch) <= 3 for batch in loaded_batches))
        self.assertTrue(all(len(batch) * 3 <= 7 for batch in loaded_batches))

    def test_noop_refresh_rebuilds_invalid_zoekt_shard_for_same_snapshot(self) -> None:
        source = self.repository / "service.py"
        source.write_text("VALUE = 'ZOEKT_REPAIR'\n", encoding="utf-8")
        repo = self.settings.repo("service")
        repo.source_sha = "sha-zoekt"
        snapshot_indexes(self.settings, changed_only=False, publish=False)

        with (
            mock.patch("brain.backends.zoekt.immutable_snapshot_available", return_value=True),
            mock.patch("brain.backends.zoekt.valid_shard_manifest", return_value=False),
            mock.patch("brain.backends.zoekt.build", return_value={"service": {"status": "built"}}) as built,
        ):
            _, updated = snapshot_indexes(self.settings, changed_only=True, publish=False)
        self.assertEqual([], updated)
        self.assertEqual(["service"], [item.name for item in built.call_args.args[1]])

    def test_removed_repository_is_retained_for_gc_but_not_republished(self) -> None:
        second = self.root / "retired-service"
        second.mkdir()
        (self.repository / "service.py").write_text("ACTIVE = True\n", encoding="utf-8")
        (second / "retired.py").write_text("RETIRED = True\n", encoding="utf-8")
        self.config.write_text(
            "[project]\nname='atlas-test'\n[graph]\nenabled=false\n"
            "[experience]\nenabled=false\n"
            "[[repositories]]\nname='service'\npath='service'\n"
            "[[repositories]]\nname='retired-service'\npath='retired-service'\n",
            encoding="utf-8",
        )
        initial = load_settings(self.config)
        initial_state, _ = snapshot_indexes(initial, changed_only=False)
        self.assertEqual({"service", "retired-service"}, set(initial_state))

        self.config.write_text(
            "[project]\nname='atlas-test'\n[graph]\nenabled=false\n"
            "[experience]\nenabled=false\n"
            "[[repositories]]\nname='service'\npath='service'\n",
            encoding="utf-8",
        )
        current = load_settings(self.config)
        current_state, _ = snapshot_indexes(current, changed_only=True)
        self.assertEqual({"service"}, set(current_state))
        generation = current_generation_ref(current)
        self.assertIsNotNone(generation)
        self.assertEqual({"service"}, set(generation.snapshots))
        zoekt_shards = generation.component("zoekt")["details"]["shards"]
        self.assertNotIn("retired-service", {row.get("repo") for row in zoekt_shards})
        self.assertIn(
            ("retired-service", str(initial_state["retired-service"]["sha"])),
            membership_snapshots(current),
        )

    def test_missing_pinned_generation_fails_closed_without_rewriting_ticket_identity(self) -> None:
        generation = self.publish("sha-g1", "old.py", "G1_MARKER")
        start_session(self.settings, "PIN-MISSING", "Keep the exact generation pin.")
        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            connection.execute("DELETE FROM index_generations WHERE generation=?", (generation.generation,))
            connection.commit()
        finally:
            connection.close()
        request = """CONTEXT_REQUEST:
  version: 1
  objective: Locate G1_MARKER.
  searches:
    - query: G1_MARKER
      repos: [service]
  paths: []
  symbols: []
  files: []
  history: []
"""
        with self.assertRaisesRegex(RuntimeError, "pinned Atlas generation is unavailable"):
            create_context(self.settings, "PIN-MISSING", request)
        state = session_state(self.settings, "PIN-MISSING")
        self.assertEqual(generation.identity, state["atlas_generation_id"])
        self.assertEqual(generation.generation, state["generation"])

    def test_number_only_unavailable_pin_never_substitutes_a_matching_generation(self) -> None:
        self.publish("sha-number-pin", "pinned.py", "NUMBER_PIN")
        start_session(self.settings, "PIN-NUMBER-ONLY", "Keep the exact numeric generation pin.")
        state_path = self.settings.runs_dir / "PIN-NUMBER-ONLY" / "session.json"
        poisoned = json.loads(state_path.read_text(encoding="utf-8"))
        poisoned["generation"] = 1000
        poisoned["atlas_generation_id"] = None
        poisoned["generation_mode"] = "atlas"
        state_path.write_text(json.dumps(poisoned, indent=2) + "\n", encoding="utf-8")
        before = state_path.read_bytes()
        request = """CONTEXT_REQUEST:
  version: 1
  objective: Locate NUMBER_PIN.
  searches:
    - query: NUMBER_PIN
      repos: [service]
  paths: []
  symbols: []
  files: []
  history: []
"""
        with self.assertRaisesRegex(BrainError, "pinned Atlas generation is unavailable"):
            create_context(self.settings, "PIN-NUMBER-ONLY", request)
        self.assertEqual(before, state_path.read_bytes())

    def test_nongit_ticket_pin_never_falls_through_to_a_new_working_tree(self) -> None:
        source = self.repository / "service.py"
        source.write_text("VALUE = 'OLD_ONLY'\n", encoding="utf-8")
        first_state, _ = snapshot_indexes(self.settings, changed_only=False)
        first = current_generation_ref(self.settings)
        self.assertIsNotNone(first)
        first_snapshot = first.snapshots["service"]
        self.assertTrue(first_snapshot.startswith("nongit-"))
        start_session(self.settings, "NONGIT-A", "Keep the first non-Git generation pinned.")

        source.write_text("VALUE = 'NEW_ONLY'\n", encoding="utf-8")
        second_state, updated = snapshot_indexes(self.settings, changed_only=True)
        second = current_generation_ref(self.settings)
        self.assertEqual(["service"], updated)
        self.assertIsNotNone(second)
        self.assertNotEqual(first_snapshot, second.snapshots["service"])
        self.assertNotEqual(first_state["service"]["sha"], second_state["service"]["sha"])

        old_request = """CONTEXT_REQUEST:
  version: 1
  objective: Locate OLD_ONLY in the pinned source.
  searches:
    - query: OLD_ONLY
      repos: [service]
  paths: []
  symbols: []
  files: []
  history: []
"""
        old_context, _, _ = create_context(self.settings, "NONGIT-A", old_request)
        self.assertIn("OLD_ONLY", old_context)
        self.assertNotIn("NEW_ONLY", old_context)

        start_session(self.settings, "NONGIT-B", "Use the current non-Git generation.")
        new_context, _, _ = create_context(
            self.settings, "NONGIT-B", old_request.replace("OLD_ONLY", "NEW_ONLY"),
        )
        self.assertIn("NEW_ONLY", new_context)

    def test_unborn_git_ticket_pin_uses_content_addressed_immutable_snapshots(self) -> None:
        subprocess.run([native_command("git"), "init"], cwd=self.repository, check=True, capture_output=True)
        source = self.repository / "unborn.py"
        source.write_text("VALUE = 'UNBORN_G1'\n", encoding="utf-8")
        settings = load_settings(self.config)
        snapshot_indexes(settings, changed_only=False)
        first = current_generation_ref(settings)
        self.assertIsNotNone(first)
        first_snapshot = first.snapshots["service"]
        self.assertTrue(first_snapshot.startswith("worktree-"))
        start_session(settings, "UNBORN-A", "Keep unborn Git G1 pinned.")

        source.write_text("VALUE = 'UNBORN_G2'\n", encoding="utf-8")
        snapshot_indexes(settings, changed_only=True)
        second = current_generation_ref(settings)
        self.assertIsNotNone(second)
        self.assertNotEqual(first_snapshot, second.snapshots["service"])
        request = """CONTEXT_REQUEST:
  version: 1
  objective: Locate UNBORN_G1.
  searches:
    - query: UNBORN_G1
      repos: [service]
  paths: []
  symbols: []
  files: []
  history: []
"""
        old_context, _, _ = create_context(settings, "UNBORN-A", request)
        self.assertIn("UNBORN_G1", old_context)
        self.assertNotIn("UNBORN_G2", old_context)
        start_session(settings, "UNBORN-B", "Use unborn Git G2.")
        new_context, _, _ = create_context(
            settings, "UNBORN-B", request.replace("UNBORN_G1", "UNBORN_G2"),
        )
        self.assertIn("UNBORN_G2", new_context)
        gc(settings, dry_run=False, keep_recent=1)
        self.assertTrue((settings.state_dir / "snapshots" / "service" / first_snapshot).is_dir())
        self.assertNotIn("OLD_ONLY", new_context)

    def test_pinned_lexical_component_rejects_rebound_valid_path_membership(self) -> None:
        generation = self.publish("sha-g1", "old.py", "G1_MARKER")
        database = self.settings.state_dir / "search.sqlite3"
        poisoned = "def evidence():\n    return 'POISONED_VALID_BLOB'\n"
        blob = "sha256:" + hashlib.sha256(poisoned.encode("utf-8")).hexdigest()
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "INSERT INTO blobs(blob,content,size) VALUES (?,?,?)",
                (blob, poisoned, len(poisoned.encode("utf-8"))),
            )
            connection.execute("INSERT INTO blob_fts(blob,content) VALUES (?,?)", (blob, poisoned))
            connection.execute(
                "UPDATE file_membership SET blob=? WHERE repo='service' AND snapshot_sha='sha-g1' AND path='old.py'",
                (blob,),
            )
            connection.commit()
        finally:
            connection.close()
        pinned = replace(
            self.settings,
            atlas_generation=generation,
            atlas_generation_mode="pinned",
            repositories=[replace(
                self.settings.repo("service"), source_path=None, source_sha="sha-g1",
            )],
        )
        self.assertEqual([], search(pinned, "POISONED_VALID_BLOB", fixed=True))
        with self.assertRaisesRegex(BrainError, "Pinned indexed source is unavailable"):
            read_source(pinned, SearchHit("service", "old.py", 1, "POISONED_VALID_BLOB"))

    def test_search_v1_migration_keeps_current_membership_queryable(self) -> None:
        database = self.settings.state_dir / "search.sqlite3"
        connection = sqlite3.connect(database)
        connection.executescript(
            "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "INSERT INTO metadata VALUES ('schema_version', '1');"
            "CREATE TABLE repositories (name TEXT PRIMARY KEY, sha TEXT, indexed_at TEXT NOT NULL, file_count INTEGER NOT NULL);"
            "CREATE TABLE blobs (blob TEXT PRIMARY KEY, content TEXT NOT NULL, size INTEGER NOT NULL);"
            "CREATE TABLE files (repo TEXT NOT NULL, path TEXT NOT NULL, blob TEXT NOT NULL, PRIMARY KEY(repo, path));"
            "CREATE VIRTUAL TABLE blob_fts USING fts5(blob UNINDEXED, content, tokenize='trigram');"
            "CREATE VIRTUAL TABLE path_fts USING fts5(repo UNINDEXED, path, tokenize='trigram');"
            "INSERT INTO repositories VALUES ('service', 'sha-v1', 'now', 1);"
            "INSERT INTO blobs VALUES ('blob-v1', 'V1_ONLY', 7);"
            "INSERT INTO files VALUES ('service', 'legacy.py', 'blob-v1');"
            "INSERT INTO blob_fts VALUES ('blob-v1', 'V1_ONLY');"
            "INSERT INTO path_fts VALUES ('service', 'legacy.py');"
        )
        connection.close()
        repo = self.settings.repo("service")
        repo.source_sha = "sha-v1"
        self.assertEqual([("legacy.py", 1, "V1_ONLY")], query_index(
            self.settings,
            repo,
            "V1_ONLY",
            max_results=10,
            snapshot_sha="sha-v1",
        ))
        self.assertEqual({("service", "sha-v1")}, membership_snapshots(self.settings))

    def test_search_v2_migration_streams_large_snapshot_membership(self) -> None:
        database = self.settings.state_dir / "search.sqlite3"
        connection = sqlite3.connect(database)
        _initialize_connection(connection)
        for snapshot_index in range(300):
            snapshot = f"sha-{snapshot_index:04d}"
            connection.execute(
                "INSERT INTO indexed_snapshots(repo,snapshot_sha,indexed_at,file_count) VALUES (?,?,?,?)",
                ("service", snapshot, "now", 20),
            )
            connection.executemany(
                "INSERT INTO file_membership(repo,snapshot_sha,path,blob) VALUES (?,?,?,?)",
                (("service", snapshot, f"src/file-{item:03d}.py", f"blob-{item:03d}") for item in range(20)),
            )
        for trigger in (
            "file_membership_identity_insert", "file_membership_identity_delete",
            "file_membership_identity_update",
        ):
            connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute("ALTER TABLE indexed_snapshots DROP COLUMN membership_hash")
        connection.execute("UPDATE metadata SET value='2' WHERE key='schema_version'")
        connection.commit()
        connection.close()

        from brain import index as index_module

        original = index_module._membership_hash

        def streamed(repo, snapshot, rows):
            self.assertNotIsInstance(rows, list)
            return original(repo, snapshot, rows)

        with mock.patch("brain.index._membership_hash", side_effect=streamed) as hashed:
            migrated = connect_search(self.settings)
        try:
            self.assertEqual(300, hashed.call_count)
            self.assertEqual("3", migrated.execute(
                "SELECT value FROM metadata WHERE key='schema_version'",
            ).fetchone()[0])
            self.assertEqual(300, migrated.execute(
                "SELECT COUNT(*) FROM indexed_snapshots WHERE membership_hash IS NOT NULL",
            ).fetchone()[0])
        finally:
            migrated.close()

    def test_catalog_upgrade_is_serialized_across_processes(self) -> None:
        database = self.settings.state_dir / "catalog.sqlite3"
        connection = sqlite3.connect(database)
        connection.executescript(
            "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "INSERT INTO metadata VALUES ('schema_version', '1');"
            "CREATE TABLE embedding_cache (cache_key TEXT PRIMARY KEY, pack_id TEXT, dimension INTEGER, vector_json TEXT, created_at TEXT);"
            "CREATE TABLE index_generations (generation INTEGER PRIMARY KEY, created_at TEXT NOT NULL, manifest_path TEXT NOT NULL, status TEXT NOT NULL);"
            "CREATE TABLE atlas_retrieval_cache (generation INTEGER NOT NULL, cache_key TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL, last_used_at TEXT NOT NULL, PRIMARY KEY(generation, cache_key));"
        )
        connection.close()
        context = multiprocessing.get_context("spawn")
        with context.Pool(processes=8) as pool:
            versions = pool.map(_catalog_upgrade_worker, [str(self.config)] * 12)
        self.assertEqual(["12"] * 12, versions)
        migrated = sqlite3.connect(database)
        try:
            self.assertIn("identity", {row[1] for row in migrated.execute("PRAGMA table_info(index_generations)")})
            self.assertIn("payload_hash", {row[1] for row in migrated.execute("PRAGMA table_info(atlas_retrieval_cache)")})
        finally:
            migrated.close()

    def test_search_upgrade_is_serialized_across_processes_without_duplicate_fts_rows(self) -> None:
        database = self.settings.state_dir / "search.sqlite3"
        connection = sqlite3.connect(database)
        connection.executescript(
            "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "INSERT INTO metadata VALUES ('schema_version', '1');"
            "CREATE TABLE repositories (name TEXT PRIMARY KEY, sha TEXT, indexed_at TEXT NOT NULL, file_count INTEGER NOT NULL);"
            "CREATE TABLE blobs (blob TEXT PRIMARY KEY, content TEXT NOT NULL, size INTEGER NOT NULL);"
            "CREATE TABLE files (repo TEXT NOT NULL, path TEXT NOT NULL, blob TEXT NOT NULL, PRIMARY KEY(repo, path));"
            "CREATE VIRTUAL TABLE blob_fts USING fts5(blob UNINDEXED, content, tokenize='trigram');"
            "CREATE VIRTUAL TABLE path_fts USING fts5(repo UNINDEXED, path, tokenize='trigram');"
            "INSERT INTO repositories VALUES ('service', 'sha-v1', 'now', 1);"
            "INSERT INTO blobs VALUES ('blob-v1', 'LEGACY', 6);"
            "INSERT INTO files VALUES ('service', 'legacy.py', 'blob-v1');"
            "INSERT INTO blob_fts VALUES ('blob-v1', 'LEGACY');"
            "INSERT INTO path_fts VALUES ('service', 'legacy.py');"
        )
        connection.close()
        context = multiprocessing.get_context("spawn")
        with context.Pool(processes=8) as pool:
            results = pool.map(_search_upgrade_worker, [str(self.config)] * 12)
        self.assertEqual([1] * 12, results)
        migrated = sqlite3.connect(database)
        try:
            self.assertEqual(1, migrated.execute("SELECT COUNT(*) FROM file_membership").fetchone()[0])
            self.assertEqual(1, migrated.execute("SELECT COUNT(*) FROM path_membership_fts").fetchone()[0])
        finally:
            migrated.close()

    def test_search_upgrade_rechecks_newer_schema_after_waiting_for_migration_lock(self) -> None:
        database = self.settings.state_dir / "search.sqlite3"
        seed = sqlite3.connect(database)
        seed.executescript(
            "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "INSERT INTO metadata VALUES ('schema_version', '1');"
            "CREATE TABLE repositories (name TEXT PRIMARY KEY, sha TEXT, indexed_at TEXT NOT NULL, file_count INTEGER NOT NULL);"
            "CREATE TABLE blobs (blob TEXT PRIMARY KEY, content TEXT NOT NULL, size INTEGER NOT NULL);"
            "CREATE TABLE files (repo TEXT NOT NULL, path TEXT NOT NULL, blob TEXT NOT NULL, PRIMARY KEY(repo, path));"
            "CREATE VIRTUAL TABLE blob_fts USING fts5(blob UNINDEXED, content, tokenize='trigram');"
            "CREATE VIRTUAL TABLE path_fts USING fts5(repo UNINDEXED, path, tokenize='trigram');"
        )
        seed.close()
        inspection = sqlite3.connect(database)
        try:
            before_objects = inspection.execute(
                "SELECT type,name,sql FROM sqlite_master ORDER BY type,name",
            ).fetchall()
        finally:
            inspection.close()

        connection = sqlite3.connect(database)

        class PublishNewerSchemaBeforeLock:
            def __init__(self, inner: sqlite3.Connection) -> None:
                self.inner = inner
                self.published = False

            def execute(self, statement: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
                if statement.strip().upper() == "BEGIN IMMEDIATE" and not self.published:
                    publisher = sqlite3.connect(database)
                    try:
                        publisher.execute(
                            "UPDATE metadata SET value='999' WHERE key='schema_version'",
                        )
                        publisher.commit()
                    finally:
                        publisher.close()
                    self.published = True
                return self.inner.execute(statement, parameters)

            def __getattr__(self, name: str) -> object:
                return getattr(self.inner, name)

        try:
            with self.assertRaisesRegex(sqlite3.DatabaseError, "search schema 999 is newer"):
                _initialize_connection(PublishNewerSchemaBeforeLock(connection))  # type: ignore[arg-type]
            connection.rollback()
            verified = sqlite3.connect(database)
            try:
                self.assertEqual("999", verified.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'",
                ).fetchone()[0])
                self.assertEqual(before_objects, verified.execute(
                    "SELECT type,name,sql FROM sqlite_master ORDER BY type,name",
                ).fetchall())
            finally:
                verified.close()
        finally:
            connection.close()

    def test_catalog_upgrade_rechecks_newer_schema_before_any_ddl(self) -> None:
        import brain.catalog as catalog_module

        database = self.settings.state_dir / "catalog.sqlite3"
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        seed = sqlite3.connect(database)
        seed.executescript(
            "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "INSERT INTO metadata VALUES ('schema_version', '1');"
        )
        seed.close()
        inspection = sqlite3.connect(database)
        try:
            before_objects = inspection.execute(
                "SELECT type,name,sql FROM sqlite_master ORDER BY type,name",
            ).fetchall()
        finally:
            inspection.close()
        real_connect = sqlite3.connect

        class PublishNewerSchemaBeforeLock:
            def __init__(self, inner: sqlite3.Connection) -> None:
                self.inner = inner
                self.published = False

            def execute(self, statement: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
                if statement.strip().upper() == "BEGIN IMMEDIATE" and not self.published:
                    publisher = real_connect(database)
                    try:
                        publisher.execute("UPDATE metadata SET value='999' WHERE key='schema_version'")
                        publisher.commit()
                    finally:
                        publisher.close()
                    self.published = True
                return self.inner.execute(statement, parameters)

            def __getattr__(self, name: str) -> object:
                return getattr(self.inner, name)

        wrapped = PublishNewerSchemaBeforeLock(real_connect(database))
        with mock.patch.object(catalog_module.sqlite3, "connect", return_value=wrapped):
            with self.assertRaisesRegex(sqlite3.DatabaseError, "catalog schema 999 is newer"):
                catalog_module.connect(self.settings)
        verified = real_connect(database)
        try:
            self.assertEqual("999", verified.execute(
                "SELECT value FROM metadata WHERE key='schema_version'",
            ).fetchone()[0])
            self.assertEqual(before_objects, verified.execute(
                "SELECT type,name,sql FROM sqlite_master ORDER BY type,name",
            ).fetchall())
        finally:
            verified.close()

    def test_current_schema_connections_never_take_the_sqlite_writer_lane(self) -> None:
        import brain.catalog as catalog_module

        catalog = catalog_module.connect(self.settings)
        catalog.close()
        search_path = self.settings.state_dir / "search.sqlite3"
        search = sqlite3.connect(search_path)
        _initialize_connection(search)
        search.close()

        class TraceConnection:
            def __init__(self, inner: sqlite3.Connection) -> None:
                self.inner = inner
                self.statements: list[str] = []

            def execute(self, statement: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
                self.statements.append(statement.strip())
                return self.inner.execute(statement, parameters)

            def __getattr__(self, name: str) -> object:
                return getattr(self.inner, name)

        search_trace = TraceConnection(sqlite3.connect(search_path))
        try:
            _initialize_connection(search_trace)  # type: ignore[arg-type]
            self.assertFalse(any(
                statement.upper().startswith(("BEGIN IMMEDIATE", "CREATE "))
                for statement in search_trace.statements
            ))
        finally:
            search_trace.close()

        catalog_trace = TraceConnection(sqlite3.connect(self.settings.state_dir / "catalog.sqlite3"))
        with mock.patch.object(catalog_module.sqlite3, "connect", return_value=catalog_trace):
            opened = catalog_module.connect(self.settings)
            opened.close()
        self.assertFalse(any(
            statement.upper().startswith(("BEGIN IMMEDIATE", "CREATE "))
            for statement in catalog_trace.statements
        ))

    def test_search_connection_closes_when_schema_initialization_fails(self) -> None:
        database = self.settings.state_dir / "search.sqlite3"
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database)
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO metadata VALUES ('schema_version', '999')")
        connection.commit()
        connection.close()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            self.assertIsNone(query_index(self.settings, self.settings.repo("service"), "anything", max_results=1))
            garbage_collector.collect()
        self.assertFalse([item for item in caught if issubclass(item.category, ResourceWarning)])

    def test_future_catalog_and_search_schemas_are_rejected_without_disk_mutation(self) -> None:
        from brain.catalog import connect as connect_catalog

        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        cases = [
            (self.settings.state_dir / "catalog.sqlite3", "catalog", connect_catalog),
            (self.settings.state_dir / "search.sqlite3", "search", None),
        ]
        for database, label, opener in cases:
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("INSERT INTO metadata VALUES ('schema_version', '999')")
            connection.execute("CREATE TABLE future_owned (value TEXT)")
            connection.execute("INSERT INTO future_owned VALUES ('preserve-me')")
            connection.commit()
            connection.close()
            before = hashlib.sha256(database.read_bytes()).hexdigest()

            if opener is not None:
                with self.assertRaisesRegex(sqlite3.DatabaseError, "newer"):
                    opener(self.settings)
            else:
                candidate = sqlite3.connect(database)
                try:
                    with self.assertRaisesRegex(sqlite3.DatabaseError, "newer"):
                        _initialize_connection(candidate)
                finally:
                    candidate.close()

            self.assertEqual(before, hashlib.sha256(database.read_bytes()).hexdigest(), label)

    def test_relationship_projection_replaces_child_symlink_without_touching_source(self) -> None:
        target = self.settings.repo("service").path / "SOURCE.txt"
        target.write_text("repository source must remain read-only\n", encoding="utf-8")
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        projection = self.settings.state_dir / "relationships.json"
        projection.symlink_to(target)

        with mock.patch("brain.relations.analyze_relationships", return_value=([], [])):
            generate_relationship_map(self.settings)

        self.assertEqual("repository source must remain read-only\n", target.read_text(encoding="utf-8"))
        self.assertFalse(projection.is_symlink())
        self.assertEqual([], json.loads(projection.read_text(encoding="utf-8"))["relationships"])

    def test_managed_projection_writers_never_follow_predictable_temp_symlinks(self) -> None:
        from brain.catalog import _write_current_projection
        from brain.index import _write_source_snapshot_state

        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        generations = self.settings.state_dir / "generations"
        generations.mkdir()
        outside = self.settings.repo("service").path / "SOURCE-MARKER.txt"
        outside.write_text("preserve repository source\n", encoding="utf-8")
        traps = [
            self.settings.state_dir / "indexes.tmp",
            self.settings.state_dir / "sources.tmp",
            generations / "CURRENT-000001.tmp",
        ]
        for trap in traps:
            trap.symlink_to(outside)

        write_state(self.settings, {})
        _write_source_snapshot_state(self.settings, [])
        _write_current_projection(generations, 1)

        self.assertEqual("preserve repository source\n", outside.read_text(encoding="utf-8"))
        self.assertEqual({}, json.loads((self.settings.state_dir / "indexes.json").read_text(encoding="utf-8")))
        self.assertEqual("generation-000001\n", (generations / "CURRENT").read_text(encoding="utf-8"))

    def test_settings_never_attach_a_symlinked_source_snapshot(self) -> None:
        external = self.root / "external-live-tree"
        external.mkdir()
        (external / "private.py").write_text("EXTERNAL_LIVE_SOURCE\n", encoding="utf-8")
        snapshot = self.settings.state_dir / "snapshots" / "service" / "sha-g1"
        snapshot.parent.mkdir(parents=True)
        snapshot.symlink_to(external, target_is_directory=True)
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        (self.settings.state_dir / "sources.json").write_text(json.dumps({
            "service": {
                "status": "current", "ref": "refs/heads/main", "sha": "sha-g1",
                "snapshot": str(snapshot),
            },
        }), encoding="utf-8")

        reloaded = load_settings(self.config)
        repository = reloaded.repo("service")
        self.assertIsNone(repository.source_path)
        self.assertEqual(repository.path, repository.scan_path)
        self.assertIn("Ignored unsafe", str(repository.source_warning))

    def test_sqlite_roots_reject_leaf_symlinks_without_mutating_targets(self) -> None:
        from brain.catalog import connect as connect_catalog
        from brain.index import _connect as connect_search

        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        for name, opener in (("catalog.sqlite3", connect_catalog), ("search.sqlite3", connect_search)):
            outside = self.root / f"outside-{name}"
            connection = sqlite3.connect(outside)
            connection.close()
            managed = self.settings.state_dir / name
            try:
                managed.symlink_to(outside)
            except OSError as error:
                self.skipTest(f"file symlinks unavailable: {error}")
            with self.assertRaisesRegex(ValueError, "direct regular file"):
                opener(self.settings)
            verified = sqlite3.connect(outside)
            try:
                self.assertEqual([], verified.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'",
                ).fetchall())
            finally:
                verified.close()
            managed.unlink()

    def test_git_snapshot_item_and_deadline_bounds_preserve_prior_target(self) -> None:
        target = self.settings.state_dir / "snapshots" / "service" / "sha-bounded"
        target.mkdir(parents=True)
        (target / "prior.txt").write_text("prior immutable target\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "item limit"):
            _snapshot_metadata(target, max_items=0)
        with self.assertRaises((TimeoutError, ValueError)):
            _snapshot_seal(target, "sha-bounded", deadline=time.monotonic())

        def archive(_repo, _ref, archive_path, **_kwargs):
            with tarfile.open(archive_path, "w") as output:
                for index in range(3):
                    member = tarfile.TarInfo(f"file-{index}.txt")
                    member.size = 0
                    output.addfile(member)
            return subprocess.CompletedProcess([], 0, b"", b"")

        with mock.patch("brain.sync._git_archive_to_path", side_effect=archive):
            self.assertIsNone(_export_snapshot(
                self.settings.repo("service"), "HEAD", "sha-bounded", self.settings.state_dir,
                max_items=2,
            ))
        self.assertEqual("prior immutable target\n", (target / "prior.txt").read_text(encoding="utf-8"))
        seal_limited = self.settings.state_dir / "snapshots" / "service" / "sha-seal-limited"
        with (
            mock.patch("brain.sync._git_archive_to_path", side_effect=archive),
            mock.patch("brain.sync.MAX_GIT_SNAPSHOT_SEAL_BYTES", 64),
        ):
            self.assertIsNone(_export_snapshot(
                self.settings.repo("service"), "HEAD", "sha-seal-limited", self.settings.state_dir,
            ))
        self.assertFalse(seal_limited.exists())

    def test_git_archive_stderr_is_physically_bounded(self) -> None:
        destination = self.settings.state_dir / "bounded-error.tar"
        destination.parent.mkdir(parents=True, exist_ok=True)
        with mock.patch("brain.sync.MAX_GIT_COMMAND_STDERR_BYTES", 8):
            result = _git_archive_to_path(
                self.settings.repo("service"), "definitely-missing-ref", destination,
            )
        self.assertEqual(125, result.returncode)
        self.assertLessEqual(len(result.stderr), 8)

    def test_nonnumeric_schema_metadata_degrades_without_escaping_or_leaking(self) -> None:
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        catalog = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        catalog.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        catalog.execute("INSERT INTO metadata VALUES ('schema_version', 'not-a-number')")
        catalog.commit()
        catalog.close()
        search = sqlite3.connect(self.settings.state_dir / "search.sqlite3")
        search.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        search.execute("INSERT INTO metadata VALUES ('schema_version', 'not-a-number')")
        search.commit()
        search.close()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            self.assertIsNone(current_generation_ref(self.settings))
            self.assertIsNone(query_index(self.settings, self.settings.repo("service"), "anything", max_results=1))
            garbage_collector.collect()
        self.assertFalse([item for item in caught if issubclass(item.category, ResourceWarning)])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

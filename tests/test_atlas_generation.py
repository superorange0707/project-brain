from __future__ import annotations

import gc as garbage_collector
import json
import sqlite3
import tempfile
import unittest
import warnings
from dataclasses import replace
from pathlib import Path
from unittest import mock

from brain.catalog import (
    canonical_atlas_identity,
    collect_generation_components,
    current_generation_ref,
    publish_generation,
    resolve_generation,
)
from brain.core import create_context, load_settings, session_state, snapshot_indexes, start_session
from brain.experience import build_experience_index, similar_cases
from brain.graph import graph_symbol_hits
from brain.index import membership_snapshots, query_index, write_state
from brain.ops import gc
from brain.relations import generate_relationship_map, related_relationships
from brain.semantic import build_semantic_index, search_semantic


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

    def test_mandatory_component_failure_does_not_move_current(self) -> None:
        generation = self.publish("sha-g1", "old.py", "G1_MARKER")
        state = json.loads((self.settings.state_dir / "indexes.json").read_text(encoding="utf-8"))
        with self.assertRaises(sqlite3.IntegrityError):
            publish_generation(
                self.settings,
                state,
                components={"lexical": {"schema_version": "2", "status": "unavailable"}},
            )
        self.assertEqual(generation.generation, current_generation_ref(self.settings).generation)

    def test_snapshot_index_does_not_report_success_when_atlas_publication_fails(self) -> None:
        generation = self.publish("sha-g1", "old.py", "G1_MARKER")
        with mock.patch("brain.catalog.publish_generation", side_effect=sqlite3.OperationalError("publish failed")):
            with self.assertRaises(sqlite3.OperationalError):
                snapshot_indexes(self.settings, changed_only=False)
        self.assertEqual(generation.generation, current_generation_ref(self.settings).generation)

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

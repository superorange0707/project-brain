"""Upgrade regression: real persisted USearch vectors survive cache eviction."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest import mock

from brain.atlas import build_atlas
from brain.catalog import collect_generation_components, connect, current_generation_ref, publish_generation
from brain.core import Repository, load_settings, snapshot_indexes, start_session, session_state
from brain.locks import workspace_operation
from brain.semantic import build_semantic_index, _usearch, _serving_state, semantic_schema_version


@unittest.skipUnless(importlib.util.find_spec("usearch"), "requires optional semantic extra")
class SemanticIncrementalTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="brain-incremental-")))
        for name in ("a", "b"):
            (self.root / name).mkdir()
            for i in range(10):
                (self.root / name / f"file_{i}.py").write_text(
                    f"def function_{i}():\n    return 'original_{i}'\n", encoding="utf-8")
        # A file edit changes every source chunk ID, including the untouched method.
        (self.root / "a/file_0.py").write_text(
            "def function_0():\n    return 'original_0'\ndef untouched():\n    return 'KEEP'\n", encoding="utf-8")
        config = self.root / "brain.toml"
        config.write_text("[project]\nname='incremental'\n[graph]\nenabled=false\n[experience]\nenabled=false\n"
                          "[[repositories]]\nname='a'\npath='a'\n[[repositories]]\nname='b'\npath='b'\n", encoding="utf-8")
        self.settings = load_settings(config)
        self.calls = []
        self.manifest = {"pack_id": "incremental-test", "embedding_dimension": 8, "test_only": True}
        self.runtime = mock.Mock()
        self.runtime.embed.side_effect = self.embed
        for target in ("brain.semantic.active_pack", "brain.semantic.verified_pack", "brain.models.verified_pack"):
            self.stack.enter_context(mock.patch(target, return_value=self.manifest))
        self.stack.enter_context(mock.patch("brain.semantic.runtime_for_pack", return_value=self.runtime))
        self.stack.enter_context(mock.patch("brain.semantic._check_pack_integrity"))
        self.stack.enter_context(mock.patch("brain.semantic.embedding_batch_size", return_value=2))
        # Use the real normal LRU eviction, not a deletion/reset of user state.
        self.stack.enter_context(mock.patch("brain.semantic.MAX_EMBEDDING_CACHE_ROWS", 24))
        self.stack.enter_context(workspace_operation(self.settings))
        self.first, self.state1, self.events1 = self.build()
        self.inputs1 = set(self.calls)
        self.artifacts1 = {Path(s["path"]): Path(s["path"]).read_bytes() for s in self.state1["shards"]}
        self.calls.clear()

    def embed(self, cards, **kwargs):
        self.calls.extend(cards)
        return [[1.0, *[float(x) / 255 for x in hashlib.sha256(card.encode()).digest()[:7]]] for card in cards]

    def build(self):
        indexed, _ = snapshot_indexes(self.settings, changed_only=True, publish=False)
        payload = build_atlas(self.settings, indexed)
        events = []
        build_semantic_index(self.settings, atlas_cards=payload["cards"], progress=events.append)
        components = collect_generation_components(self.settings, indexed, atlas_payload=payload)
        self.assertEqual("ready", components["semantic"]["status"], components["semantic"]["details"])
        publish_generation(self.settings, indexed, components=components, atlas_payload=payload)
        return current_generation_ref(self.settings), json.loads((self.settings.state_dir / "semantic-index.json").read_text()), events

    def change(self, name="a"):
        path = self.root / name / "file_0.py"
        path.write_text(path.read_text().replace("original_0", "CHANGED"), encoding="utf-8")

    def assert_preserved(self):
        for path, data in self.artifacts1.items():
            self.assertEqual(data, path.read_bytes())
        self.assertEqual(self.state1, _serving_state(self.settings, self.first))

    def test_legacy_upgrade_evicted_cache_reuses_exact_inputs_and_retains_vectors(self):
        self.assertEqual("1:1:3:1:2", semantic_schema_version())  # v1.0.7 contract: no migration
        with closing(connect(self.settings)) as connection:
            self.assertLess(connection.execute("SELECT COUNT(*) FROM embedding_cache").fetchone()[0], len(self.inputs1))
        self.change()
        Index, numpy = _usearch()
        with mock.patch.object(Index, "restore", wraps=Index.restore) as restore:
            second, state2, events = self.build()
        self.assertEqual(1, restore.call_count)  # changed repo only, one native view
        self.assertNotEqual(self.first.identity, second.identity)
        self.assertTrue(self.calls)
        self.assertFalse(set(self.calls) & self.inputs1, "unchanged model inputs must never be re-embedded")
        self.assertGreater(events[-1]["shard_vectors_reused"], 0)
        self.assertEqual(1, events[-1]["semantic_shards_reused"])
        self.assertEqual(1, events[-1]["semantic_shards_rebuilt"])
        self.assertEqual(next(s["path"] for s in self.state1["shards"] if s["repo"] == "b"),
                         next(s["path"] for s in state2["shards"] if s["repo"] == "b"))
        self.assert_preserved()
        old = next(s for s in self.state1["shards"] if s["repo"] == "a")
        new = next(s for s in state2["shards"] if s["repo"] == "a")
        old_index, new_index = Index.restore(old["path"], view=True), Index.restore(new["path"], view=True)
        try:
            for entry in old["entries"]:
                if entry in new["entries"]:
                    numpy.testing.assert_array_equal(old_index.get(old["entries"].index(entry)), new_index.get(new["entries"].index(entry)))
        finally:
            old_index.reset()
            new_index.reset()
        self.calls.clear()
        with mock.patch("brain.semantic._chunk_groups", side_effect=AssertionError("unchanged generation should skip chunking")):
            _, _, noop = self.build()
        self.assertEqual([], self.calls)
        self.assertEqual("reused", noop[-1]["generation_state"])

    def test_changed_refresh_uses_registration_not_stale_compatibility_projection(self):
        (self.settings.state_dir / "semantic-index.json").write_text('{"stale":true}', encoding="utf-8")
        self.change()
        _, _, events = self.build()
        self.assertFalse(set(self.calls) & self.inputs1)
        self.assertGreater(events[-1]["shard_vectors_reused"], 0)
        self.assert_preserved()

    def test_input_reuse_does_not_trust_poisoned_atlas_card_content(self):
        self.change()
        with closing(connect(self.settings)) as connection:
            connection.execute("UPDATE atlas_cards SET content='POISONED' WHERE repo='a'")
            connection.commit()
        # Corrupted old cards cannot supply vector identity. Source-only recovery
        # remains safe, and Atlas publication must still reject immutable damage.
        indexed, _ = snapshot_indexes(self.settings, changed_only=True, publish=False)
        payload = build_atlas(self.settings, indexed)
        events = []
        build_semantic_index(self.settings, atlas_cards=payload["cards"], progress=events.append)
        self.assertFalse(any("POISONED" in card for card in self.calls))
        self.assertGreater(events[-1]["shard_vectors_reused"], 0)  # exact old source inputs still reusable
        components = collect_generation_components(self.settings, indexed, atlas_payload=payload)
        with self.assertRaisesRegex(RuntimeError, "immutable Atlas row mismatch"):
            publish_generation(self.settings, indexed, components=components, atlas_payload=payload)
        self.assertEqual(self.first.identity, current_generation_ref(self.settings).identity)

    def test_changed_refresh_keeps_safe_reuse_counters_through_public_progress_filter(self):
        from brain.ops import progress_event, format_refresh_progress

        self.change()
        _, _, events = self.build()
        raw = dict(events[-1], semantic_rebuild_reason="private path / secret", private_token="do not expose")
        safe = progress_event(**raw)
        self.assertEqual(events[-1]["shard_vectors_reused"], safe["shard_vectors_reused"])
        self.assertEqual(1, safe["semantic_shards_reused"])
        self.assertNotIn("private", json.dumps(safe))
        self.assertIn("old vectors", format_refresh_progress(safe))
        self.assertIn("shards reused 1 rebuilt 1", format_refresh_progress(safe))

    def test_changed_pack_never_reuses_old_vectors(self):
        self.manifest["document_instruction"] = "NEW CONTRACT: "
        self.change()
        _, _, events = self.build()
        self.assertEqual(0, events[-1]["shard_vectors_reused"])
        self.assertEqual(0, events[-1]["semantic_shards_reused"])
        self.assertEqual(events[-1]["semantic_cards_total"], len(self.calls))
        self.assertTrue(any(e.get("semantic_rebuild_reason") == "model_contract_changed" for e in events))

    def test_missing_unrelated_shard_does_not_discard_valid_parent_computations(self):
        missing = next(Path(s["path"]) for s in self.state1["shards"] if s["repo"] == "b")
        missing.unlink()  # isolated corruption fixture only
        self.change()
        _, _, events = self.build()
        self.assertGreater(events[-1]["shard_vectors_reused"], 0)
        self.assertFalse(any(card in self.inputs1 for card in self.calls if card.startswith("Repository: a\n")))
        self.assertIsNone(_serving_state(self.settings, self.first), "damaged pinned state must not substitute G2")

    def test_corrupt_shard_rebuilds_and_reports_why_without_false_reuse(self):
        shard = next(Path(s["path"]) for s in self.state1["shards"] if s["repo"] == "a")
        data = shard.read_bytes()
        shard.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
        self.change()
        _, _, events = self.build()
        self.assertEqual(0, events[-1]["shard_vectors_reused"])
        self.assertTrue(set(self.calls) & self.inputs1)
        self.assertTrue(any(e.get("semantic_rebuild_reason") == "prior_shard_invalid" for e in events))

    def test_reuse_budget_exhaustion_falls_back_explicitly(self):
        self.change()
        from brain.semantic import _reuse_shard_vectors

        def exhausted(*args, **kwargs):
            kwargs["budget"]["seconds"] = 0
            return _reuse_shard_vectors(*args, **kwargs)

        with mock.patch("brain.semantic._reuse_shard_vectors", side_effect=exhausted):
            _, _, events = self.build()
        self.assertEqual(0, events[-1]["shard_vectors_reused"])
        self.assertTrue(any(e.get("semantic_vector_reuse_reason") == "reuse_budget_exhausted" for e in events))
        self.assert_preserved()

    def test_failed_model_build_keeps_old_registered_generation_usable(self):
        self.change()
        self.runtime.embed.side_effect = RuntimeError("fixture model failure")
        with self.assertRaises(RuntimeError):
            self.build()
        self.assertEqual(self.first.identity, current_generation_ref(self.settings).identity)
        self.assert_preserved()
        self.runtime.embed.side_effect = self.embed
        _, _, events = self.build()
        self.assertGreater(events[-1]["shard_vectors_reused"], 0)
        self.assertFalse(set(self.calls) & self.inputs1)

    def test_publication_rollback_retry_recovers_from_registered_parent(self):
        self.change()
        with mock.patch("brain.catalog._write_current_projection", side_effect=OSError("fixture rollback")):
            with self.assertRaises(OSError):
                self.build()
        self.assertEqual(self.first.identity, current_generation_ref(self.settings).identity)
        self.assert_preserved()
        self.calls.clear()
        _, _, events = self.build()
        self.assertFalse(set(self.calls) & self.inputs1)  # unregistered G2-only cache entries may be evicted
        self.assertGreater(events[-1]["shard_vectors_reused"], 0)

    def test_pinned_ticket_and_gc_keep_parent_while_new_ticket_uses_rebuilt_generation(self):
        from brain.ops import gc

        start_session(self.settings, "TICKET-A", "Keep original generation")
        self.change()
        second, _, _ = self.build()
        start_session(self.settings, "TICKET-B", "Use new generation")
        self.assertEqual(self.first.generation, session_state(self.settings, "TICKET-A")["generation"])
        self.assertEqual(second.generation, session_state(self.settings, "TICKET-B")["generation"])
        report = gc(self.settings, dry_run=True, keep_recent=1)
        removed = {item["path"] for item in report["remove"]}
        self.assertFalse({str(path) for path in self.artifacts1} & removed)
        self.assert_preserved()
        (self.settings.runs_dir / "TICKET-A/session.json").unlink()  # remove the fixture pin, never real user state
        report = gc(self.settings, dry_run=True, keep_recent=1)
        old_a = next(s["path"] for s in self.state1["shards"] if s["repo"] == "a")
        self.assertIn(old_a, {item["path"] for item in report["remove"]})

    def test_ten_fifty_hundred_repos_restore_only_the_changed_shard(self):
        Index, _ = _usearch()
        old_inputs = set(self.inputs1)
        for count in (10, 50, 100):
            with self.subTest(repositories=count):
                while len(self.settings.repositories) < count:
                    name = f"repo_{len(self.settings.repositories):03d}"
                    directory = self.root / name
                    directory.mkdir()
                    (directory / "one.py").write_text("def one():\n    return 1\n", encoding="utf-8")
                    self.settings.repositories.append(Repository(name=name, path=directory))
                self.calls.clear()
                self.build()
                old_inputs.update(self.calls)
                self.calls.clear()
                path = self.root / "a/file_0.py"
                path.write_text(path.read_text().replace("return '", f"return '{count}_", 1), encoding="utf-8")
                with mock.patch.object(Index, "restore", wraps=Index.restore) as restore:
                    _, _, events = self.build()
                self.assertEqual(1, restore.call_count)
                self.assertEqual(count - 1, events[-1]["semantic_shards_reused"])
                self.assertEqual(1, events[-1]["semantic_shards_rebuilt"])
                self.assertFalse(old_inputs & set(self.calls))
                old_inputs.update(self.calls)

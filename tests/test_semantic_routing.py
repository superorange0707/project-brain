"""Native-vector regressions for global routing before bounded source search."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from contextlib import ExitStack, closing
from dataclasses import replace
from pathlib import Path
from unittest import mock

from brain.atlas import build_atlas
from brain.catalog import collect_generation_components, connect, current_generation_ref, publish_generation
from brain.core import load_settings, pack_context, parse_context_request, retrieve_context, snapshot_indexes
from brain.locks import workspace_operation
from brain.semantic import build_semantic_index, search_semantic, semantic_schema_version, _usearch
from brain.retrieval.models import RetrievalTrace


@unittest.skipUnless(importlib.util.find_spec("usearch"), "requires optional semantic extra")
class SemanticRoutingTest(unittest.TestCase):
    def workspace(self, count=20):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="brain-routing-")))
        config = "[project]\nname='routing'\n[graph]\nenabled=false\n[experience]\nenabled=false\n"
        for index in range(count):
            name = f"service-{index:03}"
            path = self.root / name
            path.mkdir()
            (path / "entry.py").write_text("def execute():\n    return 'retained'\n", encoding="utf-8")
            config += f"[[repositories]]\nname='{name}'\npath='{name}'\n"
        (self.root / "brain.toml").write_text(config, encoding="utf-8")
        self.settings = load_settings(self.root / "brain.toml")
        self.target = f"service-{count - 1:03}"
        self.manifest = {"pack_id": "routing-fixture", "embedding_dimension": 2, "test_only": True}
        self.runtime = mock.Mock()
        # Deliberately isolate scope selection from learned-model accuracy.
        self.runtime.embed.side_effect = lambda values, **kwargs: [
            [1.0, 0.0] if self.target in value or "Repository:" not in value else [0.0, 1.0]
            for value in values
        ]
        for target in ("brain.semantic.active_pack", "brain.semantic.verified_pack", "brain.models.verified_pack"):
            self.stack.enter_context(mock.patch(target, return_value=self.manifest))
        self.stack.enter_context(mock.patch("brain.semantic.runtime_for_pack", return_value=self.runtime))
        self.stack.enter_context(mock.patch("brain.semantic._check_pack_integrity"))
        self.stack.enter_context(mock.patch("brain.semantic.embedding_batch_size", return_value=16))
        self.stack.enter_context(workspace_operation(self.settings))
        self.generation = self.build()
        self.runtime.reset_mock()
        self.pinned = replace(self.settings, atlas_generation=self.generation, atlas_generation_mode="pinned")
        self.question = "Unhandled revocations after account changes"

    def build(self):
        indexed, _ = snapshot_indexes(self.settings, publish=False)
        payload = build_atlas(self.settings, indexed)
        build_semantic_index(self.settings, atlas_cards=payload["cards"])
        components = collect_generation_components(self.settings, indexed, atlas_payload=payload)
        self.assertEqual("ready", components["semantic"]["status"])
        publish_generation(self.settings, indexed, components=components, atlas_payload=payload)
        return current_generation_ref(self.settings)

    def retrieve(self):
        request = parse_context_request(json.dumps({"INVESTIGATION_REQUEST": {
            "version": 5, "mode": "root_cause", "objective": self.question,
        }}))
        with mock.patch("brain.editions.current_edition", return_value="semantic"):
            return retrieve_context(self.pinned, request)

    def test_global_repo_vectors_reach_evidence_outside_the_initial_sixteen(self):
        self.workspace()
        bundle = self.retrieve()
        self.assertIn(self.target, bundle.trace["semantic_repo_scope"])
        self.assertLessEqual(len(bundle.trace["semantic_repo_scope"]), self.settings.widen_repo_limit)
        self.assertTrue(any(item.repo == self.target for item in bundle.evidence))
        self.assertIn(self.target, pack_context(self.pinned, "GLOBAL-ROUTING", 1, bundle))
        self.assertEqual("ready", bundle.trace["semantic_status"])
        self.assertEqual(1, self.runtime.embed.call_count)
        self.assertEqual([self.question], self.runtime.embed.call_args.args[0])
        self.assertEqual("1:1:3:1:2", semantic_schema_version())

    def test_evicted_roots_recover_once_without_document_inference_or_new_schema(self):
        self.workspace()
        with closing(connect(self.settings)) as connection:
            schema = connection.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name").fetchall()
            connection.execute("DELETE FROM embedding_cache")  # Synthetic cache-eviction fixture only.
            connection.commit()
        Index, _ = _usearch()
        with mock.patch.object(Index, "restore", wraps=Index.restore) as restore:
            cold = self.retrieve()
        self.assertIn(self.target, cold.trace["semantic_repo_scope"])
        self.assertEqual(20 + self.settings.widen_repo_limit, restore.call_count)
        self.assertIn("semantic-repo-recovery", cold.trace["backend_ms"])
        self.question += " next question"
        with mock.patch.object(Index, "restore", wraps=Index.restore) as restore:
            warm = self.retrieve()
        self.assertEqual(self.settings.widen_repo_limit, restore.call_count)
        self.assertNotIn("semantic-repo-recovery", warm.trace["backend_ms"])
        self.assertEqual(2, self.runtime.embed.call_count)
        self.assertTrue(all(len(call.args[0]) == 1 and "Repository:" not in call.args[0][0]
                            for call in self.runtime.embed.call_args_list))
        with closing(connect(self.settings)) as connection:
            self.assertEqual(schema, connection.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name").fetchall())
            self.assertEqual(20, connection.execute("SELECT COUNT(*) FROM embedding_cache WHERE cache_key NOT LIKE 'query:%'").fetchone()[0])

    def test_ten_fifty_and_hundred_repos_keep_native_source_search_bounded(self):
        for count in (10, 50, 100):
            with self.subTest(repositories=count):
                self.workspace(count)
                Index, _ = _usearch()
                with mock.patch.object(Index, "restore", wraps=Index.restore) as restore:
                    bundle = self.retrieve()
                self.assertEqual(min(count, self.settings.widen_repo_limit), restore.call_count)
                self.assertIn(self.target, bundle.trace["semantic_repo_scope"])
                self.assertTrue(any(item.repo == self.target for item in bundle.evidence))
                self.assertLess(bundle.trace["physical_backend_operations"], 40)
                self.stack.close()

    def test_explicit_repo_scope_remains_a_hard_filter(self):
        self.workspace()
        trace = RetrievalTrace()
        rows = search_semantic(self.pinned, self.question, generation=self.generation,
                               repos={"service-000"}, repo_limit=1, trace=trace)
        self.assertTrue(rows)
        self.assertEqual({"service-000"}, {row["repo"] for row in rows})
        self.assertNotIn("semantic-repo-cache", trace.backend_ms)

    def test_routing_large_entry_metadata_only_loads_repo_card_inputs(self):
        from brain.semantic import _atlas_chunk, _repo_card_scores, _serving_state

        self.workspace(100)
        state = _serving_state(self.pinned, self.generation)
        # Metadata complexity probe: not a substitute for artifact validation.
        # Published state and artifacts stay untouched; only this private input
        # to the root-routing helper contains the extra non-root entries.
        shards = [dict(shard, entries=[*shard["entries"], *[{"kind": "function"}] * 995])
                  for shard in state["shards"]]
        Index, _ = _usearch()
        with mock.patch("brain.semantic._atlas_chunk", wraps=_atlas_chunk) as inputs, \
                mock.patch.object(Index, "restore", wraps=Index.restore) as restore:
            scores, complete = _repo_card_scores(
                self.pinned, shards, self.generation, [1.0, 0.0], manifest=self.manifest, state=state,
                backend=_usearch(), trace=RetrievalTrace(), reserve_operations=16,
            )
        self.assertTrue(complete)
        self.assertEqual(self.target, max(scores, key=scores.get))
        self.assertEqual(100, inputs.call_count)
        self.assertEqual(0, restore.call_count)
        self.runtime.embed.assert_not_called()

    def test_physical_budget_preserves_source_search_and_reports_actual_scope(self):
        self.workspace()
        with closing(connect(self.settings)) as connection:
            connection.execute("DELETE FROM embedding_cache")
            connection.commit()
        trace, status = RetrievalTrace(max_physical_backend_operations=3), {}
        rows = search_semantic(self.pinned, self.question, generation=self.generation, repo_limit=2,
                               trace=trace, serving_status=status)
        self.assertTrue(rows)
        self.assertLessEqual(trace.physical_backend_operations, 3)
        self.assertEqual(2, len(trace.semantic_repo_scope))
        self.assertEqual("degraded", status["status"])
        self.assertIn("semantic_repo_routing_incomplete", trace.fallback_reasons)

    def test_native_semantic_search_preserves_source_hydration_headroom(self):
        self.workspace()
        with closing(connect(self.settings)) as connection:
            connection.execute("DELETE FROM embedding_cache")  # Cold routing in this temporary fixture.
            connection.commit()
        for iteration, budget in enumerate((0, 1, 2, 3, 30, 30)):
            with self.subTest(budget=budget, iteration=iteration):
                trace = RetrievalTrace(max_physical_backend_operations=budget)
                protected = min(2, max(0, budget - 1))
                trace._set_backend_headroom(protected)
                status = {}
                rows = search_semantic(self.pinned, self.question, generation=self.generation,
                                       repo_limit=2, trace=trace, serving_status=status)
                used = trace.physical_backend_operations
                self.assertLessEqual(used, budget - protected)
                self.assertEqual(protected, trace._backend_headroom)
                self.assertEqual(bool(budget), bool(rows))
                if budget < 30:
                    self.assertEqual('degraded', status['status'])
                if iteration == 4:
                    self.assertIn('semantic-repo-recovery', trace.backend_ms)
                if iteration == 5:
                    self.assertNotIn('semantic-repo-recovery', trace.backend_ms)
                trace._set_backend_headroom(0)
                self.assertEqual(used, trace.physical_backend_operations)
                self.assertGreaterEqual(trace.physical_budget_remaining, protected)

    def test_old_generation_routes_old_roots_and_never_substitutes_new_shards(self):
        self.workspace()
        old_state = json.loads((self.settings.state_dir / "semantic-index.json").read_text())
        old_bytes = {Path(shard["path"]): Path(shard["path"]).read_bytes() for shard in old_state["shards"]}
        self.settings = replace(self.settings, repositories=[
            replace(repo, description="NEW_PRIMARY" if repo.name == "service-018" else "NEW_ROOT")
            if repo.name in {"service-018", "service-019"} else repo for repo in self.settings.repositories
        ])
        self.runtime.embed.side_effect = lambda values, **kwargs: [
            [1.0, 0.0] if "NEW_PRIMARY" in value or ("service-019" in value and "NEW_ROOT" not in value)
            or "Repository:" not in value else [0.0, 1.0] for value in values
        ]
        (self.root / "service-019/entry.py").write_text("def execute():\n    return 'NEW_GENERATION'\n", encoding="utf-8")
        second = self.build()
        self.assertNotEqual(self.generation.identity, second.identity)
        for path, content in old_bytes.items():
            self.assertEqual(content, path.read_bytes())
        # Stale compatibility projection must not replace either registration.
        (self.settings.state_dir / "semantic-index.json").write_text('{"stale":true}', encoding="utf-8")
        for generation, expected in ((self.generation, "service-019"), (second, "service-018"), (self.generation, "service-019")):
            trace = RetrievalTrace()
            rows = search_semantic(self.pinned, self.question, generation=generation, repo_limit=1, trace=trace)
            self.assertTrue(rows)
            self.assertEqual([expected], trace.semantic_repo_scope)
        old_shard = next(shard for shard in old_state["shards"] if shard["repo"] == "service-019")
        old_path = Path(old_shard["path"])
        content = old_path.read_bytes()
        old_path.write_bytes(b"X" + content[1:])
        status = {}
        self.assertEqual([], search_semantic(self.pinned, self.question, generation=self.generation,
                                             repo_limit=1, serving_status=status))
        self.assertEqual("unavailable", status["status"])

    def test_poisoned_card_and_cold_recovery_limits_are_explicitly_degraded(self):
        self.workspace()
        with closing(connect(self.settings)) as connection:
            connection.execute("UPDATE atlas_cards SET content='UNTRUSTED' WHERE level='repo' AND repo=?", (self.target,))
            connection.commit()
        status, trace = {}, RetrievalTrace()
        search_semantic(self.pinned, self.question, generation=self.generation, repo_limit=1,
                        serving_status=status, trace=trace)
        self.assertEqual("degraded", status["status"])
        self.assertIn("semantic_repo_routing_incomplete", trace.fallback_reasons)
        with closing(connect(self.settings)) as connection:
            connection.execute("DELETE FROM embedding_cache")
            connection.commit()
        with mock.patch("brain.semantic.SEMANTIC_ROUTING_RECOVERY_BYTES", 0):
            status, trace = {}, RetrievalTrace()
            rows = search_semantic(self.pinned, self.question, generation=self.generation, repo_limit=2,
                                   serving_status=status, trace=trace)
        self.assertTrue(rows)
        self.assertEqual("degraded", status["status"])
        self.assertIn("semantic_repo_routing_incomplete", trace.fallback_reasons)
        self.assertNotIn("semantic-repo-recovery", trace.backend_ms)

    def test_routing_cache_write_failure_keeps_recovered_vectors_usable(self):
        self.workspace()
        with closing(connect(self.settings)) as connection:
            connection.execute("DELETE FROM embedding_cache")
            connection.commit()
        with mock.patch("brain.semantic._reserve_embedding_cache", side_effect=RuntimeError("unavailable")):
            # Use a validated query vector so only optional root persistence fails.
            with mock.patch("brain.semantic._query_vector", return_value=[1.0, 0.0]):
                trace, status = RetrievalTrace(), {}
                rows = search_semantic(self.pinned, self.question, generation=self.generation,
                                       repo_limit=1, trace=trace, serving_status=status)
        self.assertEqual("ready", status["status"])
        self.assertEqual([self.target], trace.semantic_repo_scope)
        self.assertTrue(rows)
        self.assertIn("semantic_repo_cache_write_unavailable", trace.fallback_reasons)

    def test_cold_root_persistence_can_use_remaining_routing_budget(self):
        from brain.semantic import _reserve_embedding_cache
        import time

        self.workspace()
        with closing(connect(self.settings)) as connection:
            connection.execute("DELETE FROM embedding_cache")
            connection.commit()
        clock = [time.monotonic()]

        def reserve(connection, entries, **kwargs):
            # Model a large existing cache's capacity accounting, without adding
            # hundreds of MiB to every CI worker or relying on host speed.
            clock[0] += 0.1
            connection.execute(
                "WITH RECURSIVE ticks(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM ticks WHERE n<1000) SELECT SUM(n) FROM ticks"
            ).fetchone()
            return _reserve_embedding_cache(connection, entries, **kwargs)

        with mock.patch("brain.semantic.time.monotonic", side_effect=lambda: clock[0]), \
                mock.patch("brain.semantic._query_vector", return_value=[1.0, 0.0]), \
                mock.patch("brain.semantic._reserve_embedding_cache", side_effect=reserve):
            trace = RetrievalTrace()
            search_semantic(self.pinned, self.question, generation=self.generation, repo_limit=1, trace=trace)
        self.assertNotIn("semantic_repo_cache_write_unavailable", trace.fallback_reasons)
        with closing(connect(self.settings)) as connection:
            self.assertEqual(20, connection.execute("SELECT COUNT(*) FROM embedding_cache").fetchone()[0])

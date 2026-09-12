from __future__ import annotations

import tempfile
import time
import unittest
import json
import re
import shutil
import subprocess
from pathlib import Path
from unittest import mock

from brain.core import ContextBundle, Repository, SearchHit, load_settings, pack_context, snapshot_indexes
from brain.catalog import current_generation_ref
from brain.retrieval.ranker import fuse_and_rank


class OptimizationTests(unittest.TestCase):
    def test_verification_parses_each_source_once_per_context(self) -> None:
        from brain import investigation
        from brain.core import create_context, start_session

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "service"
            repo.mkdir()
            source = '@RestController\nclass Routes {\n' + "".join(
                f'  @GetMapping("/routes/{number}") String route{number}() {{ return "ok"; }}\n'
                for number in range(16)
            ) + "}\n/*" + " documentation" * 3000 + "*/\n"
            (repo / "Routes.java").write_text(source, encoding="utf-8")
            config = root / "brain.toml"
            config.write_text("[project]\nname='verification-reuse'\n[graph]\nenabled=false\n"
                              "[[repositories]]\nname='service'\npath='service'\n", encoding="utf-8")
            settings = load_settings(config)
            snapshot_indexes(settings)
            start_session(settings, "VERIFY-101", "Inspect the local routes")
            request = {"version": 5, "mode": "flow_trace", "objective": "Inspect Routes",
                       "anchors": [{"kind": "endpoint", "value": f"/routes/{number}"} for number in range(16)],
                       "files": [{"repo": "service", "path": "Routes.java"}]}
            with mock.patch.object(investigation, "_java_file_intelligence", wraps=investigation._java_file_intelligence) as parsed, \
                    mock.patch.object(investigation, "_mask_java_comments_uncached", wraps=investigation._mask_java_comments_uncached) as masked:
                content, _, _ = create_context(settings, "VERIFY-101", json.dumps({"INVESTIGATION_REQUEST": request}))
            for number in range(16):
                self.assertIn(f'@GetMapping("/routes/{number}")', content)
            self.assertEqual(1, sum(call.args[2] == "verification" for call in parsed.call_args_list))
            self.assertEqual(2, sum(call.args[0] == source for call in masked.call_args_list))
            self.assertIsNone(investigation._SOURCE_VERIFICATION_CACHE.get())

    def test_verification_reuse_preserves_authority_and_source_identity(self) -> None:
        from dataclasses import replace
        from brain import investigation
        from brain.catalog import AtlasGenerationRef
        from brain.core import Evidence

        source = '@RestController\n@RequestMapping("/api")\nclass Routes {\n@GetMapping("/old") String route() { return "ok"; }\n}\n'
        evidence = Evidence("repo", "Routes.java", 1, 5, source, "code", 100, verification_content=source)
        generation = AtlasGenerationRef(1, "g1", None, "s1", {"repo": "snapshot"}, {}, {})
        bundle = ContextBundle("verify", evidence=[evidence], atlas_generation=generation)

        def verified(value="/api/old", **changes):
            selected = replace(bundle, **changes)
            return investigation._verified_value_location(selected, "repo", "Routes.java", 4, value, kind="endpoint")

        with investigation.source_verification_scope():
            self.assertTrue(verified())
            self.assertFalse(verified(atlas_generation=None))
            self.assertFalse(verified(atlas_generation=replace(generation, snapshots={})))
            for kind in ("knowledge", "local diff", "user-supplied external evidence"):
                self.assertFalse(verified(evidence=[replace(evidence, kind=kind)]))
            self.assertFalse(verified(evidence=[replace(evidence, line_end=3)]))
            self.assertFalse(verified(evidence=[replace(evidence, line_start=4, content=source.splitlines()[3], verification_content=None)]))
            changed = source.replace("/old", "/new")
            updated = replace(evidence, content=changed, verification_content=changed)
            self.assertFalse(verified(evidence=[updated]))
            self.assertTrue(verified("/api/new", evidence=[updated]))
            self.assertTrue(verified(), "another source at the same path cannot replace the pinned input")

    def test_verification_cache_is_byte_bounded_and_cleared_on_failure(self) -> None:
        from brain import investigation

        sources = ['// comment\nString s = "https://example/\\\""; /* hidden */\n',
                   'String text = """\n/* literal */\n"""; // comment\n', '/* 界 */ int n;\n']
        with self.assertRaisesRegex(RuntimeError, "expected"), investigation.source_verification_scope():
            cached = investigation._SOURCE_VERIFICATION_CACHE.get()
            with investigation.source_verification_scope():
                self.assertIs(cached, investigation._SOURCE_VERIFICATION_CACHE.get())
                for source in sources:
                    for strings in (False, True):
                        self.assertEqual(investigation._mask_java_comments_uncached(source, strings=strings),
                                         investigation._mask_java_comments(source, strings=strings))
            for number in range(30):
                investigation._mask_java_comments(f"/*{number}*/ class Test {{}}")
                cached[1]("repo", f"Test{number}.java", "class Test {}")
            self.assertEqual(16, cached[0].cache_info().currsize)
            self.assertEqual(8, cached[1].cache_info().currsize)
            with mock.patch.object(investigation, "MAX_REFRESH_FILE_BYTES", 16):
                for source in ("/*" + "a" * 17 + "*/", "/*" + "界" * 5 + "*/"):
                    before = cached[0].cache_info()
                    self.assertFalse(investigation._cacheable_verification_source(source))
                    self.assertEqual(investigation._mask_java_comments_uncached(source), investigation._mask_java_comments(source))
                    self.assertEqual(before, cached[0].cache_info())
            raise RuntimeError("expected")
        self.assertIsNone(investigation._SOURCE_VERIFICATION_CACHE.get())
        self.assertEqual([0, 0], [item.cache_info().currsize for item in cached])
        with investigation.source_verification_scope():
            fresh = investigation._SOURCE_VERIFICATION_CACHE.get()
            self.assertIsNot(cached, fresh)
            self.assertEqual([0, 0], [item.cache_info().currsize for item in fresh])

    def test_verification_scopes_are_thread_local(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from brain import investigation

        barrier = Barrier(2)

        def verify():
            with investigation.source_verification_scope():
                cached = investigation._SOURCE_VERIFICATION_CACHE.get()
                investigation._mask_java_comments("/*shared text*/ class Test {}")
                barrier.wait(timeout=5)
                self.assertEqual(1, cached[0].cache_info().misses)
            self.assertIsNone(investigation._SOURCE_VERIFICATION_CACHE.get())
            self.assertEqual(0, cached[0].cache_info().currsize)
            return cached

        with ThreadPoolExecutor(max_workers=2) as executor:
            first, second = list(executor.map(lambda _: verify(), range(2)))
        self.assertIsNot(first, second)

    def test_dense_atlas_call_ownership_avoids_repeated_definition_and_prefix_scans(self) -> None:
        import hashlib
        from brain import atlas

        source = "class Outer {\n  void helper() {}\n  class Inner {\n    void helper() {}\n"
        source += "    void nested() { this.helper(); peer.helper(); }\n  }\n"
        source += "".join(f"  void action{number}() {{ this.helper(); other.missing(); }}\n" for number in range(500))
        source += "}\nclass Sibling { void helper() {} void run() { this.helper(); } }\n"
        accesses = [0]

        class CountedEntity(dict):
            def __getitem__(self, key):
                if key in {"line_start", "line_end"}:
                    accesses[0] += 1
                return super().__getitem__(key)

        extract = atlas._java_entities
        search = re.search
        receiver_bytes = []

        def entities(*args, **kwargs):
            return [CountedEntity(item) for item in extract(*args, **kwargs)]

        def observe_search(pattern, value, *args, **kwargs):
            if pattern == r"([A-Za-z_$][\w$]*)\.$":
                receiver_bytes.append(len(value))
            return search(pattern, value, *args, **kwargs)

        with mock.patch("brain.atlas._java_entities", side_effect=entities), \
                mock.patch("brain.atlas.re.search", side_effect=observe_search), \
                mock.patch("brain.atlas.time.monotonic", return_value=0):
            payload = atlas._file_intelligence("repo", "src/Outer.java", "blob", source)
        # Pin the complete pre-optimization payload, including ambiguous and
        # nested-class dispatch; performance must not change evidence identity.
        self.assertEqual("1b25b361a50c333f10e914bbe19bb5fbf0474644ba6642703507ed6f37ff490b",
                         hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest())
        self.assertLess(accesses[0], 50_000, "ownership lookup must not multiply definitions by calls")
        self.assertTrue(receiver_bytes)
        self.assertLessEqual(max(receiver_bytes), len("other."), "receiver parsing must not scan the file prefix")

    def test_slow_precision_keeps_verified_source_without_starting_later_reads(self) -> None:
        from dataclasses import replace
        from brain.core import parse_context_request, retrieve_context
        from brain.index import read_generation_files

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "repo").mkdir()
            for name in ("A", "B", "C", "Requested"):
                (root / f"repo/{name}.java").write_text(
                    f"class {name} {{\n  void execute() {{\n    original{name}(); // " + "界" * 2_000 + "\n  }\n}\n",
                    encoding="utf-8")
            config = root / "brain.toml"
            config.write_text("[project]\nname='slow-precision'\n[graph]\nenabled=false\n[experience]\nenabled=false\n"
                              "[[repositories]]\nname='repo'\npath='repo'\n", encoding="utf-8")
            settings = load_settings(config)
            snapshot_indexes(settings)
            pinned = replace(settings, atlas_generation=current_generation_ref(settings), atlas_generation_mode="pinned",
                             repositories=[replace(repo) for repo in settings.repositories])
            for path in (root / "repo").glob("*.java"):
                path.write_text("NEWER_GENERATION_IS_NOT_EVIDENCE\n", encoding="utf-8")
            snapshot_indexes(settings)
            self.assertNotEqual(pinned.atlas_generation.identity, current_generation_ref(settings).identity)

            for protected, direct, hydrate_limit, context_bytes in (
                (False, False, 18, 180_000), (True, False, 18, 180_000), (True, True, 18, 180_000),
                (False, False, 1, 180_000), (False, False, 18, 20_000),
            ):
                with self.subTest(protected=protected, direct=direct, hydrate_limit=hydrate_limit, context_bytes=context_bytes):
                    serving = replace(pinned, hydrate_limit=hydrate_limit, hard_context_chars=context_bytes)
                    clock = [0.0]
                    request = parse_context_request(json.dumps({"INVESTIGATION_REQUEST": {
                        "version": 5, "mode": "root_cause", "objective": "Explain the refusal path",
                        "resolve": ["review-marker"],
                        "files": [{"repo": "repo", "path": "Requested.java"}] if direct else [],
                    }}))
                    route = {"candidates": [{"repo": "repo", "path": "A.java", "line": 2,
                                             "kind": "definition", "score": 100,
                                             "found_by": ["Atlas hierarchical router"]}]} if protected else {}
                    paths = ["B.java", "C.java"] if protected else ["A.java", "B.java", "C.java"]

                    def semantic(*args, **kwargs):
                        kwargs["serving_status"]["status"] = "ready"
                        return [{"repo": "repo", "path": path, "line": 2, "symbol": "execute",
                                 "kind": "method", "score": 0.9} for path in paths]

                    def rerank(query, documents, instruction=""):
                        clock[0] = 20.0  # The optional call crosses the 10-second query deadline.
                        return [0.5] * len(documents)

                    runtime = mock.Mock()
                    runtime.rerank.side_effect = rerank
                    with mock.patch("brain.core.time.perf_counter", side_effect=lambda: clock[0]), \
                            mock.patch("brain.editions.current_edition", return_value="precision"), \
                            mock.patch("brain.atlas.route", return_value=route), \
                            mock.patch("brain.semantic.search_semantic", side_effect=semantic), \
                            mock.patch("brain.models.active_pack", return_value={"pack_id": "reranker"}), \
                            mock.patch("brain.models.runtime_for_pack", return_value=runtime), \
                            mock.patch("brain.index.read_generation_files", wraps=read_generation_files) as reads:
                        bundle = retrieve_context(serving, request)
                    delivered_paths = paths[:1] if hydrate_limit == 1 or context_bytes == 20_000 else paths
                    expected = set(delivered_paths) | ({"Requested.java"} if direct else set())
                    self.assertEqual(expected, {item.path for item in bundle.evidence})
                    self.assertEqual(1, reads.call_count, "a slow model must not start a second optional source batch")
                    self.assertEqual(set(paths), {path for _, path in reads.call_args.args[2]})
                    self.assertEqual("context_budget" if context_bytes == 20_000 else "time_budget", bundle.trace["stop_reason"])
                    self.assertEqual(pinned.atlas_generation.identity, bundle.atlas_generation.identity)
                    self.assertEqual({"A.java", "B.java", "C.java"} - expected, {item.path for item in bundle.additional_candidates})
                    self.assertLessEqual(sum(len(item.content.encode("utf-8")) for item in bundle.evidence),
                                         max(10_000, context_bytes - 40_000))
                    delivered = pack_context(serving, "SLOW-PRECISION", 1, bundle)
                    for path in delivered_paths:
                        self.assertIn("original" + Path(path).stem + "()", delivered)
                    self.assertNotIn("NEWER_GENERATION_IS_NOT_EVIDENCE", delivered)
                    self.assertLessEqual(len(delivered.encode("utf-8")), context_bytes)
                    runtime.shutdown.assert_called_once()

    def test_precision_reranks_semantic_candidates_from_pinned_code_not_only_names(self) -> None:
        from dataclasses import replace
        from brain.core import parse_context_request, retrieve_context
        from brain.index import read_generation_files

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "repo").mkdir()
            (root / "repo/A.java").write_text(
                "class A {\n  void execute() {\n    submit();\n  }\n}\n", encoding="utf-8")
            (root / "repo/B.java").write_text(
                "class B {\n  void execute() {\n    if (requiresSecondSignature()) {\n      reject();\n    }\n    submit();\n  }\n}\n", encoding="utf-8")
            config = root / "brain.toml"
            config.write_text("[project]\nname='precision-source'\n[graph]\nenabled=false\n[experience]\nenabled=false\n"
                              "[[repositories]]\nname='repo'\npath='repo'\n", encoding="utf-8")
            settings = load_settings(config)
            snapshot_indexes(settings)
            pinned = replace(settings, atlas_generation=current_generation_ref(settings),
                             atlas_generation_mode="pinned", hydrate_limit=1)
            (root / "repo/B.java").write_text("CURRENT_CHECKOUT_IS_NOT_EVIDENCE\n", encoding="utf-8")
            snapshot_indexes(settings)
            self.assertNotEqual(pinned.atlas_generation.identity, current_generation_ref(settings).identity)
            request = parse_context_request(json.dumps({"INVESTIGATION_REQUEST": {
                "version": 5, "mode": "root_cause", "objective": "Which route refuses unapproved transfers?",
            }}))

            def semantic(*args, **kwargs):
                kwargs["serving_status"]["status"] = "ready"
                return [{"repo": "repo", "path": path, "line": 2, "symbol": "execute", "kind": "method", "score": score}
                        for path, score in (("A.java", 0.95), ("B.java", 0.94))]

            runtime = mock.Mock()
            runtime.rerank.side_effect = lambda query, documents, instruction="": [
                float("requiresSecondSignature()" in document) for document in documents
            ]
            with mock.patch("brain.editions.current_edition", return_value="precision"), \
                    mock.patch("brain.atlas.route", return_value={}), \
                    mock.patch("brain.semantic.search_semantic", side_effect=semantic), \
                    mock.patch("brain.models.active_pack", return_value={"pack_id": "reranker"}), \
                    mock.patch("brain.models.runtime_for_pack", return_value=runtime), \
                    mock.patch("brain.index.read_generation_files", wraps=read_generation_files) as reads:
                # Ablate only previews to reproduce the former label-only input.
                with mock.patch("brain.query.rerank_snippets", side_effect=lambda settings, hits, positions, cache, **kwargs: {
                    index: hits[index].text for index in positions
                }):
                    before = retrieve_context(pinned, request)
                self.assertEqual(["A.java"], [item.path for item in before.evidence])
                self.assertFalse(any("requiresSecondSignature()" in document for document in runtime.rerank.call_args.args[1]))
                runtime.reset_mock()
                reads.reset_mock()
                bundle = retrieve_context(pinned, request)
            documents = runtime.rerank.call_args.args[1]
            self.assertTrue(any("requiresSecondSignature()" in document for document in documents))
            self.assertFalse(any("CURRENT_CHECKOUT" in document for document in documents))
            self.assertEqual(["B.java"], [item.path for item in bundle.evidence])
            self.assertIn("requiresSecondSignature()", bundle.evidence[0].content)
            self.assertEqual(1, reads.call_count, "final hydration should reuse the verified preview source")
            runtime.shutdown.assert_called_once()

    def test_rerank_source_previews_are_bounded_and_never_hold_the_model_lane(self) -> None:
        from dataclasses import replace
        from brain.models import rerank_candidates
        from brain.retrieval.models import RetrievalTrace
        from brain.index import read_generation_files

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "repo").mkdir()
            for number in range(6):
                (root / f"repo/file{number}.py").write_text(
                    "def execute():\n    return '" + "界" * 2_000 + "'\n", encoding="utf-8")
            config = root / "brain.toml"
            config.write_text("[project]\nname='preview-bounds'\n[graph]\nenabled=false\n"
                              "[[repositories]]\nname='repo'\npath='repo'\n", encoding="utf-8")
            settings = load_settings(config)
            snapshot_indexes(settings)
            pinned = replace(settings, atlas_generation=current_generation_ref(settings), atlas_generation_mode="pinned")
            hits = [SearchHit("repo", f"file{number}.py", 1, "execute", "semantic candidate", 90,
                              ["local semantic index"]) for number in range(6)]
            events = []

            def read(*args, **kwargs):
                self.assertEqual([], events)
                return read_generation_files(*args, **kwargs)

            runtime = mock.Mock()
            runtime.rerank.return_value = [0.0, 0.1, 0.2]
            trace, cache = RetrievalTrace(), {}
            with mock.patch("brain.models.active_pack", return_value={"pack_id": "reranker"}), \
                    mock.patch("brain.models._reranker_tuning", return_value=(3, 3)), \
                    mock.patch("brain.models.runtime_for_pack", return_value=runtime) as start, \
                    mock.patch("brain.index.read_generation_files", side_effect=read) as reads, \
                    mock.patch("brain.models.model_lane") as lane:
                lane.return_value.__enter__.side_effect = lambda: events.append("lane")
                rerank_candidates(pinned, "question", hits, trace=trace, _source_cache=cache)
            self.assertEqual(["lane"], events)
            self.assertEqual(1, start.call_count)
            self.assertEqual(1, reads.call_count)
            self.assertEqual(3, len(reads.call_args.args[2]))
            self.assertEqual(8 * 1024 * 1024, reads.call_args.kwargs["max_bytes"])
            self.assertLessEqual(reads.call_args.kwargs["max_seconds"], 0.25)
            self.assertEqual(3, len(cache))
            self.assertEqual(1, trace.physical_backend_operations)
            self.assertEqual(3, trace.rerank_input_count)
            for document in runtime.rerank.call_args.args[1]:
                self.assertLessEqual(len(document.split("Snippet: ", 1)[1].encode("utf-8")), 1_200)
            self.assertEqual(["execute"] * 6, [hit.text for hit in hits])

            runtime.reset_mock()
            trace = RetrievalTrace()
            with mock.patch("brain.index.read_generation_files", return_value=None), \
                    mock.patch("brain.models.model_lane") as lane:
                rerank_candidates(pinned, "question", hits, runtime=runtime, trace=trace, _source_cache={})
            lane.assert_not_called()
            runtime.rerank.assert_not_called()
            self.assertEqual(0, trace.rerank_input_count)
            self.assertIn("rerank_source_preview_incomplete", trace.fallback_reasons)

    def test_rerank_preview_reads_shared_file_once_and_keeps_path_safety(self) -> None:
        from dataclasses import replace
        from brain.query import rerank_snippets
        from brain.retrieval.models import RetrievalTrace

        class CountedSource(str):
            splits = 0

            def splitlines(self, *args, **kwargs):
                self.splits += 1
                return super().splitlines(*args, **kwargs)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "repo").mkdir()
            (root / "repo/methods.py").write_text("def execute():\n    return 1\n", encoding="utf-8")
            config = root / "brain.toml"
            config.write_text("[project]\nname='shared-preview'\n[graph]\nenabled=false\n"
                              "[[repositories]]\nname='repo'\npath='repo'\n", encoding="utf-8")
            settings = load_settings(config)
            snapshot_indexes(settings)
            pinned = replace(settings, atlas_generation=current_generation_ref(settings), atlas_generation_mode="pinned")
            source = CountedSource("def execute():\n    return 'original'\n" * 40)
            hits = [SearchHit("repo", "methods.py", number + 1, "execute", "semantic candidate", 90,
                              ["local semantic index"]) for number in range(40)]
            hits += [SearchHit("repo", path, 1, "label", "semantic candidate", 90, ["local semantic index"])
                     for path in (".env", "../outside.py")]
            hits += [SearchHit("repo", "observed.py", 1, "observed original clues", "code", 90, ["sqlite trigram index"])]
            cache = {}
            with mock.patch("brain.index.read_generation_files", return_value={("repo", "methods.py"): source}) as reads:
                snippets = rerank_snippets(pinned, hits, list(range(len(hits))), cache, trace=RetrievalTrace())
            self.assertEqual([("repo", "methods.py")], reads.call_args.args[2])
            self.assertEqual(1, source.splits)
            self.assertEqual(41, len(snippets))
            self.assertEqual("observed original clues", snippets[42])
            self.assertNotIn(40, snippets)
            self.assertNotIn(41, snippets)

    def test_reranker_receives_distinct_local_clues_not_only_a_symbol_label(self) -> None:
        from brain.models import rerank_candidates
        from brain.query import prune_candidates

        hits = [
            SearchHit("repo", "policy.py", 40, "invoice_scope = record.customer", score=95,
                      found_by=["sqlite trigram index", "lexical anchor 1"]),
            SearchHit("repo", "policy.py", 41, "invoice_note = '" + "客户" * 1_200, score=96,
                      found_by=["sqlite trigram index", "lexical anchor 1"]),
            SearchHit("repo", "policy.py", 42, "invoice_audit = record.reference", score=96,
                      found_by=["sqlite trigram index", "lexical anchor 1"]),
            SearchHit("repo", "policy.py", 60, "if cancellation_requested: require_reversal()", score=95,
                      found_by=["sqlite trigram index", "lexical anchor 2"]),
            SearchHit("repo", "policy.py", 60, "Policy.cancel", "semantic candidate", 100,
                      ["local semantic index"]),
        ]
        original = [hit.text for hit in hits]
        fused = fuse_and_rank(hits[-2:])
        self.assertIn("require_reversal()", fused[0].text)
        candidates, _ = prune_candidates(mock.Mock(source_window_lines=150), hits, 20)
        self.assertEqual(1, len(candidates))
        self.assertIn("invoice", candidates[0].text)
        self.assertIn("cancellation_requested", candidates[0].text)
        self.assertLessEqual(len(candidates[0].text.encode("utf-8")), 1_200)
        self.assertEqual(original, [hit.text for hit in hits])
        runtime = mock.Mock()
        runtime.rerank.return_value = [1.0]
        with mock.patch("brain.models.model_lane"):
            rerank_candidates(mock.Mock(), "Explain invoice cancellation", candidates, runtime=runtime)
        documents = runtime.rerank.call_args.args[1]
        self.assertIn("require_reversal()", documents[0])
        self.assertIn("invoice", documents[0])

    def test_large_file_matches_survive_region_merging_and_pinned_delivery(self) -> None:
        from dataclasses import replace
        from brain.core import parse_context_request, retrieve_context

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "repo").mkdir()
            source = ["# unrelated source"] * 600
            source[39] = "invoice_policy = 1"
            source[129] = "if cancellation_requested: raise RuntimeError('reversal required')"
            path = root / "repo/policy.py"
            path.write_text("\n".join(source) + "\n", encoding="utf-8")
            config = root / "brain.toml"
            config.write_text("[project]\nname='large-source-quality'\n[graph]\nenabled=false\n[[repositories]]\nname='repo'\npath='repo'\n", encoding="utf-8")
            settings = load_settings(config)
            snapshot_indexes(settings)
            pinned = replace(settings, atlas_generation=current_generation_ref(settings), atlas_generation_mode="pinned")
            path.write_text("LATEST_SOURCE_MUST_NOT_APPEAR\n", encoding="utf-8")
            request = parse_context_request(json.dumps({"INVESTIGATION_REQUEST": {
                "version": 5, "mode": "root_cause", "objective": "invoice cancellation",
            }}))
            bundle = retrieve_context(pinned, request)
            delivered = "\n".join(item.content for item in bundle.evidence)
            self.assertIn("invoice_policy = 1", delivered)
            self.assertIn("reversal required", delivered)
            self.assertNotIn("LATEST_SOURCE_MUST_NOT_APPEAR", delivered)
            self.assertIn("reversal required", pack_context(pinned, "QUALITY-LARGE", 1, bundle))
            limited = retrieve_context(replace(pinned, hydrate_limit=1), request)
            self.assertTrue(
                any(item.line_start <= 130 <= item.line_end for item in limited.evidence)
                or any(item.path == "policy.py" and item.line == 130 for item in limited.additional_candidates),
                "A source match outside the delivered window must remain requestable, not disappear during merging",
            )

    def test_merged_candidate_windows_cover_all_original_matching_lines(self) -> None:
        import random
        from brain.query import _candidate_regions

        settings = mock.Mock(source_window_lines=150)
        rng = random.Random(2026)
        for positions in ([10, 80, 150], *[sorted(rng.sample(range(1, 1_000), 20)) for _ in range(50)]):
            hits = [SearchHit("repo", "large.py", line, f"line {line}", score=40 + index,
                              found_by=["sqlite trigram index"]) for index, line in enumerate(positions)]
            merged = _candidate_regions(settings, hits)
            for hit in hits:
                self.assertTrue(any(abs(hit.line - region.line) <= 75 for region in merged),
                                (hit.line, [region.line for region in merged]))

    def test_literal_batch_precedes_native_shards_and_preserves_fallback(self) -> None:
        from dataclasses import replace
        from brain.core import _ACTIVE_RETRIEVAL_CACHE, _ACTIVE_RETRIEVAL_TRACE, search
        from brain.retrieval.models import RetrievalTrace

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "repo").mkdir()
            (root / "repo/policy.py").write_text("invoice = 1\n", encoding="utf-8")
            config = root / "brain.toml"
            config.write_text("[project]\nname='literal-batch'\n[graph]\nenabled=false\n[[repositories]]\nname='repo'\npath='repo'\n", encoding="utf-8")
            settings = load_settings(config)
            snapshot_indexes(settings)
            generation = current_generation_ref(settings)
            generation = replace(generation, components={**generation.components, "zoekt": {
                "status": "ready", "details": {"shards": [{"repo": "repo", "snapshot": settings.repo("repo").source_sha, "manifest_hash": "pinned-shard"}]},
            }})
            settings = replace(settings, atlas_generation=generation, atlas_generation_mode="pinned")
            trace = RetrievalTrace()
            trace_token = _ACTIVE_RETRIEVAL_TRACE.set(trace)
            cache_token = _ACTIVE_RETRIEVAL_CACHE.set({})
            try:
                with mock.patch("brain.backends.zoekt.search", side_effect=AssertionError("literal query fanned out")):
                    self.assertEqual(["policy.py"], [hit.path for hit in search(settings, "invoice", fixed=True)])
                    self.assertTrue(search(settings, "invoice"))
                    self.assertEqual([], search(settings, "nonexistent", fixed=True))
                    self.assertEqual([], search(settings, "nonexistent"))
                self.assertEqual(2, trace.physical_backend_operations)
                self.assertEqual(0, trace.subprocess_count)
            finally:
                _ACTIVE_RETRIEVAL_CACHE.reset(cache_token)
                _ACTIVE_RETRIEVAL_TRACE.reset(trace_token)
            native_result = ([('policy.py', 1, 'invoice = 1', 1.0)], {"elapsed_ms": 0, "raw_hits": 1})
            with mock.patch("brain.index.query_generation_indexes", return_value=None), mock.patch(
                "brain.backends.zoekt.search", return_value=native_result,
            ) as native:
                self.assertTrue(search(settings, "invoice", fixed=True))
                self.assertTrue(search(settings, "invoic[e]"))
                self.assertEqual(2, native.call_count)
                self.assertEqual([True, False], [call.kwargs["fixed"] for call in native.call_args_list])
                self.assertTrue(all(call.kwargs["expected_manifest_hash"] == "pinned-shard" for call in native.call_args_list))

    def test_joint_query_ranking_is_bounded_local_and_cache_isolated(self) -> None:
        from brain.core import _ACTIVE_RETRIEVAL_CACHE, _cached_hits, _store_hits
        from brain.query import _candidate_regions

        settings = mock.Mock(source_window_lines=150)
        base = SearchHit("repo", "policy.py", 10, "policy", score=95, found_by=["sqlite trigram index"])
        baseline = _candidate_regions(settings, [base])[0].score
        for anchors, bonus in (([1, 1], 0), ([1, 2], 5), (list(range(20)), 20)):
            hits = [SearchHit(base.repo, base.path, base.line, base.text, score=95,
                              found_by=[*base.found_by, f"lexical anchor {anchor}"]) for anchor in anchors]
            self.assertAlmostEqual(baseline + bonus, _candidate_regions(settings, hits)[0].score, places=3)
        far = [SearchHit("repo", "policy.py", line, "policy", score=95,
                         found_by=["sqlite trigram index", f"lexical anchor {anchor}"])
               for line, anchor in ((10, 1), (1_000, 2))]
        regions = _candidate_regions(settings, far)
        self.assertEqual(2, len(regions))
        self.assertEqual([hit.score for hit in fuse_and_rank(far)], [hit.score for hit in regions])
        token = _ACTIVE_RETRIEVAL_CACHE.set({})
        try:
            _store_hits(("query",), [base])
            cached = _cached_hits(("query",))
            cached[0].found_by.append("lexical anchor 1")
            _candidate_regions(settings, cached)
            self.assertEqual(["sqlite trigram index"], _cached_hits(("query",))[0].found_by)
            self.assertEqual(95, base.score)
        finally:
            _ACTIVE_RETRIEVAL_CACHE.reset(token)

    def test_hydration_preserves_requested_definitions_after_optional_ranking(self) -> None:
        from brain.query import prune_candidates, select_candidates

        settings = mock.Mock(source_window_lines=150, candidate_limit=20, hydrate_limit=1,
                             max_regions_per_file=2, max_regions_per_repo=8)
        hits = [SearchHit("repo", "policy.py", 1, "class InvoicePolicy:", "definition", 100, ["symbol"]),
                SearchHit("repo", "noise.py", 1, "invoice notes", "code", 95,
                          ["sqlite trigram index", *(f"lexical anchor {number}" for number in range(8))])]
        candidates, _ = prune_candidates(settings, hits, 20)
        for hit in candidates:
            if hit.path == "noise.py":
                hit.score += 20  # The optional reranker can add a bounded bonus too.
        selected, omitted = select_candidates(settings, candidates, already_fused=True)
        self.assertEqual(["policy.py"], [hit.path for hit in selected])
        self.assertEqual(["noise.py"], [hit.path for hit in omitted])

    def test_reranker_skips_protected_only_work_and_flat_scores_add_no_relevance(self) -> None:
        from brain.models import rerank_candidates
        from brain.retrieval.models import RetrievalTrace

        protected = [SearchHit("repo", "policy.py", 1, "policy", "definition", 100),
                     SearchHit("repo", "read.py", 1, "requested", "requested file", 100),
                     SearchHit("repo", "known.py", 1, "known", "verified path", 100)]
        with mock.patch("brain.models.model_lane") as lane, mock.patch("brain.models.active_pack", return_value=None) as pack:
            self.assertIs(protected, rerank_candidates(mock.Mock(), "policy", protected, trace=RetrievalTrace()))
            lane.assert_not_called()
            pack.assert_not_called()
        hits = [SearchHit("repo", f"file{index}.py", 1, "policy", score=90 + index) for index in range(3)]
        runtime = mock.Mock()
        runtime.rerank.return_value = [0.0, 0.0]
        with mock.patch("brain.models.model_lane"):
            rerank_candidates(mock.Mock(), "policy", hits, runtime=runtime, limit=2)
        self.assertEqual([90, 91, 92], [hit.score for hit in hits])
        runtime.shutdown.assert_not_called()  # Injected runtimes remain caller-owned.

    def test_compound_evidence_quality_at_repository_scale(self) -> None:
        from dataclasses import replace
        from brain.core import parse_context_request, retrieve_context

        for count in (10, 50, 100):
            with self.subTest(repositories=count), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config_text = "[project]\nname='compound-scale'\n[graph]\nenabled=false\n"
                for number in range(count):
                    name = f"service-{number:03}"
                    repo = root / name
                    repo.mkdir()
                    for index in range(12):
                        (repo / f"audit_{index:02}.py").write_text(
                            "def audit_invoice(invoice):\n    return invoice.reference\n", encoding="utf-8",
                        )
                    config_text += f"[[repositories]]\nname='{name}'\npath='{name}'\n"
                (repo / "policy.py").write_text(
                    "def invoice_policy(record):\n    # cancellation requires a reversal event\n    return record.cancelled\n", encoding="utf-8",
                )
                config = root / "brain.toml"
                config.write_text(config_text, encoding="utf-8")
                settings = load_settings(config)
                snapshot_indexes(settings)
                settings = replace(settings, atlas_generation=current_generation_ref(settings), atlas_generation_mode="pinned", hydrate_limit=1)
                request = parse_context_request(json.dumps({"INVESTIGATION_REQUEST": {
                    "version": 5, "mode": "root_cause", "objective": "Explain invoice cancellation",
                }}))
                for warm in (False, True):
                    with self.subTest(warm=warm):
                        bundle = retrieve_context(settings, request)
                        self.assertEqual([(name, "policy.py")], [(item.repo, item.path) for item in bundle.evidence])
                        self.assertIn("return record.cancelled", bundle.evidence[0].content)
                        self.assertLessEqual(bundle.trace["physical_backend_operations"], 8)

    def test_compound_query_prioritizes_joint_evidence_over_single_term_noise(self) -> None:
        from brain.core import parse_context_request, retrieve_context

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "billing"
            repo.mkdir()
            for index in range(24):
                (repo / f"audit_{index:02}.py").write_text(
                    "def audit_invoice(invoice):\n    return invoice.reference\n", encoding="utf-8",
                )
            (repo / "z_policy.py").write_text(
                "def invoice_policy(record):\n    # cancellation requires a reversal event\n    return record.cancelled\n", encoding="utf-8",
            )
            config = root / "brain.toml"
            config.write_text("[project]\nname='compound-quality'\n[graph]\nenabled=false\n[[repositories]]\nname='billing'\npath='billing'\n", encoding="utf-8")
            settings = load_settings(config)
            snapshot_indexes(settings)
            settings.hydrate_limit = 1
            request = parse_context_request(json.dumps({"INVESTIGATION_REQUEST": {
                "version": 5, "mode": "root_cause", "objective": "invoice cancellation",
            }}))
            # Isolate lexical ordering; the full pipeline must retain the same
            # exact implementation even when no Atlas candidate rescues it.
            for isolate_lexical in (True, False):
                with self.subTest(isolate_lexical=isolate_lexical):
                    if isolate_lexical:
                        with mock.patch("brain.atlas.route", return_value={"repos": ["billing"]}):
                            bundle = retrieve_context(settings, request)
                    else:
                        bundle = retrieve_context(settings, request)
                    self.assertEqual([("billing", "z_policy.py")], [(item.repo, item.path) for item in bundle.evidence])
                    self.assertIn("return record.cancelled", bundle.evidence[0].content)
                    self.assertLessEqual(bundle.trace["physical_backend_operations"], 4)

    def test_masker_reuses_delimiter_free_source_and_preserves_literal_offsets(self) -> None:
        from brain.investigation import _mask_java_comments

        content = "COMMON_TOKEN EXACT_USEFUL\n" + "x" * 700_000
        for strings in (False, True):
            self.assertIs(content, _mask_java_comments(content, strings=strings))
        literal = '"https://example/path/*not-comment*/"'
        source = "String url = " + literal + "; // comment\n/* block */\nchar slash = '/';\n"
        self.assertEqual("String url = " + literal + "; " + " " * 10 + "\n" + " " * 11 + "\nchar slash = '/';\n", _mask_java_comments(source))
        self.assertEqual("String url = " + " " * len(literal) + "; " + " " * 10 + "\n" + " " * 11 + "\nchar slash = " + " " * 3 + ";\n", _mask_java_comments(source, strings=True))

    def test_documentation_only_route_widens_to_exact_implementation(self) -> None:
        from brain.core import parse_context_request, retrieve_context

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("docs", "service"):
                (root / name).mkdir()
            (root / "docs/README.md").write_text("EligibilityAdaptor is mentioned here, but no implementation.\n", encoding="utf-8")
            (root / "service/adaptor.py").write_text("class EligibilityAdaptor:\n    def eligible(self, customer):\n        return customer.active\n", encoding="utf-8")
            config = root / "brain.toml"
            config.write_text("[project]\nname='quality'\n[graph]\nenabled=false\n[retrieval]\ninitial_repo_limit=1\nwiden_repo_limit=2\n"
                              "[[repositories]]\nname='docs'\npath='docs'\n[[repositories]]\nname='service'\npath='service'\n", encoding="utf-8")
            settings = load_settings(config)
            snapshot_indexes(settings)
            request = parse_context_request(json.dumps({"INVESTIGATION_REQUEST": {
                "version": 5, "mode": "root_cause", "objective": "Find EligibilityAdaptor behavior",
            }}))
            with mock.patch("brain.atlas.route", return_value={"repos": ["docs", "service"]}):
                bundle = retrieve_context(settings, request)
            self.assertIn(("service", "adaptor.py"), {(item.repo, item.path) for item in bundle.evidence})
            self.assertTrue(any("return customer.active" in item.content for item in bundle.evidence))
            self.assertIn("lexical_documentation_only", bundle.trace["fallback_reasons"])
            self.assertLessEqual(bundle.trace["physical_backend_operations"], settings.max_backend_operations)

    def test_documentation_never_claims_production_or_test_source_coverage(self) -> None:
        from brain.core import Evidence, _coverage

        for path in ("README.md", "tests/setup.md", "architecture.rst", "guide.adoc", "LICENSE"):
            coverage = _coverage(ContextBundle("Read source", evidence=[Evidence("repo", path, 1, 1, "Navigation only", "code", 100)]))
            self.assertFalse(coverage["production_source"], path)
            self.assertFalse(coverage["tests"], path)
        coverage = _coverage(ContextBundle("Read source", evidence=[Evidence("repo", "main.py", 1, 1, "pass", "code", 100)]))
        self.assertTrue(coverage["production_source"])

    def test_history_mode_compiles_source_identity_not_whole_question(self) -> None:
        from brain.core import parse_context_request

        request = parse_context_request(json.dumps({"INVESTIGATION_REQUEST": {
            "version": 5, "mode": "history", "objective": "Find when EligibilityAdaptor changed",
        }}))
        self.assertEqual([{"query": "EligibilityAdaptor", "repos": []}], request["history"])
        self.assertEqual("required", request["coverage"]["history"])

    def test_symbol_reference_cannot_hide_definition_at_repository_scale(self) -> None:
        from brain.core import parse_context_request, retrieve_context

        for count in (10, 50, 100):
            with self.subTest(repositories=count), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                repos = [f"service-{number:03}" for number in range(count)]
                config_text = "[project]\nname='quality-scale'\n[graph]\nenabled=false\n[retrieval]\ninitial_repo_limit=1\nwiden_repo_limit=2\n"
                for name in repos:
                    (root / name).mkdir()
                    (root / name / "usage.py").write_text("def caller():\n    return EligibilityAdaptor()\n", encoding="utf-8")
                    config_text += f"[[repositories]]\nname='{name}'\npath='{name}'\n"
                (root / repos[-1] / "adaptor.py").write_text(
                    "class EligibilityAdaptor:\n    def eligible(self, customer):\n        return customer.active and not customer.blocked\n", encoding="utf-8",
                )
                config = root / "brain.toml"
                config.write_text(config_text, encoding="utf-8")
                settings = load_settings(config)
                snapshot_indexes(settings)
                settings.hydrate_limit = 1
                request = parse_context_request(json.dumps({"INVESTIGATION_REQUEST": {
                    "version": 5, "mode": "root_cause", "objective": "Find EligibilityAdaptor behavior",
                }}))
                with mock.patch("brain.atlas.route", return_value={"repos": repos}):
                    bundle = retrieve_context(settings, request)
                self.assertEqual([(repos[-1], "adaptor.py")], [(item.repo, item.path) for item in bundle.evidence])
                self.assertIn("not customer.blocked", bundle.evidence[0].content)
                self.assertIn("lexical_references_only", bundle.trace["fallback_reasons"])
                self.assertLessEqual(bundle.trace["physical_backend_operations"], count + 2)
                self.assertLessEqual(bundle.trace["physical_backend_operations"], settings.max_backend_operations)

    def test_objective_prioritizes_method_identifiers_without_querying_prose(self) -> None:
        from brain.retrieval.planner import objective_terms

        for objective, expected in (
            ("Trace getRestrictions from caller to implementation.", ["getRestrictions"]),
            ("Trace evaluate_policy from caller to implementation.", ["evaluate_policy"]),
            ("Trace getRestrictions through the REST handler.", ["getRestrictions", "REST"]),
            ("Find HTTPClient and parseHTTPResponse.", ["HTTPClient", "parseHTTPResponse"]),
            ("Find EligibilityAdaptor behavior", ["EligibilityAdaptor"]),
            ("Inspect API_TIMEOUT", ["API_TIMEOUT"]),
            ("invoice cancellation", ["invoice", "cancellation"]),
        ):
            with self.subTest(objective=objective):
                self.assertEqual(expected, objective_terms(objective))

    def test_method_references_cannot_hide_the_implementation_at_repository_scale(self) -> None:
        from dataclasses import replace
        from brain.core import parse_context_request, retrieve_context

        for count in (10, 50, 100):
            with self.subTest(repositories=count), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                repos = [f"service-{number:03}" for number in range(count)]
                config_text = "[project]\nname='method-quality-scale'\n[graph]\nenabled=false\n[retrieval]\ninitial_repo_limit=1\nwiden_repo_limit=2\n"
                for name in repos:
                    repo = root / name
                    repo.mkdir()
                    (repo / "Usage.java").write_text(
                        "class Usage {\n  Object handle() {\n    return service.getRestrictions(customerId);\n  }\n}\n",
                        encoding="utf-8",
                    )
                    (repo / "usage.py").write_text(
                        "def handle(record):\n    for rule in evaluate_policy(record):\n        yield rule\n", encoding="utf-8",
                    )
                    config_text += f"[[repositories]]\nname='{name}'\npath='{name}'\n"
                (root / repos[-1] / "Policy.java").write_text(
                    "class Policy {\n  Object getRestrictions(String id) {\n    return rules.lookup(id);\n  }\n}\n",
                    encoding="utf-8",
                )
                (root / repos[-1] / "policy.py").write_text(
                    "def evaluate_policy(record):\n    return record.active and not record.blocked\n", encoding="utf-8",
                )
                config = root / "brain.toml"
                config.write_text(config_text, encoding="utf-8")
                settings = load_settings(config)
                snapshot_indexes(settings)
                settings = replace(settings, atlas_generation=current_generation_ref(settings), atlas_generation_mode="pinned", hydrate_limit=1)
                for path in ("Policy.java", "policy.py"):
                    (root / repos[-1] / path).write_text("NEW_WORKTREE_MUST_NOT_LEAK\n", encoding="utf-8")
                for symbol, path, source in (
                    ("getRestrictions", "Policy.java", "return rules.lookup(id)"),
                    ("evaluate_policy", "policy.py", "not record.blocked"),
                ):
                    request = parse_context_request(json.dumps({"INVESTIGATION_REQUEST": {
                        "version": 5, "mode": "root_cause", "objective": f"Trace {symbol} from caller to implementation.",
                    }}))
                    for warm, route in ((False, None), (True, None), (False, {"repos": repos})):
                        # Exercise both the full router and the lexical fallback
                        # when only usage repositories were ranked first.
                        with self.subTest(symbol=symbol, warm=warm, lexical_only=route is not None):
                            if route is None:
                                bundle = retrieve_context(settings, request)
                            else:
                                with mock.patch("brain.atlas.route", return_value=route):
                                    bundle = retrieve_context(settings, request)
                            self.assertEqual([(repos[-1], path)], [(item.repo, item.path) for item in bundle.evidence])
                            self.assertIn(source, bundle.evidence[0].content)
                            self.assertNotIn("NEW_WORKTREE_MUST_NOT_LEAK", bundle.evidence[0].content)
                            self.assertIn(source, pack_context(settings, "METHOD-QUALITY", 1, bundle))
                            if warm:
                                self.assertTrue(bundle.trace["atlas_route"]["cache_hit"])
                            self.assertLessEqual(bundle.trace["physical_backend_operations"], 4)

    def test_python_declarations_do_not_credit_control_flow_or_string_references(self) -> None:
        from brain.core import _symbol_declaration, symbol_hits

        for path in ("policy.py", "policy.pyi"):
            declaration = _symbol_declaration("evaluate_policy", path=path)
            for line in (
                "for rule in evaluate_policy(record):", "if evaluate_policy(record):",
                "while evaluate_policy(record):", "with evaluate_policy(record):",
                "assert evaluate_policy(record)", "result = record and evaluate_policy(record)",
                "return evaluate_policy(record)", "# def evaluate_policy(record):",
                "example = 'def evaluate_policy(record):'",
            ):
                with self.subTest(path=path, line=line):
                    self.assertIsNone(declaration.search(line))
            for line in ("def evaluate_policy(record):", "    async def evaluate_policy(record):"):
                self.assertIsNotNone(declaration.search(line))
        self.assertIsNotNone(_symbol_declaration("Policy", path="policy.py").search("class Policy:"))
        self.assertIsNotNone(_symbol_declaration("evaluate_policy", path="Policy.java").search(
            "    public List<Rule> evaluate_policy(Record record) {",
        ))
        reference = SearchHit("repo", "usage.py", 2, "for rule in evaluate_policy(record):")
        definition = SearchHit("repo", "policy.py", 1, "def evaluate_policy(record):")
        with mock.patch("brain.core.search", return_value=[reference, definition]), \
                mock.patch("brain.graph.graph_symbol_hits", return_value=[]):
            self.assertEqual([definition], symbol_hits(mock.Mock(), "evaluate_policy"))

    def test_response_format_score_never_claims_causal_correctness(self) -> None:
        from brain.evaluation import evaluate_m365_response

        response = "FINAL_SOLUTION\nTicket interpretation\nVerified current behavior\nRoot cause\nExact repository\nTests\nValidation\nEdge cases\nImplementation order\nE0001\nNo actual argument."
        result = evaluate_m365_response(response, ["E0001"])
        self.assertEqual(1.0, result["final_contract_coverage"])
        self.assertEqual("not_evaluated", result["causal_correctness"])
        self.assertEqual("format_and_citation_presence_only", result["evaluation_scope"])

    def test_fenced_json_requests_share_raw_json_validation_and_signature(self) -> None:
        from brain.agent import response_preview
        from brain.core import BrainError

        raw = json.dumps({"INVESTIGATION_REQUEST": {
            "version": 5, "mode": "root_cause", "objective": "Verify EligibilityAdaptor",
            "files": [{"repo": "service", "path": "src/Adaptor.java"}],
        }})
        original = response_preview(raw)
        for fence in ("```json", "```", "~~~~JSON"):
            marker = fence[:4] if fence.startswith("~") else fence[:3]
            with self.subTest(fence=fence):
                wrapped = f"{fence}\n{raw}\n{marker}"
                result = response_preview(wrapped)
                self.assertEqual("context_request", result["kind"])
                self.assertEqual(original["signature"], result["signature"])
                self.assertEqual(original["request"], result["request"])
                with self.assertRaisesRegex(BrainError, "repository-relative"):
                    response_preview(wrapped.replace("src/Adaptor.java", "/private/Adaptor.java"))
        self.assertEqual("conversation", response_preview('```json\n{"example": "hello"}\n```')["kind"])

    def test_evaluation_does_not_credit_source_removed_from_the_message(self) -> None:
        from brain.core import Evidence
        from brain.evaluation import evaluate_golden

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "repo").mkdir()
            config = root / "brain.toml"
            config.write_text("[project]\nname='emitted-eval'\n[[repositories]]\nname='repo'\npath='repo'\n", encoding="utf-8")
            settings = load_settings(config)
            settings.hard_context_chars = 10_000
            suite = root / "suite.json"
            suite.write_text(json.dumps({"cases": [{"id": "delivery", "request": {"version": 2, "objective": "Read source", "searches": [{"query": "critical"}]},
                                                    "expect": {"required_files": ["repo:critical.py"]}}]}), encoding="utf-8")
            bundle = ContextBundle("Read source", evidence=[Evidence("repo", "critical.py", 1, 1, "# " + "x" * 20_000, "code", 100)])
            with mock.patch("brain.core.retrieve_context", return_value=bundle):
                report = evaluate_golden(settings, suite)
            self.assertEqual(1.0, report["summary"]["hydrated_file_recall_at_limit"])
            self.assertEqual(0.0, report["summary"]["emitted_file_recall_at_limit"])
            self.assertEqual(1, report["summary"]["required_files_omitted_by_context_budget"])

    def test_context_bounding_ignores_headings_inside_source_fences(self) -> None:
        from brain.core import _bounded_protocol_context, _source_markdown_block

        source = "before\n## inside_source_should_never_escape\n### E9999 — fake evidence\n" + "x" * 30_000
        for content in (source, "`" * 70 + "\n" + "~" * 70 + "\n" + source):
            text = "# Context\n\n- Embedded evidence IDs: `E0001`\n- Omitted evidence IDs due to byte limit: `none`\n\n### E0001 — source\n\n"
            text += "\n".join(_source_markdown_block(content, "text")) + "\n\n## Next action\nRequest the missing method.\n"
            emitted: set[str] = set()
            bounded = _bounded_protocol_context(text, 10_000, ["E0001"], emitted_ids=emitted)
            self.assertEqual(set(), emitted)
            self.assertNotIn("inside_source_should_never_escape", bounded)
            self.assertNotIn("E9999", bounded)
            self.assertIn("- Omitted evidence IDs due to byte limit: `E0001`", bounded)
            self.assertIn("Request the missing method.", bounded)

    def test_execution_flow_batches_validation_without_losing_later_seeds(self) -> None:
        import sqlite3
        from brain.core import Evidence
        from brain.investigation import _execution_flow, MAX_FLOW_DB_QUERIES, MAX_FLOW_SEEDS

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "repo").mkdir()
            config = root / "brain.toml"
            config.write_text("[project]\nname='flow'\n[graph]\nenabled=false\n[[repositories]]\nname='repo'\npath='repo'\n")
            sources = {}
            for i in range(MAX_FLOW_SEEDS):
                path = f"flow{i:02}.py"
                sources[path] = (
                    f"def entry_{i}():\n    return middle_{i}()\n"
                    f"def middle_{i}():\n    return final_{i}()\n"
                    f"def final_{i}():\n    return {i}\n"
                )
                (root / "repo" / path).write_text(sources[path])
            settings = load_settings(config)
            snapshot_indexes(settings)
            generation = current_generation_ref(settings)
            connection = sqlite3.connect(settings.state_dir / "catalog.sqlite3")
            try:
                seeds = [str(row[0]) for row in connection.execute(
                    "SELECT e.entity_id FROM generation_entities g JOIN atlas_entities e ON e.entity_id=g.entity_id "
                    "WHERE g.generation=? AND e.simple_name LIKE 'entry_%' ORDER BY e.path",
                    (generation.generation,),
                )]
            finally:
                connection.close()
            self.assertEqual(MAX_FLOW_SEEDS, len(seeds))
            bundle = ContextBundle("Trace every anchored entry", atlas_generation=generation, evidence=[
                Evidence("repo", path, 1, 6, content, "code", 100, verification_content=content)
                for path, content in sources.items()
            ])
            flow = _execution_flow(settings, generation, seeds, bundle)
            self.assertLessEqual(flow["database_operations"], MAX_FLOW_DB_QUERIES)
            self.assertLessEqual(flow["database_operations"], 15)
            self.assertEqual(2 * MAX_FLOW_SEEDS, len(flow["steps"]))
            self.assertTrue(all(step["state"] == "verified" for step in flow["steps"]))
            self.assertEqual([0] * MAX_FLOW_SEEDS + [1] * MAX_FLOW_SEEDS, [step["depth"] for step in flow["steps"]])
            self.assertEqual(set(seeds), {step["source_id"] for step in flow["steps"] if step["depth"] == 0})
            self.assertEqual(flow, _execution_flow(settings, generation, seeds, bundle))
            without_source = _execution_flow(
                settings, generation, seeds, ContextBundle("metadata only", atlas_generation=generation),
            )
            self.assertTrue(all(step["state"] == "candidate" for step in without_source["steps"]))
            with mock.patch("brain.investigation.MAX_FLOW_DB_QUERIES", 4):
                bounded = _execution_flow(settings, generation, seeds, bundle)
            self.assertEqual(0, bounded["database_operations"])
            self.assertEqual([], bounded["steps"])
            connection = sqlite3.connect(settings.state_dir / "catalog.sqlite3")
            try:
                connection.execute(
                    "UPDATE atlas_edges SET target_id='poisoned-target' WHERE edge_id=?",
                    (flow["steps"][-1]["identity"],),
                )
                connection.commit()
            finally:
                connection.close()
            poisoned = _execution_flow(settings, generation, seeds, bundle)
            self.assertEqual("degraded", poisoned["status"])
            self.assertEqual([], poisoned["steps"])
            self.assertIn("content identity", poisoned["reason"])

    def test_unresolved_external_call_does_not_discard_the_verified_internal_flow(self) -> None:
        import sqlite3
        from brain.core import Evidence
        from brain.investigation import _execution_flow

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "repo").mkdir()
            content = "def entry():\n    logger.info('trace')\n    return service()\ndef service():\n    return 1\n"
            (root / "repo/main.py").write_text(content)
            config = root / "brain.toml"
            config.write_text("[project]\nname='external-call'\n[graph]\nenabled=false\n[[repositories]]\nname='repo'\npath='repo'\n")
            settings = load_settings(config)
            snapshot_indexes(settings)
            generation = current_generation_ref(settings)
            connection = sqlite3.connect(settings.state_dir / "catalog.sqlite3")
            try:
                seed = str(connection.execute(
                    "SELECT e.entity_id FROM generation_entities g JOIN atlas_entities e ON e.entity_id=g.entity_id "
                    "WHERE g.generation=? AND e.simple_name='entry'", (generation.generation,),
                ).fetchone()[0])
            finally:
                connection.close()
            bundle = ContextBundle("Trace through logging", atlas_generation=generation, evidence=[
                Evidence("repo", "main.py", 1, 5, content, "code", 100, verification_content=content),
            ])
            flow = _execution_flow(settings, generation, [seed], bundle)
            self.assertEqual("ready", flow["status"])
            steps = {step["target"]: step for step in flow["steps"]}
            self.assertEqual("verified", steps["service"]["state"])
            self.assertEqual("candidate", steps["info"]["state"])
            self.assertEqual("atlas_candidate", steps["info"]["evidence_authority"])
            self.assertFalse(any(step["source_id"] == steps["info"]["target_id"] for step in flow["steps"]))

    def test_full_storage_inventory_streams_more_than_half_a_million_entries(self) -> None:
        import stat
        from types import SimpleNamespace
        from brain.ops import _directory_bytes

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            visits = []
            info = SimpleNamespace(st_mode=stat.S_IFREG, st_size=7)
            entry = SimpleNamespace(stat=lambda **kwargs: info)

            def entries():
                for i in range(500_010):
                    if i % 100_000 == 0:
                        visits.append(i)
                    yield entry

            with mock.patch("brain.ops.os.scandir") as scan:
                scan.return_value.__enter__.return_value = entries()
                self.assertEqual(500_010 * 7, _directory_bytes(root))
            self.assertEqual(6, len(visits))

    def test_storage_quota_depth_and_incomplete_probe_never_authorize_writes(self) -> None:
        from dataclasses import replace
        from brain.ops import _directory_bytes, ensure_write_capacity, StateCapacityError

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "repo").mkdir()
            config = root / "brain.toml"
            config.write_text("[project]\nname='inventory'\n[[repositories]]\nname='repo'\npath='repo'\n")
            settings = load_settings(config)
            nested = settings.state_dir / "nested/deeper"
            nested.mkdir(parents=True)
            (nested / "payload").write_bytes(b"x" * 1024)
            with self.assertRaisesRegex(StateCapacityError, "quota"):
                ensure_write_capacity(replace(settings, max_state_gb=0.0000001))
            with self.assertRaisesRegex(StateCapacityError, "Quick storage check"):
                _directory_bytes(settings.state_dir, scan_seconds=0)
            with mock.patch.object(Path, "lstat", side_effect=PermissionError("denied")), self.assertRaises(PermissionError):
                _directory_bytes(settings.state_dir)
            with mock.patch("brain.ops.MAX_INVENTORY_DEPTH", 1), self.assertRaisesRegex(StateCapacityError, "directory-depth"):
                _directory_bytes(settings.state_dir)
            self.assertEqual(1024, _directory_bytes(settings.state_dir, stop_after=100))

    def test_gc_payload_inventory_does_not_spend_reachability_item_budget(self) -> None:
        from brain.ops import _GcScanBudget, _gc_path_bytes

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for i in range(50):
                (root / str(i)).write_bytes(b"abc")
            budget = _GcScanBudget(remaining_items=2)
            self.assertEqual(150, _gc_path_bytes(None, root, budget))
            self.assertEqual(1, budget.remaining_items)
            self.assertEqual(150, budget.accounted_bytes)

    def test_golden_mapping_uses_the_same_request_validation_as_the_ui(self) -> None:
        from brain.core import BrainError
        from brain.evaluation import _request

        with self.assertRaisesRegex(BrainError, "symbols\\[0\\].name"):
            _request({"objective": "Find a method", "symbols": [{"query": "invalid field"}]})
        result = _request({"objective": "Find a method", "symbols": [{"name": "validMethod"}]})
        self.assertEqual(["definition"], result["symbols"][0]["include"])

    def test_public_cross_repository_quality_and_hydrated_evidence_are_measured_separately(self) -> None:
        from brain.demo import create_demo
        from brain.evaluation import evaluate_golden
        from brain.ops import refresh_brain

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = load_settings(create_demo(root))
            refresh_brain(settings, fetch=False, discover=False)
            suite = root / "quality.json"
            suite.write_text(json.dumps({"name": "public-workspace-quality", "cases": [
                {"id": "unscoped-event-consumer", "request": {
                    "objective": "Locate the customer.updated Kafka consumer and its regression tests.",
                    "searches": [{"query": "KafkaListener"}, {"query": "recalculate"}],
                }, "expect": {"required_files": [
                    "trading-service:src/main/java/demo/CustomerChangedListener.java",
                    "trading-service:src/test/java/demo/CustomerChangedListenerTest.java",
                ]}},
                {"id": "cross-repository-contract", "request": {
                    "objective": "Find the risk Feign caller and the REST implementation.",
                    "symbols": [{"name": "RiskClient"}, {"name": "RiskController"}],
                }, "expect": {"required_files": [
                    "trading-service:src/main/java/demo/RiskClient.java",
                    "risk-service:src/main/java/demo/RiskController.java",
                ]}},
                {"id": "configuration-evidence", "request": {
                    "objective": "Find topic and service configuration, not a similarly named class.",
                    "searches": [{"query": "topics.customer"}],
                }, "expect": {"required_files": [
                    "trading-service:src/main/resources/application.properties",
                ]}},
            ]}))
            report = evaluate_golden(settings, suite)
            self.assertEqual(3, report["summary"]["evaluated_cases"])
            for case in report["cases"]:
                self.assertEqual(1.0, case["hydrated_file_recall_at_limit"], case)
            settings.hydrate_limit = 1
            limited = evaluate_golden(settings, suite)
            self.assertGreater(limited["summary"]["candidate_file_recall_at_limit"], limited["summary"]["hydrated_file_recall_at_limit"])

    def test_packaged_ui_javascript_boots_without_optional_browser_storage(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is unavailable for the optional packaged JavaScript smoke test")
        html = (Path(__file__).parents[1] / "brain/ui.html").read_text(encoding="utf-8")
        script = html.split("<script>", 1)[1].split("</script>", 1)[0]
        ids = re.findall(r'\bid="([^"]+)"', html)
        self.assertEqual(len(ids), len(set(ids)), "duplicate DOM IDs break workspace actions")
        result = subprocess.run([node, "-e", r'''
const vm = require("node:vm");
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
// Test UI assertions, not toast animation latency or the runner's Node startup.
const timers = new Map();
const watchdog = setTimeout(() => {console.error("UI assertions did not complete"); process.exit(1);}, 10000);
const elements = Object.fromEntries(input.ids.map(id => [id, {
  dataset: {}, listeners: {}, textContent: "", hidden: false, style: {},
  addEventListener(name, fn) { this.listeners[name] = fn; },
  querySelectorAll() { return []; },
  classList: { add() {}, remove() {}, toggle() {} }
}]));
const ctx = { URLSearchParams, location: {search:""},
  window: { matchMedia() {return {matches:true};} },
  localStorage: { getItem() {throw Error("disabled");}, setItem() {throw Error("disabled");} },
  document: {documentElement:{dataset:{}}, querySelectorAll() {return [];},
    getElementById(id) {if (!elements[id]) throw Error("Missing DOM node: " + id); return elements[id];}},
  fetch: async () => {throw Error("connection lost");},
  setTimeout(fn, milliseconds) {
    if (milliseconds !== 2600) throw Error("unexpected unmocked UI timer: " + milliseconds);
    const id = Symbol(); timers.set(id, fn); return id;
  },
  clearTimeout(id) { timers.delete(id); }
};
vm.createContext(ctx);
vm.runInContext(input.script, ctx, {timeout:5000});
ctx.renderRecovery({action:"retry_refresh", title:"Atlas timeout", message:"Known file exceeded parse budget"});
if (elements["retry-atlas-refresh"].hidden) throw Error("Atlas timeout has no retry action");
const normalRefresh = ctx.refreshBrain;
let atlasRetries = 0;
ctx.refreshBrain = button => {if (button === elements["retry-atlas-refresh"]) atlasRetries += 1;};
elements["retry-atlas-refresh"].listeners.click.call(elements["retry-atlas-refresh"]);
ctx.refreshBrain = normalRefresh;
if (atlasRetries !== 1) throw Error("Atlas recovery did not reuse the normal refresh path");
ctx.renderRecovery({action:"diagnostics", message:"Row limit needs diagnosis"});
if (!elements["retry-atlas-refresh"].hidden) throw Error("Permanent row limit must not suggest blind retry");
if (ctx.document.documentElement.dataset.theme !== "light") throw Error("system theme ignored");
elements["theme-button"].listeners.click();
if (ctx.document.documentElement.dataset.theme !== "dark") throw Error("theme toggle failed");
ctx.setView("request");
if (elements["page-title"].textContent !== "Continue with AI") throw Error("navigation failed");
ctx.setJob({name:"model-verify", phase:"Verifying", status:"running"});
if (elements["activity-button"].dataset.go !== "models") throw Error("model progress opens the wrong view");
const progress = {semantic_cards_total:30150, cached_embeddings_reused:75, shard_vectors_reused:2000,
  new_embeddings_completed:8161, embedding_elapsed_ms:3264400, elapsed_ms:3300000,
  semantic_shards_reused:32, semantic_shards_rebuilt:1, remaining_embeddings_known:0};
ctx.renderRefreshProgress({name:"refresh", status:"running", progress});
if (!elements["refresh-counts"].innerHTML.includes("Shards reused 32")) throw Error("shard reuse is hidden");
if (!elements["refresh-counts"].innerHTML.includes("Recovered old vectors 2,000")) throw Error("durable reuse is hidden");
if (elements["refresh-counts"].innerHTML.includes("Estimated remaining embedding time")) throw Error("ETA guessed before reuse checks");
if (elements["refresh-progress-fill"].style.width !== "34%") throw Error("durable vectors omitted from progress");
progress.remaining_embeddings_known = 1;
progress.remaining_embeddings = 25;
ctx.renderRefreshProgress({name:"refresh", status:"running", progress});
if (!elements["refresh-counts"].innerHTML.includes("Estimated remaining embedding time 00:10")) throw Error("ETA must use model-only rate");
ctx.renderBrain({health:"Freshness unverified", core:{ready:true}, semantic:{aligned:true}, effective:"Precision active"});
if (!elements["brain-status"].innerHTML.includes("Published generation ready")) throw Error("Git probe misreported as broken index");
if (!elements["recovery-guidance"].textContent.includes("remains usable")) throw Error("unverified Git triggers misleading rebuild advice");
ctx.state.ticket = "TICKET-A";
ctx.state.preview = {valid: true};
ctx.state.deliveries["view-request"] = {content:"A private evidence", total:1};
ctx.selectTicket("TICKET-B");
if (ctx.state.preview || ctx.state.deliveries["view-request"].content) throw Error("old ticket context leaked");
if (!elements["run-request"].disabled || elements["review-ticket"].value !== "TICKET-B") throw Error("ticket state not synchronized");
(async function () {
  await ctx.api("/api/status").then(() => {throw Error("connection failure hidden");}, error => {
    if (!error.message.includes("may still be running")) throw error;
  });
  const paused = {valid:true, kind:"context_request", operation_count:1, actions:[],
    objective:"Additional evidence", continuation:{required:true, reason:"Automatic allowance reached",
      next_wave:5, generation:1, physical_operations_per_wave:32, context_bytes_per_wave:48000, token:"a".repeat(64)}};
  ctx.state.ticket = "TICKET-B";
  elements["request-ticket"].value = "TICKET-B";
  elements["request-text"].value = "new focused request";
  ctx.renderPreview(paused);
  if (elements["run-request"].textContent !== "Continue gathering evidence") throw Error("continuation action hidden");
  if (!elements["request-message"].innerHTML.includes("no refresh or reset")) throw Error("wrong continuation guidance");
  let calls = [];
  ctx.api = async (path, options) => {calls.push({path, body:JSON.parse(options.body)}); return {id:"job"};};
  ctx.waitForJob = async () => ({kind:"context_request"});
  ctx.loadStatus = async () => {};
  ctx.window.confirm = () => false;
  await elements["run-request"].listeners.click.call(elements["run-request"]);
  if (calls.length) throw Error("cancelled continuation ran retrieval");
  ctx.window.confirm = message => {
    if (!message.includes("wave 5") || !message.includes("generation 1") || !message.includes("32 backend")) throw Error("approval is not scoped");
    return true;
  };
  await elements["run-request"].listeners.click.call(elements["run-request"]);
  if (calls.length !== 1 || calls[0].body.continue_investigation !== true || calls[0].body.continuation_token !== paused.continuation.token) throw Error("approval not sent");
  if (ctx.state.preview || !elements["run-request"].disabled) throw Error("approval remains armed after use");
  await elements["run-request"].listeners.click.call(elements["run-request"]);
  if (calls.length !== 1) throw Error("approval reused without preview");
  ctx.renderPreview(paused);
  elements["request-text"].listeners.input();
  if (ctx.state.preview) throw Error("changed request retains approval");
  ctx.report({message:"Investigation paused", recovery:{action:"continue_investigation", message:"Continue with AI"}});
  if (elements["error-recovery"].dataset.go !== "request" || elements["page-title"].textContent !== "Continue with AI") throw Error("wave pause routed to health/refresh");
  ctx.api = async () => ({delivery:{target:"m365"}, artifacts:[], session_path:".runs/TICKET-B", handoff_path:"generated/handoffs/TICKET-B"});
  elements["session-select"].value = "TICKET-B";
  await ctx.openSession();
  if (elements["request-target"].value !== "m365") throw Error("reopened ticket lost its delivery target");
  calls = [];
  ctx.api = async (path, options) => {calls.push({path, body:JSON.parse(options.body)}); return {delivery:{content:"handover"}};};
  elements["review-ticket"].value = "TICKET-B";
  await elements["create-feedback"].listeners.click.call(elements["create-feedback"]);
  if (calls[0].body.target !== "m365") throw Error("feedback silently switched to clipboard");
  elements["resume-notes"].value = "Next blocker: verify retry";
  await elements["resume-chat"].listeners.click.call(elements["resume-chat"]);
  if (calls[1].path !== "/api/resume" || calls[1].body.target !== "m365" || calls[1].body.notes !== "Next blocker: verify retry") throw Error("new-chat handover lost ticket settings");
  ctx.renderPreview({...paused, continuation:{required:false}});
  elements["request-text"].value = "A fresh retrieval";
  ctx.api = async path => path.startsWith("/api/artifact") ? {content:path.includes("delta") ? "CONTINUATION_ONLY" : "EARLY_SOURCE"} : {id:"job"};
  ctx.waitForJob = async (job, onProgress) => {
    await onProgress({progress:{checkpoint_artifact:"checkpoint-001.md"}});
    if (ctx.state.deliveries["view-request"].content !== "EARLY_SOURCE") throw Error("early evidence missing");
    return {kind:"context_request", delivery:{current:1,total:1,content:"COMPLETE_ROUND_SOURCE"},
      progressive_delivery:{continuation_artifact:"checkpoint-delta-001.md"}};
  };
  await elements["run-request"].listeners.click.call(elements["run-request"]);
  if (ctx.state.deliveries["view-request"].content !== "COMPLETE_ROUND_SOURCE") throw Error("showing an early checkpoint incorrectly assumed it was delivered");
  ctx.window.confirm = () => false;
  await elements["checkpoint-continuation"].listeners.click.call(elements["checkpoint-continuation"]);
  if (ctx.state.deliveries["view-request"].content !== "COMPLETE_ROUND_SOURCE") throw Error("cancelled continuation discarded source");
  ctx.window.confirm = () => true;
  await elements["checkpoint-continuation"].listeners.click.call(elements["checkpoint-continuation"]);
  if (ctx.state.deliveries["view-request"].content !== "CONTINUATION_ONLY") throw Error("explicit checkpoint continuation unavailable");
  ctx.selectTicket("TICKET-C");
  if (ctx.state.checkpointContinuation || !elements["checkpoint-continuation"].hidden) throw Error("checkpoint continuation crossed tickets");
  for (const fn of timers.values()) fn();
  timers.clear();
})().then(() => {
  clearTimeout(watchdog);
  console.log("UI smoke assertions complete");
}).catch(error => {clearTimeout(watchdog); console.error(error); process.exitCode = 1;});
'''], input=json.dumps({"script": script, "ids": ids}), capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("UI smoke assertions complete", result.stdout)

    def test_rrf_uses_per_channel_candidate_rank_without_mutating_inputs(self) -> None:
        hits = [
            SearchHit("r", "first.py", 1, "", score=100, found_by=["lexical"]),
            SearchHit("r", "second.py", 1, "", score=100, found_by=["semantic"]),
            SearchHit("r", "second.py", 1, "", score=90, found_by=["lexical"]),
        ]
        ranked = fuse_and_rank(hits)
        self.assertEqual("second.py", ranked[0].path)
        self.assertEqual(round(100 + 100 * (1 / 61 + 1 / 62), 3), ranked[0].score)
        self.assertEqual([100, 100, 90], [hit.score for hit in hits])
        self.assertEqual(ranked, fuse_and_rank(list(reversed(hits))))
        self.assertEqual(ranked, fuse_and_rank(hits))

    def test_java_entity_identity_and_lines_match_reference_brace_scanning(self) -> None:
        from brain.atlas import _java_entities
        from bisect import bisect_left as actual_bisect

        content = "class Outer {\n" + "\n".join(
            f"public void method{i}() {{ if (true) {{ run(); }} }}" for i in range(1000)
        ) + "\nclass Inner { void child() { run(); } }\n}\n"
        lines = [i for i, char in enumerate(content) if char == "\n"]
        lookups = []

        def reference(values, position):
            self.assertEqual(lines, values)
            lookups.append(position)
            self.assertEqual(content.count("\n", 0, position), actual_bisect(values, position))
            return actual_bisect(values, position)

        with mock.patch("brain.atlas.bisect_left", side_effect=reference):
            entities = _java_entities("repo", "Outer.java", "blob", "module", content, content, time.monotonic() + 10)
        self.assertEqual(1003, len(entities))
        self.assertGreater(len(lookups), 1000)
        for item in entities:
            from brain.atlas import _valid_entity_content_identity
            self.assertTrue(_valid_entity_content_identity(item))
        child = next(item for item in entities if item["simple_name"] == "child")
        inner = next(item for item in entities if item["simple_name"] == "Inner")
        self.assertEqual(inner["entity_id"], child["parent_entity_id"])
        self.assertEqual((1002, 1002), (child["line_start"], child["line_end"]))

    def test_pinned_context_pack_does_not_probe_current_head_for_every_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "service").mkdir()
            (root / "service/main.py").write_text("def main(): return 1\n")
            config = root / "brain.toml"
            config.write_text("[project]\nname='pack'\n[graph]\nenabled=false\n[[repositories]]\nname='service'\npath='service'\n")
            settings = load_settings(config)
            snapshot_indexes(settings)
            generation = current_generation_ref(settings)
            with mock.patch("brain.core.git_head", side_effect=AssertionError("live HEAD is not pinned evidence")):
                text = pack_context(settings, "PACK-1", 1, ContextBundle("Inspect", atlas_generation=generation))
            self.assertIn(generation.snapshots["service"][:12], text)
            self.assertIn("not probed (pinned)", text)

    def test_freshness_100_repositories_share_one_probe_time_budget(self) -> None:
        from brain.ops import freshness

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "brain.toml"
            (root / "initial").mkdir()
            config.write_text("[project]\nname='bounded'\n[graph]\nenabled=false\n[[repositories]]\nname='initial'\npath='initial'\n")
            settings = load_settings(config)
            repositories = []
            for i in range(100):
                path = root / f"repo{i}"
                (path / ".git").mkdir(parents=True)
                repositories.append(Repository(name=f"repo{i}", path=path))
            settings.repositories = repositories
            with mock.patch("brain.ops.MAX_FRESHNESS_PROBE_SECONDS", 0), mock.patch("brain.core.git_head") as probe:
                result = freshness(settings)
            self.assertEqual(100, len(result["repositories"]))
            probe.assert_not_called()
            self.assertTrue(all(not row["current"] and row["source_sha"] is None for row in result["repositories"]))


if __name__ == "__main__":
    unittest.main()

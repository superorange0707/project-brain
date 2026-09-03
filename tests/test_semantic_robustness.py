from __future__ import annotations

import hashlib
import json
import importlib.util
import io
import socket
import sqlite3
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.client import RemoteDisconnected
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from brain.cli import main
from brain.core import load_settings, snapshot_indexes
from brain.editions import capabilities
from brain.models import LlamaCppRuntime, ManagedLlamaCppRuntime, embedding_request_bytes
from brain.semantic import (
    MAX_SEMANTIC_CHUNKS_TOTAL,
    SEMANTIC_MAX_CARD_INPUT_BYTES,
    SEMANTIC_MAX_REQUEST_BODY_BYTES,
    Chunk,
    SemanticEmbeddingError,
    _atomic_index_save,
    _bounded_embedding_batches,
    _bounded_semantic_card,
    _cache_vectors,
    _partition_semantic_inputs,
    _query_vector,
    _shard_sha256,
    build_semantic_index,
    chunk_source,
    search_semantic,
)


class SemanticRobustnessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "workload"
        self.second_repository = self.root / "second-workload"
        (self.repository / "src").mkdir(parents=True)
        (self.second_repository / "src").mkdir(parents=True)
        self.source = self.repository / "src" / "workload.py"
        self.second_source = self.second_repository / "src" / "workload.py"
        self.source.write_text("def stable():\n    return 'initial'\n", encoding="utf-8")
        self.second_source.write_text("def second_stable():\n    return 'initial'\n", encoding="utf-8")
        self.config = self.root / "brain.toml"
        self.config.write_text(
            "[project]\nname='semantic-workload'\n[graph]\nenabled=false\n"
            "[[repositories]]\nname='workload'\npath='workload'\n"
            "[[repositories]]\nname='second-workload'\npath='second-workload'\n",
            encoding="utf-8",
        )
        self.settings = load_settings(self.config)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _chunks(count: int, *, failing: bool = False) -> list[Chunk]:
        cards: list[Chunk] = []
        for index in range(count):
            tag = "TRANSPORT_FAIL" if failing and index == count - 1 else f"card-{index}"
            cards.append(
                Chunk(
                    str(index), "blob", f"src/{tag}.py", 1, 1, "symbol", tag,
                    f"Repository: public-fixture\nPath: src/{tag}.py\nLanguage: Python\nKind: symbol\n"
                    f"Symbol: {tag}\nIdentifiers: workload\nCode:\ndef {tag}():\n    return '{tag}'\n",
                )
            )
        return cards

    @staticmethod
    def _vectors(cards: list[str]) -> list[list[float]]:
        return [[1.0, float(index + 1)] for index, _ in enumerate(cards)]

    def test_complete_input_and_exact_json_body_bounds_apply_before_batching(self) -> None:
        path = "src/" + ("nested-" * 400) + "workload.py"
        symbol = "symbol_" + ("identifier_" * 180)
        chunk = chunk_source("public-fixture", path, f"def {symbol}():\n    return '你好 \\\"quoted\\\"' * 300\n")[0]
        instruction = "Represent the code evidence faithfully. " * 30
        suffix = "\n<eos>" * 80
        bounded = _bounded_semantic_card(chunk.card, document_instruction=instruction, input_suffix=suffix, dimension=2)

        self.assertIn(f"Path: {path}", bounded)
        self.assertIn(f"Symbol: {symbol}", bounded)
        self.assertIn("[semantic card code truncated]", bounded)
        self.assertLessEqual(len((instruction + bounded + suffix).encode("utf-8")), SEMANTIC_MAX_CARD_INPUT_BYTES)
        self.assertLessEqual(
            embedding_request_bytes([bounded], instruction=instruction, input_suffix=suffix, dimension=2),
            SEMANTIC_MAX_REQUEST_BODY_BYTES,
        )

        cards = [
            f"Repository: fixture\nPath: src/{index}.py\nSymbol: item_{index}\nCode:\n" + ("x\\\"你好" * 500)
            for index in range(20)
        ]
        bounded_cards = [
            _bounded_semantic_card(card, document_instruction=instruction, input_suffix=suffix, dimension=2)
            for card in cards
        ]
        chunks = [Chunk(str(index), "blob", f"src/{index}.py", 1, 1, "symbol", f"item_{index}", card) for index, card in enumerate(cards)]
        batches = list(
            _bounded_embedding_batches(
                chunks, list(range(len(chunks))), 16, cards=bounded_cards,
                document_instruction=instruction, input_suffix=suffix, dimension=2,
            )
        )
        self.assertTrue(all(len(batch) <= 16 for batch in batches))
        self.assertTrue(any(len(batch) < 16 for batch in batches))
        self.assertGreater(len(batches), 1)
        for batch in batches:
            self.assertLessEqual(
                embedding_request_bytes(
                    [bounded_cards[index] for index in batch], instruction=instruction, input_suffix=suffix, dimension=2,
                ),
                SEMANTIC_MAX_REQUEST_BODY_BYTES,
            )

    def test_hundred_repository_semantic_inputs_are_partitioned_in_one_pass(self) -> None:
        class CountingManifest(dict[tuple[str, str], str]):
            visits = 0

            def items(self):  # type: ignore[override]
                for item in super().items():
                    self.visits += 1
                    yield item

        class CountingCards(list[dict[str, object]]):
            visits = 0

            def __iter__(self):  # type: ignore[override]
                for item in super().__iter__():
                    self.visits += 1
                    yield item

        repositories = [SimpleNamespace(name=f"repo-{index:03d}") for index in range(100)]
        manifest = CountingManifest({
            (repo.name, "src/Main.java"): f"blob-{index}"
            for index, repo in enumerate(repositories)
        })
        cards = CountingCards([
            {"repo": repo.name, "card_id": f"card-{index}"}
            for index, repo in enumerate(repositories)
        ])

        manifests, partitioned_cards = _partition_semantic_inputs(
            repositories, manifest, cards,
        )

        self.assertEqual(100, manifest.visits)
        self.assertEqual(100, cards.visits)
        self.assertIsNotNone(manifests)
        assert manifests is not None
        self.assertTrue(all(len(manifests[repo.name]) == 1 for repo in repositories))
        self.assertTrue(all(len(partitioned_cards[repo.name]) == 1 for repo in repositories))

    def test_windows_shard_hash_does_not_reuse_same_stat_projection(self) -> None:
        shard = self.root / "semantic-shard.usearch"
        shard.write_bytes(b"original")
        stable_stat = shard.stat()
        with (
            mock.patch("brain.semantic.os.name", "nt"),
            mock.patch.object(Path, "stat", return_value=stable_stat),
        ):
            original = _shard_sha256(shard)
            shard.write_bytes(b"tampered")
            tampered = _shard_sha256(shard)
        self.assertNotEqual(original, tampered)

    def test_runtime_posts_the_same_utf8_json_body_that_size_accounting_measures(self) -> None:
        runtime = LlamaCppRuntime("http://127.0.0.1:9999", input_suffix="\n<eos>你好")
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"data":[{"embedding":[1.0,0.0]}]}'
        with mock.patch.object(runtime, "_open", return_value=response) as opened:
            self.assertEqual([[1.0, 0.0]], runtime.embed(['card with "quotes" and \\ escapes'], instruction="instruction: ", dimension=2))
        request = opened.call_args.args[0]
        self.assertEqual(
            embedding_request_bytes(['card with "quotes" and \\ escapes'], instruction="instruction: ", input_suffix="\n<eos>你好", dimension=2),
            len(request.data or b""),
        )
        self.assertEqual(
            ["instruction: card with \"quotes\" and \\ escapes\n<eos>你好"],
            json.loads((request.data or b"").decode("utf-8"))["input"],
        )

    def test_runtime_response_is_physically_byte_bounded_before_json_decode(self) -> None:
        runtime = LlamaCppRuntime("http://127.0.0.1:9999")
        response = mock.MagicMock()
        response.__enter__.return_value.read.side_effect = lambda size: b"x" * size
        with (
            mock.patch.object(runtime, "_open", return_value=response),
            mock.patch("brain.models.MAX_MODEL_RUNTIME_RESPONSE_BYTES", 32),
            self.assertRaisesRegex(RuntimeError, "response exceeds its byte limit"),
        ):
            runtime.embed(["card"], dimension=2)
        response.__enter__.return_value.read.assert_called_once_with(33)

    def test_semantic_shard_save_ignores_predictable_symlink_traps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shard = root / "generation.usearch"
            outside = root / "outside.bin"
            outside.write_bytes(b"outside marker")
            shard.with_suffix(".building").symlink_to(outside)

            class FakeIndex:
                @staticmethod
                def save(path: str) -> None:
                    Path(path).write_bytes(b"new shard")

            _atomic_index_save(FakeIndex(), shard)
            self.assertEqual(b"outside marker", outside.read_bytes())
            self.assertEqual(b"new shard", shard.read_bytes())

    def test_non_finite_embedding_vectors_fail_closed_and_cached_values_self_heal(self) -> None:
        runtime = LlamaCppRuntime("http://127.0.0.1:9999")
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"data":[{"embedding":[NaN,1.0]}]}'
        with mock.patch.object(runtime, "_open", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "incomplete vector batch"):
                runtime.embed(["card"], dimension=2)

        chunks = self._chunks(2)
        _cache_vectors(self.settings, "finite-pack", chunks, dimension=2, embed=self._vectors)
        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            connection.execute(
                "UPDATE embedding_cache SET vector_json=? WHERE pack_id=?",
                (json.dumps([float("nan"), 1.0]), "finite-pack"),
            )
            connection.commit()
        finally:
            connection.close()
        rebuilt: list[list[str]] = []

        def finite(cards: list[str]) -> list[list[float]]:
            rebuilt.append(cards)
            return self._vectors(cards)

        vectors = _cache_vectors(self.settings, "finite-pack", chunks, dimension=2, embed=finite)
        self.assertEqual(2, len(vectors))
        self.assertTrue(rebuilt)

        _query_vector(self.settings, "finite query", pack_id="finite-pack", dimension=2, embed=self._vectors)
        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            connection.execute(
                "UPDATE embedding_cache SET vector_json=? WHERE cache_key LIKE 'query:%'",
                (json.dumps([float("inf"), 1.0]),),
            )
            connection.commit()
        finally:
            connection.close()
        query_calls: list[list[str]] = []

        def query_embed(values: list[str]) -> list[list[float]]:
            query_calls.append(values)
            return [[1.0, 2.0]]

        self.assertEqual(
            [1.0, 2.0],
            _query_vector(self.settings, "finite query", pack_id="finite-pack", dimension=2, embed=query_embed),
        )
        self.assertEqual([["finite query"]], query_calls)

    def test_embedding_cache_capacity_fails_before_pruning_or_inserting(self) -> None:
        from brain.catalog import connect

        connection = connect(self.settings)
        try:
            connection.execute(
                "INSERT INTO embedding_cache(cache_key,pack_id,dimension,vector_json,created_at,last_used_at) "
                "VALUES ('sentinel','old-pack',2,'[]','2020','2020')"
            )
            connection.commit()
        finally:
            connection.close()
        with mock.patch("brain.ops.remaining_write_capacity", return_value=0), self.assertRaisesRegex(
            SemanticEmbeddingError, "query embedding cache",
        ):
            _query_vector(
                self.settings, "disk-full-query", pack_id="query-pack", dimension=2, embed=self._vectors,
            )
        connection = connect(self.settings)
        try:
            self.assertEqual(
                0,
                connection.execute("SELECT COUNT(*) FROM embedding_cache WHERE cache_key LIKE 'query:%'").fetchone()[0],
            )
        finally:
            connection.close()
        with mock.patch("brain.ops.remaining_write_capacity", return_value=0), self.assertRaisesRegex(
            SemanticEmbeddingError, "remaining managed write capacity",
        ):
            _cache_vectors(
                self.settings, "disk-full-pack", self._chunks(1), dimension=2, embed=self._vectors,
            )
        connection = connect(self.settings)
        try:
            self.assertEqual(
                [("sentinel", "[]")],
                connection.execute("SELECT cache_key,vector_json FROM embedding_cache").fetchall(),
            )
        finally:
            connection.close()
        with mock.patch("brain.semantic.MAX_EMBEDDING_CACHE_BYTES", 4), self.assertRaisesRegex(
            SemanticEmbeddingError, "cache entry batch exceeds",
        ):
            _cache_vectors(
                self.settings, "new-pack", self._chunks(1), dimension=2, embed=self._vectors,
            )
        connection = connect(self.settings)
        try:
            self.assertEqual(
                [("sentinel", "[]")],
                connection.execute("SELECT cache_key,vector_json FROM embedding_cache").fetchall(),
            )
        finally:
            connection.close()

    def test_bulk_embedding_cache_uses_one_capacity_scan_and_bounded_commits(self) -> None:
        from brain.catalog import connect

        connection = connect(self.settings)
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        with (
            mock.patch("brain.catalog.connect", return_value=connection),
            mock.patch("brain.ops.remaining_write_capacity", return_value=1024 * 1024 * 1024),
        ):
            vectors = _cache_vectors(
                self.settings, "bulk-cache-pack", self._chunks(40), dimension=2,
                embed=self._vectors, batch_size=1,
            )

        self.assertEqual(40, len(vectors))
        self.assertEqual(1, sum(
            "SELECT COUNT(*),COALESCE(SUM" in statement for statement in statements
        ))
        self.assertEqual(3, sum(statement == "COMMIT" for statement in statements))

    def test_semantic_shard_capacity_is_reserved_before_native_save(self) -> None:
        saved: list[str] = []

        class Runtime:
            @staticmethod
            def embed(cards: list[str], *, instruction: str = "", dimension: int = 0) -> list[list[float]]:
                return [[1.0, 0.0] for _ in cards]

            @staticmethod
            def shutdown() -> None:
                return None

        class Index:
            def __init__(self, **_: object) -> None:
                pass

            @staticmethod
            def add(keys: object, values: object) -> None:
                return None

            @staticmethod
            def save(path: str) -> None:
                saved.append(path)
                Path(path).write_bytes(b"native shard")

        class Numpy:
            uint64 = "uint64"
            float32 = "float32"

            @staticmethod
            def arange(value: int, dtype: object = None) -> list[int]:
                return list(range(value))

            @staticmethod
            def asarray(value: object, dtype: object = None) -> object:
                return value

        manifest = {
            "pack_id": "capacity-pack", "embedding_dimension": 2, "test_only": True,
            "pack_compatibility_identity": "sha256:" + "1" * 64,
        }
        with (
            mock.patch("brain.semantic.active_pack", return_value=manifest),
            mock.patch("brain.semantic.runtime_for_pack", return_value=Runtime()),
            mock.patch("brain.semantic._usearch", return_value=(Index, Numpy)),
            mock.patch("brain.ops.remaining_write_capacity", return_value=1024 * 1024),
            self.assertRaisesRegex(SemanticEmbeddingError, "Semantic shard exceeds"),
        ):
            build_semantic_index(self.settings)
        self.assertEqual([], saved)
        self.assertFalse(list((self.settings.state_dir / "semantic-shards").glob("*.usearch")))

    def test_bulk_semantic_build_keeps_runtime_resident_and_revalidates_before_publish(self) -> None:
        class Runtime:
            def __init__(self, manifest: dict[str, object]) -> None:
                self.manifest = manifest

            @staticmethod
            def embed(cards: list[str], *, instruction: str = "", dimension: int = 0) -> list[list[float]]:
                return [[1.0, 0.0] for _ in cards]

            @staticmethod
            def shutdown() -> None:
                return None

        class Index:
            def __init__(self, **_: object) -> None:
                pass

            @staticmethod
            def add(keys: object, values: object) -> None:
                return None

            @staticmethod
            def save(path: str) -> None:
                Path(path).write_bytes(b"native shard")

        class Numpy:
            uint64 = "uint64"
            float32 = "float32"

            @staticmethod
            def arange(value: int, dtype: object = None) -> list[int]:
                return list(range(value))

            @staticmethod
            def asarray(value: object, dtype: object = None) -> object:
                return value

        manifest: dict[str, object] = {
            "pack_id": "resident-pack", "embedding_dimension": 2, "test_only": True,
            "pack_compatibility_identity": "sha256:" + "3" * 64,
        }
        runtime = Runtime(manifest)
        with (
            mock.patch("brain.semantic.active_pack", return_value=manifest),
            mock.patch("brain.semantic.runtime_for_pack", return_value=runtime) as runtime_factory,
            mock.patch("brain.semantic._usearch", return_value=(Index, Numpy)),
            mock.patch("brain.semantic._check_pack_integrity") as integrity,
        ):
            result = build_semantic_index(self.settings)

        self.assertGreater(result["chunks"], 0)
        runtime_factory.assert_called_once_with(
            manifest, default_max_requests=MAX_SEMANTIC_CHUNKS_TOTAL + 1,
        )
        integrity.assert_called_once_with(manifest)

    def test_semantic_chunk_limit_fails_before_embedding_or_shard_write(self) -> None:
        calls: list[list[str]] = []

        def embed(cards: list[str]) -> list[list[float]]:
            calls.append(cards)
            return self._vectors(cards)

        with (
            mock.patch("brain.semantic.MAX_SEMANTIC_CHUNKS_PER_REPOSITORY", 0),
            self.assertRaisesRegex(SemanticEmbeddingError, "chunk limit"),
        ):
            build_semantic_index(self.settings, embed=embed, pack_id="bounded-chunks")
        self.assertEqual([], calls)
        self.assertFalse((self.settings.state_dir / "semantic-index.json").exists())

    def test_semantic_build_streams_preflighted_membership_without_full_manifest(self) -> None:
        snapshot_indexes(self.settings)
        embedded: list[list[str]] = []

        def embed(cards: list[str]) -> list[list[float]]:
            embedded.append(cards)
            return self._vectors(cards)

        with mock.patch(
            "brain.index.indexed_snapshot_file_manifest",
            side_effect=AssertionError("complete manifest must not be materialized"),
        ):
            result = build_semantic_index(
                self.settings, embed=embed, pack_id="streamed-source",
            )
        self.assertGreater(result["chunks"], 0)
        self.assertTrue(embedded)

    def test_semantic_source_budget_fails_before_embedding(self) -> None:
        snapshot_indexes(self.settings)
        embedded: list[list[str]] = []
        with (
            mock.patch("brain.semantic.MAX_SEMANTIC_SOURCE_FILES_PER_REPOSITORY", 0),
            self.assertRaisesRegex(SemanticEmbeddingError, "source budget"),
        ):
            build_semantic_index(
                self.settings,
                embed=lambda cards: embedded.append(cards) or self._vectors(cards),
                pack_id="bounded-source",
            )
        self.assertEqual([], embedded)

    def test_semantic_stream_rejects_same_size_membership_substitution(self) -> None:
        from brain.index import (
            indexed_snapshot_source_contents,
            indexed_snapshot_source_projection,
        )

        first = self.repository / "src" / "alpha.py"
        second = self.repository / "src" / "bravo.py"
        first.write_text("def alpha():\n    return 1\n", encoding="utf-8")
        second.write_text("def bravo():\n    return 2\n", encoding="utf-8")
        state, _ = snapshot_indexes(self.settings)
        workload_snapshot = str(state["workload"]["sha"])
        snapshots = {"workload": workload_snapshot}
        projection = indexed_snapshot_source_projection(
            self.settings,
            snapshots,
            max_repositories=10,
            max_items_per_repository=100,
            max_items=1_000,
            max_bytes_per_repository=1_000_000,
            max_bytes=2_000_000,
            max_file_bytes=3_000_000,
            max_seconds=10.0,
        )
        connection = sqlite3.connect(self.settings.state_dir / "search.sqlite3")
        try:
            replacement = connection.execute(
                "SELECT blob FROM file_membership WHERE repo='workload' "
                "AND snapshot_sha=? AND path='src/bravo.py'",
                (workload_snapshot,),
            ).fetchone()[0]
            connection.execute(
                "UPDATE file_membership SET blob=? WHERE repo='workload' "
                "AND snapshot_sha=? AND path='src/alpha.py'",
                (replacement, workload_snapshot),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(sqlite3.DatabaseError, "changed before Semantic build"):
            list(indexed_snapshot_source_contents(
                self.settings,
                "workload",
                workload_snapshot,
                projection["workload"],
                max_seconds=10.0,
            ))

    def test_legacy_semantic_walk_is_entry_bounded_before_embedding(self) -> None:
        embedded: list[list[str]] = []
        with (
            mock.patch("brain.semantic.MAX_SEMANTIC_LEGACY_SCAN_ENTRIES", 1),
            self.assertRaisesRegex(SemanticEmbeddingError, "bounded repository contract"),
        ):
            build_semantic_index(
                self.settings,
                embed=lambda cards: embedded.append(cards) or self._vectors(cards),
                pack_id="bounded-legacy-source",
            )
        self.assertEqual([], embedded)

    def test_corrupt_current_shard_is_rebuilt_from_cached_embeddings(self) -> None:
        saved: list[str] = []
        embedded: list[list[str]] = []

        class Runtime:
            def embed(self, cards: list[str], *, instruction: str = "", dimension: int = 0) -> list[list[float]]:
                embedded.append(cards)
                return [[1.0, float(index + 1)] for index, _ in enumerate(cards)]

            @staticmethod
            def shutdown() -> None:
                return None

        class Index:
            def __init__(self, **_: object) -> None:
                pass

            @staticmethod
            def add(keys: object, values: object) -> None:
                return None

            @staticmethod
            def save(path: str) -> None:
                saved.append(path)
                Path(path).write_bytes(b"valid shard " + str(len(saved)).encode("ascii"))

        class Numpy:
            uint64 = "uint64"
            float32 = "float32"

            @staticmethod
            def arange(value: int, dtype: object = None) -> list[int]:
                return list(range(value))

            @staticmethod
            def asarray(value: object, dtype: object = None) -> object:
                return value

        manifest = {
            "pack_id": "repair-pack", "embedding_dimension": 2, "test_only": True,
            "pack_compatibility_identity": "sha256:" + "2" * 64,
        }
        patches = (
            mock.patch("brain.semantic.active_pack", return_value=manifest),
            mock.patch("brain.semantic.runtime_for_pack", return_value=Runtime()),
            mock.patch("brain.semantic._usearch", return_value=(Index, Numpy)),
        )
        with patches[0], patches[1], patches[2]:
            build_semantic_index(self.settings)
        state_path = self.settings.state_dir / "semantic-index.json"
        first = json.loads(state_path.read_text(encoding="utf-8"))
        corrupt = Path(str(first["shards"][0]["path"]))
        corrupt.write_bytes(b"corrupt shard")
        saved.clear()
        embedded.clear()
        with (
            mock.patch("brain.semantic.active_pack", return_value=manifest),
            mock.patch("brain.semantic.runtime_for_pack", return_value=Runtime()),
            mock.patch("brain.semantic._usearch", return_value=(Index, Numpy)),
        ):
            build_semantic_index(self.settings)
        repaired = json.loads(state_path.read_text(encoding="utf-8"))
        repaired_path = Path(str(next(
            shard["path"] for shard in repaired["shards"] if shard["repo"] == first["shards"][0]["repo"]
        )))
        self.assertNotEqual(corrupt, repaired_path)
        self.assertTrue(saved)
        self.assertEqual([], embedded)
        self.assertEqual(hashlib.sha256(repaired_path.read_bytes()).hexdigest(), next(
            shard["artifact_sha256"] for shard in repaired["shards"] if shard["path"] == str(repaired_path)
        ))

    def test_same_pack_id_with_different_definition_never_reuses_legacy_vectors(self) -> None:
        chunks = self._chunks(1)
        first_identity = "sha256:" + "1" * 64
        second_identity = "sha256:" + "2" * 64
        _cache_vectors(
            self.settings, "replaced-pack", chunks, dimension=2, embed=lambda _cards: [[1.0, 0.0]],
            pack_compatibility_identity=first_identity,
        )
        calls: list[list[str]] = []

        def replacement(cards: list[str]) -> list[list[float]]:
            calls.append(cards)
            return [[0.0, 1.0]]

        vectors = _cache_vectors(
            self.settings, "replaced-pack", chunks, dimension=2, embed=replacement,
            pack_compatibility_identity=second_identity,
        )
        self.assertEqual([[0.0, 1.0]], vectors)
        self.assertTrue(calls)

    def test_cached_query_vector_skips_runtime_and_model_lane_startup(self) -> None:
        build_semantic_index(self.settings, embed=self._vectors, pack_id="query-cache-pack")
        state = json.loads((self.settings.state_dir / "semantic-index.json").read_text(encoding="utf-8"))
        dimension = int(state["dimension"])
        _query_vector(
            self.settings, "cached semantic query", pack_id="query-cache-pack", dimension=dimension,
            embed=self._vectors,
        )
        manifest = {
            "pack_id": "query-cache-pack", "query_instruction": "", "embedding_dimension": dimension,
            "pack_compatibility_identity": state["pack_compatibility_identity"],
            "test_only": True,
        }
        with (
            mock.patch("brain.semantic.verified_pack", return_value=manifest),
            mock.patch("brain.semantic.runtime_for_pack") as runtime,
            mock.patch("brain.semantic.model_lane") as lane,
        ):
            results = search_semantic(self.settings, "cached semantic query")
        self.assertTrue(results)
        runtime.assert_not_called()
        lane.assert_not_called()

    def test_transport_disconnect_restarts_then_adaptively_splits_16_to_single_cards(self) -> None:
        calls: list[int] = []
        restarts = 0

        def embed(cards: list[str]) -> list[list[float]]:
            calls.append(len(cards))
            if len(cards) > 1:
                raise RemoteDisconnected("synthetic runtime disconnect")
            return self._vectors(cards)

        def restart() -> None:
            nonlocal restarts
            restarts += 1

        vectors = _cache_vectors(
            self.settings, "adaptive-pack", self._chunks(16), dimension=2, embed=embed, batch_size=16, restart=restart,
        )
        self.assertEqual(16, len(vectors))
        self.assertEqual([16, 8, 4, 2, 1], calls[:5])
        self.assertGreaterEqual(restarts, 4)

    def test_single_card_failure_preserves_published_state_and_cached_successes(self) -> None:
        def healthy(cards: list[str]) -> list[list[float]]:
            return self._vectors(cards)

        first = build_semantic_index(self.settings, embed=healthy, pack_id="atomic-pack")
        self.assertGreater(first["chunks"], 0)
        state_path = self.settings.state_dir / "semantic-index.json"
        published = state_path.read_bytes()

        self.source.write_text(
            "def stable():\n    return 'changed'\n\ndef failing():\n    return 'TRANSPORT_FAIL'\n",
            encoding="utf-8",
        )

        def failing(cards: list[str]) -> list[list[float]]:
            if any("TRANSPORT_FAIL" in card for card in cards):
                raise RemoteDisconnected("synthetic runtime disconnect")
            return self._vectors(cards)

        with self.assertRaisesRegex(SemanticEmbeddingError, r"batch=16 cards=1 max_card_chars=\d+ request_bytes=\d+"):
            build_semantic_index(self.settings, embed=failing, pack_id="atomic-pack")
        self.assertEqual(published, state_path.read_bytes())

        rerun_calls: list[list[str]] = []

        def recovered(cards: list[str]) -> list[list[float]]:
            rerun_calls.append(cards)
            return self._vectors(cards)

        rerun = build_semantic_index(self.settings, embed=recovered, pack_id="atomic-pack")
        self.assertGreater(rerun["chunks"], 0)
        self.assertNotEqual(published, state_path.read_bytes())
        # The published generation supplies the known dimension, and the prior
        # failed run committed its successful stable-card embedding.  Only the
        # former failing card needs another cache-miss embedding request.
        self.assertEqual(1, len(rerun_calls))
        self.assertIn("TRANSPORT_FAIL", rerun_calls[0][0])

    def test_tuned_batch_ceiling_is_read_from_disk_but_payload_limit_still_wins(self) -> None:
        source = []
        for index in range(20):
            source.append(f"def feature_{index}():")
            source.extend("    # workload " + ("x" * 100) for _ in range(79))
        self.source.write_text("\n".join(source) + "\n", encoding="utf-8")
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        (self.settings.state_dir / "model-tuning.json").write_text(
            json.dumps({"pack_id": "tuned-pack", "recommendations": {"embedding_batch_size": 16}}), encoding="utf-8"
        )
        calls: list[list[str]] = []

        def embed(cards: list[str]) -> list[list[float]]:
            calls.append(cards)
            return self._vectors(cards)

        build_semantic_index(self.settings, embed=embed, pack_id="tuned-pack")
        # The first request discovers the injected test runtime's dimension.
        batches = calls[1:]
        self.assertTrue(batches)
        self.assertTrue(all(len(batch) <= 16 for batch in batches))
        self.assertTrue(any(len(batch) < 16 for batch in batches))
        self.assertTrue(all(embedding_request_bytes(batch, dimension=2) <= SEMANTIC_MAX_REQUEST_BODY_BYTES for batch in batches))

    def test_identical_refresh_reuses_the_published_semantic_generation(self) -> None:
        calls: list[list[str]] = []

        def embed(cards: list[str]) -> list[list[float]]:
            calls.append(cards)
            return self._vectors(cards)

        first = build_semantic_index(self.settings, embed=embed, pack_id="reuse-pack")
        state_path = self.settings.state_dir / "semantic-index.json"
        published = state_path.read_bytes()
        calls.clear()
        second = build_semantic_index(self.settings, embed=embed, pack_id="reuse-pack")
        self.assertEqual(first, second)
        self.assertEqual([], calls)
        self.assertEqual(published, state_path.read_bytes())

    def test_routed_search_hashes_only_selected_shard_and_ignores_other_repo_corruption(self) -> None:
        from brain.catalog import _content_hash, source_signature
        from brain.semantic import (
            ATLAS_CARD_VERSION, CARD_VERSION, CHUNK_SCHEMA_VERSION,
            SEMANTIC_EMBEDDING_INPUT_VERSION, SEMANTIC_SHARD_MANIFEST_VERSION,
            _SERVING_STATE_CACHE, _SHARD_HASH_CACHE, _injected_pack_identity, semantic_schema_version,
        )

        shard_root = self.settings.state_dir / "semantic-shards"
        shard_root.mkdir(parents=True, exist_ok=True)
        shards = []
        for repo, content in (("workload", b"healthy-a"), ("second-workload", b"healthy-b")):
            path = shard_root / f"{repo}.usearch"
            path.write_bytes(content)
            shards.append({
                "repo": repo, "snapshot": "working-tree", "path": str(path),
                "artifact_ref": path.name, "artifact_bytes": len(content),
                "artifact_sha256": hashlib.sha256(content).hexdigest(),
                "entries": [{"path": "src/workload.py", "line": 1, "chunk_id": repo}],
            })
        snapshots = {"workload": "working-tree", "second-workload": "working-tree"}
        state = {
            "chunk_schema_version": CHUNK_SCHEMA_VERSION, "card_version": CARD_VERSION,
            "embedding_input_version": SEMANTIC_EMBEDDING_INPUT_VERSION,
            "atlas_card_version": ATLAS_CARD_VERSION,
            "shard_manifest_version": SEMANTIC_SHARD_MANIFEST_VERSION,
            "backend": "usearch", "pack_id": "routed-pack", "dimension": 2, "stale": False,
            "pack_compatibility_identity": _injected_pack_identity("routed-pack"),
            "snapshots": snapshots, "entries": [], "shards": shards,
        }
        artifact = self.settings.state_dir / "generations" / "generation-000001" / "semantic.json"
        artifact.parent.mkdir(parents=True)
        artifact.write_text(json.dumps(state), encoding="utf-8")
        component = {
            "schema_version": semantic_schema_version(), "status": "ready",
            "content_hash": _content_hash(state),
            "artifact_ref": str(artifact.relative_to(self.settings.state_dir)),
            "details": {
                "pack_id": "routed-pack", "dimension": 2, "backend": "usearch",
                "pack_compatibility_identity": state["pack_compatibility_identity"],
                "snapshots": snapshots, "source_signature": source_signature(snapshots),
            },
        }

        class Generation:
            identity = "routed-generation"

            def __init__(self) -> None:
                self.snapshots = snapshots

            @staticmethod
            def component(name: str) -> dict[str, object]:
                return component if name == "semantic" else {}

        class Index:
            restored: list[str] = []

            @classmethod
            def restore(cls, path: str, view: bool = True) -> object:
                cls.restored.append(path)
                return cls()

            @staticmethod
            def search(vector: object, limit: int) -> list[object]:
                return []

        class Numpy:
            float32 = object()

            @staticmethod
            def asarray(value: object, dtype: object = None) -> object:
                return value

        # Preserve manifest metadata but corrupt the unselected repository's bytes.
        Path(str(shards[1]["path"])).write_bytes(b"corrupt-b")
        hashed: list[Path] = []

        def digest(path: Path) -> str:
            hashed.append(path)
            return hashlib.sha256(path.read_bytes()).hexdigest()

        _SERVING_STATE_CACHE.clear()
        _SHARD_HASH_CACHE.clear()
        serving: dict[str, str] = {}
        with (
            mock.patch("brain.semantic._usearch", return_value=(Index, Numpy)),
            mock.patch("brain.semantic._shard_sha256", side_effect=digest),
        ):
            results = search_semantic(
                self.settings, "workload", repos={"workload"}, embed=lambda values: [[1.0, 0.0]],
                generation=Generation(), serving_status=serving,
            )
        self.assertEqual([], results)
        self.assertEqual("ready", serving["status"])
        self.assertEqual([Path(str(shards[0]["path"]))], hashed)
        self.assertEqual([str(shards[0]["path"])], Index.restored)

        _SERVING_STATE_CACHE.clear()
        _SHARD_HASH_CACHE.clear()
        both_serving: dict[str, str] = {}
        with (
            mock.patch("brain.semantic._usearch", return_value=(Index, Numpy)),
            mock.patch("brain.semantic._shard_sha256", side_effect=digest),
        ):
            search_semantic(
                self.settings, "workload", repos={"workload", "second-workload"},
                embed=lambda values: [[1.0, 0.0]], generation=Generation(),
                serving_status=both_serving,
            )
        self.assertEqual("degraded", both_serving["status"])
        from brain.core import _effective_retrieval_edition

        self.assertEqual(
            "Degraded Core",
            _effective_retrieval_edition(
                "precision", semantic_used=True, reranker_used=True, semantic_status=both_serving["status"],
            ),
        )

    def test_semantic_progress_reports_cold_build_cache_reuse_and_generation_reuse(self) -> None:
        self.source.write_text(
            "\n".join(f"def private_progress_fixture_{index}():\n    return {index}" for index in range(12)) + "\n",
            encoding="utf-8",
        )
        self.second_source.write_text(
            "\n".join(f"def other_progress_fixture_{index}():\n    return {index}" for index in range(8)) + "\n",
            encoding="utf-8",
        )
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        (self.settings.state_dir / "model-tuning.json").write_text(
            json.dumps({"pack_id": "progress-pack", "recommendations": {"embedding_batch_size": 8}}), encoding="utf-8"
        )
        events: list[dict[str, object]] = []
        build_semantic_index(self.settings, embed=self._vectors, pack_id="progress-pack", progress=events.append)

        phases = [str(event["phase"]) for event in events]
        self.assertLess(phases.index("semantic_manifest"), phases.index("semantic_embedding"))
        self.assertLess(phases.index("semantic_embedding"), phases.index("semantic_shard"))
        self.assertEqual("semantic_publish", phases[-1])
        self.assertEqual("rebuilt", events[-1]["generation_state"])
        embedding = [event for event in events if event["phase"] == "semantic_embedding"]
        self.assertTrue(any(event.get("embedding_batch_size") == 8 for event in embedding))
        self.assertEqual(20, max(int(event.get("semantic_cards_total") or 0) for event in events))
        self.assertEqual(20, max(int(event.get("new_embeddings_completed") or 0) for event in events))
        self.assertEqual(0, min(int(event.get("cached_embeddings_reused") or 0) for event in embedding))
        self.assertEqual(0, int(events[-1].get("remaining_embeddings") or 0))
        safe_keys = {
            "phase", "phase_label", "elapsed_ms", "semantic_repository_current", "semantic_repository_total",
            "semantic_cards_discovered", "semantic_cards_total", "cached_embeddings_reused", "new_embeddings_completed",
            "remaining_embeddings", "embedding_batch_size", "embedding_batches_completed", "semantic_shards_completed",
            "semantic_shards_total", "semantic_shards_reused", "semantic_shards_rebuilt", "generation_state",
        }
        self.assertTrue(all(set(event) <= safe_keys for event in events))
        self.assertNotIn("private_progress_fixture", json.dumps(events))

        self.source.write_text(self.source.read_text(encoding="utf-8") + "\ndef changed_progress_fixture():\n    return 99\n", encoding="utf-8")
        rebuilt: list[dict[str, object]] = []
        build_semantic_index(self.settings, embed=self._vectors, pack_id="progress-pack", progress=rebuilt.append)
        self.assertGreater(max(int(event.get("cached_embeddings_reused") or 0) for event in rebuilt), 0)
        self.assertGreater(max(int(event.get("new_embeddings_completed") or 0) for event in rebuilt), 0)
        self.assertEqual("rebuilt", rebuilt[-1]["generation_state"])

        reused: list[dict[str, object]] = []
        build_semantic_index(self.settings, embed=self._vectors, pack_id="progress-pack", progress=reused.append)
        self.assertEqual("semantic_reuse", reused[-1]["phase"])
        self.assertEqual("reused", reused[-1]["generation_state"])
        self.assertEqual(0, reused[-1]["new_embeddings_completed"])

    def test_semantic_progress_failure_never_reports_publication(self) -> None:
        self.second_source.write_text("def private_transport_marker():\n    return 'TRANSPORT_FAIL'\n", encoding="utf-8")
        events: list[dict[str, object]] = []

        def failing(cards: list[str]) -> list[list[float]]:
            if any("TRANSPORT_FAIL" in card for card in cards):
                raise RemoteDisconnected("synthetic disconnect")
            return self._vectors(cards)

        with self.assertRaises(SemanticEmbeddingError):
            build_semantic_index(self.settings, embed=failing, pack_id="progress-failure-pack", progress=events.append)
        self.assertNotIn("semantic_publish", [event["phase"] for event in events])
        self.assertEqual("failed", events[-1]["generation_state"])
        self.assertNotIn("private_transport_marker", json.dumps(events))

    @unittest.skipUnless(importlib.util.find_spec("usearch"), "requires optional semantic extra")
    def test_unpublished_shards_do_not_replace_a_prior_generation(self) -> None:
        manifest = {"pack_id": "shard-pack", "embedding_dimension": 2, "document_instruction": "", "input_suffix": ""}

        def build_with(embed):
            runtime = mock.Mock()
            runtime.embed.side_effect = lambda cards, instruction="", dimension=None: embed(cards)
            with mock.patch("brain.semantic.active_pack", return_value=manifest), mock.patch("brain.semantic.runtime_for_pack", return_value=runtime):
                return build_semantic_index(self.settings)

        build_with(self._vectors)
        state_path = self.settings.state_dir / "semantic-index.json"
        published = state_path.read_bytes()
        published_paths = {Path(item["path"]) for item in json.loads(published)["shards"]}
        self.assertTrue(all(path.is_file() for path in published_paths))

        reuse_calls: list[list[str]] = []

        def tracked(cards: list[str]) -> list[list[float]]:
            reuse_calls.append(cards)
            return self._vectors(cards)

        self.assertEqual(build_with(self._vectors), build_with(tracked))
        self.assertEqual([], reuse_calls)

        self.second_source.write_text("def second_failure():\n    return 'TRANSPORT_FAIL'\n", encoding="utf-8")

        def failing(cards: list[str]) -> list[list[float]]:
            if any("TRANSPORT_FAIL" in card for card in cards):
                raise RemoteDisconnected("synthetic runtime disconnect")
            return self._vectors(cards)

        with self.assertRaises(SemanticEmbeddingError):
            build_with(failing)
        self.assertEqual(published, state_path.read_bytes())
        self.assertTrue(all(path.is_file() for path in published_paths))
        staged = set((self.settings.state_dir / "semantic-shards").glob("*.usearch")) - published_paths
        self.assertTrue(all(path.is_file() for path in staged))
        self.assertFalse(list((self.settings.state_dir / "semantic-shards").glob("*.building")))

    @unittest.skipUnless(importlib.util.find_spec("usearch"), "requires optional semantic extra")
    def test_cli_semantic_rebuild_recovers_from_real_http_disconnects(self) -> None:
        self.source.write_text("\n".join(f"def feature_{index}():\n    return {index}" for index in range(16)) + "\n", encoding="utf-8")
        request_sizes: list[int] = []

        class EmbeddingHandler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return None

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                inputs = json.loads(body.decode("utf-8"))["input"]
                request_sizes.append(len(inputs))
                if len(inputs) > 1:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                encoded = json.dumps({"data": [{"embedding": [1.0, 0.0]}]}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        server = ThreadingHTTPServer(("127.0.0.1", 0), EmbeddingHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        runtime = ManagedLlamaCppRuntime({})
        client = LlamaCppRuntime(f"http://127.0.0.1:{server.server_port}", direct_loopback=True)
        manifest = {"pack_id": "managed-http-fixture", "embedding_dimension": 2, "document_instruction": "", "input_suffix": ""}
        try:
            with mock.patch.object(runtime, "_start", return_value=client), mock.patch.object(runtime, "shutdown", wraps=runtime.shutdown) as shutdown, mock.patch(
                "brain.semantic.active_pack", return_value=manifest
            ), mock.patch("brain.semantic.runtime_for_pack", return_value=runtime), redirect_stdout(io.StringIO()):
                self.assertEqual(0, main(["-c", str(self.config), "index", "rebuild", "--backend", "semantic"]))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)

        self.assertEqual([16, 16, 8, 8, 4, 4, 2, 2, 1], request_sizes[:9])
        self.assertGreaterEqual(shutdown.call_count, 9)
        self.assertGreater(capabilities(self.settings)["semantic_chunks"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

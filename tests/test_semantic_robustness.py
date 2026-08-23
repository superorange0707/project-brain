from __future__ import annotations

import json
import importlib.util
import io
import socket
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.client import RemoteDisconnected
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from brain.cli import main
from brain.core import load_settings
from brain.editions import capabilities
from brain.models import LlamaCppRuntime, ManagedLlamaCppRuntime, embedding_request_bytes
from brain.semantic import (
    SEMANTIC_MAX_CARD_INPUT_BYTES,
    SEMANTIC_MAX_REQUEST_BODY_BYTES,
    Chunk,
    SemanticEmbeddingError,
    _bounded_embedding_batches,
    _bounded_semantic_card,
    _cache_vectors,
    build_semantic_index,
    chunk_source,
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
            "semantic_shards_total", "generation_state",
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
        self.assertTrue(staged)

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

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from brain.cli import _refresh_all, main
from brain.core import load_settings
from brain.ops import RefreshOutcome, format_refresh_progress, refresh_brain
from brain.sync import SyncResult
from brain.ui import _Server, serve_ui


class LocalUiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        repo = self.root / "service-a"
        (repo / "src/main/java/demo").mkdir(parents=True)
        (repo / "src/main/java/demo/HelloService.java").write_text(
            "package demo;\nclass HelloService { String hello() { return \"hello\"; } }\n",
            encoding="utf-8",
        )
        self.config = self.root / "brain.toml"
        self.config.write_text(
            '[project]\nname="ui-demo"\n[graph]\nenabled=false\n[[repositories]]\nname="service-a"\npath="service-a"\n',
            encoding="utf-8",
        )
        self.settings = load_settings(self.config)
        self.token = "test-local-token"
        self.server = _Server(("127.0.0.1", 0), self.settings, self.token)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temporary.cleanup()

    def get(self, path: str, *, authorized: bool = True) -> tuple[int, dict | str, dict]:
        headers = {"X-Brain-Token": self.token} if authorized else {}
        request = Request(self.base + path, headers=headers)
        with urlopen(request, timeout=5) as response:
            content_type = response.headers.get("Content-Type", "")
            raw = response.read().decode("utf-8")
            value = json.loads(raw) if "application/json" in content_type else raw
            return response.status, value, dict(response.headers)

    def post(self, path: str, body: dict) -> tuple[int, dict, dict]:
        request = Request(
            self.base + path,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={"X-Brain-Token": self.token, "Content-Type": "application/json"},
        )
        with urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read()), dict(response.headers)

    def job(self, job_id: str) -> dict:
        for _ in range(50):
            _, value, _ = self.get("/api/job?id=" + quote(job_id, safe=""))
            job = value["data"]
            if job["status"] not in {"pending", "running"}:
                return job
            time.sleep(0.02)
        self.fail("operation job did not finish")

    @staticmethod
    def refresh_outcome(*, aligned: bool = True, status: str = "ready") -> RefreshOutcome:
        return RefreshOutcome(
            additions=[],
            sync=[],
            graph=[],
            experience={"cases": 0, "evaluated_sessions": 0},
            semantic={"required": True, "status": status, "aligned": aligned, "chunks": 1, "reason": None if aligned else "Semantic generation is not aligned."},
        )

    def test_ui_page_and_api_are_local_token_protected(self) -> None:
        status, html, headers = self.get("/", authorized=False)
        self.assertEqual(200, status)
        self.assertIn("Project Brain", html)
        self.assertIn("Branch / status", html)
        self.assertIn('esc(repo.ref || "working tree")', html)
        self.assertIn("Continue with AI", html)
        self.assertIn("Retrieval plan", html)
        self.assertIn("M365 agent", html)
        self.assertIn("Refresh Brain", html)
        self.assertIn("Operations cockpit", html)
        self.assertIn("Local model packs", html)
        self.assertIn("Retrieval transparency", html)
        self.assertIn('id="refresh-progress"', html)
        self.assertIn("Reused published generation", html)
        self.assertIn("Current batch", html)
        self.assertIn("Ticket memory", html)
        self.assertIn('id="experience-count"', html)
        self.assertIn('document.getElementById("request-text").value = ""', html)
        self.assertIn('setDelivery(data.delivery, "view-request")', html)
        self.assertNotIn('document.querySelectorAll("[data-output]")', html)
        self.assertIn('id="delete-session"', html)
        self.assertIn("Repositories, branches, and source files will not be touched", html)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        with self.assertRaises(HTTPError) as caught:
            self.get("/api/status", authorized=False)
        self.assertEqual(403, caught.exception.code)
        caught.exception.close()

        _, status_data, _ = self.get("/api/status")
        self.assertEqual(0, status_data["data"]["summary"]["experience_cases"])

    def test_request_preview_start_context_feedback_and_artifacts(self) -> None:
        request_text = """The next evidence I need is:
```yaml
CONTEXT_REQUEST:
  version: 1
  objective: Find HelloService and its usage.
  searches:
    - query: HelloService
      repos: [service-a]
  symbols:
    - name: HelloService
      repos: [service-a]
      include: [definition, callers, tests]
  files: []
  history: []
```
"""
        _, conversation, _ = self.post("/api/preview", {"text": "Which environment is active?"})
        self.assertTrue(conversation["data"]["valid"])
        self.assertEqual("conversation", conversation["data"]["kind"])

        _, preview, _ = self.post("/api/preview", {"text": request_text})
        self.assertTrue(preview["data"]["valid"])
        self.assertEqual(4, preview["data"]["operation_count"])

        _, started, _ = self.post(
            "/api/start",
            {"ticket": "UI-1", "ticket_text": "Find how hello works.", "sync": False, "target": "claude"},
        )
        self.assertEqual("UI-1", started["data"]["ticket"])
        self.assertIn("PROJECT BRAIN — START", started["data"]["delivery"]["content"])

        _, context, _ = self.post(
            "/api/continue",
            {"ticket": "UI-1", "text": request_text, "include_diff": False, "target": "claude"},
        )
        self.assertEqual(1, context["data"]["request"])
        self.assertEqual("context_request", context["data"]["kind"])
        self.assertIn("HelloService.java", context["data"]["delivery"]["content"])
        retrieval = context["data"]["session"]["retrieval"]
        self.assertEqual("core", retrieval["requested_edition"])
        self.assertEqual("Core", retrieval["effective_edition"])
        self.assertIn("candidate_count", retrieval)

        _, duplicate, _ = self.post("/api/preview", {"ticket": "UI-1", "text": request_text})
        self.assertEqual(1, duplicate["data"]["duplicate_of"])

        _, final, _ = self.post(
            "/api/continue",
            {"ticket": "UI-1", "text": "FINAL_SOLUTION\nChange HelloService.", "target": "m365"},
        )
        self.assertEqual("final_solution", final["data"]["kind"])
        self.assertEqual("ready_to_implement", final["data"]["session"]["status"])
        self.assertTrue((self.root / ".runs/UI-1/current-handoff.md").is_file())
        self.assertTrue((self.root / "generated/handoffs/UI-1-current.md").is_file())

        _, feedback, _ = self.post(
            "/api/feedback",
            {
                "ticket": "UI-1",
                "notes": "Changed the method.",
                "test_command": "mvn test",
                "test_output": "BUILD SUCCESS",
                "include_diff": True,
                "repos": [],
                "target": "claude",
            },
        )
        self.assertEqual(1, feedback["data"]["feedback"])
        self.assertIn("BUILD SUCCESS", feedback["data"]["delivery"]["content"])

        _, detail, _ = self.get("/api/session?ticket=UI-1")
        names = {item["name"] for item in detail["data"]["artifacts"]}
        self.assertIn("start.md", names)
        self.assertIn("context-001.md", names)
        self.assertIn("feedback-001.md", names)
        self.assertIn("final-solution.md", names)
        self.assertNotIn("current-handoff.md", names)

        _, artifact, _ = self.get("/api/artifact?ticket=UI-1&name=context-001.md")
        self.assertIn("HelloService.java", artifact["data"]["content"])

        _, kit, _ = self.post("/api/agent-kit", {})
        self.assertIn("Project Brain", kit["data"]["instructions"])
        self.assertIn("Investigate a ticket", kit["data"]["suggested_prompts"])
        self.assertTrue(Path(kit["data"]["instructions_path"]).is_file())

    def test_artifact_path_traversal_is_rejected(self) -> None:
        self.post("/api/start", {"ticket": "UI-2", "ticket_text": "Test security.", "sync": False})
        with self.assertRaises(HTTPError) as caught:
            self.get("/api/artifact?ticket=UI-2&name=" + quote("../session.json", safe=""))
        self.assertEqual(400, caught.exception.code)
        caught.exception.close()

    def test_sync_discovers_a_newly_cloned_repository_once(self) -> None:
        (self.root / "service-b/.git").mkdir(parents=True)

        _, refreshed, _ = self.post("/api/sync", {})
        self.assertEqual(["service-b"], refreshed["data"]["discovered"])
        self.assertEqual(2, refreshed["data"]["status"]["summary"]["repositories"])
        self.assertIn('name = "service-b"', self.config.read_text(encoding="utf-8"))

        _, repeated, _ = self.post("/api/sync", {})
        self.assertEqual([], repeated["data"]["discovered"])
        self.assertEqual(1, self.config.read_text(encoding="utf-8").count('name = "service-b"'))

    def test_cli_and_ui_sync_delegate_to_the_same_refresh_service(self) -> None:
        outcome = self.refresh_outcome()
        with patch("brain.ops.refresh_brain", return_value=outcome) as refresh:
            _, synced, _ = self.post("/api/sync", {})
            additions, results, graphs = _refresh_all(self.settings, fetch=True, discover=True)
        self.assertEqual([], synced["data"]["discovered"])
        self.assertEqual([], additions)
        self.assertEqual([], results)
        self.assertEqual([], graphs)
        self.assertEqual(2, refresh.call_count)

    def test_shared_refresh_builds_semantic_generation_when_edition_requires_it(self) -> None:
        semantic = {"required": True, "status": "ready", "aligned": True, "chunks": 3, "reason": None}
        with patch("brain.core.discover_and_configure_repositories", return_value=[]), patch(
            "brain.sync.sync_repositories", return_value=[]
        ), patch("brain.core.snapshot_indexes", return_value=({}, [])), patch("brain.core.generate_map"), patch(
            "brain.relations.generate_relationship_map"
        ), patch("brain.experience.build_experience_index", return_value={"cases": []}), patch(
            "brain.experience.evaluate_sessions", return_value={"evaluated_sessions": 0}
        ), patch("brain.graph.index_graph", return_value=[]), patch(
            "brain.editions.current_edition", return_value="semantic"
        ), patch("brain.semantic.build_semantic_index", return_value={"chunks": 3}) as build, patch(
            "brain.ops.semantic_status", return_value=semantic
        ):
            outcome = refresh_brain(self.settings)
        build.assert_called_once_with(self.settings)
        self.assertEqual("ready", outcome.semantic["status"])
        self.assertTrue(outcome.semantic["aligned"])

    def test_shared_refresh_emits_ordered_safe_structured_events_and_cli_uses_them(self) -> None:
        semantic = {"required": True, "status": "ready", "aligned": True, "chunks": 3, "reason": None}
        events: list[dict] = []

        def semantic_build(_settings, *, progress=None):
            self.assertIsNotNone(progress)
            progress({
                "phase": "semantic_embedding",
                "semantic_cards_discovered": 3,
                "semantic_cards_total": 3,
                "cached_embeddings_reused": 2,
                "new_embeddings_completed": 1,
                "remaining_embeddings": 0,
                "embedding_batch_size": 8,
                "embedding_batches_completed": 1,
                "semantic_shards_completed": 1,
                "semantic_shards_total": 1,
                "generation_state": "private-runtime-token",
                "ignored_private_source": "private-token-and-path",
            })
            progress({"phase": "semantic_publish", "generation_state": "rebuilt"})
            return {"chunks": 3}

        with patch("brain.core.discover_and_configure_repositories", return_value=[]), patch(
            "brain.sync.sync_repositories", return_value=[SyncResult("service-a", "current", None, None, None, False)]
        ), patch("brain.core.snapshot_indexes", return_value=({}, ["service-a"])), patch("brain.core.generate_map"), patch(
            "brain.relations.generate_relationship_map"
        ), patch("brain.experience.build_experience_index", return_value={"cases": []}), patch(
            "brain.experience.evaluate_sessions", return_value={"evaluated_sessions": 0}
        ), patch("brain.graph.index_graph", return_value=[]), patch(
            "brain.editions.current_edition", return_value="semantic"
        ), patch("brain.semantic.build_semantic_index", side_effect=semantic_build), patch(
            "brain.ops.semantic_status", return_value=semantic
        ):
            refresh_brain(self.settings, progress=events.append)

        phases = [event["phase"] for event in events]
        self.assertLess(phases.index("discovery"), phases.index("sync"))
        self.assertLess(phases.index("core_index"), phases.index("semantic_manifest"))
        self.assertLess(phases.index("semantic_embedding"), phases.index("semantic_publish"))
        self.assertEqual("complete", phases[-1])
        self.assertEqual(1, events[-1]["repository_current"])
        self.assertEqual(3, events[-1]["semantic_cards_total"])
        self.assertEqual(2, events[-1]["cached_embeddings_reused"])
        safe_keys = {
            "phase", "phase_label", "elapsed_ms", "repository_current", "repository_total", "repositories_changed",
            "repositories_unchanged", "semantic_cards_discovered", "semantic_cards_total", "cached_embeddings_reused",
            "new_embeddings_completed", "remaining_embeddings", "embedding_batch_size", "embedding_batches_completed",
            "semantic_shards_completed", "semantic_shards_total", "generation_state", "semantic_status",
        }
        self.assertTrue(all(set(event) <= safe_keys for event in events))
        self.assertNotIn("private-token-and-path", json.dumps(events))
        self.assertNotIn("private-runtime-token", json.dumps(events))
        self.assertIn("cards 3/3", format_refresh_progress(events[-1]))

        output = []
        outcome = self.refresh_outcome()

        def cli_refresh(*_args, progress=None, **_kwargs):
            progress(events[-1])
            return outcome

        with patch("brain.ops.refresh_brain", side_effect=cli_refresh), patch("builtins.print", side_effect=lambda *parts, **_kwargs: output.append(" ".join(map(str, parts)))):
            self.assertEqual(0, main(["-c", str(self.config), "refresh", "--no-fetch", "--no-discover"]))
        self.assertTrue(any("cards 3/3" in line for line in output))

    def test_start_with_sync_refuses_unaligned_semantic_without_explicit_choice(self) -> None:
        outcome = self.refresh_outcome(aligned=False, status="failed")
        dashboard = {"effective": "Degraded", "reason": "Semantic generation is not aligned"}
        with patch("brain.ops.refresh_brain", return_value=outcome), patch("brain.editions.current_edition", return_value="precision"), patch(
            "brain.ops.dashboard_status", return_value=dashboard
        ):
            with self.assertRaises(HTTPError) as caught:
                self.post("/api/start", {"ticket": "UI-STALE", "ticket_text": "Do not start stale Precision.", "sync": True})
        self.assertEqual(400, caught.exception.code)
        caught.exception.close()
        self.assertFalse((self.root / ".runs/UI-STALE").exists())

    def test_start_with_sync_refuses_precision_when_reranker_is_unavailable(self) -> None:
        dashboard = {"effective": "Degraded", "reason": "Verified compatible reranker pack is unavailable"}
        with patch("brain.ops.refresh_brain", return_value=self.refresh_outcome()), patch(
            "brain.editions.current_edition", return_value="precision"
        ), patch("brain.ops.dashboard_status", return_value=dashboard):
            with self.assertRaises(HTTPError) as caught:
                self.post("/api/start", {"ticket": "UI-NO-RERANK", "ticket_text": "Do not silently downgrade Precision.", "sync": True})
        self.assertEqual(400, caught.exception.code)
        caught.exception.close()
        self.assertFalse((self.root / ".runs/UI-NO-RERANK").exists())

    def test_dashboard_and_invalid_edition_transition_are_explicit(self) -> None:
        _, status, _ = self.get("/api/status")
        brain = status["data"]["brain"]
        self.assertIn(brain["health"], {"Healthy", "Degraded", "Action required"})
        self.assertIn("semantic", brain)
        self.assertEqual("loopback direct enforced", brain["managed_runtime"])

        code, requested, _ = self.post("/api/edition", {"edition": "precision", "refresh": False})
        self.assertEqual(202, code)
        job = self.job(requested["data"]["id"])
        self.assertEqual("failed", job["status"])
        self.assertIn("edition requires", job["error"])
        _, after, _ = self.get("/api/status")
        self.assertEqual("core", after["data"]["brain"]["edition"])

    def test_model_operations_reject_arbitrary_sources_and_keep_errors_safe(self) -> None:
        with patch("brain.ops.model_operation", side_effect=RuntimeError("proxy://user:secret@example.invalid")):
            code, requested, _ = self.post("/api/model", {"action": "install", "pack": "https://example.invalid/pack"})
            self.assertEqual(202, code)
            job = self.job(requested["data"]["id"])
        self.assertEqual("failed", job["status"])
        self.assertEqual("Operation failed (RuntimeError).", job["error"])
        self.assertNotIn("secret", job["error"])

    def test_refresh_jobs_report_progress_and_reject_overlapping_mutations(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        outcome = self.refresh_outcome()

        def slow_refresh(*args, **kwargs):
            progress = kwargs.get("progress")
            if progress:
                progress({
                    "phase": "semantic_embedding", "phase_label": "private-source-contents", "elapsed_ms": 10,
                    "semantic_cards_discovered": 4, "semantic_cards_total": 10, "cached_embeddings_reused": 3,
                    "new_embeddings_completed": 1, "remaining_embeddings": 6, "embedding_batch_size": 8,
                    "embedding_batches_completed": 1,
                })
            entered.set()
            self.assertTrue(release.wait(3))
            if progress:
                progress({
                    "phase": "semantic_embedding", "phase_label": "Building Semantic index", "elapsed_ms": 20,
                    "semantic_cards_discovered": 10, "semantic_cards_total": 10, "cached_embeddings_reused": 3,
                    "new_embeddings_completed": 7, "remaining_embeddings": 0, "embedding_batch_size": 4,
                    "embedding_batches_completed": 3,
                })
            return outcome

        with patch("brain.ops.refresh_brain", side_effect=slow_refresh):
            code, requested, _ = self.post("/api/refresh", {"fetch": False, "discover": False})
            self.assertEqual(202, code)
            self.assertTrue(entered.wait(3))
            _, running, _ = self.get("/api/job?id=" + quote(requested["data"]["id"], safe=""))
            first = running["data"]["progress"]
            self.assertEqual("semantic_embedding", first["phase"])
            self.assertEqual("Building Semantic index", first["phase_label"])
            self.assertEqual(4, first["semantic_cards_discovered"])
            with self.assertRaises(HTTPError) as caught:
                self.post("/api/refresh", {"fetch": False, "discover": False})
            self.assertEqual(400, caught.exception.code)
            caught.exception.close()
            release.set()
            job = self.job(requested["data"]["id"])
        self.assertEqual("succeeded", job["status"])
        self.assertEqual("Completed", job["phase"])
        self.assertGreaterEqual(job["progress"]["semantic_cards_discovered"], first["semantic_cards_discovered"])
        self.assertGreaterEqual(job["progress"]["new_embeddings_completed"], first["new_embeddings_completed"])
        self.assertLessEqual(job["progress"]["remaining_embeddings"], first["remaining_embeddings"])
        self.assertNotIn("private", json.dumps(job["progress"]))

    def test_delete_session_removes_only_brain_history(self) -> None:
        self.post(
            "/api/start",
            {"ticket": "UI-DELETE", "ticket_text": "Delete this history.", "sync": False, "target": "m365"},
        )
        self.post(
            "/api/start",
            {"ticket": "UI-DELETE-OTHER", "ticket_text": "Keep this history.", "sync": False, "target": "m365"},
        )

        _, deleted, _ = self.post("/api/session/delete", {"ticket": "UI-DELETE"})
        self.assertEqual("UI-DELETE", deleted["data"]["ticket"])
        self.assertFalse((self.root / ".runs/UI-DELETE").exists())
        self.assertFalse((self.root / "generated/handoffs/UI-DELETE-current.md").exists())
        self.assertFalse((self.root / "generated/handoffs/UI-DELETE-start.md").exists())
        self.assertTrue((self.root / ".runs/UI-DELETE-OTHER").is_dir())
        self.assertTrue((self.root / "generated/handoffs/UI-DELETE-OTHER-current.md").is_file())
        self.assertTrue((self.root / "service-a").is_dir())


class UiShutdownTest(unittest.TestCase):
    @patch("brain.ui._Server")
    def test_repeated_interrupt_during_shutdown_is_quiet(self, server_type) -> None:
        server = server_type.return_value
        server.server_address = ("127.0.0.1", 8765)
        server.serve_forever.side_effect = KeyboardInterrupt
        server.server_close.side_effect = KeyboardInterrupt

        serve_ui(object(), port=8765, open_browser=False)

        server.server_close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

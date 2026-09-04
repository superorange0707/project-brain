from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from brain.auto_refresh import AutoRefreshService, FreshnessDecision
from brain.cli import _refresh_all, main
from brain.core import BrainError, load_settings, session_dir, start_session
from brain.ops import RefreshOutcome, format_refresh_progress, refresh_brain
from brain.sync import SyncResult
from brain.ui import (
    MAX_SESSION_ARTIFACT_RESULTS,
    MAX_SESSION_RESULTS,
    _OperationCoordinator,
    _Server,
    _artifact,
    _session_artifacts,
    _session_detail,
    _sessions,
    project_status,
    serve_ui,
    ui_instance,
)


class OperationCoordinatorTest(unittest.TestCase):
    def test_two_tickets_run_concurrently_but_same_ticket_and_mutation_are_blocked(self) -> None:
        coordinator = _OperationCoordinator(max_retrievals=2)
        entered = [threading.Event(), threading.Event()]
        release = threading.Event()

        def operation(index):
            def run(progress):
                progress({"phase": "repo_routing", "phase_label": "private source", "repo_total": 6, "candidate_count": index})
                entered[index].set()
                release.wait(3)
                return {"ticket": f"TICKET-{index}"}
            return run

        first = coordinator.start("retrieval", operation(0), kind="retrieval", ticket="TICKET-A")
        second = coordinator.start("retrieval", operation(1), kind="retrieval", ticket="TICKET-B")
        self.assertTrue(all(event.wait(3) for event in entered))
        with self.assertRaisesRegex(BrainError, "this ticket"):
            coordinator.start("retrieval", operation(0), kind="retrieval", ticket="TICKET-A")
        with self.assertRaisesRegex(BrainError, "state-changing operation"):
            coordinator.start("refresh", operation(0))
        jobs = coordinator.list()
        self.assertEqual({"TICKET-A", "TICKET-B"}, {job["ticket"] for job in jobs})
        self.assertNotIn("private", json.dumps(jobs))
        release.set()
        for job in (first, second):
            for _ in range(100):
                if coordinator.get(job["id"])["status"] == "succeeded":
                    break
                time.sleep(0.01)
            self.assertEqual("succeeded", coordinator.get(job["id"])["status"])

    def test_interrupted_refresh_progress_is_restored_without_claiming_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            coordinator = _OperationCoordinator(state_dir=state)
            release = threading.Event()
            started = threading.Event()

            def refresh(progress):
                progress({"phase": "semantic_embeddings", "semantic_cards_total": 50, "new_embeddings_completed": 12})
                coordinator._last_persist = 0
                progress({"phase": "semantic_embeddings", "semantic_cards_total": 50, "new_embeddings_completed": 16})
                started.set()
                release.wait(3)
                return {}

            job = coordinator.start("refresh", refresh, resume={"fetch": True, "discover": False})
            self.assertTrue(started.wait(3))
            restored = _OperationCoordinator(state_dir=state).list()
            self.assertEqual(1, len(restored))
            self.assertEqual("interrupted", restored[0]["status"])
            self.assertEqual(16, restored[0]["progress"]["new_embeddings_completed"])
            self.assertEqual({"fetch": True, "discover": False}, restored[0]["resume"])
            self.assertNotEqual("succeeded", restored[0]["status"])
            resumed = _OperationCoordinator(state_dir=state)
            resumed_job = resumed.start("refresh", lambda _progress: {})
            for _ in range(100):
                if resumed.get(resumed_job["id"])["status"] == "succeeded":
                    break
                time.sleep(0.01)
            self.assertFalse(any(item["status"] == "interrupted" for item in resumed.list()))
            release.set()
            for _ in range(100):
                if coordinator.get(job["id"])["status"] == "succeeded":
                    break
                time.sleep(0.01)

    def test_capacity_failure_returns_a_safe_one_click_recovery(self) -> None:
        from brain.ops import StateCapacityError

        coordinator = _OperationCoordinator()

        def fail(_progress):
            raise StateCapacityError(
                "state_inventory_limit",
                "Project Brain state capacity scan budget exceeded; no data was changed",
            )

        started = coordinator.start("refresh", fail)
        for _ in range(100):
            job = coordinator.get(started["id"])
            if job["status"] == "failed":
                break
            time.sleep(0.01)
        self.assertEqual("failed", job["status"])
        self.assertEqual("safe_gc", job["recovery"]["action"])
        self.assertIn("no data was changed", job["error"])


class SessionSummaryTest(unittest.TestCase):
    def test_ui_refresh_reloads_repository_configuration_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "service-a").mkdir()
            (root / "service-b").mkdir()
            config = root / "brain.toml"
            config.write_text(
                '[project]\nname="ui-reload"\n[[repositories]]\nname="service-a"\npath="service-a"\n',
                encoding="utf-8",
            )
            settings = load_settings(config)
            server = object.__new__(_Server)
            server.settings = settings
            config.write_text(
                config.read_text(encoding="utf-8")
                + '[[repositories]]\nname="service-b"\npath="service-b"\n',
                encoding="utf-8",
            )
            reloaded = server.reload_settings()
            self.assertIs(settings, reloaded)
            self.assertEqual({"service-a", "service-b"}, {repo.name for repo in settings.repositories})

    def test_project_status_never_reads_symlinked_graph_or_evaluation_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "service").mkdir()
            config = root / "brain.toml"
            config.write_text(
                "[project]\nname='ui-managed-state'\n"
                "[[repositories]]\nname='service'\npath='service'\n",
                encoding="utf-8",
            )
            settings = load_settings(config)
            settings.repo("service").source_sha = "sha-g1"
            settings.state_dir.mkdir(parents=True, exist_ok=True)
            outside_graph = root / "outside-graphs.json"
            outside_eval = root / "outside-eval.json"
            outside_graph.write_text(json.dumps({"service": {"sha": "sha-g1"}}), encoding="utf-8")
            outside_eval.write_text(json.dumps({"evaluated_sessions": 999}), encoding="utf-8")
            try:
                (settings.state_dir / "graphs.json").symlink_to(outside_graph)
                (settings.state_dir / "experience-eval.json").symlink_to(outside_eval)
            except OSError as error:
                self.skipTest(f"file symlinks unavailable: {error}")
            dashboard = {
                "edition": "core", "capabilities": {}, "freshness": {"components": {}},
                "health": "Action required", "core": {"ready": False},
            }
            with (
                patch("brain.ui.load_source_state", return_value={
                    "service": {"status": "current", "sha": "sha-g1"},
                }),
                patch("brain.ui.load_index_state", return_value={"service": {"sha": "sha-g1"}}),
                patch("brain.ui.load_experience_index", return_value={}),
                patch("brain.ui._sessions", return_value=[]),
                patch("brain.ops.dashboard_status", return_value=dashboard),
                patch("brain.ops.storage", return_value={}),
                patch("brain.metrics.benchmark_report", return_value={}),
                patch("brain.catalog.current_generation", return_value=None),
            ):
                status = project_status(settings)
            self.assertFalse(status["repositories"][0]["structural"])
            self.assertEqual(0, status["summary"]["evaluated_sessions"])
            self.assertEqual(999, json.loads(outside_eval.read_text(encoding="utf-8"))["evaluated_sessions"])

    def test_cockpit_session_summaries_reject_unsafe_state_and_cap_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "service").mkdir()
            config = root / "brain.toml"
            config.write_text(
                "[project]\nname='ui-session-bounds'\n[graph]\nenabled=false\n"
                "[[repositories]]\nname='service'\npath='service'\n",
                encoding="utf-8",
            )
            settings = load_settings(config)
            secret = root / "outside-session.json"
            secret.write_text(json.dumps({"ticket": "OUTSIDE-SECRET"}), encoding="utf-8")
            linked = settings.runs_dir / "LINKED"
            linked.mkdir()
            try:
                (linked / "session.json").symlink_to(secret)
            except OSError as error:
                self.skipTest(f"symbolic links unavailable: {error}")
            oversized = settings.runs_dir / "OVERSIZED"
            oversized.mkdir()
            (oversized / "session.json").write_bytes(b"x" * (4 * 1024 * 1024 + 1))
            for index in range(MAX_SESSION_RESULTS + 5):
                directory = settings.runs_dir / f"SAFE-{index:03d}"
                directory.mkdir()
                (directory / "session.json").write_text(
                    json.dumps({"ticket": f"SAFE-{index:03d}", "requests": index}),
                    encoding="utf-8",
                )
            summaries = _sessions(settings)
            self.assertLessEqual(len(summaries), MAX_SESSION_RESULTS)
            rendered = json.dumps(summaries)
            self.assertNotIn("OUTSIDE-SECRET", rendered)
            self.assertNotIn("OVERSIZED", rendered)

            detail = settings.runs_dir / "DETAIL"
            detail.mkdir()
            (detail / "session.json").write_text(
                json.dumps({"ticket": "DETAIL", "requests": 0}), encoding="utf-8",
            )
            ticket = detail / "ticket.md"
            ticket.symlink_to(secret)
            with self.assertRaisesRegex(BrainError, "Invalid managed session artifact"):
                _session_detail(settings, "DETAIL")
            ticket.unlink()
            ticket.write_bytes(b"x" * (1024 * 1024 + 1))
            with self.assertRaisesRegex(BrainError, "exceeds its byte limit"):
                _session_detail(settings, "DETAIL")
            ticket.write_text("bounded ticket", encoding="utf-8")
            for index in range(MAX_SESSION_ARTIFACT_RESULTS + 5):
                (detail / f"context-{index:03d}.md").write_text("bounded", encoding="utf-8")
            artifacts = _session_artifacts(settings, "DETAIL")
            self.assertLessEqual(len(artifacts), MAX_SESSION_ARTIFACT_RESULTS)
            linked_artifact = detail / "context-linked.md"
            linked_artifact.symlink_to(secret)
            with self.assertRaisesRegex(BrainError, "Artifact does not exist"):
                _artifact(settings, "DETAIL", linked_artifact.name)


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
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            _, value, _ = self.get("/api/job?id=" + quote(job_id, safe=""))
            job = value["data"]
            if job["status"] not in {"pending", "running"}:
                return job
            time.sleep(0.05)
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
        self.assertIn("Detailed profiler", html)
        self.assertIn('id="refresh-progress"', html)
        self.assertIn('id="auto-refresh-mode"', html)
        self.assertIn('id="storage-recover"', html)
        self.assertIn("Safely reclaim unpinned state", html)
        self.assertIn('value="when_idle">When idle', html)
        self.assertIn("Reused published generation", html)
        self.assertIn("resumeActiveRefresh(data)", html)
        self.assertIn("state.refreshJobId === job.id", html)
        self.assertIn("Current batch", html)
        self.assertIn("estimated remaining", html)
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
        self.assertNotIn(str(self.root), json.dumps(status_data["data"]["retrieval"]["storage"]))
        self.assertIsInstance(self.server.auto_refresh, AutoRefreshService)

    def test_storage_recovery_uses_reachability_gc_and_never_returns_paths(self) -> None:
        private = str(self.root / "state/snapshots/private/sha")
        report = {
            "dry_run": False,
            "pinned_generations": [1],
            "pinned_snapshots": [str(self.root / "state/snapshots/pinned/sha")],
            "remove": [{"kind": "snapshot", "path": private, "bytes": 4096}],
            "reclaim_bytes": 4096,
            "semantic_gc_blocked": [],
            "reachability_gc_blocked": [],
        }
        with patch("brain.ops.gc", return_value=report) as collected:
            code, requested, _ = self.post("/api/gc", {"apply": True})
            self.assertEqual(202, code)
            job = self.job(requested["data"]["id"])
        collected.assert_called_once_with(self.settings, dry_run=False, keep_recent=2)
        self.assertEqual("succeeded", job["status"])
        self.assertTrue(job["result"]["applied"])
        self.assertEqual({"snapshot": 1}, job["result"]["counts"])
        self.assertNotIn(str(self.root), json.dumps(job))

    def test_auto_refresh_preference_api_exposes_only_safe_local_status(self) -> None:
        self.server.auto_refresh._detector = lambda _settings: FreshnessDecision.ready()

        _, enabled, _ = self.post("/api/auto-refresh", {"mode": "when_idle"})
        self.assertEqual("when_idle", enabled["data"]["mode"])
        _, status, _ = self.get("/api/status")
        auto = status["data"]["auto_refresh"]
        self.assertEqual(
            {"mode", "last_check", "last_refresh", "pending", "pending_reason", "status"},
            set(auto),
        )
        self.assertNotIn(str(self.root), json.dumps(auto))
        self.assertEqual("when_idle", json.loads(
            (self.settings.state_dir / "auto-refresh.json").read_text(encoding="utf-8")
        )["mode"])

        _, disabled, _ = self.post("/api/auto-refresh", {"mode": "off"})
        self.assertEqual("off", disabled["data"]["mode"])

    def test_auto_refresh_job_failure_never_exposes_a_path(self) -> None:
        with patch("brain.ops.refresh_brain", side_effect=BrainError(f"failed below {self.root}")):
            with self.assertRaises(BrainError):
                self.server._auto_refresh()

        job = self.server.operations.list()[0]
        self.assertEqual("Operation failed (BrainError).", job["error"])
        self.assertNotIn(str(self.root), json.dumps(job))

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
            {"ticket": "UI-1", "text": """FINAL_SOLUTION
## Ticket interpretation and remaining assumptions
The requested change is bounded.
## Verified current behavior
Pinned source verifies the behavior.
## Ordered execution flow and integration flow
The ordered flow is established.
## Root cause
The verified branch omits the case.
## Exact repositories, files, symbols, and configuration/data
Exact paths and symbols are listed.
## Suggested production changes
Update the existing branch.
## Impact and test surfaces; tests and assertions
The exact test assertions are listed.
## Validation commands
Run the approved tests.
## Edge cases and compatibility risks
Compatibility is preserved.
## Implementation order
Source, tests, validation.
## Remaining assumptions
None beyond the stated boundary.
""", "target": "m365"},
        )
        self.assertEqual("final_solution", final["data"]["kind"])
        self.assertEqual("ready_to_implement", final["data"]["session"]["status"])
        self.assertTrue((self.root / ".runs/UI-1/current-handoff.md").is_file())
        self.assertTrue((self.root / "generated/handoffs/UI-1/current.md").is_file())

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

    def test_sync_adds_new_repository_to_config(self) -> None:
        (self.root / "service-b/.git").mkdir(parents=True)
        _, response, _ = self.post("/api/sync", {})
        self.assertEqual(["service-b"], response["data"]["discovered"])
        self.assertEqual({"service-a", "service-b"}, {repo.name for repo in self.settings.repositories})
        self.assertEqual({"service-a", "service-b"}, {repo.name for repo in load_settings(self.config).repositories})

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
        ), patch("brain.graph.index_graph", return_value=[]) as graph, patch(
            "brain.editions.current_edition", return_value="semantic"
        ), patch("brain.semantic.build_semantic_index", return_value={"chunks": 3}) as build, patch(
            "brain.ops.semantic_status", return_value=semantic
        ):
            outcome = refresh_brain(self.settings)
        graph.assert_called_once_with(self.settings, defer_lazy=True)
        build.assert_called_once_with(self.settings)
        self.assertEqual("ready", outcome.semantic["status"])
        self.assertTrue(outcome.semantic["aligned"])

    def test_mandatory_atlas_publication_failure_is_not_reported_as_refresh_success(self) -> None:
        with patch("brain.core.discover_and_configure_repositories", return_value=[]), patch(
            "brain.sync.sync_repositories", return_value=[]
        ), patch("brain.core.snapshot_indexes", return_value=({}, [])), patch("brain.core.generate_map"), patch(
            "brain.relations.generate_relationship_map"
        ), patch("brain.experience.build_experience_index", return_value={"cases": []}), patch(
            "brain.experience.evaluate_sessions", return_value={"evaluated_sessions": 0}
        ), patch("brain.graph.index_graph", return_value=[]), patch(
            "brain.editions.current_edition", return_value="core"
        ), patch("brain.catalog.publish_generation", side_effect=sqlite3.DatabaseError("synthetic publication failure")):
            with self.assertRaises(sqlite3.DatabaseError):
                refresh_brain(self.settings)

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
            _, status, _ = self.get("/api/status")
            self.assertEqual(requested["data"]["id"], status["data"]["jobs"][0]["id"])
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
        handoffs = self.root / "generated/handoffs"
        for ticket in ("UI-DELETE", "UI-DELETE-OTHER"):
            (handoffs / ticket).mkdir(parents=True, exist_ok=True)
        for suffix in ("checkpoint-001", "checkpoint-delta-001"):
            (handoffs / "UI-DELETE" / f"{suffix}.md").write_text("private exact source", encoding="utf-8")
            (handoffs / "UI-DELETE-OTHER" / f"{suffix}.md").write_text("keep", encoding="utf-8")
        (handoffs / "UI-DELETE-context-099.md").write_text("legacy delete", encoding="utf-8")
        (handoffs / "UI-DELETE-OTHER-context-099.md").write_text("legacy keep", encoding="utf-8")

        _, deleted, _ = self.post("/api/session/delete", {"ticket": "UI-DELETE"})
        self.assertEqual("UI-DELETE", deleted["data"]["ticket"])
        self.assertFalse((self.root / ".runs/UI-DELETE").exists())
        self.assertFalse((self.root / "generated/handoffs/UI-DELETE").exists())
        self.assertFalse((handoffs / "UI-DELETE-context-099.md").exists())
        self.assertTrue((self.root / ".runs/UI-DELETE-OTHER").is_dir())
        self.assertTrue((self.root / "generated/handoffs/UI-DELETE-OTHER/current.md").is_file())
        self.assertTrue((handoffs / "UI-DELETE-OTHER/checkpoint-001.md").is_file())
        self.assertTrue((handoffs / "UI-DELETE-OTHER/checkpoint-delta-001.md").is_file())
        self.assertTrue((handoffs / "UI-DELETE-OTHER-context-099.md").is_file())
        self.assertTrue((self.root / "service-a").is_dir())

    def test_delete_session_rejects_symlinked_handoff_root_before_session_mutation(self) -> None:
        from brain.ui import _delete_session

        start_session(self.settings, "UI-SYMLINK-DELETE", "Keep this session on unsafe deletion.")
        session = session_dir(self.settings, "UI-SYMLINK-DELETE")
        handoffs = self.settings.generated_dir / "handoffs"
        preserved = self.settings.generated_dir / "handoffs-preserved"
        handoffs.mkdir(parents=True, exist_ok=True)
        handoffs.rename(preserved)
        outside = self.root / "outside-handoffs"
        outside.mkdir()
        (outside / "UI-SYMLINK-DELETE-current.md").write_text("outside", encoding="utf-8")
        handoffs.symlink_to(outside, target_is_directory=True)
        try:
            with self.assertRaisesRegex(BrainError, "handoff directory escapes"):
                _delete_session(self.settings, "UI-SYMLINK-DELETE")
            self.assertTrue(session.is_dir())
            self.assertTrue((outside / "UI-SYMLINK-DELETE-current.md").is_file())
        finally:
            handoffs.unlink(missing_ok=True)
            preserved.rename(handoffs)

    def test_delete_session_rejects_substituted_generated_root(self) -> None:
        from brain.ui import _delete_session

        ticket = "UI-GENERATED-ROOT"
        start_session(self.settings, ticket, "Keep this session on unsafe deletion.")
        session = session_dir(self.settings, ticket)
        generated = self.settings.generated_dir
        preserved = generated.with_name("generated-preserved")
        outside = self.root / "outside-generated"
        outside_handoffs = outside / "handoffs"
        outside_handoffs.mkdir(parents=True)
        outside_marker = outside_handoffs / f"{ticket}-current.md"
        outside_marker.write_text("outside", encoding="utf-8")
        generated.rename(preserved)
        try:
            generated.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            preserved.rename(generated)
            self.skipTest(f"symbolic links unavailable: {error}")
        try:
            with self.assertRaisesRegex(BrainError, "handoff directory escapes"):
                _delete_session(self.settings, ticket)
            self.assertTrue(session.is_dir())
            self.assertEqual(b"outside", outside_marker.read_bytes())
        finally:
            generated.unlink(missing_ok=True)
            preserved.rename(generated)

    def test_session_listing_ignores_a_substituted_runs_root(self) -> None:
        from brain.ui import _sessions

        preserved = self.settings.runs_dir.with_name("runs-ui-preserved")
        outside = self.root / "outside-ui-runs"
        external = outside / "EXTERNAL"
        external.mkdir(parents=True)
        (external / "session.json").write_text(
            json.dumps({"ticket": "EXTERNAL", "status": "must-not-leak"}),
            encoding="utf-8",
        )
        self.settings.runs_dir.rename(preserved)
        try:
            self.settings.runs_dir.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            preserved.rename(self.settings.runs_dir)
            self.skipTest(f"symbolic links unavailable: {error}")
        try:
            self.assertEqual([], _sessions(self.settings))
        finally:
            self.settings.runs_dir.unlink(missing_ok=True)
            preserved.rename(self.settings.runs_dir)

    def test_progressive_checkpoint_and_continuation_are_both_reachable_in_ui(self) -> None:
        source = self.root / "service-a/src/main/java/demo/HelloService.java"
        source.write_text(
            'package demo;\nclass HelloService { @GetMapping("/hello") String hello() { return "hello"; } }\n',
            encoding="utf-8",
        )
        _refresh_all(self.settings, fetch=False, discover=False)
        self.post(
            "/api/start",
            {"ticket": "UI-PROGRESSIVE", "ticket_text": "Trace /hello.", "sync": False, "target": "m365"},
        )
        request = json.dumps({"INVESTIGATION_REQUEST": {
            "version": 5, "mode": "root_cause", "objective": "Trace /hello",
            "runtime_facts": [], "hypotheses": [], "required": ["production entry point"],
            "resolve": ["/hello"], "anchors": [{"kind": "endpoint", "value": "/hello"}],
            "base_context_id": None, "checkpoint": True, "wave": 1,
        }})
        from brain.investigation import build_ticket_runtime as original_build

        entered = threading.Event()
        release = threading.Event()

        def slow_build(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return original_build(*args, **kwargs)

        with patch("brain.investigation.build_ticket_runtime", side_effect=slow_build):
            code, requested, _ = self.post(
                "/api/retrieval",
                {"ticket": "UI-PROGRESSIVE", "text": request, "include_diff": False, "target": "m365"},
            )
            self.assertEqual(202, code)
            self.assertTrue(entered.wait(5))
            _, running, _ = self.get("/api/job?id=" + quote(requested["data"]["id"], safe=""))
            checkpoint_name = running["data"]["progress"]["checkpoint_artifact"]
            _, checkpoint, _ = self.get(
                "/api/artifact?ticket=UI-PROGRESSIVE&name=" + quote(checkpoint_name, safe=""),
            )
            self.assertIn("FIRST USEFUL CHECKPOINT", checkpoint["data"]["content"])
            release.set()
            completed = self.job(requested["data"]["id"])
        progressive = completed["result"]["progressive_delivery"]
        self.assertEqual(checkpoint_name, progressive["checkpoint_artifact"])
        self.assertTrue(progressive["continuation_artifact"])
        _, continuation, _ = self.get(
            "/api/artifact?ticket=UI-PROGRESSIVE&name="
            + quote(progressive["continuation_artifact"], safe=""),
        )
        self.assertIn("Base context ID: `CTX-001-P1`", continuation["data"]["content"])
        self.assertTrue(Path(progressive["checkpoint_handoff_artifact"]).is_file())
        self.assertTrue(Path(progressive["continuation_handoff_artifact"]).is_file())


class UiShutdownTest(unittest.TestCase):
    def test_second_ui_command_reuses_instance_and_idle_stop_closes_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "service").mkdir()
            config = root / "brain.toml"
            config.write_text(
                '[project]\nname="ui-reuse"\n[[repositories]]\nname="service"\npath="service"\n',
                encoding="utf-8",
            )
            settings = load_settings(config)
            output: list[str] = []

            def run() -> None:
                with patch("builtins.print", side_effect=lambda value="", **_kwargs: output.append(str(value))):
                    serve_ui(settings, port=0, open_browser=False)

            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            for _ in range(100):
                if ui_instance(settings, "status")["running"]:
                    break
                time.sleep(0.02)
            self.assertTrue(ui_instance(settings, "status")["running"])
            with patch("builtins.print") as printed, patch("brain.ui._Server") as server_type:
                serve_ui(settings, port=8765, open_browser=False)
            server_type.assert_not_called()
            self.assertIn("already running", str(printed.call_args.args[0]))
            self.assertTrue(ui_instance(settings, "stop")["stopping"])
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertFalse(ui_instance(settings, "status")["running"])
            self.assertTrue(any("Project Brain UI stopped" in line for line in output))

    def test_ui_instance_status_and_stop_do_not_expose_the_private_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "service").mkdir()
            config = root / "brain.toml"
            config.write_text(
                '[project]\nname="ui-instance"\n[[repositories]]\nname="service"\npath="service"\n',
                encoding="utf-8",
            )
            settings = load_settings(config)
            instance = {
                "schema_version": 1,
                "pid": 123,
                "port": 8765,
                "token": "a" * 43,
            }
            (settings.state_dir / "ui-instance.json").write_text(json.dumps(instance), encoding="utf-8")
            with patch("brain.ui._probe_ui_instance", side_effect=[True, True]):
                status = ui_instance(settings, "stop")
            self.assertEqual({"running": True, "stopping": True, "port": 8765}, status)
            self.assertNotIn(instance["token"], json.dumps(status))

    @patch("brain.ui._Server")
    def test_repeated_interrupt_during_shutdown_is_quiet(self, server_type) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "service").mkdir()
            config = root / "brain.toml"
            config.write_text(
                '[project]\nname="ui-shutdown"\n[[repositories]]\nname="service"\npath="service"\n',
                encoding="utf-8",
            )
            settings = load_settings(config)
            server = server_type.return_value
            server.server_address = ("127.0.0.1", 8765)
            server.serve_forever.side_effect = KeyboardInterrupt
            server.server_close.side_effect = KeyboardInterrupt

            with patch("builtins.print") as printed:
                serve_ui(settings, port=8765, open_browser=False)

        server.server_close.assert_called_once_with()
        startup = [call for call in printed.call_args_list if "Project Brain UI:" in str(call.args[0])]
        self.assertEqual(1, len(startup))
        self.assertTrue(startup[0].kwargs.get("flush"))


if __name__ == "__main__":
    unittest.main()

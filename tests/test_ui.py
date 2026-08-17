from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from brain.core import load_settings
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

    def test_ui_page_and_api_are_local_token_protected(self) -> None:
        status, html, headers = self.get("/", authorized=False)
        self.assertEqual(200, status)
        self.assertIn("Project Brain", html)
        self.assertIn("Branch / status", html)
        self.assertIn('esc(repo.ref || "working tree")', html)
        self.assertIn("Continue with AI", html)
        self.assertIn("Retrieval plan", html)
        self.assertIn("M365 agent", html)
        self.assertIn("Discover &amp; sync", html)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        with self.assertRaises(HTTPError) as caught:
            self.get("/api/status", authorized=False)
        self.assertEqual(403, caught.exception.code)
        caught.exception.close()

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
        self.assertIn("current-handoff.md", names)

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

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from brain.core import load_settings
from brain.ui import _Server


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
        _, invalid, _ = self.post("/api/preview", {"text": "please inspect HelloService"})
        self.assertFalse(invalid["data"]["valid"])
        self.assertIn("repair_prompt", invalid["data"])

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
            "/api/context",
            {"ticket": "UI-1", "text": request_text, "include_diff": False, "target": "claude"},
        )
        self.assertEqual(1, context["data"]["request"])
        self.assertIn("HelloService.java", context["data"]["delivery"]["content"])

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

        _, artifact, _ = self.get("/api/artifact?ticket=UI-1&name=context-001.md")
        self.assertIn("HelloService.java", artifact["data"]["content"])

    def test_artifact_path_traversal_is_rejected(self) -> None:
        self.post("/api/start", {"ticket": "UI-2", "ticket_text": "Test security.", "sync": False})
        with self.assertRaises(HTTPError) as caught:
            self.get("/api/artifact?ticket=UI-2&name=" + quote("../session.json", safe=""))
        self.assertEqual(400, caught.exception.code)
        caught.exception.close()


if __name__ == "__main__":
    unittest.main()

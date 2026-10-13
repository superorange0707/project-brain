from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from brain import mcp
from brain.core import load_settings
from brain.demo import create_demo
from brain.ops import refresh_brain
from test_brain import FINAL_SOLUTION

ROOT = Path(__file__).resolve().parents[1]


def call(identifier: int, name: str, **arguments) -> dict:
    return {"jsonrpc": "2.0", "id": identifier, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


class McpServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.config = create_demo(Path(cls.temporary.name) / "demo")
        cls.workspace = cls.config.parent
        refresh_brain(load_settings(cls.config), fetch=False, discover=False)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def exchange(self, *messages) -> list[dict]:
        source = io.BytesIO(b"".join((json.dumps(item).encode() if not isinstance(item, bytes) else item) + b"\n"
                                     for item in messages))
        sink = io.BytesIO()
        self.assertEqual(0, mcp.serve(self.config, source, sink))
        return [json.loads(line) for line in sink.getvalue().splitlines()]

    @staticmethod
    def text(response: dict) -> str:
        return response["result"]["content"][0]["text"]

    def test_protocol_handshake_listing_and_errors(self) -> None:
        responses = self.exchange(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
            b"not json",
            {"jsonrpc": "2.0", "id": 4, "method": "resources/subscribe"},
            call(5, "brain_delete_everything"),
        )
        self.assertEqual([1, 2, 3, None, 4, 5], [item["id"] for item in responses])
        self.assertEqual("2025-06-18", responses[0]["result"]["protocolVersion"])
        self.assertIn("brain_start", responses[0]["result"]["instructions"])
        self.assertEqual({}, responses[1]["result"])
        names = {tool["name"] for tool in responses[2]["result"]["tools"]}
        self.assertEqual({"brain_status", "brain_start", "brain_continue", "brain_handoff", "brain_session",
                          "brain_resume", "brain_feedback"}, names)
        self.assertEqual([-32700, -32601, -32602], [item["error"]["code"] for item in responses[3:]])

    def test_ticket_round_trip_with_repairs_duplicates_paging_and_final_solution(self) -> None:
        request = {"version": 5, "mode": "root_cause",
                   "objective": "Find where CustomerChanged events trigger eligibility recalculation"}
        responses = self.exchange(
            call(1, "brain_start", ticket="MCP-1", ticket_text="Eligibility is not recalculated after a customer update."),
            call(2, "brain_continue", ticket="MCP-1", reply="Let me think about it."),
            call(3, "brain_continue", ticket="MCP-1", reply=request),
            call(4, "brain_continue", ticket="MCP-1", reply={"INVESTIGATION_REQUEST": request}),
            call(5, "brain_handoff", ticket="MCP-1", part=1),
            call(6, "brain_session", ticket="MCP-1"),
            call(7, "brain_continue", ticket="MCP-1", reply="FINAL_SOLUTION\n## Root cause\nTBD"),
            call(8, "brain_continue", ticket="MCP-1", reply=FINAL_SOLUTION),
            call(9, "brain_handoff", ticket="NOPE", part=1),
        )
        flags = [item["result"]["isError"] for item in responses]
        self.assertEqual([False, True, False, True, False, False, True, False, True], flags)
        self.assertTrue(self.text(responses[0]).startswith("# PROJECT BRAIN — START"))
        self.assertIn("INVESTIGATION_REQUEST", self.text(responses[1]))
        context = self.text(responses[2])
        self.assertTrue(context.startswith("# PROJECT BRAIN CONTEXT"))
        self.assertIn("## Source evidence", context)
        self.assertIn("already ran as request 001", self.text(responses[3]))
        self.assertEqual(context, self.text(responses[4]))
        self.assertEqual(1, json.loads(self.text(responses[5]))["requests"])
        self.assertIn("Add these top-level sections", self.text(responses[6]))
        self.assertIn("FINAL_SOLUTION archived", self.text(responses[7]))
        for response in responses:
            self.assertLessEqual(len(self.text(response)), mcp.PAGE_CHARS + 500)

    def test_stdout_carries_only_protocol_messages(self) -> None:
        environment = {**os.environ, "PYTHONPATH": str(ROOT)}
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    call(2, "brain_status"),
                    call(3, "brain_start", ticket="MCP-2", ticket_text="Check the risk client contract.")]
        completed = subprocess.run(
            [sys.executable, "-m", "brain.cli", "-c", str(self.config), "mcp"], cwd=self.workspace, env=environment,
            input="".join(json.dumps(item) + "\n" for item in messages), capture_output=True, text=True, timeout=120,
            check=True)
        lines = completed.stdout.splitlines()
        self.assertEqual([1, 2, 3], [json.loads(line)["id"] for line in lines])
        self.assertFalse(json.loads(lines[2])["result"]["isError"])

    def test_host_configs_use_absolute_paths_merge_and_never_hold_the_token(self) -> None:
        settings = load_settings(self.config)
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"APPDATA": home, "XDG_CONFIG_HOME": home}), \
                mock.patch.object(Path, "home", return_value=Path(home)):
            desktop, key, entry = mcp.host_config(settings, "claude-desktop")
            desktop.parent.mkdir(parents=True)
            desktop.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}))
            self.assertEqual(desktop, mcp.write_host_config(settings, "claude-desktop"))
            written = json.loads(desktop.read_text())
            self.assertEqual({"other", mcp.server_name(settings)}, set(written["mcpServers"]))
            self.assertEqual("dark", written["theme"])
            self.assertTrue(Path(entry["command"]).is_absolute())
            self.assertEqual(["-c", str(self.config.resolve()), "mcp"], entry["args"][-3:])
            desktop.write_text("{broken")
            with self.assertRaises(Exception):
                mcp.write_host_config(settings, "claude-desktop")
        for host, relative, servers in (("claude-code", ".mcp.json", "mcpServers"),
                                        ("vscode", ".vscode/mcp.json", "servers"),
                                        ("cursor", ".cursor/mcp.json", "mcpServers")):
            path = mcp.write_host_config(settings, host)
            self.assertEqual(self.workspace / relative, path)
            self.assertIn(mcp.server_name(settings), json.loads(path.read_text())[servers])
            self.assertNotIn("token", path.read_text().lower())
        self.assertIsNone(mcp.write_host_config(settings, "jetbrains"))
        self.assertIn("claude mcp add --scope user", mcp.describe(settings, "claude-code"))


if __name__ == "__main__":
    unittest.main()

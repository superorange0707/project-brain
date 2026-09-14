from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from unittest import mock

from brain.agent import response_preview
from brain.core import BrainError, parse_context_request, request_repair_prompt, simple_yaml_load


class RequestTransportTests(unittest.TestCase):
    def test_complete_chat_reply_routes_json_and_yaml_without_changing_values(self):
        body = {"version": 5, "mode": "root_cause", "objective": "Trace customer updates: why retry fails",
                "anchors": [{"kind": "symbol", "value": "Handler.handle(String, int)"}]}
        encoded = json.dumps({"INVESTIGATION_REQUEST": body})
        yaml = ('INVESTIGATION_REQUEST:\n  version: 5\n  mode: root_cause\n'
                '  objective: "Trace customer updates: why retry fails"\n'
                '  anchors:\n    - kind: symbol\n      value: "Handler.handle(String, int)"\n')
        for text in (encoded, "\ufeff" + encoded,
                     "Here is the next request:\n```json\n" + encoded + "\n```\nPaste it into Brain.",
                     "Here is the next request:\n```yaml\n" + encoded + "\n```\nPaste it into Brain.",
                     "Here is the next request:\n~~~yaml\n" + yaml + "~~~\nPaste it into Brain.",
                     "  \ufeff```JSON\r\n" + encoded + "\r\n```\r\n"):
            with self.subTest(text=text):
                preview = response_preview(text)
                self.assertEqual("context_request", preview["kind"])
                self.assertEqual(body["objective"], preview["request"]["objective"])
                self.assertEqual(body["anchors"], preview["request"]["anchors"])

    def test_newest_request_wins_but_marker_inside_scalar_is_not_a_directive(self):
        old = 'CONTEXT_REQUEST:\n  version: 3\n  objective: Old request\n'
        new = json.dumps({"INVESTIGATION_REQUEST": {
            "version": 5, "mode": "root_cause", "objective": "Inspect the literal INVESTIGATION_REQUEST: in a log"}})
        self.assertEqual("Inspect the literal INVESTIGATION_REQUEST: in a log",
                         parse_context_request(old + "\n```json\n" + new + "\n```\n")["objective"])
        latest = 'INVESTIGATION_REQUEST:\n  version: 5\n  mode: root_cause\n  objective: Latest request\n'
        self.assertEqual("Latest request", parse_context_request("```json\n" + new + "\n```\n" + latest)["objective"])
        literal = latest.replace("Latest request", '"Inspect the literal INVESTIGATION_REQUEST: in a log"')
        self.assertEqual("Inspect the literal INVESTIGATION_REQUEST: in a log", parse_context_request(literal)["objective"])

    def test_dependency_free_yaml_preserves_flow_collections_quotes_and_comments(self):
        text = '''INVESTIGATION_REQUEST:
  version: 5 # protocol
  mode: root_cause
  objective: "Trace retries: preserve exact inputs"
  anchors: [{kind: symbol, value: "Handler.handle(String, int)"}]
  runtime_facts:
    - "HTTP: 503"
    - https://example.invalid/a#fragment
  resolve: ["why, exactly", "owner: handler"] # retain both strings
  files:
    - {repo: service, path: src/Handler.java, lines: "1-20"}
'''
        with mock.patch.dict("sys.modules", {"yaml": None}):
            request = parse_context_request(text)
        self.assertEqual([{"kind": "symbol", "value": "Handler.handle(String, int)"}], request["anchors"])
        self.assertEqual(["HTTP: 503", "https://example.invalid/a#fragment"], request["runtime_facts"])
        self.assertEqual(["why, exactly", "owner: handler"], request["resolve"])
        self.assertEqual("1-20", request["files"][0]["lines"])
        self.assertEqual({"text": 'He said "retry", then left.'}, simple_yaml_load('text: "He said \\"retry\\", then left."'))

    def test_invalid_latest_request_is_reported_without_executing_an_older_one(self):
        valid = 'CONTEXT_REQUEST:\n  version: 3\n  objective: Old request\n'
        with self.assertRaisesRegex(BrainError, "JSON"):
            parse_context_request(valid + '\n```json\n{"INVESTIGATION_REQUEST": {"version": 5,}}\n```')
        for text in ('```json\n{"INVESTIGATION_REQUEST": {"version": 5,}}\n```',
                     'INVESTIGATION_REQUEST:\n  version: 5\n  mode: root_cause\n  objective: X\n  surprise: true'):
            with self.subTest(text=text), self.assertRaises(BrainError):
                response_preview(text)

    def test_yaml_document_headers_and_nested_examples_keep_the_outer_request(self):
        yaml = '''INVESTIGATION_REQUEST:
  version: 5
  mode: root_cause
  objective: |
    {"objective": "example", "files": [{"repo": "wrong", "path": "x.py"}]}
  files:
    - repo: real
      path: src/main.py
'''
        for text in (yaml, "```yaml\n# Agent request\n---\n" + yaml + "```\n"):
            with self.subTest(text=text):
                preview = response_preview(text)
                self.assertEqual(5, preview["protocol_version"])
                self.assertEqual("real", preview["request"]["files"][0]["repo"])
        for version in ([], {}, True, "5"):
            with self.subTest(version=version), self.assertRaisesRegex(BrainError, "version"):
                response_preview(json.dumps({"INVESTIGATION_REQUEST": {"version": version, "objective": "x"}}))

    def test_first_request_templates_are_executable_json_without_fake_lineage(self):
        for name in ("m365_agent_instructions.md", "prompt.md"):
            text = (Path(__file__).parents[1] / "brain" / name).read_text(encoding="utf-8")
            block = re.search(r"```json\n(.*?)\n```", text, re.S)
            self.assertIsNotNone(block, name)
            request = json.loads(block.group(1))["INVESTIGATION_REQUEST"]
            self.assertNotIn("wave", request)
            self.assertNotIn("base_context_id", request)
            self.assertEqual(5, parse_context_request(block.group(0))["version"])
        repair = request_repair_prompt("Invalid request")
        self.assertIn("```json", repair)
        self.assertEqual(5, parse_context_request(repair)["version"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from brain import edits, mcp
from brain.agent import _has_production_change_block, response_preview
from brain.cli import main
from brain.core import load_settings, session_state, start_session
from brain.demo import create_demo
from brain.edits import check_edits, parse_edit_blocks, verify_edits
from brain.ops import refresh_brain
from brain.ui import _Handler
from test_brain import FINAL_SOLUTION

GIT = shutil.which("git")


def block(target: str, search: str, replace: str) -> str:
    return f"FILE: {target}\n<<<<<<< SEARCH\n{search}\n=======\n{replace}\n>>>>>>> REPLACE\n"


def git(path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([GIT, *args], cwd=path, capture_output=True, text=True, check=False)


class ParseEditBlockTests(unittest.TestCase):
    def test_file_lines_markdown_variants_and_malformed_blocks(self) -> None:
        reply = (
            "Some prose.\r\n**FILE:** `svc/src/A.java`\r\n```java\r\n<<<<<<< SEARCH\r\n  old\r\n=======\r\n  new\r\n"
            ">>>>>>> REPLACE\r\n<<<<<<< SEARCH\r\nsecond\r\n=======\r\n\r\n>>>>>>> REPLACE\r\n```\r\n"
            "FILE: svc/src/B.java (new file)\n<<<<<<< SEARCH\n=======\nclass B {}\n>>>>>>> REPLACE\n"
            "FILE: svc/src/C.java\n<<<<<<< SEARCH\nmissing divider\n>>>>>>> REPLACE\n"
            "<<<<<<< SEARCH\nnever closed\n=======\n"
        )
        blocks = parse_edit_blocks(reply)
        self.assertEqual(["svc/src/A.java", "svc/src/A.java", "svc/src/B.java", "svc/src/C.java", "svc/src/C.java"],
                         [item.target for item in blocks])
        self.assertEqual((["  old"], ["  new"]), (blocks[0].search, blocks[0].replace))
        self.assertEqual([], blocks[1].replace)
        self.assertEqual(([], ["class B {}"]), (blocks[2].search, blocks[2].replace))
        self.assertIn("=======", blocks[3].problem)
        self.assertIn("not closed", blocks[4].problem)
        self.assertTrue(edits.has_edit_blocks(reply))
        self.assertFalse(edits.has_edit_blocks("<<<<<<< SEARCH only"))


@unittest.skipUnless(GIT, "edit checks need Git")
class VerifyEditTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "svc"
        (self.repo / "src").mkdir(parents=True)
        (self.root / "brain.toml").write_text(
            "[project]\nname='edits'\n[[repositories]]\nname='svc'\npath='svc'\n"
            "[[repositories]]\nname='other'\npath='other'\n", encoding="utf-8")
        (self.root / "other").mkdir()
        self.write("src/A.java", "\ufeffpackage demo;\r\nclass A {\r\n    void run() {\r\n        int x = 1;\r\n    }\r\n}")
        self.write("src/Twice.java", "a();\nb();\na();\nb();\n")
        self.write("src/Trailing.txt", "keep   \nchange\n")
        self.write("src/with space.properties", "alpha=1\nbeta=2\n")
        self.write("models/Flow.mps", "<model/>\n")
        git(self.repo, "init", "-q")
        # Byte-exact assertions below must not depend on a host-wide core.autocrlf (Git for Windows default).
        git(self.repo, "config", "core.autocrlf", "false")
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "seed")
        self.settings = load_settings(self.root / "brain.toml")

    def write(self, relative: str, text: str) -> None:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))

    def test_verified_patch_keeps_crlf_bom_and_missing_final_newline_and_applies_with_git(self) -> None:
        reply = "\n".join([
            # The chat dropped the method's indentation; one uniform shift is restored.
            block("svc/src/A.java", "void run() {\n    int x = 1;\n}", "void run() {\n    int x = 2;\n    log();\n}"),
            block("svc:src/New.java", "", "package demo;\nclass New {}"),
            block("svc/src/Trailing.txt", "keep\nchange", "keep\nchanged"),
            block("svc/src/with space.properties", "beta=2", "beta=3"),
            block("svc/src/with space.properties", "beta=3", "beta=4\ngamma=5"),
        ])
        report, patches = verify_edits(self.settings, reply)
        self.assertEqual(("verified", 5, []), (report["status"], report["verified"], report["failures"]))
        self.assertEqual(2, len(report["notes"]))
        self.assertEqual(["svc"], list(patches))
        patch = self.root / "svc.patch"
        patch.write_bytes(patches["svc"][1])
        applied = git(self.repo, "apply", str(patch))
        self.assertEqual(0, applied.returncode, applied.stderr)
        self.assertEqual(b"\xef\xbb\xbfpackage demo;\r\nclass A {\r\n    void run() {\r\n        int x = 2;\r\n"
                         b"        log();\r\n    }\r\n}", (self.repo / "src/A.java").read_bytes())
        self.assertEqual("package demo;\nclass New {}\n", (self.repo / "src/New.java").read_text(encoding="utf-8"))
        self.assertEqual("keep   \nchanged\n", (self.repo / "src/Trailing.txt").read_text(encoding="utf-8"))
        self.assertEqual("alpha=1\nbeta=4\ngamma=5\n",
                         (self.repo / "src/with space.properties").read_text(encoding="utf-8"))

    def test_failures_stage_nothing_and_explain_how_to_repair(self) -> None:
        cases = {
            "not-found": block("svc/src/A.java", "void run() {\n    int y = 1;\n}", "x"),
            "ambiguous": block("svc/src/Twice.java", "a();\nb();", "c();"),
            "missing-file": block("svc/src/Missing.java", "x", "y"),
            "exists": block("svc/src/A.java", "", "class A {}"),
            "unknown-repo": block("nope/src/A.java", "x", "y"),
            "absolute": block("/etc/passwd", "x", "y"),
            "drive": block("C:\\work\\svc\\A.java", "x", "y"),
            "traversal": block("svc/../other/A.java", "x", "y"),
            "git-dir": block("svc/.git/config", "x", "y"),
            "mps": block("svc/models/Flow.mps", "<model/>", "<model></model>"),
            "no-file": "<<<<<<< SEARCH\nx\n=======\ny\n>>>>>>> REPLACE\n",
        }
        expected = {
            "not-found": "Closest current lines", "ambiguous": "matches 2 places (lines 1, 3)",
            "missing-file": "does not exist", "exists": "already exists", "unknown-repo": "configured repository",
            "absolute": "not an absolute path", "drive": "not an absolute path", "traversal": "not a safe",
            "git-dir": "not a safe", "mps": "MPS editor", "no-file": "no FILE: line",
        }
        for name, reply in cases.items():
            with self.subTest(name):
                report, patches = verify_edits(self.settings, reply)
                self.assertEqual(("needs_repair", {}), (report["status"], patches))
                self.assertIn(expected[name], report["failures"][0]["problem"])
                self.assertIn("PROJECT BRAIN — EDIT CHECK FAILED", report["repair_prompt"])
        report, _ = verify_edits(self.settings, cases["not-found"])
        self.assertIn("        int x = 1;", report["repair_prompt"])
        self.assertIn("lines 1-6", report["repair_prompt"])

    def test_one_failed_block_withholds_every_patch_and_git_rejection_is_reported(self) -> None:
        good = block("svc/src/Trailing.txt", "change", "changed")
        report, patches = verify_edits(self.settings, good + block("svc/src/Twice.java", "zzz", "y"))
        self.assertEqual((1, 1, {}), (report["verified"], len(report["failures"]), patches))
        with mock.patch.object(edits, "_git_check", return_value="error: patch failed"):
            report, patches = verify_edits(self.settings, good)
        self.assertEqual(("needs_repair", {}), (report["status"], patches))
        self.assertIn("git apply --check rejected", report["failures"][0]["problem"])

    def test_unreadable_targets_fail_closed(self) -> None:
        (self.repo / "src/latin1.txt").write_bytes(b"caf\xe9\n")
        report, _ = verify_edits(self.settings, block("svc/src/latin1.txt", "caf", "cafe"))
        self.assertIn("not UTF-8", report["failures"][0]["problem"])
        if os.name != "nt":
            (self.repo / "src/link.txt").symlink_to(self.repo / "src/Trailing.txt")
            report, _ = verify_edits(self.settings, block("svc/src/link.txt", "change", "x"))
            self.assertIn("not a regular file", report["failures"][0]["problem"])

    def test_declarations_ignore_calls_accessors_and_common_names(self) -> None:
        lines = ["    public static String formatIban(String value) {", "    return normalize(value);",
                 "    String text = render(value);", "    public String getName() {", "    public boolean equals(Object o) {",
                 "class IbanFormatter {", "    // this class handles it", "    eligibility.recalculate(id);"]
        self.assertEqual({"formatIban", "IbanFormatter"}, edits._declared_names("x/Y.java", lines))
        self.assertEqual({"format_iban"}, edits._declared_names("x/y.py", ["def format_iban(value):", "def get(x):"]))
        self.assertEqual({"formatIban", "toAmount"}, edits._declared_names(
            "x/y.ts", ["export function formatIban(v) {", "const toAmount = (v) => v * 100;"]))


@unittest.skipUnless(GIT, "edit checks need Git")
class EditWorkflowTests(unittest.TestCase):
    """The demo ticket end to end: verified fix, reuse warning, CLI, UI and MCP routing."""

    FIX = block(
        "trading-service/src/main/java/demo/CustomerChangedListener.java",
        "        if (event.type() == CustomerEvent.Type.ADDRESS_CHANGED) {\n"
        "            eligibility.recalculate(event.customerId());\n"
        "        }\n"
        "        // Bug: jurisdiction changes are ignored until the nightly batch.",
        "        if (event.type() == CustomerEvent.Type.ADDRESS_CHANGED\n"
        "                || event.type() == CustomerEvent.Type.JURISDICTION_CHANGED) {\n"
        "            eligibility.recalculate(event.customerId());\n"
        "        }",
    )
    DUPLICATE = block(
        "batch-service/src/main/java/demo/NightlyEligibilityJob.java",
        "class NightlyEligibilityJob { void refreshAllCustomers() {} }",
        "class NightlyEligibilityJob {\n    void refreshAllCustomers() {}\n\n"
        "    void recalculate(String customerId) {\n    }\n}",
    )

    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.config = create_demo(Path(cls.temporary.name) / "demo")
        cls.settings = load_settings(cls.config)
        refresh_brain(cls.settings, fetch=False, discover=False)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def start(self, ticket: str) -> None:
        start_session(self.settings, ticket, "Jurisdiction changes refresh only overnight.")

    def test_final_solution_stages_patch_reports_reuse_and_records_history(self) -> None:
        self.start("EDIT-1")
        reply = FINAL_SOLUTION.replace(
            "Update the existing branch using the local pattern.\n",
            "Reuse the existing online recalculation.\n```java\n" + self.FIX + "```\n```java\n" + self.DUPLICATE + "```\n")
        preview = response_preview(reply, self.settings, "EDIT-1")
        self.assertEqual(("final_solution", 2), (preview["kind"], preview["edit_blocks"]))
        self.assertIn("check its 2 SEARCH/REPLACE blocks", preview["message"])

        data = _Handler._execute_retrieval(self.settings, {"ticket": "EDIT-1", "text": reply, "target": "m365"}, None)
        report = data["edits"]
        self.assertEqual(("final-solution.md", "verified_with_warnings", 1), (data["path"], report["status"], report["check"]))
        self.assertEqual(["batch-service", "trading-service"], sorted(item["repo"] for item in report["patches"]))
        warning = next(item for item in report["warnings"] if item["name"] == "recalculate")
        self.assertIn("TradingEligibilityService.java", " ".join(match["path"] for match in warning["existing"]))
        self.assertIn("PROJECT BRAIN — REUSE CHECK", report["review_prompt"])
        trading = next(item for item in report["patches"] if item["repo"] == "trading-service")
        self.assertTrue(Path(trading["path"]).name.startswith("edits-001-trading-service"))
        self.assertIn("apply", trading["apply"])
        self.assertIn(trading["path"], trading["apply"].replace('"', "").replace("'", ""))
        repo = self.settings.repo("trading-service").path
        applied = git(repo, "apply", trading["path"])
        self.assertEqual(0, applied.returncode, applied.stderr)
        self.assertIn("JURISDICTION_CHANGED", (repo / "src/main/java/demo/CustomerChangedListener.java").read_text())
        git(repo, "checkout", "-q", ".")
        history = session_state(self.settings, "EDIT-1")["edit_checks"]
        self.assertEqual([("verified_with_warnings", 2, 0)], [(item["status"], item["blocks"], item["failed"]) for item in history])
        self.assertEqual("ready_to_implement", session_state(self.settings, "EDIT-1")["status"])

    def test_cli_routes_edit_only_replies_and_prints_the_repair_prompt(self) -> None:
        self.start("EDIT-2")
        broken = self.FIX.replace("ADDRESS_CHANGED) {", "ADDRESS_CHANGED)  {\n// drifted")
        self.assertEqual("code_edits", response_preview(broken, self.settings, "EDIT-2")["kind"])
        reply = Path(self.temporary.name) / "reply.md"
        reply.write_text(broken, encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(0, main(["-c", str(self.config), "continue", "EDIT-2", "--file", str(reply), "--no-copy"]))
        self.assertIn("0 of 1 blocks apply", output.getvalue())
        self.assertIn("PROJECT BRAIN — EDIT CHECK FAILED", output.getvalue())
        reply.write_text(self.FIX, encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(0, main(["-c", str(self.config), "continue", "EDIT-2", "--file", str(reply), "--json"]))
        result = json.loads(output.getvalue())
        self.assertEqual(("code_edits", "verified", 2, None), (result["kind"], result["edits"]["status"],
                                                              result["edits"]["check"], result["path"]))
        self.assertEqual("edits-002-trading-service.patch", Path(result["edits"]["patches"][0]["path"]).name)

    def test_mcp_returns_the_check_and_repair_prompt(self) -> None:
        self.start("EDIT-3")
        message = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "brain_continue", "arguments": {"ticket": "EDIT-3", "reply": self.FIX}}},
                             lambda: self.settings)
        text = message["result"]["content"][0]["text"]
        self.assertFalse(message["result"]["isError"])
        self.assertIn("1 of 1 blocks apply", text)
        self.assertIn("apply", text)
        broken = self.FIX.replace("// Bug:", "// Fixed:")
        message = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                              "params": {"name": "brain_continue", "arguments": {"ticket": "EDIT-3", "reply": broken}}},
                             lambda: self.settings)
        self.assertIn("Send the corrected SEARCH/REPLACE blocks", message["result"]["content"][0]["text"])

    def test_fenced_text_edit_block_counts_as_a_production_change(self) -> None:
        reply = FINAL_SOLUTION.replace("Update the existing branch using the local pattern.\n",
                                       "```text\n" + self.FIX + "```\n")
        self.assertTrue(_has_production_change_block(reply))
        self.start("EDIT-4")
        self.assertEqual("none", check_edits(self.settings, "EDIT-4", "no edits")["status"])
        self.assertNotIn("edit_checks", session_state(self.settings, "EDIT-4"))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from brain import agent
from brain.agent import M365_KNOWLEDGE_FILE, agent_kit_status, create_m365_agent_kit
from brain.core import load_settings


class AgentKitStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "service").mkdir()
        (self.root / "brain.toml").write_text(
            "[project]\nname='kit'\n[[repositories]]\nname='service'\npath='service'\n", encoding="utf-8")
        self.settings = load_settings(self.root / "brain.toml")
        agent._KIT_STATUS_CACHE.clear()

    def test_knowledge_is_txt_hashed_and_status_tracks_changes(self) -> None:
        self.assertEqual("missing", agent_kit_status(self.settings)["status"])
        directory = self.settings.generated_dir / "m365-agent"
        directory.mkdir(parents=True)
        (directory / "PROJECT_KNOWLEDGE.md").write_text("old\n", encoding="utf-8")

        kit = create_m365_agent_kit(self.settings)
        self.assertEqual(M365_KNOWLEDGE_FILE, Path(kit["knowledge_path"]).name)
        self.assertFalse((directory / "PROJECT_KNOWLEDGE.md").exists())
        manifest = json.loads((directory / "AGENT_KIT.json").read_text(encoding="utf-8"))
        self.assertEqual(hashlib.sha256(kit["instructions"].encode()).hexdigest(), manifest["instructions_sha256"])
        self.assertEqual(hashlib.sha256((directory / M365_KNOWLEDGE_FILE).read_bytes()).hexdigest(),
                         manifest["knowledge_sha256"])
        self.assertIn("report when the rendered Instructions or PROJECT_KNOWLEDGE.txt changed", kit["setup"])
        self.assertEqual({"status": "current", "changed": []},
                         {k: v for k, v in agent_kit_status(self.settings).items() if k != "directory"})

        # A new template is reported for re-paste; knowledge stays current.
        agent._KIT_STATUS_CACHE.clear()
        real = agent._render_m365_pasted_texts
        with mock.patch.object(agent, "_render_m365_pasted_texts",
                               side_effect=lambda s: (real(s)[0] + "\nNew rule.", real(s)[1])):
            self.assertEqual(["INSTRUCTIONS.md"], agent_kit_status(self.settings)["changed"])

        # Knowledge inputs (glossary) change the knowledge file only, without a template change.
        self.settings.knowledge_dir.mkdir(parents=True, exist_ok=True)
        (self.settings.knowledge_dir / "glossary.md").write_text("## PE\nPayment execution.\n", encoding="utf-8")
        status = agent_kit_status(self.settings)
        self.assertEqual(("stale", [M365_KNOWLEDGE_FILE]), (status["status"], status["changed"]))

        create_m365_agent_kit(self.settings)
        self.assertEqual("current", agent_kit_status(self.settings)["status"])
        self.assertLessEqual(len(create_m365_agent_kit(replace(self.settings, name="x" * 512))["instructions"]), 8000)

    def test_broken_manifest_never_breaks_status(self) -> None:
        from brain.ops import dashboard_status

        create_m365_agent_kit(self.settings)
        (self.settings.generated_dir / "m365-agent" / "AGENT_KIT.json").write_text("{broken", encoding="utf-8")
        agent._KIT_STATUS_CACHE.clear()
        self.assertEqual("unknown", agent_kit_status(self.settings)["status"])
        self.assertEqual("unknown", dashboard_status(self.settings)["agent_kit"]["status"])


if __name__ == "__main__":
    unittest.main()

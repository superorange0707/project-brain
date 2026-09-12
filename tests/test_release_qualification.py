from __future__ import annotations

import unittest
import io
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from brain import __version__
from scripts.verify_model_pack_reuse import CONTRACTS, FILES, fingerprint


class ReleaseQualificationTest(unittest.TestCase):
    def test_native_diagnostic_initializes_an_isolated_valid_workspace(self):
        root = Path(__file__).parents[1]
        workflow = (root / ".github/workflows/model-runtime-diagnostic.yml").read_text()
        script = "\n".join(line[10:] for line in workflow.split("        run: |\n", 1)[1].splitlines())
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict("os.environ", {"RUNNER_TEMP": directory}), mock.patch(
            "tempfile.mkdtemp", return_value=directory,
        ), mock.patch("subprocess.run"), mock.patch(
            "brain.models.install_official_pack", return_value={"pack_id": "public-test"},
        ) as install, mock.patch(
            "brain.models.verify_pack", return_value={"verified": True, "conformance": {"passed": True}},
        ) as verify, redirect_stdout(io.StringIO()):
            exec(compile(script, "native-runtime-diagnostic", "exec"), {})
            settings, alias = install.call_args.args
            self.assertEqual("precision", alias)
            self.assertEqual(["fixture"], [repo.name for repo in settings.repositories])
            self.assertTrue(settings.state_dir.is_relative_to(Path(directory).resolve()))
            verify.assert_called_once_with(settings, "public-test")

    def test_only_unchanged_model_contracts_can_reuse_qualification(self):
        root = Path(__file__).parents[1]
        sources = {path: (root / path).read_text(encoding="utf-8") for path in {*FILES, *CONTRACTS, "pyproject.toml"}}
        expected = fingerprint(sources)
        # The executable version and unrelated ticket code do not change a pack.
        changed = {**sources, "pyproject.toml": sources["pyproject.toml"].replace(f'version = "{__version__}"', 'version = "9.9.9"')}
        self.assertNotEqual(sources["pyproject.toml"], changed["pyproject.toml"])
        changed["brain/core.py"] += "\ndef unrelated_ticket_helper(): pass\n"
        self.assertEqual(expected, fingerprint(changed))
        for path in FILES:
            self.assertNotEqual(expected, fingerprint({**sources, path: sources[path] + "\n# changed\n"}))
        self.assertNotEqual(expected, fingerprint({**sources, "pyproject.toml": sources["pyproject.toml"].replace('>=3.11', '>=3.12')}))
        self.assertNotEqual(expected, fingerprint({**sources, "brain/semantic.py": sources["brain/semantic.py"].replace('SEMANTIC_MAX_CARD_INPUT_BYTES = 8_192', 'SEMANTIC_MAX_CARD_INPUT_BYTES = 16_384')}))
        with self.assertRaises(ValueError):
            fingerprint({**sources, "brain/core.py": "pass"})

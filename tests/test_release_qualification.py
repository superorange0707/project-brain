from __future__ import annotations

import unittest
from pathlib import Path

from scripts.verify_model_pack_reuse import CONTRACTS, FILES, fingerprint


class ReleaseQualificationTest(unittest.TestCase):
    def test_only_unchanged_model_contracts_can_reuse_qualification(self):
        root = Path(__file__).parents[1]
        sources = {path: (root / path).read_text(encoding="utf-8") for path in {*FILES, *CONTRACTS, "pyproject.toml"}}
        expected = fingerprint(sources)
        # The executable version and unrelated ticket code do not change a pack.
        changed = {**sources, "pyproject.toml": sources["pyproject.toml"].replace('version = "1.0.14"', 'version = "9.9.9"')}
        changed["brain/core.py"] += "\ndef unrelated_ticket_helper(): pass\n"
        self.assertEqual(expected, fingerprint(changed))
        for path in FILES:
            self.assertNotEqual(expected, fingerprint({**sources, path: sources[path] + "\n# changed\n"}))
        self.assertNotEqual(expected, fingerprint({**sources, "pyproject.toml": sources["pyproject.toml"].replace('>=3.11', '>=3.12')}))
        self.assertNotEqual(expected, fingerprint({**sources, "brain/semantic.py": sources["brain/semantic.py"].replace('SEMANTIC_MAX_CARD_INPUT_BYTES = 8_192', 'SEMANTIC_MAX_CARD_INPUT_BYTES = 16_384')}))
        with self.assertRaises(ValueError):
            fingerprint({**sources, "brain/core.py": "pass"})

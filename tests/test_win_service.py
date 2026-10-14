from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from brain import win_service
from brain.core import BrainError, load_settings


class WindowsLoginStartupTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "My Workspace"
        (root / "service").mkdir(parents=True)
        (root / "brain.toml").write_text("[project]\nname='w'\n[[repositories]]\nname='service'\npath='service'\n")
        self.settings = load_settings(root / "brain.toml")
        self.started = []
        patches = [
            mock.patch("brain.ui.start_ui", side_effect=lambda settings, port, open_browser: self.started.append(port)),
            mock.patch("brain.ui.ui_instance", return_value={"running": False, "stopping": False, "port": None}),
        ]
        if os.name != "nt":  # Elsewhere, the registry is a dictionary; Windows CI uses the real HKCU Run key.
            store: dict[str, str] = {}
            patches += [mock.patch.object(win_service, "_read", side_effect=store.get),
                        mock.patch.object(win_service, "_write", side_effect=store.__setitem__),
                        mock.patch.object(win_service, "_delete", side_effect=lambda name: store.pop(name, None))]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(win_service._delete, win_service._name(self.settings))

    def test_install_keeps_port_survives_spaces_and_uninstall_removes_only_this_workspace(self) -> None:
        self.assertFalse(win_service.installed(self.settings))
        with self.assertRaises(BrainError):
            win_service.service(self.settings, "start")
        status = win_service.service(self.settings, "install", port=8899)
        self.assertTrue(status["installed"])
        command = win_service._read(win_service._name(self.settings))
        self.assertIn("--no-open", command)
        self.assertIn(str(self.settings.config_path.resolve()), command)
        self.assertIn(subprocess.list2cmdline([str(self.settings.config_path.resolve())]), command)  # quoted: path has a space
        win_service.service(self.settings, "install")
        win_service.service(self.settings, "start")
        self.assertEqual([8899, 8899, 8899], self.started)
        self.assertFalse(win_service.service(self.settings, "uninstall")["installed"])
        self.assertFalse(win_service.installed(self.settings))


if __name__ == "__main__":
    unittest.main()

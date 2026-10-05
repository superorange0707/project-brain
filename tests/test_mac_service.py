from __future__ import annotations

import io
import json
import os
import plistlib
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from brain import mac_service
from brain.cli import main
from brain.core import BrainError, load_settings
from brain.ui import _load_ui_instance, ui_instance


class MacServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="brain launchd ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "repository").mkdir()
        (self.root / "repository" / "app.py").write_text("def hello(): return 'launchd'\n", encoding="utf-8")
        config = self.root / "brain.toml"
        config.write_text("[project]\nname='launchd-test'\n[graph]\nenabled=false\n"
                          "[[repositories]]\nname='repository'\npath='repository'\n", encoding="utf-8")
        self.settings = load_settings(config)
        # Even the real launchd regression writes only into its temporary workspace.
        self.agent = self.root / "LaunchAgents" / mac_service._agent_path(self.settings).name
        self.path_patch = patch("brain.mac_service._agent_path", return_value=self.agent)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)

    def test_definition_is_user_scoped_private_and_does_not_save_credentials(self) -> None:
        with patch.dict(os.environ, {"COMPANY_TOKEN": "never-save", "HTTPS_PROXY": "https://secret@proxy",
                                     "SSH_AUTH_SOCK": "/temporary/agent"}):
            value = mac_service._plist(self.settings, 0)
        self.assertEqual(self.agent.stem, value["Label"])
        self.assertTrue(value["RunAtLoad"])
        self.assertEqual({"SuccessfulExit": False}, value["KeepAlive"])
        self.assertEqual(30, value["ThrottleInterval"])
        self.assertEqual(0o077, value["Umask"])
        self.assertEqual(["-c", str(self.settings.config_path), "ui", "--foreground", "--no-open", "--port", "0"], value["ProgramArguments"][-7:])
        self.assertEqual({"PATH", "PROJECT_BRAIN_SERVICE", "PYTHONPATH"}, set(value["EnvironmentVariables"]))
        serialized = plistlib.dumps(value)
        self.assertNotIn(b"never-save", serialized)
        self.assertNotIn(b"secret@proxy", serialized)
        self.assertNotIn(b"temporary/agent", serialized)
        self.assertNotIn("UserName", value)

    def test_install_status_and_uninstall_only_manage_this_workspace(self) -> None:
        loaded = False
        calls = []

        def launchctl(*args, **kwargs):
            nonlocal loaded
            calls.append(args)
            if args[0] == "print":
                return subprocess.CompletedProcess(args, 0 if loaded else 113, "", "")
            if args[0] in {"bootstrap", "bootout"}:
                loaded = args[0] == "bootstrap"
            return subprocess.CompletedProcess(args, 0, "", "")

        with patch("brain.mac_service._require_macos"), patch("brain.mac_service.os.getuid", return_value=501, create=True), \
                patch("brain.mac_service._launchctl", side_effect=launchctl), \
                patch("brain.mac_service._wait_ready"), patch("sys.stdout", new_callable=io.StringIO) as output:
            result = mac_service.service(self.settings, "install", port=0)
            self.assertTrue(result["installed"])
            self.assertTrue(result["loaded"])
            original = self.agent.read_bytes()
            self.assertEqual(mac_service._plist(self.settings, 0), plistlib.loads(original))
            mac_service.service(self.settings, "install")
            self.assertEqual(original, self.agent.read_bytes())
            self.assertEqual(1, sum(args[0] == "bootstrap" for args in calls))
            self.assertEqual(0, main(["-c", str(self.settings.config_path), "service", "status", "--json"]))
            self.assertTrue(json.loads(output.getvalue())["installed"])
            stopped = mac_service.service(self.settings, "stop")
            self.assertTrue(stopped["installed"])
            self.assertFalse(stopped["loaded"])
            self.assertEqual(original, self.agent.read_bytes())
            result = mac_service.service(self.settings, "uninstall")
        self.assertFalse(result["installed"])
        self.assertFalse(self.agent.exists())
        self.assertTrue(self.settings.config_path.exists())
        self.assertTrue((self.settings.state_dir / "ui.log").exists())
        self.assertEqual(1, sum(args[0] == "bootout" for args in calls))
        self.assertFalse(any("-k" in args or "sudo" in args for args in calls))
        if os.name != "nt":
            self.assertEqual(0o600, (self.settings.state_dir / "ui.log").stat().st_mode & 0o777)

    def test_busy_or_uncertain_ui_blocks_install_and_uninstall_without_changes(self) -> None:
        self.agent.parent.mkdir()
        original = plistlib.dumps(mac_service._plist(self.settings, 0))
        self.agent.write_bytes(original)
        for action in ("install", "uninstall"):
            with self.subTest(action=action), patch("brain.mac_service._require_macos"), \
                    patch("brain.mac_service.os.getuid", return_value=501, create=True), \
                    patch("brain.mac_service._loaded", return_value=True), \
                    patch("brain.mac_service.ui_instance", side_effect=BrainError("busy or uncertain")), \
                    patch("brain.mac_service._launchctl") as launchctl:
                with self.assertRaisesRegex(BrainError, "busy or uncertain"):
                    mac_service.service(self.settings, action, **({"port": 8765} if action == "install" else {}))
                self.assertEqual(original, self.agent.read_bytes())
                launchctl.assert_not_called()

    def test_foreign_definition_and_unsupported_platform_are_preserved(self) -> None:
        self.agent.parent.mkdir()
        original = plistlib.dumps({"Label": "not-brain", "ProgramArguments": ["/bin/echo", "unrelated"]})
        self.agent.write_bytes(original)
        with self.assertRaisesRegex(BrainError, "preserved"):
            mac_service.installed(self.settings)
        self.assertEqual(original, self.agent.read_bytes())
        with patch("brain.mac_service.sys.platform", "linux"), patch("brain.mac_service._launchctl") as launchctl:
            with self.assertRaisesRegex(BrainError, "currently support macOS"):
                mac_service.service(self.settings, "install")
            launchctl.assert_not_called()

    def test_stop_waits_for_record_cleanup_after_authenticated_shutdown(self) -> None:
        with patch("brain.mac_service.ui_instance", return_value={"running": True}) as request, \
                patch("brain.mac_service._load_ui_instance", side_effect=[{"pid": 123}, None, None]), \
                patch("brain.mac_service._ui_lock", side_effect=[BrainError("already stopping"), nullcontext()]) as lease, \
                patch("brain.mac_service.time.sleep"):
            mac_service._stop(self.settings)
            request.assert_called_once_with(self.settings, "stop")
            self.assertEqual(2, lease.call_count)
        with patch("brain.mac_service.ui_instance", return_value={"running": True}), \
                patch("brain.mac_service._load_ui_instance", return_value={"pid": 123}), \
                patch("brain.mac_service.time.monotonic", side_effect=[0, 0, 31]), \
                patch("brain.mac_service.time.sleep"):
            with self.assertRaisesRegex(BrainError, "still stopping.*preserved"):
                mac_service._stop(self.settings)

    def test_homebrew_command_keeps_stable_link_across_versions(self) -> None:
        executable = self.root / "Cellar" / "project-brain" / "1.0.28" / "bin" / "brain"
        executable.parent.mkdir(parents=True)
        executable.write_text("fixture", encoding="utf-8")
        link = self.root / "opt" / "project-brain" / "bin" / "brain"
        link.parent.mkdir(parents=True)
        try:
            link.symlink_to(executable)
        except OSError as error:
            self.skipTest(f"symlinks unavailable: {error}")
        with patch("brain.mac_service.sys.frozen", True, create=True):
            self.assertEqual(str(link), mac_service._stable_executable(str(executable)))
        newer = self.root / "Cellar" / "project-brain" / "next" / "bin" / "brain"
        newer.parent.mkdir(parents=True)
        newer.write_text("new version", encoding="utf-8")
        link.unlink()
        link.symlink_to(newer)
        self.assertEqual("new version", link.read_text(encoding="utf-8"))

    @unittest.skipUnless(sys.platform == "darwin" and os.environ.get("BRAIN_TEST_LAUNCHD") == "1",
                         "requires an explicitly enabled macOS login-session integration test")
    def test_real_launchd_login_start_refresh_crash_recovery_stop_and_uninstall(self) -> None:
        domain = f"gui/{os.getuid()}"
        if mac_service._launchctl("print", domain, check=False).returncode:
            self.skipTest("no macOS GUI login domain available")
        state = self.settings.state_dir
        (state / "auto-refresh.json").write_text('{"mode":"when_idle"}', encoding="utf-8")
        original_plist = mac_service._plist
        original_wait_ready = mac_service._wait_ready
        readiness = patch("brain.mac_service._wait_ready", side_effect=lambda settings, label: original_wait_ready(settings, label, timeout=120))
        readiness.start()
        self.addCleanup(readiness.stop)
        executable = os.environ.get("BRAIN_TEST_EXECUTABLE")

        def quick_plist(settings, port):
            value = original_plist(settings, port)
            value["ThrottleInterval"] = 2  # Exercise the same policy without a 30-second crash wait.
            if executable:
                value["ProgramArguments"] = [str(Path(executable).resolve()), *value["ProgramArguments"][-7:]]
            return value

        def wait_for(predicate, seconds=120):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if predicate():
                    return
                time.sleep(0.1)
            details = {name: (state / name).read_text(encoding="utf-8")[-3000:]
                       for name in ("auto-refresh.json", "ui-refresh.json") if (state / name).exists()}
            self.fail(json.dumps(details))

        target = f"{domain}/{self.agent.stem}"
        try:
            with patch("brain.mac_service._plist", side_effect=quick_plist):
                self.assertTrue(mac_service.service(self.settings, "install", port=0)["running"])
                first = _load_ui_instance(self.settings)
                wait_for(lambda: json.loads((state / "auto-refresh.json").read_text(encoding="utf-8")).get("last_refresh"))
                self.assertTrue(mac_service.service(self.settings, "install", port=0)["running"])
                self.assertEqual(first["pid"], _load_ui_instance(self.settings)["pid"])
            self.assertEqual(0o600, self.agent.stat().st_mode & 0o777)
            # Existing UI stop must remain stopped for longer than launchd's throttle.
            self.assertTrue(ui_instance(self.settings, "stop")["stopping"])
            wait_for(lambda: _load_ui_instance(self.settings) is None)
            time.sleep(3)
            self.assertFalse(ui_instance(self.settings, "status")["running"])
            with patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(0, main(["-c", str(self.settings.config_path), "ui", "--no-open"]))
            second = _load_ui_instance(self.settings)
            self.assertEqual(self.agent.stem, second["service"])
            os.kill(second["pid"], signal.SIGKILL)  # Only this test's authenticated temporary service.
            wait_for(lambda: bool((instance := _load_ui_instance(self.settings)) and
                                  instance["pid"] != second["pid"] and ui_instance(self.settings, "status")["running"]))
            self.assertEqual("when_idle", json.loads((state / "auto-refresh.json").read_text())["mode"])
            # Re-loading the saved definition exercises the login RunAtLoad path.
            with patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(0, main(["-c", str(self.settings.config_path), "ui", "stop"]))
            self.assertFalse(mac_service._loaded(target))
            mac_service._launchctl("bootstrap", domain, str(self.agent))
            wait_for(lambda: ui_instance(self.settings, "status")["running"])
            self.assertFalse(mac_service.service(self.settings, "uninstall")["installed"])
            self.assertFalse(mac_service._loaded(target))
            self.assertTrue((state / "auto-refresh.json").exists())
        except Exception as error:
            details = {name: (state / name).read_text(encoding="utf-8")[-3000:]
                       for name in ("ui.log", "auto-refresh.json", "ui-refresh.json") if (state / name).exists()}
            details["launchd"] = mac_service._launchctl("print", target, check=False).stdout[-6000:]
            diagnostic = re.sub(r"token=[A-Za-z0-9_-]+", "token=<redacted>", json.dumps(details))
            raise AssertionError(f"{error}\n{diagnostic}") from error
        finally:
            # No login item or process from this isolated integration test is retained.
            mac_service._launchctl("bootout", target, check=False)
            self.agent.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()

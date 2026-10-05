from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from brain.catalog import current_generation_ref
from brain.cli import main
from brain.core import BrainError, load_settings
from brain.platforms import process_group_kwargs
from brain.ui import _load_ui_instance, _ui_lock, serve_ui, start_ui, ui_instance


class BackgroundUiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="brain background ")
        self.addCleanup(self._cleanup_workspace)
        root = Path(self.temporary.name)
        (root / "service").mkdir()
        (root / "service" / "app.py").write_text("def hello(): return 'hello'\n", encoding="utf-8")
        self.config = root / "brain.toml"
        self.config.write_text(
            "[project]\nname='background'\n[graph]\nenabled=false\n"
            "[[repositories]]\nname='service'\npath='service'\n", encoding="utf-8",
        )
        self.settings = load_settings(self.config)
        self.instance = {"schema_version": 1, "port": 9876, "token": "a" * 43}

    def _cleanup_workspace(self) -> None:
        # Windows retains the onefile parent's log handle briefly after UI shutdown.
        deadline = time.monotonic() + 30
        while True:
            try:
                self.temporary.cleanup()
                return
            except PermissionError:
                if os.name != "nt" or time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)

    def test_cli_defaults_to_background_and_keeps_foreground_opt_in(self) -> None:
        for flags, expected in (([], "start_ui"), (["--foreground"], "serve_ui")):
            with self.subTest(flags=flags), patch("brain.ui.start_ui") as background, patch("brain.ui.serve_ui") as foreground:
                self.assertEqual(0, main(["-c", str(self.config), "ui", "--no-open", "--port", "0", *flags]))
                chosen, other = (background, foreground) if expected == "start_ui" else (foreground, background)
                chosen.assert_called_once()
                self.assertEqual({"port": 0, "open_browser": False}, chosen.call_args.kwargs)
                other.assert_not_called()

    def test_auto_refresh_starts_after_private_instance_publication(self) -> None:
        with patch("brain.auto_refresh.AutoRefreshService") as scheduler, \
                patch("brain.ui.ThreadingHTTPServer.__init__", autospec=True) as initialize, \
                patch("brain.ui.ThreadingHTTPServer.serve_forever"), \
                patch("brain.ui.ThreadingHTTPServer.server_close"), patch("builtins.print"):
            initialize.side_effect = lambda server, address, handler: setattr(server, "server_address", address)
            scheduler.return_value.start.side_effect = lambda: self.assertIsNotNone(_load_ui_instance(self.settings))
            serve_ui(self.settings, port=9876, open_browser=False)
            scheduler.return_value.start.assert_called_once()
            scheduler.return_value.stop.assert_called_once()
        self.assertIsNone(_load_ui_instance(self.settings))

    def test_reopen_and_uncertain_health_never_launch_a_competing_process(self) -> None:
        record = self.settings.state_dir / "ui-instance.json"
        record.write_text(json.dumps(self.instance), encoding="utf-8")
        with patch("brain.ui.subprocess.Popen") as spawn, patch("brain.ui.webbrowser.open") as browser, patch("builtins.print"):
            with patch("brain.ui._probe_ui_instance", return_value=True):
                start_ui(self.settings)
            browser.assert_called_once()
            with patch("brain.ui._probe_ui_instance", side_effect=BrainError("uncertain")):
                with self.assertRaisesRegex(BrainError, "uncertain"):
                    start_ui(self.settings)
            spawn.assert_not_called()
        self.assertEqual(self.instance, json.loads(record.read_text(encoding="utf-8")))

    def test_detached_launch_preserves_config_and_standalone_resources(self) -> None:
        for frozen, windows in ((False, False), (True, False), (True, True)):
            with self.subTest(frozen=frozen, windows=windows):
                process = Mock()
                process.poll.return_value = None
                with patch("brain.ui.sys.frozen", frozen, create=True), \
                        patch("brain.ui.process_group_kwargs", return_value=process_group_kwargs(windows=windows, detached=True)), \
                        patch("brain.ui._load_ui_instance", side_effect=[None, self.instance]), \
                        patch("brain.ui._forget_ui_instance"), \
                        patch("brain.ui._probe_ui_instance", return_value=True), \
                        patch("brain.ui.subprocess.Popen", return_value=process) as spawn, \
                        patch("brain.ui.threading.Thread"), patch("brain.ui.webbrowser.open") as browser, \
                        patch("builtins.print"):
                    start_ui(self.settings, port=0, open_browser=False)
                command = spawn.call_args.args[0]
                self.assertEqual(sys.executable, command[0])
                self.assertEqual(not frozen, "-m" in command)
                self.assertEqual(not frozen, "-P" in command)
                self.assertEqual(["-c", str(self.config.resolve()), "ui", "--foreground", "--no-open", "--port", "0"], command[-7:])
                options = spawn.call_args.kwargs
                self.assertEqual(subprocess.DEVNULL, options["stdin"])
                self.assertNotEqual(subprocess.PIPE, options["stdout"])
                self.assertEqual(subprocess.STDOUT, options["stderr"])
                self.assertTrue(options["close_fds"])
                self.assertEqual(self.settings.root, options["cwd"])
                self.assertEqual(not windows, options.get("start_new_session", False))
                if windows:
                    self.assertEqual(0x200 | 8, options["creationflags"])
                if frozen:
                    self.assertEqual("1", options["env"]["PYINSTALLER_RESET_ENVIRONMENT"])
                browser.assert_not_called()

    def test_failed_and_slow_startup_never_report_ready_or_open_browser(self) -> None:
        for exited in (True, False):
            with self.subTest(exited=exited):
                process = Mock()
                process.poll.return_value = 2 if exited else None
                with patch("brain.ui._load_ui_instance", return_value=None), \
                        patch("brain.ui.subprocess.Popen", return_value=process), \
                        patch("brain.ui.threading.Thread"), patch("brain.ui.time.sleep"), \
                        patch("brain.ui.time.monotonic", side_effect=[0, 0, 31]), \
                        patch("brain.ui.webbrowser.open") as browser, patch("builtins.print") as printed:
                    with self.assertRaisesRegex(BrainError, "exited during startup" if exited else "not confirmed readiness"):
                        start_ui(self.settings)
                browser.assert_not_called()
                printed.assert_not_called()
                process.terminate.assert_not_called()

    def test_launch_and_server_leases_prevent_duplicate_starts(self) -> None:
        for name, launch in (("ui-launch.lock", start_ui), ("ui-server.lock", serve_ui)):
            with self.subTest(name=name), _ui_lock(self.settings, name), patch("brain.ui.subprocess.Popen") as spawn, patch("brain.ui._Server") as server:
                with self.assertRaisesRegex(BrainError, "already starting or stopping"):
                    launch(self.settings, port=0, open_browser=False)
                spawn.assert_not_called()
                server.assert_not_called()

    def test_background_log_rejects_links(self) -> None:
        outside = self.settings.root / "outside.txt"
        outside.write_text("preserve me", encoding="utf-8")
        log = self.settings.state_dir / "ui.log"
        for kind in ("hard", "symbolic"):
            with self.subTest(kind=kind):
                try:
                    log.hardlink_to(outside) if kind == "hard" else log.symlink_to(outside)
                except OSError as error:
                    self.skipTest(f"{kind} links unavailable: {error}")
                try:
                    with patch("brain.ui.subprocess.Popen") as spawn, self.assertRaises((OSError, ValueError)):
                        start_ui(self.settings, open_browser=False)
                    spawn.assert_not_called()
                    self.assertEqual("preserve me", outside.read_text(encoding="utf-8"))
                finally:
                    log.unlink()

    def test_service_refreshes_after_launcher_exits_then_reopens_and_stops(self) -> None:
        # The same regression runs against the built binary in native smoke CI.
        executable = os.environ.get("BRAIN_TEST_EXECUTABLE")
        command = [str(Path(executable).resolve())] if executable else [sys.executable, "-m", "brain.cli"]
        command += ["-c", str(self.config)]
        state = self.settings.state_dir
        shadow = self.settings.root / "brain"
        shadow.mkdir()
        (shadow / "__init__.py").write_text("raise RuntimeError('workspace package must not execute')\n", encoding="utf-8")
        (state / "auto-refresh.json").write_text('{"mode":"when_idle"}', encoding="utf-8")
        try:
            launched = subprocess.run(
                [*command, "ui", "--port", "0", "--no-open"],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=45,
            )
            self.assertEqual(0, launched.returncode, launched.stderr + (state / "ui.log").read_text(encoding="utf-8"))
            self.assertIn("you can close this terminal", launched.stdout)
            record = json.loads((state / "ui-instance.json").read_text(encoding="utf-8"))
            if os.name != "nt":
                self.assertNotEqual(os.getsid(0), os.getsid(record["pid"]))
                self.assertEqual(0o600, (state / "ui.log").stat().st_mode & 0o777)
            # No browser or launcher remains. Observe the scheduler using files only.
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                saved = json.loads((state / "auto-refresh.json").read_text(encoding="utf-8"))
                if saved.get("last_refresh"):
                    break
                time.sleep(0.1)
            self.assertTrue(saved.get("last_refresh"), saved)
            self.assertIsNotNone(current_generation_ref(self.settings))
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                start_ui(self.settings, port=0, open_browser=False)
            self.assertIn("already running", output.getvalue())
            self.assertEqual(record, json.loads((state / "ui-instance.json").read_text(encoding="utf-8")))
            self.assertTrue(ui_instance(self.settings, "status")["running"])
        finally:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    if not ui_instance(self.settings, "stop")["running"]:
                        break
                except BrainError:
                    pass  # A refresh already in progress must finish before stop.
                time.sleep(0.1)
            self.assertFalse(ui_instance(self.settings, "status")["running"])


if __name__ == "__main__":
    unittest.main()

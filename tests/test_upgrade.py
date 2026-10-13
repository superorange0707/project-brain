from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from brain import upgrade as upgrade_module
from brain.core import BrainError, load_settings
from brain.upgrade import asset_name, check_latest, checksums, finish, upgrade


PYTHON = sys.executable  # Tests patch sys.executable to the fake install.


def _script(version: str) -> bytes:
    return f"#!{PYTHON}\nprint('brain {version}')\n".encode()


def _tar(members: dict[str, bytes], *, link: str | None = None) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo("./" + name if not name.startswith("..") else name)
            info.size, info.mode = len(data), 0o755
            archive.addfile(info, io.BytesIO(data))
        if link:
            info = tarfile.TarInfo(link)
            info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
            archive.addfile(info)
    return stream.getvalue()


@unittest.skipIf(os.name == "nt", "fixture executables are POSIX scripts")
class UpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "workspace/service").mkdir(parents=True)
        (self.root / "workspace/brain.toml").write_text(
            "[project]\nname='upgrade'\n[[repositories]]\nname='service'\npath='service'\n", encoding="utf-8")
        self.settings = load_settings(self.root / "workspace/brain.toml")
        self.install = self.root / "bin"
        self.install.mkdir()
        for name, data in (("brain", _script("1.0.0")), ("zoekt", b"old zoekt"), ("notes.txt", b"user file")):
            (self.install / name).write_bytes(data)
            (self.install / name).chmod(0o755)
        self.release = self.root / "release"
        self.release.mkdir()
        self.base = self.release.as_uri()
        frozen = mock.patch.multiple(sys, frozen=True, create=True)
        executable = mock.patch.object(sys, "executable", str(self.install / "brain"))
        stop = mock.patch.object(upgrade_module, "_stop_ui", return_value={"service": False, "was_running": False})
        for patcher in (frozen, executable, stop):
            patcher.start()
            self.addCleanup(patcher.stop)

    def publish(self, archive: bytes, *, version: str = "9.9.9", sha: str | None = None) -> str:
        name = asset_name(version)
        (self.release / name).write_bytes(archive)
        digest = sha or hashlib.sha256(archive).hexdigest()
        (self.release / "SHA256SUMS.txt").write_text(f"{digest}  {name}\n{'0' * 64}  install-project-brain.sh\n", encoding="utf-8")
        return name

    def run_upgrade(self, **values) -> int:
        with mock.patch.object(upgrade_module, "_run_finish", return_value=0) as finished:
            result = upgrade(self.settings, release_base_url=self.base, **values)
        self.finished = finished
        return result

    def test_checksums_and_latest_check_cache_and_errors(self) -> None:
        self.assertEqual({"a.tar.gz": "a" * 64}, checksums(f"{'A' * 64}  a.tar.gz\nnoise\n"))
        with self.assertRaises(ValueError):
            checksums(f"{'a' * 64}  a\n{'b' * 64} *a\n")
        self.publish(b"x", version="9.9.9")
        result = check_latest(self.settings, release_base_url=self.base)
        self.assertEqual(("9.9.9", True), (result["latest"], result["update_available"]))
        (self.release / "SHA256SUMS.txt").unlink()
        self.assertEqual("9.9.9", check_latest(self.settings, release_base_url=self.base)["latest"])  # cached
        failed = check_latest(self.settings, release_base_url=self.base, force=True)
        self.assertIsNone(failed["update_available"])
        self.assertIn("FileNotFoundError", failed["error"])

    def test_self_swap_replaces_shipped_files_and_keeps_others(self) -> None:
        self.publish(_tar({"brain": _script("9.9.9"), "zoekt": b"new zoekt", "PROJECT_BRAIN_LICENSE": b"MIT"}))
        self.assertEqual(0, self.run_upgrade())
        reported = subprocess.run([str(self.install / "brain"), "--version"], capture_output=True, text=True, check=True)
        self.assertEqual("brain 9.9.9", reported.stdout.strip())
        self.assertEqual(b"new zoekt", (self.install / "zoekt").read_bytes())
        self.assertEqual(b"user file", (self.install / "notes.txt").read_bytes())
        self.assertEqual({"brain", "zoekt", "notes.txt", "PROJECT_BRAIN_LICENSE"}, {p.name for p in self.install.iterdir()})
        self.finished.assert_called_once_with(self.settings, (self.install / "brain").resolve())

    def test_refusals_leave_the_installation_untouched(self) -> None:
        cases = {
            "checksum": (_tar({"brain": _script("9.9.9")}), "0" * 64),
            "traversal": (_tar({"brain": _script("9.9.9"), "../evil": b"x"}), None),
            "link": (_tar({"brain": _script("9.9.9")}, link="./zoekt"), None),
            "wrong version": (_tar({"brain": _script("0.0.1")}), None),
            "no brain": (_tar({"zoekt": b"z"}), None),
        }
        for label, (archive, sha) in cases.items():
            with self.subTest(label):
                self.publish(archive, sha=sha)
                with self.assertRaises(BrainError):
                    self.run_upgrade(version="9.9.9")
                self.assertEqual(_script("1.0.0"), (self.install / "brain").read_bytes())
                self.assertEqual({"brain", "zoekt", "notes.txt"}, {p.name for p in self.install.iterdir()})

    def test_failed_swap_restores_every_replaced_entry(self) -> None:
        self.publish(_tar({"brain": _script("9.9.9"), "zoekt": b"new zoekt"}))
        real_replace = os.replace
        calls = []

        def flaky(source, target, **kwargs):
            if Path(target).parent.resolve() == self.install.resolve():
                calls.append(Path(target).name)
                if len(calls) == 4:  # brain aside, brain in, zoekt aside, zoekt in -> fails
                    raise PermissionError("locked")
            return real_replace(source, target, **kwargs)

        with mock.patch.object(upgrade_module.os, "replace", side_effect=flaky):
            with self.assertRaises(BrainError):
                self.run_upgrade(version="9.9.9")
        self.assertEqual(_script("1.0.0"), (self.install / "brain").read_bytes())
        self.assertEqual(b"old zoekt", (self.install / "zoekt").read_bytes())
        self.assertEqual({"brain", "zoekt", "notes.txt"}, {p.name for p in self.install.iterdir()})

    def test_offline_archive_with_sha256(self) -> None:
        archive = _tar({"brain": _script("9.9.9")})
        local = self.root / asset_name("9.9.9")
        local.write_bytes(archive)
        with mock.patch.object(upgrade_module, "_run_finish", return_value=0):
            self.assertEqual(0, upgrade(self.settings, version="9.9.9", archive=str(local),
                                        sha256=hashlib.sha256(archive).hexdigest()))
        self.assertIn(b"9.9.9", (self.install / "brain").read_bytes())

    def test_up_to_date_and_python_installs_are_not_mutated(self) -> None:
        self.publish(b"x", version=upgrade_module.__version__)
        self.assertEqual(0, self.run_upgrade())
        self.assertEqual(_script("1.0.0"), (self.install / "brain").read_bytes())
        self.publish(_tar({"brain": _script("9.9.9")}))
        with mock.patch.object(sys, "frozen", False):
            self.assertEqual(0, self.run_upgrade())
        self.assertEqual(_script("1.0.0"), (self.install / "brain").read_bytes())

    def test_homebrew_install_is_delegated_to_brew(self) -> None:
        cellar = self.root / "homebrew/Cellar/project-brain/1.0.0/bin/brain"
        cellar.parent.mkdir(parents=True)
        cellar.write_bytes(b"")
        stable = self.root / "homebrew/opt/project-brain/bin/brain"
        stable.parent.mkdir(parents=True)
        stable.write_bytes(b"")
        self.publish(b"x")
        commands = []
        with mock.patch.object(sys, "executable", str(cellar)), \
                mock.patch.object(upgrade_module.subprocess, "run",
                                  side_effect=lambda command, **_: commands.append(command[1:]) or mock.Mock(returncode=0)), \
                mock.patch("brain.mac_service.installed", return_value=False):
            self.assertEqual(0, self.run_upgrade())
        self.assertEqual([["update"], ["trust", "--formula", "superorange0707/tap/project-brain"],
                          ["upgrade", "superorange0707/tap/project-brain"]], commands)
        self.finished.assert_called_once_with(self.settings, stable.resolve())

    def test_finish_republishes_only_on_schema_drift_and_restarts_ui(self) -> None:
        upgrade_module._write_state(self.settings, {"service": False, "was_running": True})
        with mock.patch("brain.catalog.generation_predates_binary", return_value=True), \
                mock.patch("brain.ops.refresh_brain") as refresh, mock.patch("brain.ui.start_ui") as start:
            self.assertEqual(0, finish(self.settings))
        refresh.assert_called_once_with(self.settings, fetch=False, discover=False)
        start.assert_called_once()
        self.assertFalse((self.settings.state_dir / upgrade_module.STATE_FILE).exists())
        with mock.patch("brain.catalog.generation_predates_binary", return_value=False), \
                mock.patch("brain.ops.refresh_brain") as refresh, mock.patch("brain.ui.start_ui") as start:
            finish(self.settings)
        refresh.assert_not_called()
        start.assert_not_called()

    def test_retired_files_and_staging_are_cleaned_on_start(self) -> None:
        (self.install / "brain.brain-upgrade-old").write_bytes(b"old")
        (self.install / ".brain-upgrade-abc").mkdir()
        (self.install / "keep.old").write_bytes(b"keep")
        upgrade_module.cleanup_retired(self.install)
        self.assertEqual({"brain", "zoekt", "notes.txt", "keep.old"}, {p.name for p in self.install.iterdir()})


class UpdateStatusTests(unittest.TestCase):
    def test_source_runs_never_check_the_network_from_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "service").mkdir()
            (root / "brain.toml").write_text("[project]\nname='s'\n[[repositories]]\nname='service'\npath='service'\n")
            settings = load_settings(root / "brain.toml")
            with mock.patch.object(upgrade_module, "check_latest", side_effect=AssertionError("network")):
                self.assertIsNone(upgrade_module.status_update(settings))
            # An installed binary checks in the background, once per 24 h per process.
            started = []
            with mock.patch.multiple(sys, frozen=True, create=True), \
                    mock.patch.object(upgrade_module, "_CHECK_STARTED", [0.0]), \
                    mock.patch.object(upgrade_module.threading, "Thread",
                                      side_effect=lambda **kw: started.append(kw) or mock.Mock()):
                upgrade_module.status_update(settings)
                upgrade_module.status_update(settings)
                self.assertEqual(1, len(started))
                with mock.patch.object(upgrade_module.time, "monotonic",
                                       return_value=upgrade_module.time.monotonic() + upgrade_module.CHECK_TTL_SECONDS):
                    upgrade_module.status_update(settings)
                self.assertEqual(2, len(started))


if __name__ == "__main__":
    unittest.main()

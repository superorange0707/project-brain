from __future__ import annotations

import errno
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest import mock

from brain import locks
from brain.atlas import _module_path
from brain.backends.zoekt import _relative_result_path, status as zoekt_status
from brain.core import _clipboard_command, load_settings, parse_context_request
from brain.cli import _workspace_root, main
from brain.models import install_official_pack, official_packs, validate_manifest
from brain.semantic import chunk_source
from brain.platforms import (
    _close_windows_handles,
    _lock_windows_managed_directories,
    adjacent_executable,
    connect_managed_sqlite,
    logical_path,
    normalize_platform_id,
    platform_id,
    process_group_kwargs,
    open_managed_lock,
    read_managed_bytes,
    remove_tree,
    run_bounded_process,
    native_command,
    terminate_process_tree,
    trusted_path_executable,
)
from brain.sync import _export_snapshot


def _wait_for_pid(path: Path, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = path.read_text(encoding="ascii").strip()
            if value:
                return int(value)
        except (FileNotFoundError, ValueError):
            pass
        time.sleep(0.05)
    raise AssertionError(f"process did not publish a PID to {path}")


def _process_is_active(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    import ctypes

    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


class _FakeMsvcrt:
    LK_NBLCK = 1
    LK_UNLCK = 2

    def __init__(self) -> None:
        self.ranges: dict[int, tuple[int, int]] = {}

    def locking(self, descriptor: int, mode: int, length: int) -> None:
        offset = os.lseek(descriptor, 0, os.SEEK_CUR)
        if mode == self.LK_UNLCK:
            self.ranges.pop(descriptor, None)
            return
        requested = range(offset, offset + length)
        if any(set(requested).intersection(range(start, start + size)) for other, (start, size) in self.ranges.items() if other != descriptor):
            raise OSError(errno.EACCES, "locked")
        self.ranges[descriptor] = (offset, length)


class WindowsCompatibilityTest(unittest.TestCase):
    def test_windows_init_root_supports_cross_drive_repositories(self) -> None:
        current = PureWindowsPath("C:/workspace/repo-a/src")
        repositories = [
            PureWindowsPath("C:/workspace/repo-a"),
            PureWindowsPath("D:/enterprise/repo-b"),
        ]
        self.assertEqual(PureWindowsPath("C:/workspace"), _workspace_root(current, repositories))

    def test_init_rejects_explicit_config_inside_target_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "target"
            repository.mkdir()
            config = repository / "brain.toml"
            error = io.StringIO()
            with mock.patch("sys.stderr", error):
                code = main([
                    "--config", str(config), "init", str(repository), "--no-fetch",
                ])
            self.assertEqual(2, code)
            self.assertIn("outside target repositories", error.getvalue())
            self.assertFalse(config.exists())

    @unittest.skipIf(os.name == "nt", "POSIX directory-descriptor regression")
    def test_managed_lock_and_sqlite_open_remain_anchored_during_root_substitution(self) -> None:
        from brain import platforms

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            state = base / "state"
            outside = base / "outside"
            detached = base / "state-detached"
            state.mkdir()
            outside.mkdir()
            original_open = os.open
            substituted = False

            def swap_before_lock_leaf(
                name: object, flags: int, *args: object, **kwargs: object,
            ) -> int:
                nonlocal substituted
                if not substituted and name == "operations.lock" and kwargs.get("dir_fd") is not None:
                    substituted = True
                    state.rename(detached)
                    state.symlink_to(outside, target_is_directory=True)
                return original_open(name, flags, *args, **kwargs)

            try:
                with mock.patch("brain.platforms.os.open", side_effect=swap_before_lock_leaf):
                    with self.assertRaisesRegex(ValueError, "identity changed"):
                        open_managed_lock(state, state / "operations.lock")
            finally:
                if state.is_symlink():
                    state.unlink()
                if detached.exists():
                    detached.rename(state)
            self.assertFalse((outside / "operations.lock").exists())
            self.assertTrue((state / "operations.lock").is_file())

            real_connect = platforms.sqlite3.connect

            def swap_before_sqlite_open(*args: object, **kwargs: object):
                state.rename(detached)
                state.symlink_to(outside, target_is_directory=True)
                return real_connect(*args, **kwargs)

            try:
                with mock.patch("brain.platforms.sqlite3.connect", side_effect=swap_before_sqlite_open):
                    with self.assertRaises(platforms.sqlite3.OperationalError):
                        connect_managed_sqlite(state, state / "search.sqlite3")
            finally:
                if state.is_symlink():
                    state.unlink()
                if detached.exists():
                    detached.rename(state)
            self.assertFalse((outside / "search.sqlite3").exists())
            self.assertTrue((state / "search.sqlite3").is_file())

    @unittest.skipUnless(os.name == "nt", "native Windows directory-handle behavior")
    def test_windows_managed_directory_handles_deny_root_rename(self) -> None:
        with tempfile.TemporaryDirectory(prefix="Project Brain managed ") as temporary:
            base = Path(temporary)
            root = base / "state"
            parent = root / "nested"
            detached = base / "detached"
            parent.mkdir(parents=True)
            handles, _ = _lock_windows_managed_directories(root, parent, root.lstat())
            try:
                with self.assertRaises(OSError):
                    root.rename(detached)
            finally:
                _close_windows_handles(handles)
            root.rename(detached)
            detached.rename(root)

    @unittest.skipUnless(os.name == "nt", "native Windows root-substitution regression")
    def test_windows_managed_read_rejects_root_substitution_before_directory_lock(self) -> None:
        from brain import platforms

        with tempfile.TemporaryDirectory(prefix="Project Brain managed read ") as temporary:
            base = Path(temporary)
            root = base / "state"
            detached = base / "detached"
            root.mkdir()
            path = root / "session.json"
            path.write_bytes(b"ORIGINAL")
            real_parent = platforms._managed_parent

            def swap_root(*args: object, **kwargs: object) -> Path:
                parent = real_parent(*args, **kwargs)
                root.rename(detached)
                root.mkdir()
                (root / "session.json").write_bytes(b"EXTERNAL")
                return parent

            try:
                with mock.patch("brain.platforms._managed_parent", side_effect=swap_root):
                    with self.assertRaisesRegex(ValueError, "identity changed"):
                        read_managed_bytes(root, path, max_bytes=1024)
            finally:
                if root.exists():
                    remove_tree(root)
                if detached.exists():
                    detached.rename(root)

    def test_bounded_process_timeout_includes_large_stdin_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            started = time.monotonic()
            result = run_bounded_process(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                Path(temporary),
                input_bytes=b"x" * (8 * 1024 * 1024),
                max_stdout_bytes=1024,
                max_stderr_bytes=1024,
                timeout=0.2,
            )
        self.assertTrue(result.timed_out)
        self.assertLess(time.monotonic() - started, 3.0)

    def test_bounded_process_reaps_descendant_after_leader_exits_before_overrun(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pid_file = root / "descendant.pid"
            child = (
                "import os,time,pathlib;"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()));"
                "time.sleep(.2);os.write(1,b'xx');time.sleep(30)"
            )
            leader = (
                "import subprocess,sys;"
                f"subprocess.Popen([sys.executable,'-c',{child!r}]);"
            )
            started = time.monotonic()
            result = run_bounded_process(
                [sys.executable, "-c", leader],
                root,
                max_stdout_bytes=1,
                max_stderr_bytes=1_024,
                timeout=3.0,
            )
            elapsed = time.monotonic() - started
            self.assertTrue(result.output_truncated)
            self.assertLess(elapsed, 3.0)
            descendant = _wait_for_pid(pid_file)
            deadline = time.monotonic() + 2.0
            while _process_is_active(descendant) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(_process_is_active(descendant), "bounded process descendant remained alive after its leader exited")

    def test_managed_tree_cleanup_retries_read_only_snapshot_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "sealed"
            target.mkdir()
            source = target / "Source.java"
            source.write_text("class Sealed {}\n", encoding="utf-8")
            source.chmod(stat.S_IREAD)
            remove_tree(target)
            self.assertFalse(target.exists())

    def test_protocol_v5_accepts_powershell_utf8_bom_json_and_yaml(self) -> None:
        request = {
            "version": 5,
            "mode": "root_cause",
            "objective": "Trace the Windows request",
            "runtime_facts": [],
            "hypotheses": [],
            "required": [],
            "resolve": [],
            "anchors": [],
            "base_context_id": None,
            "checkpoint": False,
            "wave": 1,
        }
        parsed_json = parse_context_request("\ufeff" + json.dumps({"INVESTIGATION_REQUEST": request}))
        parsed_yaml = parse_context_request(
            "\ufeffINVESTIGATION_REQUEST:\n"
            "  version: 5\n"
            "  mode: root_cause\n"
            "  objective: Trace the Windows request\n"
            "  runtime_facts: []\n"
            "  hypotheses: []\n"
            "  required: []\n"
            "  resolve: []\n"
            "  anchors: []\n"
            "  base_context_id: null\n"
            "  checkpoint: false\n"
            "  wave: 1\n"
        )
        self.assertEqual(request, {key: parsed_json[key] for key in request})
        self.assertEqual(request, {key: parsed_yaml[key] for key in request})

    def test_platform_and_logical_path_identities_are_canonical(self) -> None:
        self.assertEqual("windows-amd64", platform_id("Windows", "AMD64"))
        self.assertEqual("windows-amd64", platform_id("win32", "x86_64"))
        self.assertEqual("darwin-arm64", normalize_platform_id("darwin-aarch64"))
        self.assertEqual("src/main/java/App.java", logical_path(r".\src\main\java\App.java"))
        self.assertEqual("src/服务/App.java", logical_path(Path("src") / "服务" / "App.java"))
        self.assertEqual("src/com.example/service", _module_path(r"src\com.example\service\App.java"))

    def test_crlf_source_keeps_stable_line_ranges_and_clean_cards(self) -> None:
        chunks = chunk_source(
            "repository", "src/App.java",
            "class App {\r\n  void run() {\r\n    service.call();\r\n  }\r\n}\r\n",
        )
        self.assertTrue(chunks)
        self.assertEqual((1, 5), (chunks[0].start_line, chunks[0].end_line))
        self.assertNotIn("\r", chunks[0].card)

    def test_native_process_group_arguments_do_not_require_a_posix_shell(self) -> None:
        self.assertEqual({"creationflags": 0x00000200}, process_group_kwargs(windows=True))
        self.assertEqual({"start_new_session": True}, process_group_kwargs(windows=False))

    def test_windows_process_tree_timeout_uses_native_taskkill_and_reaps(self) -> None:
        process = mock.Mock(pid=4242)
        process.poll.return_value = None
        process.wait.return_value = 0
        taskkill = Path("C:/Windows/System32/taskkill.exe")
        with mock.patch("brain.platforms.os.name", "nt"), mock.patch(
            "brain.platforms.windows_system_executable", return_value=taskkill,
        ), mock.patch("brain.platforms.subprocess.run") as run:
            terminate_process_tree(process, graceful_timeout=0.01)
        process.terminate.assert_not_called()
        run.assert_called_once_with(
            [str(taskkill), "/PID", "4242", "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
        process.wait.assert_called_once_with(timeout=3.0)
        process.kill.assert_not_called()

    def test_windows_clipboard_uses_native_powershell(self) -> None:
        powershell = Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
        with mock.patch("brain.core.os.name", "nt"), mock.patch(
            "brain.core.sys.platform", "win32",
        ), mock.patch("brain.core.windows_system_executable", return_value=powershell):
            self.assertEqual(
                [str(powershell), "-NoProfile", "-NonInteractive", "-Command", "Get-Clipboard -Raw"],
                _clipboard_command(False),
            )
            self.assertIn("Set-Clipboard", _clipboard_command(True)[-1])

    def test_trusted_path_resolution_never_executes_from_the_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "rg.exe"
            executable.write_bytes(b"not executable content")
            executable.chmod(0o700)
            with mock.patch("brain.platforms.Path.cwd", return_value=root):
                self.assertIsNone(
                    trusted_path_executable(
                        "rg", environment={"PATH": str(root), "PATHEXT": ".EXE"}, windows=True,
                    )
                )

    @unittest.skipIf(os.name == "nt", "POSIX executable permission fixture")
    def test_native_command_ignores_relative_path_and_repo_owned_git(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            trusted = root / "trusted"
            repository.mkdir()
            trusted.mkdir()
            marker = root / "repo-git-ran"
            fake = repository / "git"
            fake.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
            fake.chmod(0o700)
            trusted_git = trusted / "git"
            trusted_git.write_text("#!/bin/sh\nprintf trusted\n", encoding="utf-8")
            trusted_git.chmod(0o700)
            with mock.patch.dict(os.environ, {"PATH": f".{os.pathsep}{trusted}"}):
                command = native_command("git")
                completed = subprocess.run(
                    [command], cwd=repository, text=True, capture_output=True, check=False,
                )
            self.assertEqual(str(trusted_git.resolve()), command)
            self.assertEqual("trusted", completed.stdout)
            self.assertFalse(marker.exists())

    def test_standalone_helper_discovery_supports_exe_suffix_and_spaces(self) -> None:
        with tempfile.TemporaryDirectory(prefix="Brain Windows 工具 ") as temporary:
            root = Path(temporary)
            launcher = root / "brain.exe"
            helper = root / "zoekt.exe"
            launcher.write_bytes(b"launcher")
            helper.write_bytes(b"helper")
            self.assertEqual(helper.resolve(), adjacent_executable("zoekt", executable=str(launcher), windows=True))

    def test_bundled_zoekt_pair_wins_and_backend_paths_become_logical(self) -> None:
        bundled = [Path("C:/Project Brain/zoekt.exe"), Path("C:/Project Brain/zoekt-index.exe")]
        with mock.patch("brain.backends.zoekt.adjacent_executable", side_effect=bundled), mock.patch(
            "brain.backends.zoekt.trusted_path_executable", return_value=Path("C:/untrusted/zoekt.exe"),
        ):
            selected = zoekt_status()
        self.assertEqual(str(bundled[0]), selected.executable)
        self.assertEqual(str(bundled[1]), selected.indexer)
        repository = SimpleNamespace(scan_path=Path("/workspace/repository"))
        self.assertEqual(
            "src/main/App.java",
            _relative_result_path(repository, r"repository\src\main\App.java"),
        )

    def test_windows_snapshot_export_rejects_case_collisions(self) -> None:
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as target:
            for name in ("src/App.java", "src/app.java"):
                payload = b"class App {}\r\n"
                member = tarfile.TarInfo(name)
                member.size = len(payload)
                target.addfile(member, io.BytesIO(payload))
        completed = subprocess.CompletedProcess(["git", "archive"], 0, b"", b"")

        def write_archive(_repo, _ref, destination, **_kwargs):
            destination.write_bytes(archive.getvalue())
            return completed

        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "brain.sync._git_archive_to_path", side_effect=write_archive,
        ):
            self.assertIsNone(
                _export_snapshot(
                    SimpleNamespace(name="repository"), "main", "a" * 40, Path(temporary), windows=True,
                )
            )

    def test_windows_snapshot_export_rejects_invalid_and_normalizing_components(self) -> None:
        def archive_for(names: tuple[str, ...]) -> bytes:
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode="w") as target:
                for name in names:
                    payload = b"source\n"
                    member = tarfile.TarInfo(name)
                    member.size = len(payload)
                    target.addfile(member, io.BytesIO(payload))
            return archive.getvalue()

        cases = (
            ("src/CON.java",), ("src/trailing. ",), ("src/alternate:data.java",),
            ("src/question?.java",), ("src/control\x01.java",),
            ("src/é.java", "src/e\u0301.java"),
        )
        completed = subprocess.CompletedProcess(["git", "archive"], 0, b"", b"")
        for number, names in enumerate(cases):
            with self.subTest(names=names), tempfile.TemporaryDirectory() as temporary:
                payload = archive_for(names)

                def write_archive(_repo, _ref, destination, **_kwargs):
                    destination.write_bytes(payload)
                    return completed

                with mock.patch("brain.sync._git_archive_to_path", side_effect=write_archive):
                    self.assertIsNone(_export_snapshot(
                        SimpleNamespace(name=f"repository-{number}"), "main", f"{number + 1:040x}",
                        Path(temporary), windows=True,
                    ))

    def test_windows_workspace_readers_coexist_and_exclude_writer(self) -> None:
        fake = _FakeMsvcrt()
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "operations.lock"
            first = lock_path.open("a+b")
            second = lock_path.open("a+b")
            writer = lock_path.open("a+b")
            try:
                with mock.patch.object(locks, "fcntl", None), mock.patch.object(locks, "msvcrt", fake):
                    locks._acquire(first, shared=True)
                    locks._acquire(second, shared=True)
                    self.assertNotEqual(fake.ranges[first.fileno()][0], fake.ranges[second.fileno()][0])
                    with self.assertRaises(OSError):
                        locks._acquire(writer)
                    locks._release(first)
                    locks._release(second)
                    locks._acquire(writer)
                    self.assertEqual((0, locks._WINDOWS_READER_SLOTS), fake.ranges[writer.fileno()])
                    locks._release(writer)
            finally:
                first.close()
                second.close()
                writer.close()

    def test_windows_official_pack_catalog_pins_verified_native_descriptors(self) -> None:
        expected = [
            {
                "alias": "precision",
                "pack_id": "qwen3-reranker-4b-q6k-windows-amd64",
                "descriptor_url": "https://github.com/superorange0707/project-brain/releases/download/precision-pack-windows-v1.0.0/qwen3-reranker-4b-q6k-windows-amd64-descriptor.json",
            },
            {
                "alias": "semantic",
                "pack_id": "qwen3-embedding-4b-q6k-windows-amd64",
                "descriptor_url": "https://github.com/superorange0707/project-brain/releases/download/semantic-pack-windows-v1.0.0/qwen3-embedding-4b-q6k-windows-amd64-descriptor.json",
            },
        ]
        installed = {"pack_id": "qwen3-embedding-4b-q6k-windows-amd64"}
        with mock.patch("brain.models.platform_id", return_value="windows-amd64"), mock.patch(
            "brain.models.install_release_descriptor", return_value=installed,
        ) as install:
            self.assertEqual(expected, official_packs())
            self.assertEqual(installed, install_official_pack(mock.Mock(), "semantic"))
        install.assert_called_once_with(
            mock.ANY,
            expected[1]["descriptor_url"],
            "69ca378fc2a00f01b23ae047ab46a7137c1b952d3c07a478350aaf2e2c6e2a30",
        )

    def test_windows_official_pack_catalog_resolves_a_platform_descriptor_without_alias_rewrite(self) -> None:
        descriptor = {
            "pack_id": "embedding-test-windows-amd64",
            "descriptor_url": "https://example.invalid/descriptor.json",
            "descriptor_sha256": "a" * 64,
        }
        catalog = {"semantic": {"windows-amd64": descriptor}}
        with mock.patch("brain.models.platform_id", return_value="windows-amd64"), mock.patch(
            "brain.models.OFFICIAL_PACKS", catalog
        ), mock.patch("brain.models.install_release_descriptor", return_value={"pack_id": descriptor["pack_id"]}) as install:
            self.assertEqual([{
                "alias": "semantic", "pack_id": descriptor["pack_id"],
                "descriptor_url": descriptor["descriptor_url"],
            }], official_packs())
            self.assertEqual(descriptor["pack_id"], install_official_pack(mock.Mock(), "semantic")["pack_id"])
        install.assert_called_once_with(mock.ANY, descriptor["descriptor_url"], descriptor["descriptor_sha256"])

    def test_model_runtime_manifest_rejects_a_foreign_native_binary(self) -> None:
        manifest = {
            "pack_id": "foreign", "capability": "embedding", "model_family": "Qwen3",
            "upstream_model": "public", "upstream_revision": "1", "license": "Apache-2.0",
            "runtime_name": "llama.cpp", "runtime_revision": "1", "minimum_brain_version": "1.0.0",
            "runtime_binary": "llama-server.exe", "model_file": "model.gguf", "golden_suite": "suite.json",
            "golden_suite_hash": "1" * 64, "weight_format": "GGUF", "quantization": "Q6_K",
            "weight_sha256": "2" * 64, "tokenizer_file": "tokenizer.json", "tokenizer_sha256": "3" * 64,
            "pooling": "last-token", "normalization": "none", "query_instruction_version": "1",
            "document_card_version": "1", "chunk_schema_version": "1", "embedding_dimension": 2560,
            "converter_revision": "pinned", "runtime_compatibility": {"os": "darwin", "architecture": "arm64"},
            "artifacts": {"llama-server.exe": "4" * 64, "model.gguf": "2" * 64, "tokenizer.json": "3" * 64, "suite.json": "1" * 64},
        }
        with mock.patch("brain.models.platform_id", return_value="windows-amd64"):
            with self.assertRaisesRegex(ValueError, "darwin-arm64, not windows-amd64"):
                validate_manifest(manifest)

        manifest.pop("runtime_compatibility")
        with mock.patch("brain.models.platform_id", return_value="windows-amd64"):
            with self.assertRaisesRegex(ValueError, "require runtime_compatibility"):
                validate_manifest(manifest)

    def test_installed_pack_discovery_rejects_symlink_oversize_and_path_mismatch(self) -> None:
        from brain.models import active_pack, installed_packs

        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            root = state / "models"
            root.mkdir(parents=True)
            settings = SimpleNamespace(state_dir=state)

            def manifest(pack_id: str, installed_path: Path) -> dict[str, object]:
                return {
                    "pack_id": pack_id, "capability": "test", "model_family": "test",
                    "upstream_model": "local", "upstream_revision": "1", "license": "MIT",
                    "runtime_name": "deterministic-test", "runtime_revision": "1",
                    "minimum_brain_version": "1.0.0", "query_instruction_version": "1",
                    "document_card_version": "1", "chunk_schema_version": "1",
                    "embedding_dimension": 4, "converter_revision": "1", "test_only": True,
                    "verified": True, "installed_path": str(installed_path),
                }

            outside = Path(temporary) / "outside.json"
            symlink_pack = root / "symlink-pack"
            symlink_pack.mkdir()
            outside.write_text(json.dumps(manifest("symlink-pack", symlink_pack)), encoding="utf-8")
            (symlink_pack / "installed.json").symlink_to(outside)

            oversized = root / "oversized"
            oversized.mkdir()
            (oversized / "installed.json").write_bytes(b"{" + b"x" * (2 * 1024 * 1024) + b"}")

            mismatched = root / "mismatched"
            mismatched.mkdir()
            (mismatched / "installed.json").write_text(
                json.dumps(manifest("mismatched", root / "other")), encoding="utf-8",
            )

            packs = installed_packs(settings)
            self.assertEqual({"mismatched", "oversized", "symlink-pack"}, {
                str(item["pack_id"]) for item in packs if item.get("invalid")
            })
            self.assertIsNone(active_pack(settings, "test"))

    def test_windows_ci_release_and_pack_workflows_are_first_class(self) -> None:
        root = Path(__file__).resolve().parents[1]
        ci = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        release = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        semantic = (root / ".github/workflows/semantic-pack-windows.yml").read_text(encoding="utf-8")
        precision = (root / ".github/workflows/precision-pack-windows.yml").read_text(encoding="utf-8")
        semantic_builder = (root / "scripts/build_semantic_pack.py").read_text(encoding="utf-8")
        precision_builder = (root / "scripts/build_precision_pack.py").read_text(encoding="utf-8")
        self.assertIn("Native Windows Python ${{ matrix.python-version }}", ci)
        for version in ('"3.11"', '"3.12"', '"3.13"', '"3.14"'):
            self.assertIn(version, ci)
        self.assertIn("project-brain-$env:GITHUB_REF_NAME-windows-amd64.zip", release)
        self.assertIn("Native Windows Python ${{ matrix.python-version }}", release)
        self.assertIn("886b229dcd5e7bec0c9918002b77345d27c84e3c", release)
        self.assertIn("scripts/windows/zoekt-windows-amd64.patch", release)
        self.assertIn("Stable v1 model-pack catalog readiness", release)
        self.assertIn("install_official_pack(settings, alias)", release)
        self.assertIn("verify_pack(settings", release)
        self.assertIn("windows-latest", release)
        self.assertIn("macos-14", release)
        self.assertIn("Project Brain native Core retrieval smoke failed", release)
        self.assertIn("Project Brain native multi-ticket smoke failed", release)
        self.assertIn("Project Brain native Auto Refresh smoke failed", release)
        self.assertIn("Project Brain native UI status smoke failed", release)
        self.assertIn("RedirectStandardOutput $uiStdout", release)
        self.assertIn("X-Brain-Token", release)
        self.assertIn("Project Brain native UI token was not published before timeout", release)
        self.assertIn("uses: actions/attest@v4", release)
        self.assertIn('subject-path: "dist/*"', release)
        self.assertIn("name: final-release-checksums", release)
        self.assertIn("path: dist/SHA256SUMS.txt", release)
        self.assertIn("needs: [github-release, build]", release)
        self.assertIn("codebase-memory-mcp.exe,zoekt-bin/zoekt.exe,zoekt-bin/zoekt-index.exe", release)
        self.assertIn("1e5ba3cca69194b9229f89a0cafe4a6538ba9e0040d892d57bf472e992420273", release)
        self.assertIn("--platform windows-amd64", semantic)
        self.assertIn("--platform windows-amd64", precision)
        for workflow in (semantic, precision):
            self.assertEqual(1, workflow.count("runs-on: windows-2022"))
            self.assertEqual(1, workflow.count("runs-on: windows-latest"))
            self.assertIn("-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded", workflow)
            self.assertIn("-DGGML_OPENMP=OFF", workflow)
            self.assertIn('-G "Visual Studio 17 2022" -A x64', workflow)
            self.assertIn("--config Release", workflow)
            self.assertIn("/DEPENDENTS upstream/llama-server.exe", workflow)
            self.assertIn("non-portable runtime dependencies", workflow)
            self.assertIn("vcomp|libomp", workflow)
            self.assertIn("environment: model-pack-publish", workflow)
            self.assertIn("$global:LASTEXITCODE = 0", workflow)
            self.assertIn("immutable pack releases cannot be appended", workflow)
            self.assertIn("git/ref/tags/$env:RELEASE_TAG", workflow)
            self.assertIn("release or tag appeared after validation", workflow)
            self.assertIn("gh release view $env:RELEASE_TAG --json tagName,targetCommitish,isDraft", workflow)
            self.assertIn("created pack draft does not reference the reviewed builder commit", workflow)
            self.assertIn("created pack tag does not reference the reviewed builder commit", workflow)
            self.assertIn("gh release create $env:RELEASE_TAG --draft", workflow)
            self.assertIn("gh release download $env:RELEASE_TAG --dir release-verification", workflow)
            self.assertIn("gh release edit $env:RELEASE_TAG --draft=false --latest=false", workflow)
            self.assertLess(
                workflow.index("gh release create $env:RELEASE_TAG --draft"),
                workflow.index("created pack draft does not reference the reviewed builder commit"),
            )
            self.assertLess(
                workflow.index("gh release edit $env:RELEASE_TAG --draft=false --latest=false"),
                workflow.index("created pack tag does not reference the reviewed builder commit"),
            )
            self.assertIn("--builder-revision $env:BUILDER_REVISION", workflow)
        self.assertIn("default: false", semantic)
        self.assertIn("default: false", precision)
        self.assertIn("semantic-pack-windows-vX.Y.Z", semantic)
        self.assertIn("precision-pack-windows-vX.Y.Z", precision)
        self.assertIn("semantic-pack-windows-dist/*-metadata.tar.gz", semantic)
        self.assertIn("cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30", semantic)
        self.assertIn("precision-pack-windows-dist/*-metadata.tar.gz", precision)
        self.assertNotIn("path: semantic-pack-windows-dist/*\n", semantic)
        self.assertNotIn("path: precision-pack-windows-dist/*\n", precision)
        self.assertIn('"runtime_args": ["--ctx-size", "4096", "-ub", "512"]', semantic_builder)
        self.assertIn(
            '"runtime_args": ["--ctx-size", str(RERANK_CONTEXT_TOKENS), "-ub", str(RERANK_PHYSICAL_BATCH_TOKENS)]',
            precision_builder,
        )
        self.assertIn('parser.add_argument("--builder-revision", required=True)', semantic_builder)
        self.assertIn('parser.add_argument("--builder-revision", required=True)', precision_builder)

    @unittest.skipUnless(os.name == "nt", "native Windows executable smoke is covered by windows-latest")
    def test_windows_shell_free_python_process_handles_spaces_and_unicode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="Project Brain 原生 ") as temporary:
            script = Path(temporary) / "echo args.py"
            output = Path(temporary) / "结果.txt"
            script.write_text(
                "import pathlib, sys; pathlib.Path(sys.argv[2]).write_text(sys.argv[1], encoding='utf-8')",
                encoding="utf-8",
            )
            result = subprocess.run(
                [os.sys.executable, str(script), "带 空格", str(output)],
                text=True, capture_output=True, check=False, shell=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("带 空格", output.read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "nt", "native Windows process-tree behavior requires Windows")
    def test_windows_process_tree_reaps_a_real_child_process(self) -> None:
        with tempfile.TemporaryDirectory(prefix="Project Brain tree ") as temporary:
            root = Path(temporary)
            child_pid = root / "child.pid"
            script = root / "parent.py"
            script.write_text(
                "import pathlib, subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='ascii')\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            process = subprocess.Popen(
                [os.sys.executable, str(script), str(child_pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **process_group_kwargs(windows=True),
            )
            try:
                child = _wait_for_pid(child_pid)
                terminate_process_tree(process, graceful_timeout=0.1)

                deadline = time.monotonic() + 5
                while _process_is_active(child) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertFalse(_process_is_active(child))
            finally:
                if process.poll() is None:
                    terminate_process_tree(process, graceful_timeout=0)

    @unittest.skipUnless(os.name == "nt", "native Windows lock behavior requires Windows")
    def test_windows_real_shared_leases_exclude_a_cross_process_writer(self) -> None:
        with tempfile.TemporaryDirectory(prefix="Project Brain locks ") as temporary:
            root = Path(temporary)
            (root / "repository").mkdir()
            config = root / "brain.toml"
            config.write_text(
                "[project]\nname = 'windows-lock-test'\nstate_dir = 'state'\n\n"
                "[[repositories]]\nname = 'repository'\npath = 'repository'\n",
                encoding="utf-8",
            )
            script = root / "reader.py"
            script.write_text(
                "import pathlib, sys, time\n"
                "from brain.core import load_settings\n"
                "from brain.locks import workspace_retrieval\n"
                "settings = load_settings(pathlib.Path(sys.argv[1]))\n"
                "with workspace_retrieval(settings):\n"
                "    pathlib.Path(sys.argv[2]).write_text('ready', encoding='ascii')\n"
                "    time.sleep(60)\n",
                encoding="utf-8",
            )
            signals = [root / "reader-one.ready", root / "reader-two.ready"]
            readers = [
                subprocess.Popen(
                    [os.sys.executable, str(script), str(config), str(signal)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    **process_group_kwargs(windows=True),
                )
                for signal in signals
            ]
            try:
                deadline = time.monotonic() + 10
                while not all(signal.is_file() for signal in signals) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(all(signal.is_file() for signal in signals))
                settings = load_settings(config)
                with self.assertRaises(locks.WorkspaceOperationBusy):
                    with locks.workspace_operation(settings):
                        pass
            finally:
                for reader in readers:
                    if reader.poll() is None:
                        terminate_process_tree(reader, graceful_timeout=0)


if __name__ == "__main__":
    unittest.main()

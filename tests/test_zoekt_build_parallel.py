from __future__ import annotations

import json
import hashlib
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from brain.backends import zoekt


class ZoektBuildTests(unittest.TestCase):
    def test_shard_manifest_rejects_leaf_symlink_and_oversized_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "shard"
            target.mkdir()
            outside = root / "outside.json"
            outside.write_text('{"manifest_version": 3}\n', encoding="utf-8")
            manifest = target / "brain-shard.json"
            try:
                manifest.symlink_to(outside)
            except OSError as error:
                self.skipTest(f"file symlinks unavailable: {error}")
            self.assertIsNone(zoekt._manifest(target))
            self.assertEqual('{"manifest_version": 3}\n', outside.read_text(encoding="utf-8"))
            manifest.unlink()
            manifest.write_bytes(b"{" + b" " * (2 * 1024 * 1024) + b"}")
            self.assertIsNone(zoekt._manifest(target))

    def test_shard_validation_and_old_target_accounting_have_hard_entry_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            for number in range(3):
                (target / f"{number}.zoekt").write_bytes(b"shard")
            (target / "brain-shard.json").write_text(json.dumps({
                "manifest_version": zoekt.SHARD_MANIFEST_VERSION,
                "source_sha": "sha-g1", "repo": "service",
                "path_identity": zoekt.SHARD_PATH_IDENTITY,
                "shards": [
                    {"name": f"{number}.zoekt", "size": 5, "sha256": hashlib.sha256(b"shard").hexdigest()}
                    for number in range(3)
                ],
            }), encoding="utf-8")
            with mock.patch.object(zoekt, "_ZOEKT_MAX_SHARDS", 2):
                self.assertFalse(zoekt.valid_shard_manifest(target, "sha-g1"))
                self.assertIsNone(zoekt.serving_shard_manifest_identity(target, "sha-g1"))
            with mock.patch.object(zoekt, "_ZOEKT_SHARD_DIRECTORY_ITEMS", 2), mock.patch.object(
                Path, "rglob", side_effect=AssertionError("unbounded target walk"),
            ), self.assertRaisesRegex(OSError, "item or time limit"):
                zoekt._bounded_target_bytes(target)

    def test_zoekt_capacity_failure_never_publishes_the_built_shard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot"
            working = root / "working"
            snapshot.mkdir()
            working.mkdir()
            repo = SimpleNamespace(
                name="service", path=working, source_path=snapshot,
                source_sha="sha-g1", scan_path=snapshot,
            )

            def fake_run(command, _cwd, **_kwargs):
                (Path(command[2]) / "service.zoekt").write_bytes(b"built-but-over-quota")
                return subprocess.CompletedProcess(command, 0, "", "")

            settings = SimpleNamespace(
                state_dir=root / "state", root=root,
                max_state_gb=1, minimum_free_disk_gb=0,
            )
            available = zoekt.ZoektStatus(True, "zoekt", "zoekt-index")
            with mock.patch("brain.backends.zoekt.status", return_value=available), mock.patch(
                "brain.backends.zoekt.run_bounded_process", side_effect=fake_run,
            ), mock.patch("brain.ops.remaining_write_capacity", return_value=0):
                result = zoekt.build(settings, [repo])
            self.assertEqual("failed", result["service"]["status"])
            self.assertIn("capacity", result["service"]["reason"])
            self.assertFalse(zoekt.shard_path(settings.state_dir, "service", "sha-g1").exists())

    def test_search_streams_to_candidate_limit_and_reclaims_the_producer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = zoekt.shard_path(root / "state", "service", "sha-g1")
            target.mkdir(parents=True)
            shard = target / "fixture.zoekt"
            shard.write_bytes(b"valid")
            (target / "brain-shard.json").write_text(json.dumps({
                "manifest_version": zoekt.SHARD_MANIFEST_VERSION,
                "source_sha": "sha-g1", "repo": "service",
                "path_identity": zoekt.SHARD_PATH_IDENTITY,
                "shards": [{"name": shard.name, "size": 5, "sha256": hashlib.sha256(b"valid").hexdigest()}],
            }), encoding="utf-8")
            snapshot = root / "snapshot"
            snapshot.mkdir()
            repo = SimpleNamespace(
                name="service", source_sha="sha-g1", source_path=snapshot,
                scan_path=snapshot, path=root / "working",
            )
            row = (json.dumps({
                "FileName": "src/service.py", "Score": 1,
                "LineMatches": [{"LineNumber": 1, "Line": "needle"}],
            }) + "\n").encode("utf-8")

            class Output:
                calls = 0

                def readline(self, limit: int) -> bytes:
                    self.calls += 1
                    return row

                def close(self) -> None:
                    return None

            class Process:
                def __init__(self) -> None:
                    self.stdout = Output()
                    self.terminated = False

                def terminate(self) -> None:
                    self.terminated = True

                def kill(self) -> None:
                    self.terminated = True

                def wait(self, timeout: float | None = None) -> int:
                    return -15 if self.terminated else 0

            process = Process()
            available = zoekt.ZoektStatus(True, "zoekt", "zoekt-index")
            with mock.patch("brain.backends.zoekt.status", return_value=available), mock.patch(
                "brain.backends.zoekt.start_managed_process", return_value=process,
            ) as spawned, mock.patch(
                "brain.backends.zoekt.terminate_process_tree",
                side_effect=lambda child, **_kwargs: child.terminate(),
            ) as terminated:
                result = zoekt.search(
                    SimpleNamespace(state_dir=root / "state"), repo, "needle", fixed=True, max_results=1,
                )
            self.assertIsNotNone(result)
            self.assertEqual(1, len(result[0]))
            self.assertTrue(process.terminated)
            self.assertLessEqual(process.stdout.calls, 9)
            spawned.assert_called_once()
            terminated.assert_called_once_with(process, graceful_timeout=1)

    def test_pinned_manifest_hash_rejects_same_snapshot_shard_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = zoekt.shard_path(root / "state", "service", "sha-g1")
            target.mkdir(parents=True)
            shard = target / "fixture.zoekt"

            def publish(value: bytes) -> str:
                shard.write_bytes(value)
                (target / "brain-shard.json").write_text(json.dumps({
                    "manifest_version": zoekt.SHARD_MANIFEST_VERSION,
                    "source_sha": "sha-g1", "repo": "service",
                    "path_identity": zoekt.SHARD_PATH_IDENTITY,
                    "shards": [{
                        "name": shard.name, "size": len(value),
                        "sha256": hashlib.sha256(value).hexdigest(),
                    }],
                }), encoding="utf-8")
                return str(zoekt.shard_manifest_identity(target, "sha-g1"))

            first = publish(b"first")
            second = publish(b"second")
            self.assertNotEqual(first, second)
            repo = SimpleNamespace(
                name="service", source_sha="sha-g1", source_path=root / "snapshot",
                scan_path=root / "snapshot", path=root / "working",
            )
            repo.source_path.mkdir()
            repo.path.mkdir()
            available = zoekt.ZoektStatus(True, "zoekt", "zoekt-index")
            with mock.patch("brain.backends.zoekt.status", return_value=available), mock.patch(
                "brain.backends.zoekt.run_bounded_process",
            ) as run, mock.patch(
                "brain.backends.zoekt._shard_entries",
                side_effect=AssertionError("query serving must not hash every Zoekt shard"),
            ):
                self.assertIsNone(zoekt.search(
                    SimpleNamespace(state_dir=root / "state"), repo, "x", fixed=True,
                    max_results=5, expected_manifest_hash=first,
                ))
                run.assert_not_called()

    def test_same_size_shard_replacement_cannot_reuse_validation_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            shard = target / "fixture.zoekt"
            shard.write_bytes(b"original")
            (target / "brain-shard.json").write_text(json.dumps({
                "manifest_version": zoekt.SHARD_MANIFEST_VERSION,
                "source_sha": "sha-g1",
                "repo": "service",
                "path_identity": zoekt.SHARD_PATH_IDENTITY,
                "shards": [{
                    "name": shard.name, "size": shard.stat().st_size,
                    "sha256": hashlib.sha256(shard.read_bytes()).hexdigest(),
                }],
            }), encoding="utf-8")
            self.assertTrue(zoekt.valid_shard_manifest(target, "sha-g1"))
            before = shard.stat()
            shard.write_bytes(b"tampered")
            self.assertEqual(before.st_size, shard.stat().st_size)
            os.utime(shard, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertFalse(zoekt.valid_shard_manifest(target, "sha-g1"))

    def test_windows_validation_rehashes_same_stat_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            shard = target / "fixture.zoekt"
            shard.write_bytes(b"original")
            manifest = target / "brain-shard.json"
            manifest.write_text(json.dumps({
                "manifest_version": zoekt.SHARD_MANIFEST_VERSION,
                "source_sha": "sha-g1",
                "repo": "service",
                "path_identity": zoekt.SHARD_PATH_IDENTITY,
                "shards": [{
                    "name": shard.name, "size": shard.stat().st_size,
                    "sha256": hashlib.sha256(shard.read_bytes()).hexdigest(),
                }],
            }), encoding="utf-8")
            zoekt._SHARD_VALIDATION_CACHE.clear()
            with (
                mock.patch("brain.backends.zoekt.os.name", "nt"),
                mock.patch(
                    "brain.backends.zoekt._shard_entries", wraps=zoekt._shard_entries,
                ) as validated,
            ):
                self.assertTrue(zoekt.valid_shard_manifest(target, "sha-g1"))
                self.assertTrue(zoekt.valid_shard_manifest(target, "sha-g1"))
                self.assertEqual(2, validated.call_count)
                shard.write_bytes(b"tampered")
                self.assertFalse(zoekt.valid_shard_manifest(target, "sha-g1"))
                self.assertEqual(3, validated.call_count)
            self.assertEqual({}, zoekt._SHARD_VALIDATION_CACHE)

    def test_builds_changed_repositories_in_bounded_input_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            repositories = []
            for name in ("slow", "fast", "current", "failed"):
                working = root / "working" / name
                snapshot = root / "snapshots" / name
                working.mkdir(parents=True)
                snapshot.mkdir(parents=True)
                repositories.append(SimpleNamespace(
                    name=name,
                    path=working,
                    source_path=snapshot,
                    source_sha=f"sha-{name}",
                    scan_path=snapshot,
                ))

            current = repositories[2]
            current_target = zoekt.shard_path(state_dir, current.name, current.source_sha)
            current_target.mkdir(parents=True)
            current_shard = current_target / "current.zoekt"
            current_shard.write_bytes(b"current")
            (current_target / "brain-shard.json").write_text(json.dumps({
                "manifest_version": zoekt.SHARD_MANIFEST_VERSION,
                "source_sha": current.source_sha,
                "repo": current.name,
                "path_identity": zoekt.SHARD_PATH_IDENTITY,
                "shards": [{"name": current_shard.name, "size": current_shard.stat().st_size,
                            "sha256": hashlib.sha256(current_shard.read_bytes()).hexdigest()}],
            }), encoding="utf-8")

            active = 0
            maximum = 0
            guard = threading.Lock()
            slow_started = threading.Event()
            slow_release = threading.Event()
            calls = []
            timeouts = []

            def fake_run(command, *_args, **kwargs):
                nonlocal active, maximum
                name = Path(command[-1]).name
                with guard:
                    calls.append(name)
                    timeouts.append(kwargs["timeout"])
                if name == "failed":
                    return subprocess.CompletedProcess(command, 1, "", "failed")
                with guard:
                    active += 1
                    maximum = max(maximum, active)
                try:
                    if name == "slow":
                        slow_started.set()
                        slow_release.wait(timeout=1)
                    elif name == "fast":
                        slow_started.wait(timeout=1)
                        slow_release.set()
                    temporary = Path(command[2])
                    (temporary / f"{name}.zoekt").write_bytes(name.encode("utf-8"))
                    return subprocess.CompletedProcess(command, 0, "", "")
                finally:
                    with guard:
                        active -= 1

            available = zoekt.ZoektStatus(True, "zoekt", "zoekt-index")
            with mock.patch("brain.backends.zoekt.status", return_value=available), mock.patch(
                "brain.backends.zoekt.run_bounded_process", side_effect=fake_run
            ):
                result = zoekt.build(SimpleNamespace(state_dir=state_dir), repositories)

            self.assertEqual(["slow", "fast", "current", "failed"], list(result))
            self.assertCountEqual(calls, ["slow", "fast", "failed"])
            self.assertEqual(3, len(timeouts))
            self.assertTrue(all(timeout == 120 for timeout in timeouts))
            self.assertGreater(maximum, 1)
            self.assertLessEqual(maximum, 2)
            self.assertEqual("built", result["slow"]["status"])
            self.assertEqual("built", result["fast"]["status"])
            self.assertEqual("current", result["current"]["status"])
            self.assertEqual({"status": "failed", "reason": "zoekt indexing failed"}, result["failed"])

            targets = []
            for name in ("slow", "fast", "current"):
                target = zoekt.shard_path(state_dir, name, f"sha-{name}")
                targets.append(target)
                self.assertEqual(str(target), result[name]["path"])
                self.assertEqual(f"sha-{name}", result[name]["source_sha"])
                manifest = json.loads((target / "brain-shard.json").read_text(encoding="utf-8"))
                self.assertEqual(f"sha-{name}", manifest["source_sha"])
                self.assertEqual(zoekt.SHARD_PATH_IDENTITY, manifest["path_identity"])
                self.assertEqual([{
                    "name": f"{name}.zoekt", "size": len(name.encode("utf-8")),
                    "sha256": hashlib.sha256(name.encode("utf-8")).hexdigest(),
                }], manifest["shards"])
                self.assertEqual([target], list(target.parent.iterdir()))

            self.assertEqual(3, len({str(target) for target in targets}))
            self.assertEqual([], list(zoekt.shard_path(state_dir, "failed", "sha-failed").parent.iterdir()))
            self.assertEqual(b"current", current_shard.read_bytes())


if __name__ == "__main__":
    unittest.main()

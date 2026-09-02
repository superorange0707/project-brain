from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from brain.core import load_settings
from brain.graph import graph_symbol_hits, graph_trace
from brain.locks import (
    TicketOperationBusy,
    WorkspaceOperationBusy,
    model_lane,
    retrieval_capacity,
    retrieval_session,
    ticket_operation,
    workspace_operation,
    workspace_retrieval,
)
from brain.sync import sync_repositories


class WorkspaceOperationLockTests(unittest.TestCase):
    def test_lock_files_and_directories_never_follow_managed_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "repository").mkdir()
            config = root / "brain.toml"
            config.write_text(
                "[project]\nname = 'lock-test'\nstate_dir = 'state'\n\n"
                "[[repositories]]\nname = 'repository'\npath = 'repository'\n",
                encoding="utf-8",
            )
            settings = load_settings(config)
            settings.state_dir.mkdir(parents=True, exist_ok=True)
            outside = root / "outside.lock"
            outside.write_bytes(b"preserve-outside-lock")
            for name, operation in (
                ("operations.lock", lambda: workspace_operation(settings)),
                ("model-lane.lock", lambda: model_lane(settings)),
            ):
                trap = settings.state_dir / name
                try:
                    trap.symlink_to(outside)
                except OSError as error:
                    self.skipTest(f"file symlinks unavailable: {error}")
                with self.assertRaises((OSError, ValueError)):
                    with operation():
                        pass
                self.assertEqual(b"preserve-outside-lock", outside.read_bytes())
                trap.unlink()

            outside_directory = root / "outside-lock-directory"
            outside_directory.mkdir()
            for name, operation in (
                ("ticket-locks", lambda: ticket_operation(settings, "SAFE-1")),
                ("retrieval-slots", lambda: retrieval_capacity(settings)),
            ):
                trap = settings.state_dir / name
                try:
                    trap.symlink_to(outside_directory, target_is_directory=True)
                except OSError as error:
                    self.skipTest(f"directory symlinks unavailable: {error}")
                with self.assertRaises((OSError, ValueError)):
                    with operation():
                        pass
                self.assertEqual([], list(outside_directory.iterdir()))
                trap.unlink()

    def test_sync_cannot_mutate_sources_while_a_retrieval_lease_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "repository").mkdir()
            config = root / "brain.toml"
            config.write_text(
                "[project]\nname = 'lock-test'\nstate_dir = 'state'\n\n"
                "[[repositories]]\nname = 'repository'\npath = 'repository'\n",
                encoding="utf-8",
            )
            settings = load_settings(config)
            with workspace_retrieval(settings):
                with self.assertRaisesRegex(WorkspaceOperationBusy, "cannot upgrade"):
                    sync_repositories(settings, fetch=False)
            self.assertFalse((settings.state_dir / "sources.json").exists())

    def test_shared_lease_cannot_reenter_as_a_workspace_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "repository").mkdir()
            config = root / "brain.toml"
            config.write_text(
                "[project]\nname = 'lock-test'\nstate_dir = 'state'\n\n"
                "[[repositories]]\nname = 'repository'\npath = 'repository'\n",
                encoding="utf-8",
            )
            settings = load_settings(config)
            self.assertTrue(settings.graph_lazy)
            with workspace_retrieval(settings):
                with mock.patch("brain.graph.find_backend", return_value=Path(__file__)), mock.patch(
                    "brain.graph.index_graph",
                ) as index_graph:
                    self.assertEqual([], graph_symbol_hits(settings, "symbol", ["repository"]))
                    self.assertEqual(([], []), graph_trace(settings, "symbol", ["repository"]))
                    index_graph.assert_not_called()
                with workspace_retrieval(settings):
                    pass
                with self.assertRaisesRegex(WorkspaceOperationBusy, "cannot upgrade"):
                    with workspace_operation(settings):
                        pass

    def test_different_tickets_share_workspace_while_same_ticket_and_mutation_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "repository").mkdir()
            config = root / "brain.toml"
            config.write_text(
                "[project]\nname = 'lock-test'\nstate_dir = 'state'\n\n"
                "[[repositories]]\nname = 'repository'\npath = 'repository'\n",
                encoding="utf-8",
            )
            settings = load_settings(config)
            entered = {ticket: threading.Event() for ticket in ("TICKET-A", "TICKET-B")}
            release = threading.Event()

            def hold_ticket(ticket: str) -> None:
                with retrieval_session(settings, ticket):
                    entered[ticket].set()
                    release.wait(3)

            threads = [threading.Thread(target=hold_ticket, args=(ticket,)) for ticket in entered]
            for thread in threads:
                thread.start()
            self.assertTrue(all(event.wait(3) for event in entered.values()))
            with self.assertRaises(TicketOperationBusy):
                with retrieval_session(settings, "TICKET-A"):
                    pass
            with self.assertRaises(WorkspaceOperationBusy):
                with retrieval_session(settings, "TICKET-C"):
                    pass
            with self.assertRaises(WorkspaceOperationBusy):
                with workspace_operation(settings):
                    pass
            release.set()
            for thread in threads:
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())

    def test_workspace_lock_rejects_a_second_process_and_releases_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()
            config = root / "brain.toml"
            config.write_text(
                "[project]\nname = 'lock-test'\nstate_dir = 'state'\n\n"
                "[[repositories]]\nname = 'repository'\npath = 'repository'\n",
                encoding="utf-8",
            )
            settings = load_settings(config)
            commands = [
                ["refresh", "--no-fetch", "--no-discover"],
                ["index", "rebuild", "--backend", "lexical"],
                ["index", "rebuild", "--backend", "semantic"],
                ["map"],
                ["experience", "--rebuild"],
                ["edition", "set", "core"],
                ["model", "remove", "not-installed"],
                ["gc", "--no-dry-run"],
            ]

            def run(command: list[str]) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [sys.executable, "-m", "brain.cli", "-c", str(config), *command],
                    cwd=Path(__file__).resolve().parents[1],
                    capture_output=True,
                    text=True,
                    check=False,
                )

            with workspace_operation(settings):
                blocked = [run(command) for command in commands]
                read_only = run(["model", "status"])
            released = run(["edition", "set", "core"])

            self.assertEqual([2] * len(commands), [result.returncode for result in blocked])
            self.assertTrue(all(result.stdout == "" for result in blocked))
            for result in blocked:
                self.assertIn("workspace operation is already running", result.stderr)
                self.assertNotIn(str(root), result.stderr)
            self.assertEqual(0, read_only.returncode)
            self.assertEqual("[]\n", read_only.stdout)
            self.assertEqual(0, released.returncode)
            self.assertEqual("core\n", released.stdout)


if __name__ == "__main__":
    unittest.main()

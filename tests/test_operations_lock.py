from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from brain.core import load_settings
from brain.locks import TicketOperationBusy, WorkspaceOperationBusy, retrieval_session, workspace_operation


class WorkspaceOperationLockTests(unittest.TestCase):
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
                ["index", "rebuild", "--backend", "semantic"],
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

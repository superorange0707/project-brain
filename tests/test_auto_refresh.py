from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from brain.auto_refresh import AutoRefreshService, FreshnessDecision, detect_auto_refresh
from brain.cli import main
from brain.core import load_settings, session_state, start_session
from brain.ui import _OperationCoordinator


class Clock:
    def __init__(self) -> None:
        self.value = 1_000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class AutoRefreshServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "service-a"
        self.repository.mkdir()
        self.config = self.root / "brain.toml"
        self.config.write_text(
            '[project]\nname="auto-refresh"\n[graph]\nenabled=false\n'
            '[[repositories]]\nname="service-a"\npath="service-a"\n',
            encoding="utf-8",
        )
        self.settings = load_settings(self.config)
        self.settings.experience_enabled = False
        self.clock = Clock()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def service(self, detector, refresher=None, *, idle=None, debounce=5) -> AutoRefreshService:
        return AutoRefreshService(
            self.settings,
            detector=detector,
            refresher=refresher or Mock(return_value={}),
            is_idle=idle or (lambda: True),
            mode="when_idle",
            persist=False,
            interval_seconds=60,
            debounce_seconds=debounce,
            cooldown_seconds=30,
            backoff_seconds=30,
            clock=self.clock,
        )

    def test_no_change_polling_does_not_refresh(self) -> None:
        refresh = Mock(return_value={})
        service = self.service(lambda _settings: FreshnessDecision.ready(), refresh)

        status = service.poll(force_check=True)

        refresh.assert_not_called()
        self.assertEqual("ready", status["status"])
        self.assertFalse(status["pending"])

    def test_one_sha_drift_schedules_one_refresh(self) -> None:
        refresh = Mock(return_value={})
        decisions = iter([
            FreshnessDecision.refresh("Selected source snapshots changed."),
            FreshnessDecision.ready(),
        ])
        service = self.service(lambda _settings: next(decisions), refresh)

        self.assertTrue(service.poll(force_check=True)["pending"])
        refresh.assert_not_called()
        self.clock.advance(5)
        self.assertFalse(service.poll()["pending"])
        refresh.assert_called_once_with()
        self.clock.advance(30)
        service.poll(force_check=True)
        refresh.assert_called_once_with()

    def test_many_changes_during_debounce_coalesce_to_one_refresh(self) -> None:
        refresh = Mock(return_value={})
        service = self.service(
            lambda _settings: FreshnessDecision.refresh("Selected source snapshots changed."),
            refresh,
        )

        service.poll(force_check=True)
        for _ in range(20):
            service.poll(force_check=True)
        refresh.assert_not_called()
        self.clock.advance(5)
        service.poll()
        refresh.assert_called_once_with()

    def test_two_active_tickets_leave_one_pending_refresh_until_idle(self) -> None:
        coordinator = _OperationCoordinator(max_retrievals=2)
        entered = [threading.Event(), threading.Event()]
        release = threading.Event()

        def retrieval(index):
            def run(_progress):
                entered[index].set()
                release.wait(3)
                return {}
            return run

        coordinator.start("retrieval", retrieval(0), kind="retrieval", ticket="A")
        coordinator.start("retrieval", retrieval(1), kind="retrieval", ticket="B")
        self.assertTrue(all(event.wait(3) for event in entered))
        refresh = Mock(return_value={})
        service = self.service(
            lambda _settings: FreshnessDecision.refresh("Core indexes are stale."),
            refresh,
            idle=coordinator.is_idle,
            debounce=0,
        )

        status = service.poll(force_check=True)
        self.assertEqual(2, coordinator.active_retrieval_count())
        self.assertEqual("waiting_for_idle", status["status"])
        refresh.assert_not_called()
        release.set()
        for _ in range(100):
            if coordinator.is_idle():
                break
            time.sleep(0.01)
        service.poll()
        refresh.assert_called_once_with()

    def test_existing_sessions_keep_pinned_shas_after_refresh(self) -> None:
        old_snapshot = self.settings.state_dir / "snapshots/service-a/old"
        new_snapshot = self.settings.state_dir / "snapshots/service-a/new"
        old_snapshot.mkdir(parents=True)
        new_snapshot.mkdir(parents=True)
        repo = self.settings.repo("service-a")
        repo.source_path = old_snapshot
        repo.source_sha = "old-sha"
        repo.source_ref = "origin/main"
        start_session(self.settings, "TICKET-A", "Keep the original snapshot.")

        def refresh() -> dict:
            repo.source_path = new_snapshot
            repo.source_sha = "new-sha"
            return {}

        service = self.service(
            lambda _settings: FreshnessDecision.refresh("Selected source snapshots changed."),
            refresh,
            debounce=0,
        )
        service.poll(force_check=True)

        pinned = session_state(self.settings, "TICKET-A")["sources"]["service-a"]
        self.assertEqual("old-sha", pinned["sha"])
        self.assertEqual(str(old_snapshot), pinned["snapshot"])

    def test_new_ticket_can_start_from_current_snapshot_while_refresh_is_pending(self) -> None:
        snapshot = self.settings.state_dir / "snapshots/service-a/current"
        snapshot.mkdir(parents=True)
        repo = self.settings.repo("service-a")
        repo.source_path = snapshot
        repo.source_sha = "current-ready-sha"
        service = self.service(
            lambda _settings: FreshnessDecision.refresh("Selected source snapshots changed."),
            idle=lambda: False,
            debounce=0,
        )

        self.assertTrue(service.poll(force_check=True)["pending"])
        start_session(self.settings, "TICKET-NEW", "Start without waiting.")

        self.assertEqual(
            "current-ready-sha",
            session_state(self.settings, "TICKET-NEW")["sources"]["service-a"]["sha"],
        )

    def test_non_refreshable_action_required_does_not_loop(self) -> None:
        detector = Mock(return_value=FreshnessDecision.action_required("Model capability requires attention."))
        refresh = Mock(return_value={})
        service = self.service(detector, refresh, debounce=0)

        for _ in range(5):
            service.poll(force_check=True)

        refresh.assert_not_called()
        self.assertEqual("action_required", service.status()["status"])

    def test_failed_git_check_backs_off_without_refresh(self) -> None:
        detector = Mock(return_value=FreshnessDecision.action_required(
            "Git freshness check requires attention.",
            check_failed=True,
        ))
        refresh = Mock(return_value={})
        service = self.service(detector, refresh, debounce=0)

        service.poll()
        self.clock.advance(1)
        service.poll()
        self.assertEqual(1, detector.call_count)
        self.clock.advance(29)
        service.poll()
        self.assertEqual(2, detector.call_count)
        refresh.assert_not_called()

    def test_runtime_refresh_failure_is_latched_instead_of_retried(self) -> None:
        detector = Mock(side_effect=[
            FreshnessDecision.refresh("Core indexes are stale."),
            FreshnessDecision.refresh("New repositories are pending."),
        ])
        refresh = Mock(side_effect=RuntimeError("private runtime detail"))
        service = self.service(detector, refresh, debounce=0)

        first = service.poll(force_check=True)
        self.assertEqual("action_required", first["status"])
        self.assertNotIn("private", json.dumps(first))
        self.clock.advance(30)
        service.poll(force_check=True)
        refresh.assert_called_once_with()

    def test_preference_is_brain_owned_and_persistent(self) -> None:
        service = AutoRefreshService(self.settings, mode="off")
        service.set_mode("when_idle")
        path = self.settings.state_dir / "auto-refresh.json"

        self.assertTrue(path.is_file())
        self.assertEqual("when_idle", json.loads(path.read_text(encoding="utf-8"))["mode"])
        restored = AutoRefreshService(self.settings)
        self.assertEqual("when_idle", restored.status()["mode"])

    def _initialize_git_state(self) -> str:
        subprocess.run(["git", "init", "-q"], cwd=self.repository, check=True)
        subprocess.run(["git", "config", "user.email", "brain@example.invalid"], cwd=self.repository, check=True)
        subprocess.run(["git", "config", "user.name", "Project Brain"], cwd=self.repository, check=True)
        (self.repository / "tracked.txt").write_text("one\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=self.repository, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=self.repository, check=True)
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repository, check=True, capture_output=True, text=True,
        ).stdout.strip()
        (self.settings.state_dir / "sources.json").write_text(json.dumps({
            "service-a": {"status": "current", "ref": "HEAD", "sha": sha},
        }), encoding="utf-8")
        (self.settings.state_dir / "indexes.json").write_text(json.dumps({
            "service-a": {"sha": sha},
        }), encoding="utf-8")
        return sha

    def test_normal_working_tree_edits_do_not_trigger_refresh(self) -> None:
        self._initialize_git_state()
        (self.repository / "tracked.txt").write_text("uncommitted edit\n", encoding="utf-8")

        self.assertEqual("ready", detect_auto_refresh(self.settings).kind)

    def test_local_selected_sha_change_is_refreshable(self) -> None:
        self._initialize_git_state()
        (self.repository / "tracked.txt").write_text("two\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=self.repository, check=True)
        subprocess.run(["git", "commit", "-qm", "second"], cwd=self.repository, check=True)

        decision = detect_auto_refresh(self.settings)
        self.assertEqual("refresh", decision.kind)
        self.assertIn("Selected source snapshots changed.", decision.reasons)

    def test_new_repository_requires_explicit_config_action_without_refresh_loop(self) -> None:
        (self.root / "service-b/.git").mkdir(parents=True)
        with patch("brain.editions.current_edition", return_value="semantic"), patch(
            "brain.editions.capabilities", return_value={"embedding": True, "reranker": False}
        ), patch("brain.ops.semantic_status", return_value={"aligned": False}):
            decision = detect_auto_refresh(self.settings)

        self.assertEqual("action_required", decision.kind)
        self.assertEqual(("New repositories require an explicit brain.toml edit.",), decision.reasons)

    def test_model_and_storage_action_required_states_are_not_refreshable(self) -> None:
        with patch("brain.editions.current_edition", return_value="semantic"), patch(
            "brain.editions.capabilities", return_value={"embedding": False, "reranker": False}
        ):
            model = detect_auto_refresh(self.settings)
        with patch("brain.ops.ensure_write_capacity", side_effect=OSError("private disk detail")):
            storage = detect_auto_refresh(self.settings)

        self.assertEqual("action_required", model.kind)
        self.assertEqual("action_required", storage.kind)
        self.assertNotIn("private", json.dumps(storage.reasons))

    def test_git_probe_failure_becomes_safe_backed_off_action_required(self) -> None:
        self._initialize_git_state()
        subprocess.run(
            ["git", "remote", "add", "origin", str(self.root / "missing-origin")],
            cwd=self.repository,
            check=True,
        )
        source_path = self.settings.state_dir / "sources.json"
        source = json.loads(source_path.read_text(encoding="utf-8"))
        source["service-a"]["ref"] = "origin/main"
        source_path.write_text(json.dumps(source), encoding="utf-8")

        decision = detect_auto_refresh(self.settings)
        refresh = Mock(return_value={})
        detector = Mock(return_value=decision)
        service = self.service(detector, refresh, debounce=0)
        service.poll()
        self.clock.advance(1)
        service.poll()

        self.assertEqual("action_required", decision.kind)
        self.assertTrue(decision.check_failed)
        self.assertEqual(1, detector.call_count)
        refresh.assert_not_called()

    def test_cli_watch_uses_the_shared_detector_without_unconditional_refresh(self) -> None:
        detector = Mock(return_value=FreshnessDecision.ready())
        with patch("brain.auto_refresh.detect_auto_refresh", detector), patch("brain.ops.refresh_brain") as refresh, patch(
            "builtins.print"
        ):
            result = main(["-c", str(self.config), "watch", "--once"])

        self.assertEqual(0, result)
        detector.assert_called_once()
        refresh.assert_not_called()


if __name__ == "__main__":
    unittest.main()

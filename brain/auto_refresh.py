"""Shared, opt-in freshness detection and idle refresh scheduling."""

from __future__ import annotations

import json
import os
import shlex
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .locks import WorkspaceOperationBusy
from .platforms import atomic_managed_text_write, read_managed_text

if TYPE_CHECKING:
    from .core import Repository, Settings


MAX_FRESHNESS_PROBE_SECONDS = 45.0


@dataclass(frozen=True)
class FreshnessDecision:
    """A source-free decision suitable for local status surfaces."""

    kind: str
    reasons: tuple[str, ...] = ()
    check_failed: bool = False

    @classmethod
    def ready(cls) -> FreshnessDecision:
        return cls("ready")

    @classmethod
    def refresh(cls, *reasons: str) -> FreshnessDecision:
        return cls("refresh", tuple(sorted(set(reasons))))

    @classmethod
    def action_required(cls, reason: str, *, check_failed: bool = False) -> FreshnessDecision:
        return cls("action_required", (reason,), check_failed)


def _branch_name(ref: str | None) -> str | None:
    value = (ref or "").strip()
    for prefix in ("refs/remotes/origin/", "refs/heads/", "origin/"):
        if value.startswith(prefix):
            value = value.removeprefix(prefix)
            break
    return value if value and value != "HEAD" and not value.startswith("refs/") else None


def _probe_repository(
    repo: Repository,
    stored: dict[str, Any],
    branch_priority: list[str],
    deadline: float,
) -> tuple[bool, bool]:
    """Return (drift, failed) without changing refs or inspecting worktree files."""
    from .sync import _git, _ssh_endpoint

    def git(*args: str, timeout: float, extra_env: dict[str, str] | None = None) -> Any:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        return _git(
            repo, *args,
            timeout=min(timeout, max(0.05, remaining)),
            extra_env=extra_env,
        )

    def git_text(*args: str, timeout: float = 10) -> str | None:
        result = git(*args, timeout=timeout)
        if result is None or result.returncode != 0 or not str(result.stdout or "").strip():
            return None
        return str(result.stdout).strip()

    git_directory = git("rev-parse", "--git-dir", timeout=10)
    if git_directory is None:
        return False, True
    if git_directory.returncode != 0:
        return False, False
    stored_sha = str(stored.get("sha") or "")
    stored_ref = str(stored.get("ref") or "")
    remotes = (git_text("remote") or "").splitlines()
    if "origin" not in remotes or stored_ref.startswith("refs/heads/"):
        ref = stored_ref or "HEAD"
        result = git("rev-parse", "--verify", ref, timeout=10)
        if result is None:
            return False, True
        sha = str(result.stdout or "").strip()
        return (bool(stored_sha and sha and sha != stored_sha), result.returncode != 0 or not sha)

    extra_env = None
    remote = git_text("remote", "get-url", "origin")
    if _ssh_endpoint(remote):
        command = git_text("config", "--get", "core.sshCommand") or os.environ.get("GIT_SSH_COMMAND") or "ssh"
        try:
            executable = Path(shlex.split(command, posix=os.name != "nt")[0].strip("\"'")).name.lower()
        except (ValueError, IndexError):
            return False, True
        if executable not in {"ssh", "ssh.exe"}:
            return False, True
        extra_env = {"GIT_SSH_COMMAND": f"{command} -o BatchMode=yes"}
    result = git(
        "ls-remote", "--symref", "origin", "HEAD", "refs/heads/*",
        timeout=30,
        extra_env=extra_env,
    )
    if result is None or result.returncode != 0:
        return False, True
    heads: dict[str, str] = {}
    default_branch: str | None = None
    for line in str(result.stdout or "").splitlines():
        if line.startswith("ref: ") and line.endswith("\tHEAD"):
            default_branch = _branch_name(line.split("\t", 1)[0].removeprefix("ref: "))
            continue
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[1].startswith("refs/heads/"):
            heads[parts[1].removeprefix("refs/heads/")] = parts[0]
    configured = _branch_name(repo.branch)
    stored_branch = _branch_name(stored_ref)
    selected = configured if configured in heads else next(
        (branch for branch in branch_priority if branch in heads),
        default_branch if default_branch in heads else None,
    )
    selected = selected or (stored_branch if stored_branch in heads else None)
    if not selected:
        return False, True
    return (bool(stored_sha and (heads[selected] != stored_sha or selected != stored_branch)), False)


def detect_auto_refresh(settings: Settings) -> FreshnessDecision:
    """Detect only drift that the authoritative refresh pipeline can recover."""
    from .core import discover_git_repositories, load_index_state, load_source_state
    from .editions import capabilities, current_edition
    from .ops import ensure_write_capacity, semantic_status

    try:
        ensure_write_capacity(settings)
        edition = current_edition(settings)
        available = capabilities(settings)
        if edition in {"semantic", "precision"} and not available.get("embedding"):
            return FreshnessDecision.action_required("Model capability requires attention.")
        if edition == "precision" and not available.get("reranker"):
            return FreshnessDecision.action_required("Model capability requires attention.")

        sources = load_source_state(settings)
        indexes = load_index_state(settings)
        configured_paths = {repo.path.resolve() for repo in settings.repositories}
        discovered = set(discover_git_repositories([settings.root])) - configured_paths
        if discovered:
            return FreshnessDecision.action_required(
                "New repositories are ready to add with an explicit refresh."
            )
        reasons: set[str] = set()

        for repo in settings.repositories:
            source = sources.get(repo.name) or {}
            if str(source.get("status") or "") == "non-git":
                if repo.name not in indexes:
                    reasons.add("Core indexes are stale.")
                continue
            source_sha = str(source.get("sha") or repo.source_sha or "")
            index_sha = str((indexes.get(repo.name) or {}).get("sha") or "")
            if not source_sha or index_sha != source_sha:
                reasons.add("Core indexes are stale.")

        semantic = semantic_status(settings)
        if edition in {"semantic", "precision"} and not semantic.get("aligned"):
            reasons.add("Semantic generation is stale or misaligned.")

        workers = min(8, max(1, len(settings.repositories)))
        deadline = time.monotonic() + MAX_FRESHNESS_PROBE_SECONDS
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="brain-freshness") as executor:
            probes = list(executor.map(
                lambda repo: _probe_repository(
                    repo, sources.get(repo.name) or {}, settings.branch_priority, deadline,
                ),
                settings.repositories,
            ))
        if any(failed for _, failed in probes):
            return FreshnessDecision.action_required("Git freshness check requires attention.", check_failed=True)
        if any(drift for drift, _ in probes):
            reasons.add("Selected source snapshots changed.")
        return FreshnessDecision.refresh(*reasons) if reasons else FreshnessDecision.ready()
    except OSError:
        return FreshnessDecision.action_required("Storage or freshness check requires attention.", check_failed=True)
    except Exception:
        return FreshnessDecision.action_required("Freshness check requires attention.", check_failed=True)


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


class AutoRefreshService:
    """Coalesce refreshable drift and run one refresh after retrieval becomes idle."""

    MODES = {"off", "when_idle"}

    def __init__(
        self,
        settings: Settings,
        *,
        detector: Callable[[Settings], FreshnessDecision] | None = None,
        refresher: Callable[[], Any] | None = None,
        is_idle: Callable[[], bool] | None = None,
        mode: str | None = None,
        persist: bool = True,
        interval_seconds: float | None = None,
        debounce_seconds: float = 5,
        cooldown_seconds: float = 30,
        backoff_seconds: float = 30,
        max_backoff_seconds: float = 900,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.settings = settings
        self._detector = detector or detect_auto_refresh
        self._refresher = refresher or self._refresh
        self._is_idle = is_idle or (lambda: True)
        self._clock = clock or time.time
        self._interval = max(1.0, float(interval_seconds or settings.watch_interval_seconds))
        self._debounce = max(0.0, float(debounce_seconds))
        self._cooldown = max(0.0, float(cooldown_seconds))
        self._backoff = max(1.0, float(backoff_seconds))
        self._max_backoff = max(self._backoff, float(max_backoff_seconds))
        self._persist_enabled = persist
        self._path = settings.state_dir / "auto-refresh.json"
        saved = self._load() if persist else {}
        selected_mode = mode or str(saved.get("mode") or "off")
        self._mode = selected_mode if selected_mode in self.MODES else "off"
        self._last_check = str(saved.get("last_check") or "") or None
        self._last_refresh = str(saved.get("last_refresh") or "") or None
        self._pending = False
        self._pending_reason: str | None = None
        self._pending_signature: tuple[str, ...] = ()
        self._refresh_due = 0.0
        self._next_check = 0.0
        self._failures = 0
        self._blocked_signature: tuple[str, ...] = ()
        self._status = "ready" if self._mode == "when_idle" else "off"
        self._lock = threading.RLock()
        self._poll_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _load(self) -> dict[str, Any]:
        try:
            loaded = json.loads(read_managed_text(
                self.settings.state_dir, self._path, max_bytes=256 * 1024,
            ))
            return loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def _save(self) -> None:
        if not self._persist_enabled:
            return
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        atomic_managed_text_write(self.settings.state_dir, self._path, json.dumps({
            "mode": self._mode,
            "last_check": self._last_check,
            "last_refresh": self._last_refresh,
        }, indent=2) + "\n")
        try:
            self._path.chmod(0o600)
        except OSError:
            pass

    def _refresh(self) -> Any:
        from .ops import refresh_brain

        return refresh_brain(self.settings, fetch=True, discover=True)

    @staticmethod
    def _refresh_failed(result: Any) -> bool:
        semantic = getattr(result, "semantic", None)
        sync = getattr(result, "sync", None)
        if isinstance(result, dict):
            semantic = result.get("semantic", semantic)
            sync = result.get("sync", sync)
        if isinstance(semantic, dict) and semantic.get("status") == "failed":
            return True
        return any(
            (getattr(item, "status", None) if not isinstance(item, dict) else item.get("status")) == "fetch-failed"
            for item in (sync or [])
        )

    def set_mode(self, mode: str) -> dict[str, Any]:
        if mode not in self.MODES:
            raise ValueError("Auto Refresh mode must be off or when_idle")
        with self._lock:
            self._mode = mode
            self._pending = False
            self._pending_reason = None
            self._pending_signature = ()
            self._blocked_signature = ()
            self._next_check = 0.0
            self._status = "ready" if mode == "when_idle" else "off"
            self._save()
        self._wake.set()
        return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "mode": self._mode,
                "last_check": self._last_check,
                "last_refresh": self._last_refresh,
                "pending": self._pending,
                "pending_reason": self._pending_reason,
                "status": self._status,
            }

    def _mark_failure(self, now: float, signature: tuple[str, ...]) -> None:
        self._failures += 1
        delay = min(self._max_backoff, self._backoff * (2 ** (self._failures - 1)))
        self._pending = False
        self._pending_reason = "Action Required: automatic refresh failed."
        self._pending_signature = ()
        self._blocked_signature = signature
        self._next_check = now + delay
        self._status = "action_required"
        self._save()

    def _attempt_pending(self, now: float) -> bool:
        with self._lock:
            if not self._pending or now < self._refresh_due:
                return False
            if not self._is_idle():
                self._status = "waiting_for_idle"
                return True
            signature = self._pending_signature
            self._status = "refreshing"
        try:
            result = self._refresher()
            if self._refresh_failed(result):
                raise RuntimeError("authoritative refresh reported a failed stage")
        except WorkspaceOperationBusy:
            with self._lock:
                self._status = "waiting_for_idle"
            return True
        except Exception:
            with self._lock:
                self._mark_failure(now, signature)
            return True
        with self._lock:
            self._pending = False
            self._pending_reason = None
            self._pending_signature = ()
            self._blocked_signature = ()
            self._last_refresh = _iso(now)
            self._next_check = now + self._cooldown
            self._failures = 0
            self._status = "ready"
            self._save()
        return True

    def poll(self, *, force_check: bool = False) -> dict[str, Any]:
        """Run one scheduler tick; safe for the UI thread and ``brain watch``."""
        with self._poll_lock:
            now = self._clock()
            with self._lock:
                if self._mode == "off":
                    return self.status()
            if self._attempt_pending(now):
                return self.status()
            with self._lock:
                if self._pending or (not force_check and now < self._next_check):
                    return self.status()
            decision = self._detector(self.settings)
            with self._lock:
                self._last_check = _iso(now)
                if decision.kind == "refresh":
                    if self._blocked_signature:
                        self._status = "action_required"
                        self._pending_reason = "Action Required: automatic refresh is paused."
                        self._next_check = now + self._interval
                    else:
                        self._pending = True
                        self._pending_signature = decision.reasons
                        self._pending_reason = "Repository freshness changes were coalesced."
                        self._refresh_due = now + self._debounce
                        self._status = "debouncing" if self._debounce else "pending"
                elif decision.kind == "action_required":
                    self._pending = False
                    self._pending_signature = ()
                    self._pending_reason = "Action Required: " + (decision.reasons[0] if decision.reasons else "freshness check paused.")
                    self._status = "action_required"
                    if decision.check_failed:
                        self._failures += 1
                        delay = min(self._max_backoff, self._backoff * (2 ** (self._failures - 1)))
                        self._next_check = now + delay
                    else:
                        self._next_check = now + self._interval
                else:
                    self._pending = False
                    self._pending_reason = None
                    self._pending_signature = ()
                    self._blocked_signature = ()
                    self._failures = 0
                    self._next_check = now + self._interval
                    self._status = "ready"
                self._save()
            self._attempt_pending(now)
            return self.status()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        def run() -> None:
            while not self._stop.is_set():
                self.poll()
                state = self.status()
                delay = 0.25 if state["pending"] else min(60.0, self._interval)
                self._wake.wait(delay)
                self._wake.clear()

        self._thread = threading.Thread(target=run, name="project-brain-auto-refresh", daemon=True)
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=1)

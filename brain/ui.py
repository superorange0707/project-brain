from __future__ import annotations

import json
import re
import secrets
import shutil
import threading
import time
import webbrowser
from dataclasses import asdict
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files as package_files
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .agent import archive_final_solution, create_m365_agent_kit, response_preview
from .core import (
    BrainError,
    Settings,
    create_context,
    create_feedback,
    deliver,
    load_index_state,
    load_source_state,
    request_repair_prompt,
    session_dir,
    session_state,
    start_session,
)
from .experience import load_experience_index
from .locks import WorkspaceOperationBusy, ticket_exclusive
from .ops import progress_event


MAX_REQUEST_BYTES = 4 * 1024 * 1024


def _display_path(settings: Settings, path: Path) -> str:
    try:
        return str(path.relative_to(settings.root)) or "."
    except ValueError:
        return path.name


def _session_artifacts(settings: Settings, ticket: str) -> list[dict[str, Any]]:
    directory = session_dir(settings, ticket)
    if not directory.is_dir():
        return []
    artifacts: list[dict[str, Any]] = []
    paths = sorted(
        (item for item in directory.iterdir() if not item.is_symlink()),
        key=lambda item: (item.stat().st_mtime_ns, item.name),
    )
    for path in paths:
        if (
            not path.is_file()
            or path.name in {"session.json", "current-handoff.md"}
            or path.suffix not in {".md", ".yml", ".json"}
        ):
            continue
        kind = path.name.split("-", 1)[0]
        artifacts.append({
            "name": path.name,
            "kind": kind,
            "bytes": path.stat().st_size,
            "updated_at": datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
        })
    return artifacts


def _sessions(settings: Settings) -> list[dict[str, Any]]:
    if not settings.runs_dir.is_dir():
        return []
    sessions: list[dict[str, Any]] = []
    for directory in settings.runs_dir.iterdir():
        state_path = directory / "session.json"
        if not directory.is_dir() or directory.is_symlink() or not state_path.is_file():
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        sessions.append({
            "ticket": str(state.get("ticket") or directory.name),
            "requests": int(state.get("requests") or 0),
            "feedbacks": int(state.get("feedbacks") or 0),
            "status": str(state.get("status") or "investigating"),
            "no_progress_rounds": int(state.get("no_progress_rounds") or 0),
            "atlas_generation": state.get("generation"),
            "context_id": state.get("last_context_id"),
            "prefetch_status": (state.get("prefetch") or {}).get("status"),
            "started_at": state.get("started_at"),
            "updated_at": datetime.fromtimestamp(state_path.stat().st_mtime, UTC).isoformat(),
        })
    return sorted(sessions, key=lambda item: item["updated_at"], reverse=True)


def project_status(
    settings: Settings,
    jobs: list[dict[str, Any]] | None = None,
    auto_refresh: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from .catalog import current_generation
    from .metrics import benchmark_report
    from .ops import dashboard_status, storage

    sources = load_source_state(settings)
    indexes = load_index_state(settings)
    graph_path = settings.state_dir / "graphs.json"
    try:
        graphs = json.loads(graph_path.read_text(encoding="utf-8")) if graph_path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        graphs = {}
    experience = load_experience_index(settings)
    try:
        evaluation = json.loads((settings.state_dir / "experience-eval.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        evaluation = {}
    repositories: list[dict[str, Any]] = []
    current = 0
    warnings = 0
    for repo in settings.repositories:
        source = sources.get(repo.name) or {}
        indexed = indexes.get(repo.name) or {}
        status = str(source.get("status") or repo.source_status)
        sha = str(source.get("sha") or repo.source_sha or "")
        indexed_sha = str(indexed.get("sha") or "")
        fresh_index = bool(sha and indexed_sha == sha)
        graph_sha = str((graphs.get(repo.name) or {}).get("sha") or "") if isinstance(graphs, dict) else ""
        structural = settings.graph_enabled and bool(graph_sha and graph_sha == (repo.source_sha or "working-tree"))
        if status in {"current", "non-git"} and (fresh_index or status == "non-git"):
            current += 1
        if source.get("warning") or status in {"fetch-failed", "working-tree-fallback"} or not fresh_index:
            warnings += 1
        repositories.append({
            "name": repo.name,
            "path": _display_path(settings, repo.path),
            "status": status,
            "ref": source.get("ref") or repo.source_ref,
            "sha": sha[:12] or None,
            "fetched": bool(source.get("fetched")),
            "synced_at": source.get("synced_at"),
            "indexed": fresh_index,
            "structural": structural,
            "warning": source.get("warning"),
        })
    brain = dashboard_status(settings)
    sessions = _sessions(settings)
    active_jobs = [item for item in (jobs or []) if item.get("status") in {"pending", "running"}]
    by_ticket = {str(item.get("ticket")): item for item in active_jobs if item.get("ticket")}
    for session in sessions:
        job = by_ticket.get(str(session["ticket"]))
        if job:
            session["status"] = "retrieving"
            session["job"] = job
    return {
        "project": {"name": settings.name, "config": settings.config_path.name},
        "summary": {
            "repositories": len(repositories),
            "current": current,
            "warnings": warnings,
            "experience_cases": len(experience.get("cases") or []),
            "evaluated_sessions": int(evaluation.get("evaluated_sessions") or 0),
        },
        "repositories": repositories,
        "sessions": sessions,
        "jobs": active_jobs,
        "retrieval": {
            "edition": brain["edition"],
            "generation": (current_generation(settings) or {}).get("generation"),
            "capabilities": brain["capabilities"],
            "benchmark": benchmark_report(settings),
            "storage": storage(settings),
            "atlas_components": brain["freshness"].get("components") or {},
        },
        "brain": brain,
        "auto_refresh": auto_refresh or {
            "mode": "off", "last_check": None, "last_refresh": None,
            "pending": False, "pending_reason": None, "status": "off",
        },
    }


def _refresh(
    settings: Settings,
    *,
    fetch: bool = True,
    branch_values: list[str] | None = None,
) -> dict[str, Any]:
    from .ops import refresh_brain

    return refresh_brain(settings, fetch=fetch, branch_values=branch_values).as_dict()


def _delivery(settings: Settings, ticket: str, part: int | None = None) -> dict[str, Any]:
    state = session_state(settings, ticket)
    delivery = state.get("delivery") or {}
    paths = [Path(value) for value in delivery.get("parts") or []]
    if not paths:
        return {"current": 0, "total": 0, "content": "", "path": None}
    current = part or int(delivery.get("current") or 1)
    current = max(1, min(len(paths), current))
    directory = session_dir(settings, ticket).resolve()
    path = paths[current - 1].resolve()
    handoffs = (settings.generated_dir / "handoffs").resolve()
    if not (path.is_relative_to(directory) or path.is_relative_to(handoffs)) or not path.is_file():
        raise BrainError("Invalid delivery path in session state")
    return {"current": current, "total": len(paths), "content": path.read_text(encoding="utf-8"), "path": str(path)}


def _session_detail(settings: Settings, ticket: str) -> dict[str, Any]:
    directory = session_dir(settings, ticket)
    if not directory.is_dir():
        raise BrainError(f"Session {ticket} does not exist")
    state = session_state(settings, ticket)
    ticket_path = directory / "ticket.md"
    history = state.get("request_history") or []
    latest = history[-1] if history else {}
    return {
        "ticket": ticket,
        "ticket_text": ticket_path.read_text(encoding="utf-8") if ticket_path.is_file() else "",
        "requests": int(state.get("requests") or 0),
        "feedbacks": int(state.get("feedbacks") or 0),
        "status": str(state.get("status") or "investigating"),
        "no_progress_rounds": int(state.get("no_progress_rounds") or 0),
        "request_history": history,
        "retrieval": latest.get("retrieval") if isinstance(latest, dict) else {},
        "atlas_generation": state.get("generation"),
        "atlas_generation_id": state.get("atlas_generation_id"),
        "context_id": state.get("last_context_id"),
        "context_lineage": state.get("context_lineage") or [],
        "investigation_memory": state.get("investigation_memory") or {},
        "coverage_map": state.get("coverage_map") or {},
        "prefetch": state.get("prefetch") or {},
        "artifacts": _session_artifacts(settings, ticket),
        "delivery": _delivery(settings, ticket),
    }


def _artifact(settings: Settings, ticket: str, name: str) -> dict[str, Any]:
    if Path(name).name != name or not name or name == "session.json":
        raise BrainError("Invalid artifact name")
    allowed = {item["name"] for item in _session_artifacts(settings, ticket)}
    if name not in allowed:
        raise BrainError("Artifact does not exist")
    path = session_dir(settings, ticket) / name
    return {"name": name, "content": path.read_text(encoding="utf-8")}


@ticket_exclusive
def _delete_session(settings: Settings, ticket: str) -> list[str]:
    directory = session_dir(settings, ticket)
    if not directory.is_dir() or directory.is_symlink():
        raise BrainError(f"Session {ticket} does not exist")
    safe_name = directory.name
    shutil.rmtree(directory)
    removed = [str(directory)]
    handoff_directory = settings.generated_dir / "handoffs"
    pattern = re.compile(
        rf"^{re.escape(safe_name)}-(?:current|start|final|update|context-\d+|evidence-\d+|feedback-\d+)\.md$"
    )
    if handoff_directory.is_dir():
        for path in handoff_directory.iterdir():
            if path.is_file() and not path.is_symlink() and pattern.fullmatch(path.name):
                path.unlink()
                removed.append(str(path))
    return removed


class _OperationCoordinator:
    """Coordinate one workspace mutation or bounded independent ticket jobs."""

    _RETAINED = 20

    def __init__(self, max_retrievals: int = 2) -> None:
        self._lock = threading.Lock()
        self._active_mutation: str | None = None
        self._active_retrievals: dict[str, str] = {}
        self._max_retrievals = max(1, max_retrievals)
        self._jobs: dict[str, dict[str, Any]] = {}

    def _claim(self, name: str, *, kind: str = "mutation", ticket: str | None = None) -> str:
        with self._lock:
            if kind == "mutation" and (self._active_mutation is not None or self._active_retrievals):
                active_id = self._active_mutation or next(iter(self._active_retrievals.values()))
                active = self._jobs.get(active_id) or {}
                raise BrainError(f"Another state-changing operation is already running: {active.get('name') or 'operation'}")
            if kind == "retrieval":
                if self._active_mutation is not None:
                    active = self._jobs.get(self._active_mutation) or {}
                    raise BrainError(f"A workspace mutation is already running: {active.get('name') or 'operation'}")
                if not ticket:
                    raise BrainError("Ticket identifier is required for retrieval")
                if ticket in self._active_retrievals:
                    raise BrainError("Another request for this ticket is already running")
                if len(self._active_retrievals) >= self._max_retrievals:
                    raise BrainError(f"The {self._max_retrievals} concurrent investigation limit is active")
            job_id = secrets.token_urlsafe(12)
            if kind == "mutation":
                self._active_mutation = job_id
            else:
                self._active_retrievals[str(ticket)] = job_id
            self._jobs[job_id] = {
                "id": job_id,
                "name": name,
                "kind": kind,
                "ticket": ticket,
                "status": "pending",
                "phase": "Queued",
                "progress": progress_event("queued", elapsed_ms=0),
                "started_at_ms": round(time.time() * 1000),
                "result": None,
                "error": None,
                "_started": time.perf_counter(),
            }
            return job_id

    def _finish(self, job_id: str, *, result: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        with self._lock:
            job = self._jobs[job_id]
            if error is None:
                job.update({"status": "succeeded", "phase": "Completed", "result": result or {}, "error": None})
            else:
                # Errors from refresh/model runtimes can contain a local path or
                # transport context.  The UI receives only a safe class label.
                message = str(error).strip()
                safe_validation = (
                    job.get("name") != "auto-refresh"
                    and isinstance(error, (BrainError, ValueError))
                    and "://" not in message
                    and len(message) <= 280
                )
                job.update({
                    "status": "failed",
                    "phase": "Failed",
                    "progress": progress_event("failed", elapsed_ms=(time.perf_counter() - float(job["_started"])) * 1000),
                    "result": None,
                    "error": message if safe_validation else f"Operation failed ({type(error).__name__}).",
                })
            if job.get("kind") == "mutation" and self._active_mutation == job_id:
                self._active_mutation = None
            ticket = str(job.get("ticket") or "")
            if ticket and self._active_retrievals.get(ticket) == job_id:
                self._active_retrievals.pop(ticket, None)
            completed = [key for key, item in self._jobs.items() if item["status"] in {"succeeded", "failed"}]
            for key in completed[:-self._RETAINED]:
                self._jobs.pop(key, None)

    def _progress(self, job_id: str, event: dict[str, Any] | str) -> None:
        with self._lock:
            if job_id in self._jobs:
                if isinstance(event, str):
                    safe = progress_event("discovery", elapsed_ms=0, phase_label=event)
                else:
                    details = {key: value for key, value in event.items() if key not in {"phase", "phase_label", "elapsed_ms"}}
                    safe = progress_event(
                        str(event.get("phase") or "failed"),
                        elapsed_ms=event.get("elapsed_ms") or 0,
                        phase_label=str(event.get("phase_label") or "") or None,
                        **details,
                    )
                self._jobs[job_id].update({"status": "running", "phase": safe["phase_label"], "progress": safe})

    def start(self, name: str, operation: Any, *, kind: str = "mutation", ticket: str | None = None) -> dict[str, Any]:
        job_id = self._claim(name, kind=kind, ticket=ticket)

        def run() -> None:
            try:
                self._progress(job_id, "Starting")
                result = operation(lambda event: self._progress(job_id, event))
            except Exception as error:
                self._finish(job_id, error=error)
            else:
                self._finish(job_id, result=result)

        threading.Thread(target=run, name=f"project-brain-{name}", daemon=True).start()
        return self.get(job_id)

    def foreground(self, name: str, operation: Any, *, kind: str = "mutation", ticket: str | None = None) -> Any:
        job_id = self._claim(name, kind=kind, ticket=ticket)
        try:
            self._progress(job_id, "Running")
            result = operation()
        except Exception as error:
            self._finish(job_id, error=error)
            raise
        self._finish(job_id, result={})
        return result

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise BrainError("Operation does not exist")
            return {key: value for key, value in job.items() if not key.startswith("_")}

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {key: value for key, value in job.items() if not key.startswith("_")}
                for job in sorted(self._jobs.values(), key=lambda item: int(item["started_at_ms"]), reverse=True)
            ]

    def is_idle(self) -> bool:
        with self._lock:
            return self._active_mutation is None and not self._active_retrievals

    def active_retrieval_count(self) -> int:
        with self._lock:
            return len(self._active_retrievals)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], settings: Settings, token: str):
        from .auto_refresh import AutoRefreshService

        self.settings = settings
        self.token = token
        self.operations = _OperationCoordinator(settings.max_concurrent_investigations)
        self.auto_refresh = AutoRefreshService(
            settings,
            refresher=self._auto_refresh,
            is_idle=self.operations.is_idle,
        )
        super().__init__(address, _Handler)
        self.auto_refresh.start()

    def _auto_refresh(self) -> Any:
        from .ops import refresh_brain

        try:
            return self.operations.foreground("auto-refresh", lambda: refresh_brain(self.settings, fetch=True, discover=True))
        except BrainError as error:
            if not self.operations.is_idle():
                raise WorkspaceOperationBusy("workspace is not idle") from error
            raise

    def server_close(self) -> None:
        self.auto_refresh.stop()
        super().server_close()


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _headers(self, status: HTTPStatus, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        self.end_headers()

    def _json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(data))
        self.wfile.write(data)

    def _error(self, exc: Exception, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        self._json({"ok": False, "error": str(exc)}, status)

    def _authorized(self) -> bool:
        host = self.headers.get("Host", "")
        if not (host.startswith("127.0.0.1:") or host.startswith("localhost:")):
            return False
        return secrets.compare_digest(self.headers.get("X-Brain-Token", ""), self.server.token)

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise BrainError("Invalid Content-Length") from exc
        if length < 1 or length > MAX_REQUEST_BYTES:
            raise BrainError(f"Request body must be between 1 and {MAX_REQUEST_BYTES} bytes")
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise BrainError("Content-Type must be application/json")
        try:
            loaded = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise BrainError(f"Invalid JSON body: {exc.msg}") from exc
        if not isinstance(loaded, dict):
            raise BrainError("JSON body must be an object")
        return loaded

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            html = package_files("brain").joinpath("ui.html").read_bytes()
            self._headers(HTTPStatus.OK, "text/html; charset=utf-8", len(html))
            self.wfile.write(html)
            return
        if not self._authorized():
            self._error(BrainError("Unauthorized local request"), HTTPStatus.FORBIDDEN)
            return
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/status":
                self._json({"ok": True, "data": project_status(
                    self.server.settings,
                    self.server.operations.list(),
                    self.server.auto_refresh.status(),
                )})
            elif parsed.path == "/api/models":
                from .ops import model_status

                self._json({"ok": True, "data": model_status(self.server.settings)})
            elif parsed.path == "/api/job":
                self._json({"ok": True, "data": self.server.operations.get(_one(query, "id"))})
            elif parsed.path == "/api/jobs":
                self._json({"ok": True, "data": self.server.operations.list()})
            elif parsed.path == "/api/diagnostics":
                from .models import model_download_trust_status
                from .ops import dashboard_status

                trust, trust_ok = model_download_trust_status(self.server.settings)
                self._json({"ok": True, "data": {
                    "ok": trust_ok,
                    "summary": "Local diagnostics are healthy." if trust_ok else "Local diagnostics need attention.",
                    "model_download_trust": trust,
                    "brain": dashboard_status(self.server.settings),
                }})
            elif parsed.path == "/api/session":
                self._json({"ok": True, "data": _session_detail(self.server.settings, _one(query, "ticket"))})
            elif parsed.path == "/api/artifact":
                self._json({"ok": True, "data": _artifact(self.server.settings, _one(query, "ticket"), _one(query, "name"))})
            elif parsed.path == "/api/delivery":
                part = int(_one(query, "part"))
                self._json({"ok": True, "data": _delivery(self.server.settings, _one(query, "ticket"), part)})
            else:
                self._error(BrainError("Not found"), HTTPStatus.NOT_FOUND)
        except (BrainError, OSError, ValueError) as exc:
            self._error(exc)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not self._authorized():
            self._error(BrainError("Unauthorized local request"), HTTPStatus.FORBIDDEN)
            return
        try:
            body = self._body()
            if parsed.path == "/api/preview":
                text = str(body.get("text") or "")
                try:
                    ticket = str(body.get("ticket") or "").strip() or None
                    data = response_preview(text, self.server.settings, ticket)
                except BrainError as exc:
                    self._json({"ok": True, "data": {"valid": False, "error": str(exc), "repair_prompt": request_repair_prompt(str(exc))}})
                else:
                    self._json({"ok": True, "data": data})
                return
            if parsed.path == "/api/auto-refresh":
                self._json({
                    "ok": True,
                    "data": self.server.auto_refresh.set_mode(str(body.get("mode") or "")),
                })
                return
            if parsed.path in {"/api/refresh", "/api/model", "/api/edition"}:
                self._start_operation(parsed.path, body)
            elif parsed.path == "/api/retrieval":
                self._start_retrieval(body)
            elif parsed.path == "/api/agent-kit":
                self._action(parsed.path, body)
            else:
                ticket = str(body.get("ticket") or "").strip() or None
                ticket_paths = {"/api/start", "/api/context", "/api/continue", "/api/feedback", "/api/session/delete"}
                kind = "retrieval" if parsed.path in ticket_paths else "mutation"
                self.server.operations.foreground(
                    parsed.path,
                    lambda: self._action(parsed.path, body),
                    kind=kind,
                    ticket=ticket if kind == "retrieval" else None,
                )
        except (BrainError, OSError, RuntimeError, ValueError) as exc:
            self._error(exc)

    def _start_operation(self, path: str, body: dict[str, Any]) -> None:
        settings = self.server.settings
        if path == "/api/refresh":
            from .ops import refresh_brain

            fetch = bool(body.get("fetch", True))
            discover = bool(body.get("discover", True))
            job = self.server.operations.start(
                "refresh",
                lambda progress: {**refresh_brain(settings, fetch=fetch, discover=discover, progress=progress).as_dict(), "status": project_status(settings)},
            )
        elif path == "/api/edition":
            from .ops import change_edition

            edition = str(body.get("edition") or "")
            refresh = bool(body.get("refresh"))
            job = self.server.operations.start(
                "edition",
                lambda progress: {**change_edition(settings, edition, refresh=refresh, progress=progress), "status": project_status(settings)},
            )
        else:
            from .ops import model_operation

            action = str(body.get("action") or "")
            value = str(body.get("pack") or "") or None
            if action not in {"install", "verify", "remove", "benchmark", "autotune"}:
                raise BrainError("UI model action is not supported")
            job = self.server.operations.start(
                f"model-{action}",
                lambda progress: self._model_job(settings, action, value, progress),
            )
        self._json({"ok": True, "data": job}, HTTPStatus.ACCEPTED)

    def _start_retrieval(self, body: dict[str, Any]) -> None:
        ticket = str(body.get("ticket") or "").strip()
        if not ticket:
            raise BrainError("Ticket identifier is required")
        job = self.server.operations.start(
            "retrieval",
            lambda progress: self._retrieval_job(self.server.settings, body, progress),
            kind="retrieval",
            ticket=ticket,
        )
        self._json({"ok": True, "data": job}, HTTPStatus.ACCEPTED)

    @staticmethod
    def _retrieval_job(settings: Settings, body: dict[str, Any], progress: Any) -> dict[str, Any]:
        ticket = str(body.get("ticket") or "").strip()
        text = str(body.get("text") or "")
        preview = response_preview(text, settings, ticket)
        if preview["kind"] == "conversation":
            return {"ticket": ticket, **preview, "session": _session_detail(settings, ticket)}
        if preview["kind"] == "final_solution":
            artifact = archive_final_solution(settings, ticket, text)
            if _target(body) == "m365":
                deliver(settings, ticket, text, "m365", copy=False)
            return {"ticket": ticket, **preview, "path": artifact.name, "session": _session_detail(settings, ticket)}
        if preview.get("duplicate_of"):
            raise BrainError(f"This retrieval plan already ran as request {preview['duplicate_of']:03d}.")
        content, artifact, number = create_context(
            settings,
            ticket,
            text,
            bool(body.get("include_diff")),
            progress=progress,
        )
        deliver(settings, ticket, content, _target(body), copy=False)
        return {
            "ticket": ticket,
            **preview,
            "request": number,
            "path": artifact.name,
            "delivery": _delivery(settings, ticket),
            "session": _session_detail(settings, ticket),
        }

    @staticmethod
    def _model_job(settings: Settings, action: str, value: str | None, progress: Any) -> dict[str, Any]:
        from .ops import model_operation

        progress("Validating local model pack")
        result = model_operation(settings, action, value, official_only=True)
        progress("Publishing model operation result")
        return {
            "action": action,
            "pack_id": str(result.get("pack_id") or value or "") if isinstance(result, dict) else str(value or ""),
            "report": result if action in {"benchmark", "autotune"} else None,
            "status": project_status(settings),
        }

    def _action(self, path: str, body: dict[str, Any]) -> None:
        settings = self.server.settings
        if path == "/api/sync":
            from .ops import refresh_brain

            result = refresh_brain(settings, fetch=True).as_dict()
            self._json({"ok": True, "data": {**result, "status": project_status(settings)}})
            return
        if path == "/api/session/delete":
            ticket = str(body.get("ticket") or "").strip()
            if not ticket:
                raise BrainError("Ticket identifier is required")
            removed = _delete_session(settings, ticket)
            self._json({"ok": True, "data": {"ticket": ticket, "removed": removed, "status": project_status(settings)}})
            return
        if path == "/api/start":
            ticket = str(body.get("ticket") or "").strip()
            ticket_text = str(body.get("ticket_text") or "").strip()
            if not ticket or not ticket_text:
                raise BrainError("Ticket identifier and description are required")
            branch_lines = [line.strip() for line in str(body.get("branches") or "").splitlines() if line.strip()]
            if branch_lines and not body.get("sync", True):
                raise BrainError("Feature branch overrides require repository sync")
            if body.get("sync", True):
                from .editions import current_edition
                from .ops import dashboard_status, refresh_brain

                refresh = refresh_brain(settings, fetch=True, branch_values=branch_lines).as_dict()
                requested_edition = current_edition(settings)
                expected = {
                    "semantic": "Semantic active",
                    "precision": "Precision active",
                }.get(requested_edition)
                operation = dashboard_status(settings)
                degraded = bool(expected and operation["effective"] != expected)
                if degraded and not body.get("allow_degraded"):
                    reason = operation["reason"] or "the requested edition did not become active"
                    raise BrainError(
                        f"{requested_edition.capitalize()} is not active ({reason}). Investigation was not started; "
                        "refresh Brain again or explicitly continue degraded."
                    )
            else:
                refresh = {"discovered": [], "sync": [], "graph": [], "semantic": None}
                degraded = False
            content, artifact = start_session(settings, ticket, ticket_text)
            target = _target(body)
            deliver(settings, ticket, content, target, copy=False)
            self._json({
                "ok": True,
                "data": {
                    "ticket": ticket,
                    "path": artifact.name,
                    "delivery": _delivery(settings, ticket),
                    "degraded": degraded,
                    **refresh,
                    "status": project_status(settings),
                },
            })
            return
        if path == "/api/context":
            ticket = str(body.get("ticket") or "").strip()
            text = str(body.get("text") or "")
            plan = request_preview(text, settings)
            content, artifact, number = create_context(settings, ticket, text, bool(body.get("include_diff")))
            deliver(settings, ticket, content, _target(body), copy=False)
            self._json({
                "ok": True,
                "data": {"ticket": ticket, "request": number, "path": artifact.name, "plan": plan, "delivery": _delivery(settings, ticket), "session": _session_detail(settings, ticket)},
            })
            return
        if path == "/api/continue":
            ticket = str(body.get("ticket") or "").strip()
            text = str(body.get("text") or "")
            preview = response_preview(text, settings, ticket)
            if preview["kind"] == "conversation":
                self._json({"ok": True, "data": {"ticket": ticket, **preview, "session": _session_detail(settings, ticket)}})
                return
            if preview["kind"] == "final_solution":
                artifact = archive_final_solution(settings, ticket, text)
                if _target(body) == "m365":
                    deliver(settings, ticket, text, "m365", copy=False)
                self._json({
                    "ok": True,
                    "data": {"ticket": ticket, **preview, "path": artifact.name, "session": _session_detail(settings, ticket)},
                })
                return
            if preview.get("duplicate_of"):
                raise BrainError(
                    f"This retrieval plan already ran as request {preview['duplicate_of']:03d}. "
                    "Clear any old reply and paste only the AI's latest complete response. If the latest reply "
                    "is a human question, answer it directly in the AI chat; Brain should not create a new request."
                )
            content, artifact, number = create_context(settings, ticket, text, bool(body.get("include_diff")))
            deliver(settings, ticket, content, _target(body), copy=False)
            self._json({
                "ok": True,
                "data": {
                    "ticket": ticket,
                    **preview,
                    "request": number,
                    "path": artifact.name,
                    "delivery": _delivery(settings, ticket),
                    "session": _session_detail(settings, ticket),
                },
            })
            return
        if path == "/api/agent-kit":
            self._json({"ok": True, "data": create_m365_agent_kit(settings)})
            return
        if path == "/api/feedback":
            ticket = str(body.get("ticket") or "").strip()
            repos = body.get("repos") or []
            if not isinstance(repos, list):
                raise BrainError("repos must be a list")
            content, artifact, number = create_feedback(
                settings,
                ticket,
                notes=str(body.get("notes") or ""),
                test_command=str(body.get("test_command") or ""),
                test_output=str(body.get("test_output") or ""),
                repos=[str(repo) for repo in repos],
                include_diff=bool(body.get("include_diff", True)),
            )
            deliver(settings, ticket, content, _target(body), copy=False)
            self._json({
                "ok": True,
                "data": {"ticket": ticket, "feedback": number, "path": artifact.name, "delivery": _delivery(settings, ticket), "session": _session_detail(settings, ticket)},
            })
            return
        raise BrainError("Not found")


def _one(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key) or []
    if len(values) != 1 or not values[0]:
        raise BrainError(f"Query parameter {key} is required")
    return values[0]


def _target(body: dict[str, Any]) -> str:
    target = str(body.get("target") or "claude")
    if target not in {"claude", "m365"}:
        raise BrainError("target must be claude or m365")
    return target


def serve_ui(settings: Settings, *, port: int = 8765, open_browser: bool = True) -> None:
    token = secrets.token_urlsafe(32)
    try:
        server = _Server(("127.0.0.1", port), settings, token)
    except OSError as exc:
        raise BrainError(f"Could not start Project Brain UI on loopback port {port}: {exc}") from exc
    actual_port = int(server.server_address[1])
    url = f"http://127.0.0.1:{actual_port}/?token={token}"
    print(f"Project Brain UI: {url}")
    print("Local-only session. Press Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            server.server_close()
        except KeyboardInterrupt:
            pass
        print("\nProject Brain UI stopped.")

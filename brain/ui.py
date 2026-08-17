from __future__ import annotations

import json
import secrets
import threading
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
    discover_and_configure_repositories,
    generate_map,
    load_index_state,
    load_source_state,
    request_repair_prompt,
    session_dir,
    session_state,
    snapshot_indexes,
    start_session,
)
from .graph import index_graph
from .relations import generate_relationship_map
from .sync import parse_branch_overrides, sync_repositories


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
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.name == "session.json" or path.suffix not in {".md", ".yml", ".json"}:
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
            "started_at": state.get("started_at"),
            "updated_at": datetime.fromtimestamp(state_path.stat().st_mtime, UTC).isoformat(),
        })
    return sorted(sessions, key=lambda item: item["updated_at"], reverse=True)


def project_status(settings: Settings) -> dict[str, Any]:
    sources = load_source_state(settings)
    indexes = load_index_state(settings)
    graph_path = settings.state_dir / "graphs.json"
    try:
        graphs = json.loads(graph_path.read_text(encoding="utf-8")) if graph_path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        graphs = {}
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
    return {
        "project": {"name": settings.name, "config": settings.config_path.name},
        "summary": {"repositories": len(repositories), "current": current, "warnings": warnings},
        "repositories": repositories,
        "sessions": _sessions(settings),
    }


def _refresh(
    settings: Settings,
    *,
    fetch: bool = True,
    branch_values: list[str] | None = None,
) -> dict[str, Any]:
    additions = discover_and_configure_repositories(settings)
    overrides = parse_branch_overrides(settings, branch_values or [])
    synced = sync_repositories(settings, fetch=fetch, branch_overrides=overrides)
    snapshot_indexes(settings, changed_only=True)
    generate_map(settings)
    generate_relationship_map(settings)
    graphs = index_graph(settings, defer_lazy=True)
    return {
        "discovered": [repo.name for repo in additions],
        "sync": [asdict(item) for item in synced],
        "graph": [asdict(item) for item in graphs],
    }


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
    return {
        "ticket": ticket,
        "ticket_text": ticket_path.read_text(encoding="utf-8") if ticket_path.is_file() else "",
        "requests": int(state.get("requests") or 0),
        "feedbacks": int(state.get("feedbacks") or 0),
        "status": str(state.get("status") or "investigating"),
        "no_progress_rounds": int(state.get("no_progress_rounds") or 0),
        "request_history": state.get("request_history") or [],
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


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], settings: Settings, token: str):
        self.settings = settings
        self.token = token
        self.action_lock = threading.Lock()
        super().__init__(address, _Handler)


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
                self._json({"ok": True, "data": project_status(self.server.settings)})
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
            with self.server.action_lock:
                self._action(parsed.path, body)
        except (BrainError, OSError, ValueError) as exc:
            self._error(exc)

    def _action(self, path: str, body: dict[str, Any]) -> None:
        settings = self.server.settings
        if path == "/api/sync":
            result = _refresh(settings, fetch=True)
            self._json({"ok": True, "data": {**result, "status": project_status(settings)}})
            return
        if path == "/api/start":
            ticket = str(body.get("ticket") or "").strip()
            ticket_text = str(body.get("ticket_text") or "").strip()
            if not ticket or not ticket_text:
                raise BrainError("Ticket identifier and description are required")
            branch_lines = [line.strip() for line in str(body.get("branches") or "").splitlines() if line.strip()]
            if branch_lines and not body.get("sync", True):
                raise BrainError("Feature branch overrides require repository sync")
            refresh = (
                _refresh(settings, fetch=True, branch_values=branch_lines)
                if body.get("sync", True)
                else {"discovered": [], "sync": [], "graph": []}
            )
            content, artifact = start_session(settings, ticket, ticket_text)
            target = _target(body)
            deliver(settings, ticket, content, target, copy=False)
            self._json({
                "ok": True,
                "data": {"ticket": ticket, "path": artifact.name, "delivery": _delivery(settings, ticket), **refresh, "status": project_status(settings)},
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

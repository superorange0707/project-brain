"""Model Context Protocol (stdio) server: a chat/IDE agent drives Brain directly.

The tools call the same core functions as the local UI, in this process, under
the same cross-process ticket and workspace locks, so the server works with or
without a running UI. stdout carries only newline-delimited JSON-RPC messages;
anything the core prints is redirected to stderr. The tool set is the read-only
investigation surface: no refresh-by-default, deletion, model, edition or
storage operations, and no arbitrary local file reads (human-supplied evidence
stays a human action through ``brain evidence``).
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import sys
from dataclasses import replace
from importlib.resources import files as package_files
from pathlib import Path
from typing import Any, BinaryIO, Callable

from . import __version__
from .core import BrainError, Settings, load_settings

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_MESSAGE_BYTES = 4 * 1024 * 1024
# ~20k tokens: below Claude Code's 25k-token default without relying on _meta.
PAGE_CHARS = 80_000
HOSTS = ("claude-desktop", "claude-code", "vscode", "cursor", "jetbrains")

_TICKET = {"type": "string", "description": "Ticket identifier, for example PAY-1234."}
TOOLS: list[dict[str, Any]] = [
    {"name": "brain_status", "description": "Project Brain health: repositories, published index generation, "
     "readiness and existing investigations.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "brain_start", "description": "Start (or restart) a ticket investigation and return the start package: "
     "pinned generation, project map, similar tickets and request contract. Set refresh=true to fetch and "
     "re-index repositories first (slow).", "inputSchema": {"type": "object", "properties": {
         "ticket": _TICKET, "ticket_text": {"type": "string", "description": "Ticket title, description and acceptance criteria."},
         "refresh": {"type": "boolean", "default": False}}, "required": ["ticket", "ticket_text"]}},
    {"name": "brain_continue", "description": "Submit your INVESTIGATION_REQUEST (a JSON object, or text containing "
     "one fenced json block) or your FINAL_SOLUTION for this ticket. Returns the next evidence context, page 1. "
     "Requests are bounded; duplicate plans are rejected.", "inputSchema": {"type": "object", "properties": {
         "ticket": _TICKET, "reply": {"description": "INVESTIGATION_REQUEST object, or reply text.",
                                      "anyOf": [{"type": "object"}, {"type": "string"}]}},
         "required": ["ticket", "reply"]}},
    {"name": "brain_handoff", "description": "Read one page of the ticket's latest delivered handoff (start, "
     "context, resume or feedback).", "inputSchema": {"type": "object", "properties": {
         "ticket": _TICKET, "part": {"type": "integer", "minimum": 1}}, "required": ["ticket", "part"]}},
    {"name": "brain_session", "description": "Compact ticket state: status, request count, context lineage, "
     "coverage map and investigation memory.", "inputSchema": {"type": "object", "properties": {"ticket": _TICKET},
                                                                "required": ["ticket"]}},
    {"name": "brain_resume", "description": "Bounded handover of this ticket for a fresh conversation: decisions, "
     "open questions and retained evidence IDs, on the same pinned generation.", "inputSchema": {
         "type": "object", "properties": {"ticket": _TICKET, "notes": {"type": "string", "maxLength": 8000}},
         "required": ["ticket"]}},
    {"name": "brain_feedback", "description": "Package the developer's applied diff in the configured repositories "
     "plus test command/output for review against the acceptance criteria.", "inputSchema": {
         "type": "object", "properties": {"ticket": _TICKET, "notes": {"type": "string"},
                                          "test_command": {"type": "string"}, "test_output": {"type": "string"},
                                          "repos": {"type": "array", "items": {"type": "string"}}},
         "required": ["ticket"]}},
]
for _tool in TOOLS:
    if _tool["name"] in {"brain_start", "brain_continue", "brain_handoff", "brain_resume", "brain_feedback"}:
        _tool["_meta"] = {"anthropic/maxResultSizeChars": PAGE_CHARS + 2_000}


class ToolError(BrainError):
    """A tool failure the model can correct (bad request, unknown ticket, ...)."""


def _page(settings: Settings, ticket: str, part: int = 1, heading: str = "") -> str:
    from .ui import _delivery

    delivery = _delivery(settings, ticket, part)
    if not delivery["total"]:
        raise ToolError(f"Ticket {ticket} has no delivered handoff yet; call brain_start first")
    more = (f"\n\n[Page {delivery['current']}/{delivery['total']}. Call brain_handoff with part="
            f"{delivery['current'] + 1} for the rest of this handoff before answering.]"
            if delivery["current"] < delivery["total"] else "")
    return f"{heading}{delivery['content']}{more}"


def _ticket(arguments: dict[str, Any]) -> str:
    ticket = str(arguments.get("ticket") or "").strip()
    if not ticket or len(ticket) > 200:
        raise ToolError("ticket is required")
    return ticket


def _status(settings: Settings, _: dict[str, Any]) -> str:
    from .ui import project_status

    status = project_status(settings)
    brain = status.get("brain") or {}
    return json.dumps({
        "project": status["project"]["name"], "summary": status["summary"],
        "repositories": [{key: repo.get(key) for key in ("name", "status", "ref", "sha", "indexed", "warning")}
                         for repo in status["repositories"]],
        "brain": {key: brain.get(key) for key in ("version", "edition", "effective", "health", "reason",
                                                  "generation_predates_binary", "update")},
        "investigations": [{key: item.get(key) for key in ("ticket", "status", "requests")}
                           for item in status.get("sessions") or []][:50],
    }, indent=1, default=str)


def _start(settings: Settings, arguments: dict[str, Any]) -> str:
    from .core import deliver, start_session

    ticket = _ticket(arguments)
    text = str(arguments.get("ticket_text") or "").strip()
    if not text:
        raise ToolError("ticket_text is required")
    if arguments.get("refresh"):
        from .ops import refresh_brain

        refresh_brain(settings, fetch=True)
    content, _ = start_session(settings, ticket, text)
    deliver(settings, ticket, content, "claude", copy=False)
    return _page(settings, ticket)


def _continue(settings: Settings, arguments: dict[str, Any]) -> str:
    from .core import request_repair_prompt
    from .ui import _Handler

    ticket = _ticket(arguments)
    reply = arguments.get("reply")
    if isinstance(reply, dict):
        payload = reply if "INVESTIGATION_REQUEST" in reply or "CONTEXT_REQUEST" in reply else {"INVESTIGATION_REQUEST": reply}
        reply = "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
    if not isinstance(reply, str) or not reply.strip():
        raise ToolError("reply must be an INVESTIGATION_REQUEST object or reply text")
    try:
        result = _Handler._retrieval_job(settings, {"ticket": ticket, "text": reply, "target": "claude"}, None)
    except BrainError as error:
        raise ToolError(f"{error}\n\n{request_repair_prompt(str(error))}") from error
    if result.get("kind") == "conversation":
        if "FINAL_SOLUTION" in reply:
            from .agent import final_solution_contract

            missing = final_solution_contract(reply)[1]
            raise ToolError("FINAL_SOLUTION is incomplete. Add these top-level sections with real content: "
                            + "; ".join(missing))
        raise ToolError("No INVESTIGATION_REQUEST or FINAL_SOLUTION was found. " + request_repair_prompt(
            "Send one INVESTIGATION_REQUEST JSON object, or ask the user your question directly instead of calling Brain."))
    if result.get("kind") == "final_solution":
        return f"FINAL_SOLUTION archived as {result.get('path')}. Present it to the user; no further Brain request is needed."
    return _page(settings, ticket)


def _handoff(settings: Settings, arguments: dict[str, Any]) -> str:
    try:
        part = int(arguments.get("part") or 1)
    except (TypeError, ValueError) as error:
        raise ToolError("part must be a positive integer") from error
    return _page(settings, _ticket(arguments), max(1, part))


def _session(settings: Settings, arguments: dict[str, Any]) -> str:
    from .ui import _session_detail

    detail = _session_detail(settings, _ticket(arguments))
    keys = ("ticket", "status", "requests", "feedbacks", "no_progress_rounds", "atlas_generation", "context_id",
            "context_lineage", "coverage_map", "investigation_memory", "checkpoint_recovery")
    return json.dumps({key: detail.get(key) for key in keys}, indent=1, default=str)[:PAGE_CHARS]


def _resume(settings: Settings, arguments: dict[str, Any]) -> str:
    from .core import resume_session

    ticket = _ticket(arguments)
    resume_session(settings, ticket, str(arguments.get("notes") or ""), target="claude")
    return _page(settings, ticket)


def _feedback(settings: Settings, arguments: dict[str, Any]) -> str:
    from .core import create_feedback, deliver

    ticket = _ticket(arguments)
    repos = arguments.get("repos") or []
    if not isinstance(repos, list):
        raise ToolError("repos must be a list of repository names")
    content, _, _ = create_feedback(settings, ticket, notes=str(arguments.get("notes") or ""),
                                    test_command=str(arguments.get("test_command") or ""),
                                    test_output=str(arguments.get("test_output") or ""),
                                    repos=[str(repo) for repo in repos], include_diff=True)
    deliver(settings, ticket, content, "claude", copy=False)
    return _page(settings, ticket)


HANDLERS: dict[str, Callable[[Settings, dict[str, Any]], str]] = {
    "brain_status": _status, "brain_start": _start, "brain_continue": _continue, "brain_handoff": _handoff,
    "brain_session": _session, "brain_resume": _resume, "brain_feedback": _feedback,
}


def _instructions() -> str:
    prompt = package_files("brain").joinpath("prompt.md").read_text(encoding="utf-8")
    return ("Project Brain retrieves pinned, read-only evidence from the user's configured repositories. "
            "Start each ticket with brain_start, send each INVESTIGATION_REQUEST with brain_continue, read further "
            "pages with brain_handoff, and stop calling Brain once the implementation is ready.\n\n" + prompt)


def handle(message: Any, load: Callable[[], Settings]) -> dict[str, Any] | None:
    """One JSON-RPC message in, one response (or None for a notification) out."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return {"jsonrpc": "2.0", "id": message.get("id") if isinstance(message, dict) else None,
                "error": {"code": -32600, "message": "Invalid JSON-RPC request"}}
    method, identifier, params = message["method"], message.get("id"), message.get("params") or {}
    if "id" not in message:
        return None  # Notifications (initialized, cancelled, ...) need no reply.

    def result(value: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": identifier, "result": value}

    def error(code: int, text: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": text}}

    if method == "initialize":
        requested = str(params.get("protocolVersion") or "")
        return result({"protocolVersion": requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                       "capabilities": {"tools": {}},
                       "serverInfo": {"name": "project-brain", "version": __version__},
                       "instructions": _instructions()})
    if method == "ping":
        return result({})
    if method == "tools/list":
        return result({"tools": TOOLS})
    if method != "tools/call":
        return error(-32601, f"Method not found: {method}")
    name, arguments = params.get("name"), params.get("arguments") or {}
    if name not in HANDLERS or not isinstance(arguments, dict):
        return error(-32602, f"Unknown tool or invalid arguments: {name}")
    try:
        with contextlib.redirect_stdout(sys.stderr):
            text = HANDLERS[name](load(), arguments)
        return result({"content": [{"type": "text", "text": text}], "isError": False})
    except (BrainError, OSError, ValueError, RuntimeError) as failure:
        return result({"content": [{"type": "text", "text": str(failure)}], "isError": True})


def serve(config_path: Path | str, stdin: BinaryIO | None = None, stdout: BinaryIO | None = None) -> int:
    """Run until EOF. Settings are reloaded per call so brain.toml edits and refreshes apply."""
    source = stdin or sys.stdin.buffer
    sink = stdout or sys.stdout.buffer

    def load() -> Settings:
        return replace(load_settings(config_path), clipboard_chunk_chars=PAGE_CHARS)

    while line := source.readline(MAX_MESSAGE_BYTES + 1):
        if not line.strip():
            continue
        try:
            if len(line) > MAX_MESSAGE_BYTES:
                raise ValueError("message too large")
            response = handle(json.loads(line), load)
        except ValueError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if response is not None:
            sink.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
            sink.flush()
    return 0


def _command() -> tuple[str, list[str]]:
    """How a host launches this Brain: an absolute path, because hosts do not inherit shell PATH."""
    executable = Path(sys.executable).resolve()
    if getattr(sys, "frozen", False):
        from .mac_service import _stable_executable

        return _stable_executable(str(executable)), []
    script = executable.parent / ("brain.exe" if os.name == "nt" else "brain")
    if script.is_file():
        return str(script), []
    return str(executable), ["-m", "brain.cli"]


def host_config(settings: Settings, host: str) -> tuple[Path | None, str, dict[str, Any]]:
    """(config file or None to paste, servers key, server entry). Never contains the UI token."""
    if host not in HOSTS:
        raise BrainError(f"host must be one of: {', '.join(HOSTS)}")
    command, prefix = _command()
    entry: dict[str, Any] = {"command": command, "args": [*prefix, "-c", str(settings.config_path.resolve()), "mcp"]}
    if host == "claude-desktop":
        if sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support" / "Claude"
        elif os.name == "nt":
            base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Claude"
        else:
            base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "Claude"
        return base / "claude_desktop_config.json", "mcpServers", entry
    if host == "claude-code":
        return settings.root / ".mcp.json", "mcpServers", {"type": "stdio", **entry}
    if host == "vscode":
        return settings.root / ".vscode" / "mcp.json", "servers", {"type": "stdio", **entry}
    if host == "cursor":
        return settings.root / ".cursor" / "mcp.json", "mcpServers", entry
    return None, "mcpServers", entry


def server_name(settings: Settings) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", settings.name.lower()).strip("-")[:40]
    return f"project-brain-{slug}" if slug else "project-brain"


def write_host_config(settings: Settings, host: str) -> Path | None:
    """Merge this workspace's server into the host file, keeping every other server."""
    path, key, entry = host_config(settings, host)
    if path is None:
        return None
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8") or "{}")
        except ValueError as error:
            raise BrainError(f"{path} is not valid JSON; fix or move it, then retry. Nothing was changed.") from error
        if not isinstance(existing, dict) or not isinstance(existing.get(key, {}), dict):
            raise BrainError(f"{path} has an unexpected shape; nothing was changed.")
    existing.setdefault(key, {})[server_name(settings)] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".brain-tmp")
    temporary.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    if os.name != "nt":
        os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return path


def describe(settings: Settings, host: str) -> str:
    path, key, entry = host_config(settings, host)
    snippet = json.dumps({key: {server_name(settings): entry}}, indent=2)
    lines = [f"Project Brain MCP server for {host}:", snippet]
    if host == "claude-code":
        lines.append("Or register it for every project: claude mcp add --scope user " + server_name(settings) + " -- "
                     + " ".join(json.dumps(part) if " " in part else part for part in [entry["command"], *entry["args"]]))
    lines.append(f"Write it with: brain mcp config --host {host} --write" if path else
                 "Paste it into Settings | Tools | AI Assistant | Model Context Protocol (MCP).")
    return "\n".join(lines)

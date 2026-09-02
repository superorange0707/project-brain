from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from importlib.resources import files as package_files
from pathlib import Path
from typing import Any

from . import __version__
from .core import (
    BrainError,
    Settings,
    mark_active_artifacts,
    protocol_request_signature,
    request_preview,
    save_session,
    session_dir,
    session_state,
)
from .locks import ticket_exclusive


_FINAL_SOLUTION_SECTIONS = (
    ("ticket interpretation",),
    ("verified current behavior",),
    ("execution flow", "integration flow"),
    ("root cause", "required behavior change"),
    ("exact repository", "exact repositories"),
    ("suggested production changes", "suggested changes"),
    ("test surface", "tests and assertions", "exact tests"),
    ("validation commands", "validation"),
    ("edge cases", "compatibility risks"),
    ("implementation order",),
    ("remaining assumptions", "remaining uncertainties"),
)
M365_KNOWLEDGE_SOURCE_BYTES = 512 * 1024
M365_KNOWLEDGE_TOTAL_BYTES = 2 * 1024 * 1024
M365_REPOSITORY_METADATA_BYTES = 2 * 1024


def final_solution_contract(text: str) -> tuple[bool, list[str]]:
    """Validate the top-level Protocol v5 final marker and its minimum contract."""
    stripped = text.lstrip("\ufeff \t\r\n")
    marker = re.match(r"(?i)^(?:#{1,6}[ \t]+)?FINAL_SOLUTION[ \t]*(?:\r?\n|$)", stripped)
    if marker is None:
        return False, ["top-level FINAL_SOLUTION marker"]
    body = stripped[marker.end():]
    sections: list[tuple[str, str]] = []
    current_title: str | None = None
    current_body: list[str] = []
    fence: str | None = None
    for line in body.splitlines():
        fence_match = re.match(r"^\s*(`{3,}|~{3,})", line)
        if fence_match:
            marker_value = fence_match.group(1)
            if fence is None:
                fence = marker_value
            elif marker_value[0] == fence[0] and len(marker_value) >= len(fence):
                fence = None
            continue
        if fence is not None:
            continue
        heading = re.match(r"^\s*#{2,3}\s+(.+?)\s*#*\s*$", line)
        if heading:
            if current_title is not None:
                sections.append((current_title, "\n".join(current_body).strip()))
            current_title = re.sub(r"[^a-z0-9]+", " ", heading.group(1).casefold()).strip()
            current_body = []
        elif current_title is not None:
            current_body.append(line)
    if current_title is not None:
        sections.append((current_title, "\n".join(current_body).strip()))

    placeholders = re.compile(
        r"(?i)^\s*(?:none|n/?a|not (?:provided|available|known)|unknown|todo|tbd|pending|see above)[.!\s-]*$"
    )
    complete_titles = {
        title for title, content in sections
        if content and not placeholders.fullmatch(re.sub(r"[`*_>#-]", "", content).strip())
    }
    missing = [
        " / ".join(aliases)
        for aliases in _FINAL_SOLUTION_SECTIONS
        if not any(any(alias in title for alias in aliases) for title in complete_titles)
    ]
    return not missing, missing


def response_preview(text: str, settings: Settings | None = None, ticket: str | None = None) -> dict[str, Any]:
    """Classify a complete AI reply without using another model."""
    stripped = text.strip()
    if not stripped:
        raise BrainError("The AI response is empty")
    request_position = max(text.rfind("CONTEXT_REQUEST:"), text.rfind("INVESTIGATION_REQUEST:"))
    final, _ = final_solution_contract(text)
    if final:
        return {
            "valid": True,
            "kind": "final_solution",
            "label": "Ready to implement",
            "message": "The AI returned a final implementation plan. No repository retrieval is required.",
            "operation_count": 0,
            "actions": [],
        }
    looks_like_request = request_position >= 0 or (
        stripped.startswith("{") and ("CONTEXT_REQUEST" in stripped or "INVESTIGATION_REQUEST" in stripped or '"objective"' in stripped)
    )
    if looks_like_request:
        result = request_preview(text, settings)
        result["kind"] = "context_request"
        result["label"] = "Repository retrieval required"
        if ticket:
            state = session_state(settings, ticket) if settings else {}
            signature = protocol_request_signature(result, ticket, state)
            result["signature"] = signature
            previous = next(
                (
                    item
                    for item in state.get("request_history") or []
                    if item.get("signature") == result["signature"]
                    and item.get("source_signature") == state.get("source_signature")
                ),
                None,
            )
            result["duplicate_of"] = int(previous.get("number") or 0) if previous else None
        return result
    return {
        "valid": True,
        "kind": "conversation",
        "label": "Reply in the AI chat",
        "message": (
            "This response contains no Project Brain tool request. Answer the AI directly, provide the requested "
            "document or runtime result, and let the AI decide whether it needs repository evidence next."
        ),
        "operation_count": 0,
        "actions": [],
    }


@ticket_exclusive
def archive_final_solution(settings: Settings, ticket: str, text: str) -> Path:
    preview = response_preview(text, settings, ticket)
    if preview["kind"] != "final_solution":
        _, missing = final_solution_contract(text)
        raise BrainError("The AI response does not contain a complete FINAL_SOLUTION contract: " + ", ".join(missing))
    directory = session_dir(settings, ticket)
    if not directory.is_dir():
        raise BrainError(f"Session {ticket} does not exist. Run `brain start {ticket}` first.")
    # Validate compatibility before creating or replacing any ticket artifact.
    state = session_state(settings, ticket)
    path = directory / "final-solution.md"
    from .core import _atomic_session_text_write

    _atomic_session_text_write(settings, ticket, path, text.rstrip() + "\n")
    state["status"] = "ready_to_implement"
    state["finalized_at"] = datetime.now(UTC).isoformat()
    mark_active_artifacts(state, path)
    from .atlas import record_investigation

    save_session(settings, ticket, state)
    if settings.persist_investigation_records:
        try:
            record_investigation(settings, ticket, state)
        except (OSError, sqlite3.Error):
            # The ticket session is authoritative; the cross-ticket prior is derived.
            pass
    return path


def create_m365_agent_kit(settings: Settings) -> dict[str, Any]:
    from .core import (
        _atomic_generated_text_write,
        _bounded_text_file,
        _bounded_utf8_text,
        _validated_generated_artifact,
    )

    directory = settings.generated_dir / "m365-agent"
    _validated_generated_artifact(settings, directory / ".managed", create_parents=True)
    instructions = package_files("brain").joinpath("m365_agent_instructions.md").read_text(encoding="utf-8")
    project_name, _ = _bounded_utf8_text(settings.name, 512, " [truncated]")
    instructions = instructions.replace("{{PROJECT_NAME}}", project_name)
    instructions_path = directory / "INSTRUCTIONS.md"
    _atomic_generated_text_write(settings, instructions_path, instructions.rstrip() + "\n")

    knowledge = ["# Project knowledge", "", f"Project: `{settings.name}`", "", "## Repository catalog", ""]
    for repo in settings.repositories:
        detail = f" — {repo.description}" if repo.description else ""
        tags = f"; tags: {', '.join(repo.tags)}" if repo.tags else ""
        item, omitted = _bounded_utf8_text(
            f"- `{repo.name}`{detail}{tags}",
            M365_REPOSITORY_METADATA_BYTES,
            " … [metadata truncated]",
        )
        knowledge.append(item)
        if omitted:
            knowledge.append("  - Project Brain bounded this repository metadata before Agent Kit generation.")
    for title, path in (
        ("Project map", settings.knowledge_dir / "PROJECT_MAP.md"),
        ("Glossary", settings.knowledge_dir / "glossary.md"),
    ):
        if path.is_file():
            text, omitted = _bounded_text_file(path, M365_KNOWLEDGE_SOURCE_BYTES)
            knowledge.extend(["", f"## {title}", "", text.strip()])
            if omitted:
                knowledge.append("[Project Brain omitted unsafe or excess bytes from this knowledge source.]")
    knowledge_text, _ = _bounded_utf8_text(
        "\n".join(knowledge).rstrip() + "\n",
        M365_KNOWLEDGE_TOTAL_BYTES,
        "\n\n[Project Brain omitted remaining Agent Kit knowledge at the byte limit.]\n",
    )
    knowledge_path = directory / "PROJECT_KNOWLEDGE.md"
    _atomic_generated_text_write(settings, knowledge_path, knowledge_text)

    suggested = """# Suggested prompts

## Investigate a ticket

Investigate this ticket as a read-only coding agent. I will attach the latest Project Brain handoff. Reconstruct the relevant multi-repository flow, identify blocking unknowns, and decide what evidence is needed next.

## Continue with Brain evidence

Continue using the latest Project Brain handoff and context_id. Update VERIFIED, INFERRED, BLOCKING UNKNOWN, and NON-BLOCKING UNKNOWN from the delta. Request at most one focused follow-up for one fact that can materially change the implementation; otherwise return FINAL_SOLUTION.

## Read internal documentation

Use the attached internal IPF documentation together with the ticket and repository evidence. Explain what the document proves, whether it conflicts with the current implementation, and how it changes the proposed solution.

## Produce the implementation plan

Decide whether enough evidence now exists to implement safely. If yes, return FINAL_SOLUTION with exact repositories, files, methods, configuration, suggested changes, tests, validation commands, edge cases, and implementation order. Otherwise ask only the specific blocking question or return one focused INVESTIGATION_REQUEST v5 using the latest base_context_id.
"""
    suggested_path = directory / "SUGGESTED_PROMPTS.md"
    _atomic_generated_text_write(settings, suggested_path, suggested)

    setup = f"""# Microsoft 365 Copilot Agent setup

1. In Microsoft 365 Copilot, create a new agent and open **Configure**.
2. Name it `Project Brain Engineer` and select **Think deeper** as the default response mode.
3. Paste the complete contents of `{instructions_path.name}` into **Instructions**.
4. Add `{knowledge_path.name}` plus approved architecture, IPF, API, deployment, and coding-standard documents to **Knowledge**.
5. Add the four title/prompt pairs from `{suggested_path.name}` to **Suggested prompts**.
6. Enable **Only use specified sources**. Disable broad web, email, Teams, and people sources unless this project needs them.
7. Create the agent and test it with the starter prompt below.

Starter prompt:

> Investigate this ticket as a read-only coding agent. I will attach the Project Brain start package. Ask me directly for business, document, or runtime facts; emit an INVESTIGATION_REQUEST v5 only when local repository evidence is required; return FINAL_SOLUTION when the implementation is ready.

This kit uses Project Brain INVESTIGATION_REQUEST protocol v5, bounded multi-wave investigation, and delta context lineage. Protocols v1–v4 remain accepted for existing conversations. After upgrading Brain, rerun `brain agent-kit m365`, replace Agent Builder Instructions and PROJECT_KNOWLEDGE.md, and optionally refresh Suggested Prompts. A new M365 conversation is recommended for protocol validation.

For every ticket, run `brain start TICKET --ticket-file ticket.md --target m365` and upload the printed `generated/handoffs/TICKET-start.md`. During a longer v5 wave, Brain may publish `TICKET-checkpoint-NNN.md` before the remaining flow work finishes; upload it immediately if you need the first exact evidence, then apply `TICKET-checkpoint-delta-NNN.md` to that checkpoint when it appears. The normal completed-round handoff remains `TICKET-context-NNN.md`. Never upload the internal `.runs/TICKET/request-NNN.yml`; it is the AI-to-Brain command. The changing filename prevents M365 from reusing an older attachment; `TICKET-current.md` is only a local alias.
"""
    setup_path = directory / "SETUP.md"
    _atomic_generated_text_write(settings, setup_path, setup)
    protocol = """# Project Brain Investigation Protocol v5

## Request envelope

Use exactly one `INVESTIGATION_REQUEST` mapping. Required fields are `version: 5`, `mode`, and `objective`. Supported modes are `root_cause`, `implementation_plan`, `impact_analysis`, `test_surface`, `flow_trace`, and `history`. Optional bounded fields are `runtime_facts`, `hypotheses`, `required`, `resolve`, `anchors`, `base_context_id`, `checkpoint`, and `wave`.

## State and lineage

Treat Atlas cards, anchors, flow candidates, Program Slice Lite, history, and semantic ranks as navigation intelligence. Only exact source from the ticket's pinned generation or explicitly authoritative attached documentation is VERIFIED evidence. Preserve stable evidence, anchor, flow, blocker, and context IDs. Apply a delta only to its declared base; request a full checkpoint when the base is missing or stale.

## Investigation state machine

Proceed through `INTAKE → ORIENT → INVESTIGATE → CHALLENGE → SYNTHESIZE → STOP`. Use at most three normal waves and never exceed four. Challenge the leading hypothesis with disconfirming evidence before synthesis. Stop on sufficient coverage, no progress, an external blocker, or the wave/budget limit.

## Final response

Return `FINAL_SOLUTION` only when exact repositories, files, symbols/configuration, verified flow, implementation surface, tests, validation, compatibility risks, and remaining assumptions are explicit. Never present a candidate graph edge, slice statement, or historical analogue as final evidence.
"""
    protocol_path = directory / "INVESTIGATION_PROTOCOL.md"
    _atomic_generated_text_write(settings, protocol_path, protocol)
    manifest = {
        "project_brain_version": __version__,
        "manifest_version": "1.0.0",
        "agent_kit_version": 4,
        "context_request_protocol": 5,
        "investigation_protocol": 5,
        "legacy_protocols": [1, 2, 3, 4],
        "state_machine": ["INTAKE", "ORIENT", "INVESTIGATE", "CHALLENGE", "SYNTHESIZE", "STOP"],
    }
    manifest_path = directory / "AGENT_KIT.json"
    _atomic_generated_text_write(settings, manifest_path, json.dumps(manifest, indent=2) + "\n")
    return {
        "directory": str(directory),
        "instructions_path": str(instructions_path),
        "knowledge_path": str(knowledge_path),
        "suggested_prompts_path": str(suggested_path),
        "setup_path": str(setup_path),
        "protocol_path": str(protocol_path),
        "manifest_path": str(manifest_path),
        "manifest": manifest,
        "instructions": instructions,
        "knowledge": knowledge_text,
        "suggested_prompts": suggested,
        "setup": setup,
        "protocol": protocol,
    }

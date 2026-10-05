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
    _request_document,
    investigation_continuation,
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


def _final_solution_sections(text: str, *, include_fences: bool = False) -> list[tuple[str, str]] | None:
    stripped = text.lstrip("\ufeff \t\r\n")
    marker = re.match(r"(?i)^(?:#{1,6}[ \t]+)?FINAL_SOLUTION[ \t]*(?:\r?\n|$)", stripped)
    if marker is None:
        return None
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
            if include_fences and current_title is not None:
                current_body.append(line)
            continue
        if fence is not None:
            if include_fences and current_title is not None:
                current_body.append(line)
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
    return sections


def final_solution_contract(text: str) -> tuple[bool, list[str]]:
    """Validate the top-level Protocol v5 final marker and its minimum contract."""
    sections = _final_solution_sections(text)
    if sections is None:
        return False, ["top-level FINAL_SOLUTION marker"]

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
    production_section = " / ".join(_FINAL_SOLUTION_SECTIONS[5])
    if production_section in missing and _has_production_change_block(text):
        missing.remove(production_section)
    return not missing, missing


def _has_production_change_block(text: str) -> bool:
    """Detect payload presence, not applicability, completeness, or correctness."""
    for title, content in _final_solution_sections(text, include_fences=True) or []:
        if not any(alias in title for alias in _FINAL_SOLUTION_SECTIONS[5]):
            continue
        fence: str | None = None
        language = ""
        payload: list[str] = []
        for line in content.splitlines():
            if fence is None:
                opening = re.fullmatch(r"\s*(`{3,}|~{3,})([^`~]*)", line)
                if opening:
                    fence = opening.group(1)
                    language = opening.group(2).strip().casefold().split(" ")[0]
                    payload = []
            elif re.fullmatch(r"\s*" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", line):
                block = "\n".join(payload).strip()
                if (block and language not in {"text", "markdown", "md", "mermaid", "pseudo", "pseudocode"}
                        and not re.fullmatch(r"(?i)(?:todo|tbd|pending|\.\.\.)[.!\s]*", block)
                        and not re.search(r"(?i)\b(?:INVESTIGATION_REQUEST|CONTEXT_REQUEST)\b", block)):
                    return True
                fence = None
            else:
                payload.append(line)
    return False


def response_preview(text: str, settings: Settings | None = None, ticket: str | None = None) -> dict[str, Any]:
    """Classify a complete AI reply without using another model."""
    stripped = text.strip()
    if not stripped:
        raise BrainError("The AI response is empty")
    final, _ = final_solution_contract(text)
    if final:
        has_block = _has_production_change_block(text)
        return {
            "valid": True,
            "kind": "final_solution",
            "label": "Production change block supplied" if has_block else "Plan needs implementation details",
            "message": (
                "The suggested production changes include a code/configuration block. "
                "Review its file targets, acceptance criteria and tests before applying it; "
                "Brain has not evaluated the proposal's correctness or run the tests."
                if has_block else
                "No production code/configuration block was detected under Suggested production changes. "
                "Ask the AI for per-file diffs or replacement snippets with exact insertion locations, "
                "acceptance criteria and test assertions. For DSL/model changes, require precise editor steps; "
                "if no source change is needed, require the reason. This plan can still be archived."
            ),
            "operation_count": 0,
            "actions": [],
        }
    if _request_document(text) is not None:
        result = request_preview(text, settings)
        result["kind"] = "context_request"
        result["label"] = "Repository retrieval required"
        if ticket:
            state = session_state(settings, ticket) if settings else {}
            signature = protocol_request_signature(result, ticket, state)
            result["signature"] = signature
            if settings and result["request"].get("version") == 5:
                result["continuation"] = investigation_continuation(settings, state)
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

Investigate this ticket as a read-only coding agent. I will attach the latest Project Brain handoff. Reconstruct the relevant multi-repository flow, identify blocking unknowns, and decide what evidence is needed next. If repository evidence is needed, return one valid fenced JSON request using the handoff's protocol; omit wave and never invent a context ID.

## Continue with Brain evidence

Continue using the latest Project Brain handoff and context_id. Update VERIFIED, INFERRED, BLOCKING UNKNOWN, and NON-BLOCKING UNKNOWN from the delta. Continue requesting focused evidence while a material repository fact remains unresolved; there is no fixed investigation-round limit. Return FINAL_SOLUTION only when the evidence is sufficient, and report an external blocker explicitly if Brain cannot resolve it.

## Read internal documentation

Use the attached internal IPF documentation together with the ticket and repository evidence. Explain what the document proves, whether it conflicts with the current implementation, and how it changes the proposed solution.

## Produce the implementation plan

Decide whether enough evidence now exists to implement safely. If yes, return FINAL_SOLUTION with exact repositories, files, methods and configuration. Under Suggested production changes provide per-file fenced diffs or replacement code/configuration with exact edit locations and existing patterns. For projectional models provide precise editor steps at verified model/concept/node targets. Map each acceptance criterion to its edit and exact test/assertion; give dependency order, project-evidenced validation commands, risks and assumptions. Explain when no source edit is needed. Otherwise ask only the specific blocking question or return one focused INVESTIGATION_REQUEST v5 using the latest base_context_id. A plan is not proof of applied or validated delivery.
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

For every ticket, run `brain start TICKET --ticket-file ticket.md --target m365` and upload the printed `generated/handoffs/TICKET/start.md`. During a longer v5 wave, Brain may publish `checkpoint-NNN.md` in that ticket folder before the remaining flow work finishes; upload it immediately if you need the first exact evidence, then apply `checkpoint-delta-NNN.md` to that checkpoint when it appears. The normal completed-round handoff remains `context-NNN.md` in the same ticket folder. Never upload the internal `.runs/TICKET/request-NNN.yml`; it is the AI-to-Brain command. The changing filename prevents M365 from reusing an older attachment; `current.md` is only a local alias.
"""
    setup_path = directory / "SETUP.md"
    _atomic_generated_text_write(settings, setup_path, setup)
    protocol = """# Project Brain Investigation Protocol v5

## Request envelope

Use exactly one `INVESTIGATION_REQUEST` mapping. Required fields are `version: 5`, `mode`, and `objective`. Supported modes are `root_cause`, `implementation_plan`, `impact_analysis`, `test_surface`, `flow_trace`, and `history`. Optional bounded fields are `runtime_facts`, `hypotheses`, `required`, `resolve`, `anchors`, `files`, `base_context_id`, `checkpoint`, and `wave`.

Emit a valid JSON object in one fenced `json` block, using ordinary double quotes, escaped strings, no comments and no trailing commas. JSON is valid YAML and can be pasted into Brain unchanged. Keep reasoning outside the block. Omit `wave` and copy `base_context_id` only from the actual latest handoff; omit it when none was supplied. Never invent a context ID or copy a sample counter. Existing YAML requests remain accepted.

For an established file, `files: [{repo: VERIFIED_REPO, path: VERIFIED_RELATIVE_PATH}]` requests full pinned source, not another search. Optional `lines: "start-end"` requests an exact range. Large requests return whole-line byte-bounded pages with total lines, returned range and `next lines`; request the remaining range on the same ticket. Only advance after that page's evidence is actually embedded. File-only requests skip optional discovery/models. Missing handoff content does not prove the source file is absent; never ask the user to fetch configured repository code manually.

## State and lineage

Treat Atlas cards, anchors, flow candidates, Program Slice Lite, history, and semantic ranks as navigation intelligence. Only exact source from the ticket's pinned generation or explicitly authoritative attached documentation is VERIFIED evidence. Preserve stable evidence, anchor, flow, blocker, and context IDs. Apply a delta only to its declared base; request a full checkpoint when the base is missing or stale.

## Investigation state machine

Proceed through `INTAKE → ORIENT → INVESTIGATE → CHALLENGE → SYNTHESIZE → STOP`. There is no fixed investigation round limit or extra continuation approval. Each submitted request has its own resource budget. Challenge the leading hypothesis with disconfirming evidence before synthesis. Continue for a material missing repository fact; change the anchor or read the missing source when a search makes no progress. Synthesize on sufficient evidence, or ask for a genuinely external blocker. A pause or coverage label is not proof of completion. Preserve the pinned generation and all evidence/context identities. Omit `wave` or continue the ticket's sequential count; never restart the counter or add approval fields to the AI request envelope.

## Final response

Return `FINAL_SOLUTION` only when exact repositories, files, symbols/configuration, verified flow, implementation surface, tests, validation, compatibility risks, and remaining assumptions are explicit. Under Suggested production changes provide per-file fenced diffs or replacement code/configuration and exact edit locations. For projectional models provide precise editor steps at verified model/concept/node targets. Map acceptance criteria to edits and exact test assertions. Use only project-evidenced commands; review developer diff/test feedback before claiming delivery. Never present a candidate graph edge, slice statement, or historical analogue as final evidence.
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

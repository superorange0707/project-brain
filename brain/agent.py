from __future__ import annotations

import re
from datetime import UTC, datetime
from importlib.resources import files as package_files
from pathlib import Path
from typing import Any

from .core import BrainError, Settings, request_preview, save_session, session_dir, session_state


def response_preview(text: str, settings: Settings | None = None, ticket: str | None = None) -> dict[str, Any]:
    """Classify a complete AI reply without using another model."""
    stripped = text.strip()
    if not stripped:
        raise BrainError("The AI response is empty")
    request_position = text.rfind("CONTEXT_REQUEST:")
    final_matches = list(re.finditer(r"(?im)^\s*(?:#+\s*)?FINAL_SOLUTION\b", text))
    if final_matches and final_matches[-1].start() > request_position:
        return {
            "valid": True,
            "kind": "final_solution",
            "label": "Ready to implement",
            "message": "The AI returned a final implementation plan. No repository retrieval is required.",
            "operation_count": 0,
            "actions": [],
        }
    looks_like_request = request_position >= 0 or (
        stripped.startswith("{") and ("CONTEXT_REQUEST" in stripped or '"objective"' in stripped)
    )
    if looks_like_request:
        result = request_preview(text, settings)
        result["kind"] = "context_request"
        result["label"] = "Repository retrieval required"
        if ticket:
            state = session_state(settings, ticket) if settings else {}
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


def archive_final_solution(settings: Settings, ticket: str, text: str) -> Path:
    preview = response_preview(text, settings, ticket)
    if preview["kind"] != "final_solution":
        raise BrainError("The AI response does not contain FINAL_SOLUTION")
    directory = session_dir(settings, ticket)
    if not directory.is_dir():
        raise BrainError(f"Session {ticket} does not exist. Run `brain start {ticket}` first.")
    path = directory / "final-solution.md"
    path.write_text(text.rstrip() + "\n", encoding="utf-8")
    state = session_state(settings, ticket)
    state["status"] = "ready_to_implement"
    state["finalized_at"] = datetime.now(UTC).isoformat()
    save_session(settings, ticket, state)
    return path


def create_m365_agent_kit(settings: Settings) -> dict[str, Any]:
    directory = settings.generated_dir / "m365-agent"
    directory.mkdir(parents=True, exist_ok=True)
    instructions = package_files("brain").joinpath("m365_agent_instructions.md").read_text(encoding="utf-8")
    instructions = instructions.replace("{{PROJECT_NAME}}", settings.name)
    instructions_path = directory / "INSTRUCTIONS.md"
    instructions_path.write_text(instructions.rstrip() + "\n", encoding="utf-8")

    knowledge = ["# Project knowledge", "", f"Project: `{settings.name}`", "", "## Repository catalog", ""]
    for repo in settings.repositories:
        detail = f" — {repo.description}" if repo.description else ""
        tags = f"; tags: {', '.join(repo.tags)}" if repo.tags else ""
        knowledge.append(f"- `{repo.name}`{detail}{tags}")
    for title, path in (
        ("Project map", settings.knowledge_dir / "PROJECT_MAP.md"),
        ("Glossary", settings.knowledge_dir / "glossary.md"),
    ):
        if path.is_file():
            knowledge.extend(["", f"## {title}", "", path.read_text(encoding="utf-8", errors="replace").strip()])
    knowledge_path = directory / "PROJECT_KNOWLEDGE.md"
    knowledge_path.write_text("\n".join(knowledge).rstrip() + "\n", encoding="utf-8")

    suggested = """# Suggested prompts

## Investigate a ticket

Investigate this ticket as a read-only coding agent. I will attach the latest Project Brain handoff. Reconstruct the relevant multi-repository flow, identify blocking unknowns, and decide what evidence is needed next.

## Continue with Brain evidence

Continue the investigation using the attached latest Project Brain handoff. Update what is VERIFIED, INFERRED, BLOCKING UNKNOWN, and NON-BLOCKING UNKNOWN. Request more repository evidence only when it would materially change the implementation.

## Read internal documentation

Use the attached internal IPF documentation together with the ticket and repository evidence. Explain what the document proves, whether it conflicts with the current implementation, and how it changes the proposed solution.

## Produce the implementation plan

Decide whether enough evidence now exists to implement safely. If yes, return FINAL_SOLUTION with exact repositories, files, methods, configuration, suggested changes, tests, validation commands, edge cases, and implementation order. Otherwise ask only the specific blocking question or return one focused CONTEXT_REQUEST.
"""
    suggested_path = directory / "SUGGESTED_PROMPTS.md"
    suggested_path.write_text(suggested, encoding="utf-8")

    setup = f"""# Microsoft 365 Copilot Agent setup

1. In Microsoft 365 Copilot, create a new agent and open **Configure**.
2. Name it `Project Brain Engineer` and select **Think deeper** as the default response mode.
3. Paste the complete contents of `{instructions_path.name}` into **Instructions**.
4. Add `{knowledge_path.name}` plus approved architecture, IPF, API, deployment, and coding-standard documents to **Knowledge**.
5. Add the four title/prompt pairs from `{suggested_path.name}` to **Suggested prompts**.
6. Enable **Only use specified sources**. Disable broad web, email, Teams, and people sources unless this project needs them.
7. Create the agent and test it with the starter prompt below.

Starter prompt:

> Investigate this ticket as a read-only coding agent. I will attach the Project Brain start package. Ask me directly for business, document, or runtime facts; emit a CONTEXT_REQUEST only when local repository evidence is required; return FINAL_SOLUTION when the implementation is ready.

For every ticket, run `brain start TICKET --ticket-file ticket.md --target m365`, upload the newly printed round-specific file such as `generated/handoffs/TICKET-request-001.md`, and keep using the same agent conversation. The changing filename prevents M365 from reusing an older attachment; `TICKET-current.md` remains a local alias.
"""
    setup_path = directory / "SETUP.md"
    setup_path.write_text(setup, encoding="utf-8")
    return {
        "directory": str(directory),
        "instructions_path": str(instructions_path),
        "knowledge_path": str(knowledge_path),
        "suggested_prompts_path": str(suggested_path),
        "setup_path": str(setup_path),
        "instructions": instructions,
        "knowledge": knowledge_path.read_text(encoding="utf-8"),
        "suggested_prompts": suggested,
        "setup": setup,
    }

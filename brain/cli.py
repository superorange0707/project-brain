from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .agent import archive_final_solution, create_m365_agent_kit, response_preview
from .core import (
    BrainError,
    clipboard_read,
    create_feedback,
    create_context,
    create_learning_template,
    deliver,
    doctor,
    generate_map,
    git_history,
    load_settings,
    move_delivery,
    request_repair_prompt,
    search,
    session_state,
    snapshot_indexes,
    start_session,
    symbol_hits,
    trace_symbol,
)
from .relations import generate_relationship_map
from .sync import SyncResult, parse_branch_overrides, sync_repositories
from .graph import GraphIndexResult, index_graph


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brain", description="Read-only multi-repository context for chat AIs")
    parser.add_argument("-c", "--config", help="brain.toml/config.yml path")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="create a portable Project Brain workspace")
    init.add_argument("repos", nargs="*", help="project roots to scan (defaults to the current folder)")
    init.add_argument("--name", help="project name")
    init.add_argument("--force", action="store_true", help="replace an existing config")
    init.add_argument("--no-fetch", action="store_true", help="initialize from locally available commits")

    demo = commands.add_parser("demo", help="create a safe four-repository example project")
    demo.add_argument("path", nargs="?", default="project-brain-demo", help="new or empty target directory")

    commands.add_parser("doctor", help="check configuration and local capabilities")
    commands.add_parser("index", help="index current source snapshots")
    sync = commands.add_parser("sync", help="fetch every repo and create read-only remote snapshots")
    sync.add_argument("--no-fetch", action="store_true", help="snapshot locally available commits only")
    sync.add_argument("--branch", action="append", default=[], metavar="REPO=BRANCH", help="override one repository branch")
    refresh = commands.add_parser("refresh", help="sync repositories and regenerate project intelligence")
    refresh.add_argument("--no-fetch", action="store_true", help="refresh from locally available commits")
    refresh.add_argument("--branch", action="append", default=[], metavar="REPO=BRANCH", help="override one repository branch")
    commands.add_parser("map", help="regenerate deterministic project facts")

    search_parser = commands.add_parser("search", help="exact/regex search across repositories")
    search_parser.add_argument("query")
    search_parser.add_argument("--repo", action="append", default=[])
    search_parser.add_argument("--fixed", action="store_true", help="treat query as literal text")

    symbol = commands.add_parser("symbol", help="find symbol declarations with lexical fallback")
    symbol.add_argument("name")
    symbol.add_argument("--repo", action="append", default=[])

    trace = commands.add_parser("trace", help="find static callers and likely outbound calls")
    trace.add_argument("name")
    trace.add_argument("--repo", action="append", default=[])

    history = commands.add_parser("history", help="search Git change history")
    history.add_argument("query")
    history.add_argument("--repo", action="append", default=[])

    start = commands.add_parser("start", help="start a ticket investigation")
    start.add_argument("ticket")
    start.add_argument("--ticket-file")
    start.add_argument("--text")
    start.add_argument("--target", choices=("claude", "m365"), default="claude")
    start.add_argument("--copy", action=argparse.BooleanOptionalAction, default=None)
    start.add_argument("--no-sync", action="store_true", help="use the last source snapshots")
    start.add_argument("--branch", action="append", default=[], metavar="REPO=BRANCH", help="analyze a feature branch in one repository")
    start.add_argument("--json", action="store_true", help="print a stable machine-readable result")

    context = commands.add_parser("ctx", help="fulfil a CONTEXT_REQUEST")
    context.add_argument("ticket")
    source = context.add_mutually_exclusive_group()
    source.add_argument("--file")
    source.add_argument("--clipboard", action="store_true")
    context.add_argument("--target", choices=("claude", "m365"), default="claude")
    context.add_argument("--copy", action=argparse.BooleanOptionalAction, default=None)
    context.add_argument("--include-diff", action="store_true")
    context.add_argument("--json", action="store_true", help="print a stable machine-readable result")

    continue_command = commands.add_parser("continue", help="route a complete AI reply for an existing investigation")
    continue_command.add_argument("ticket")
    continue_source = continue_command.add_mutually_exclusive_group()
    continue_source.add_argument("--file")
    continue_source.add_argument("--clipboard", action="store_true")
    continue_command.add_argument("--target", choices=("claude", "m365"), default="claude")
    continue_command.add_argument("--copy", action=argparse.BooleanOptionalAction, default=None)
    continue_command.add_argument("--include-diff", action="store_true")
    continue_command.add_argument("--json", action="store_true", help="print a stable machine-readable result")

    preview = commands.add_parser("preview", help="classify and preview a complete AI reply")
    preview_source = preview.add_mutually_exclusive_group()
    preview_source.add_argument("--file")
    preview_source.add_argument("--clipboard", action="store_true")
    preview.add_argument("--ticket", help="existing investigation used for duplicate detection")
    preview.add_argument("--json", action="store_true", help="print the complete machine-readable plan")

    agent_kit = commands.add_parser("agent-kit", help="generate setup files for a persistent chat agent")
    agent_kit.add_argument("target", choices=("m365",))
    agent_kit.add_argument("--json", action="store_true", help="print generated paths as JSON")

    feedback = commands.add_parser("feedback", help="package implementation diffs and test results for AI review")
    feedback.add_argument("ticket")
    feedback.add_argument("--repo", action="append", default=[])
    notes = feedback.add_mutually_exclusive_group()
    notes.add_argument("--notes")
    notes.add_argument("--notes-file")
    feedback.add_argument("--test-command", default="")
    feedback.add_argument("--test-output-file")
    feedback.add_argument("--no-diff", action="store_true")
    feedback.add_argument("--target", choices=("claude", "m365"), default="claude")
    feedback.add_argument("--copy", action=argparse.BooleanOptionalAction, default=None)
    feedback.add_argument("--json", action="store_true", help="print a stable machine-readable result")

    status_command = commands.add_parser("status", help="show project health and investigation sessions")
    status_command.add_argument("--json", action="store_true", help="print machine-readable project status")

    ui = commands.add_parser("ui", help="open the local Project Brain investigation cockpit")
    ui.add_argument("--port", type=int, default=8765, help="loopback port; use 0 for any free port")
    ui.add_argument("--no-open", action="store_true", help="do not open the browser automatically")

    for name, help_text in (("next", "copy the next Claude chunk"), ("prev", "copy the previous Claude chunk")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("ticket")
    status = commands.add_parser("delivery-status", help="show current delivery chunk")
    status.add_argument("ticket")
    learn = commands.add_parser("learn", help="create a concise solved-ticket knowledge template")
    learn.add_argument("ticket")
    return parser


def _discover_repos(values: list[str]) -> list[Path]:
    roots = [Path(value).expanduser().resolve() for value in values] if values else [Path.cwd().resolve()]
    invalid = [path for path in roots if not path.is_dir()]
    if invalid:
        raise BrainError("Repository paths do not exist: " + ", ".join(map(str, invalid)))
    paths: set[Path] = set()
    ignored = {".git", ".runs", "state", "generated", "node_modules", "target", "build", ".venv"}
    for root in roots:
        for directory, dirs, files in os.walk(root):
            dirs[:] = [name for name in dirs if name not in ignored]
            if ".git" in dirs or ".git" in files or (Path(directory) / ".git").exists():
                paths.add(Path(directory).resolve())
        if (root / ".git").exists():
            paths.add(root)
    if not paths and len(roots) == 1:
        paths.add(roots[0])
    if not paths:
        raise BrainError("No repositories found; pass one or more repository paths")
    return sorted(paths)


def _print_sync(results: list[SyncResult]) -> None:
    for result in results:
        source = f"{result.ref or 'working tree'}@{(result.sha or 'unknown')[:12]}"
        suffix = f" — {result.warning}" if result.warning else ""
        print(f"{result.repo}: {result.status} ({source}){suffix}")


def _print_graph(results: list[GraphIndexResult]) -> None:
    for result in results:
        print(f"graph {result.repo}: {result.status}" + (f" ({result.detail})" if result.detail else ""))


def _refresh_all(
    settings,
    *,
    fetch: bool,
    branch_overrides: dict[str, str] | None = None,
) -> tuple[list[SyncResult], list[GraphIndexResult]]:
    results = sync_repositories(settings, fetch=fetch, branch_overrides=branch_overrides)
    snapshot_indexes(settings, changed_only=True)
    generate_map(settings)
    generate_relationship_map(settings)
    return results, index_graph(settings, defer_lazy=True)


def _init(args: argparse.Namespace) -> int:
    if args.config:
        config = Path(args.config).expanduser().resolve()
    elif len(args.repos) == 1:
        config = Path(args.repos[0]).expanduser().resolve() / "brain.toml"
    else:
        config = Path.cwd() / "brain.toml"
    if config.exists() and not args.force:
        raise BrainError(f"Config already exists: {config} (use --force to replace it)")
    root = config.parent
    repos = _discover_repos(args.repos)
    basenames = [path.name for path in repos]
    names: set[str] = set()
    rows = [
        "[project]",
        f"name = {json.dumps(args.name or root.name)}",
        'runs_dir = ".runs"',
        'state_dir = "state"',
        'generated_dir = "generated"',
        "",
        "[search]",
        "max_results = 100",
        "",
        "[context]",
        "source_window_lines = 150",
        "full_file_lines = 350",
        "soft_target_chars = 500000",
        "",
        "[delivery]",
        "clipboard_chunk_chars = 180000",
        "",
        "[graph]",
        'mode = "lazy"',
        "",
        "[sources]",
        'branch_priority = ["develop", "development"]',
        "",
        "[knowledge]",
        'path = "knowledge"',
    ]
    for path in repos:
        name = path.name
        if basenames.count(name) > 1:
            try:
                name = "-".join(path.relative_to(root).parts)
            except ValueError:
                name = f"{path.parent.name}-{path.name}"
        candidate = name
        counter = 2
        while name in names:
            name = f"{candidate}-{counter}"
            counter += 1
        names.add(name)
        try:
            configured_path = str(path.relative_to(root))
        except ValueError:
            configured_path = str(path)
        rows.extend([
            "",
            "[[repositories]]",
            f"name = {json.dumps(name)}",
            f"path = {json.dumps(configured_path)}",
            'description = ""',
            "tags = []",
        ])
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("\n".join(rows) + "\n", encoding="utf-8")
    knowledge = root / "knowledge"
    (knowledge / "flows").mkdir(parents=True, exist_ok=True)
    (knowledge / "tickets").mkdir(parents=True, exist_ok=True)
    for path, content in (
        (knowledge / "PROJECT_MAP.md", "# Project Map\n\nRecord domain ownership, main flows, and non-obvious architecture here.\n"),
        (knowledge / "glossary.md", "# Glossary\n\nMap ticket/business language to source-code vocabulary here.\n"),
    ):
        if not path.exists():
            path.write_text(content, encoding="utf-8")
    print(f"Created {config}")
    settings = load_settings(config)
    results, graphs = _refresh_all(settings, fetch=not args.no_fetch)
    print(f"Configured {len(repos)} repositories and built project intelligence.")
    _print_sync(results)
    _print_graph(graphs)
    print("Ready. Start a ticket with: brain start TICKET --ticket-file ticket.md")
    return 0


def _ticket_text(args: argparse.Namespace) -> str:
    if args.text:
        return args.text
    if args.ticket_file:
        return Path(args.ticket_file).expanduser().read_text(encoding="utf-8")
    if not sys.stdin.isatty():
        value = sys.stdin.read()
        if value.strip():
            return value
    return f"# {args.ticket}\n\nPaste the ticket description here before sending this context to the AI."


def _demo(args: argparse.Namespace) -> int:
    from .demo import create_demo

    config = create_demo(Path(args.path))
    settings = load_settings(config)
    results, graphs = _refresh_all(settings, fetch=False)
    print(f"Created Project Brain demo at {settings.root}")
    _print_sync(results)
    _print_graph(graphs)
    print("\nTry it:")
    print(f"  cd {settings.root}")
    print("  brain ui")
    print("\nOr use the CLI:")
    print("  brain start DEMO-101 --ticket-file ticket.md")
    return 0


def _request_text(args: argparse.Namespace) -> str:
    if args.file:
        return Path(args.file).expanduser().read_text(encoding="utf-8")
    if args.clipboard:
        return clipboard_read()
    if not sys.stdin.isatty():
        value = sys.stdin.read()
        if value.strip():
            return value
    raise BrainError("Provide a request using --file, --clipboard, or stdin")


def _read_optional(value: str | None, path: str | None) -> str:
    if path:
        return Path(path).expanduser().read_text(encoding="utf-8")
    return value or ""


def _print_hits(hits: list, empty: str = "No matches.") -> None:
    if not hits:
        print(empty)
        return
    current = None
    for hit in hits:
        if hit.repo != current:
            current = hit.repo
            print(f"\n[{current}]")
        print(f"{hit.path}:{hit.line}: {hit.text.strip()}")


def execute(args: argparse.Namespace) -> int:
    if args.command == "init":
        return _init(args)
    if args.command == "demo":
        return _demo(args)
    settings = load_settings(args.config)
    if args.command == "doctor":
        report, ok = doctor(settings)
        print(report, end="")
        return 0 if ok else 1
    if args.command == "index":
        _, updated = snapshot_indexes(settings)
        print("Snapshot updated: " + ", ".join(updated))
        _print_graph(index_graph(settings, changed_only=False))
        return 0
    if args.command == "sync":
        _print_sync(
            sync_repositories(
                settings,
                fetch=not args.no_fetch,
                branch_overrides=parse_branch_overrides(settings, args.branch),
            )
        )
        return 0
    if args.command == "refresh":
        results, graphs = _refresh_all(
            settings,
            fetch=not args.no_fetch,
            branch_overrides=parse_branch_overrides(settings, args.branch),
        )
        _print_sync(results)
        _print_graph(graphs)
        print(f"Generated {settings.generated_dir / 'PROJECT_FACTS.md'}")
        print(f"Generated {settings.generated_dir / 'PROJECT_RELATIONSHIPS.md'}")
        return 0
    if args.command == "map":
        generate_map(settings)
        generate_relationship_map(settings)
        print(settings.generated_dir / "PROJECT_FACTS.md")
        print(settings.generated_dir / "PROJECT_RELATIONSHIPS.md")
        return 0
    if args.command == "search":
        _print_hits(search(settings, args.query, args.repo, fixed=args.fixed))
        return 0
    if args.command == "symbol":
        _print_hits(symbol_hits(settings, args.name, args.repo), "No symbol evidence found.")
        return 0
    if args.command == "trace":
        hits, relationships = trace_symbol(settings, args.name, args.repo)
        _print_hits(hits, "No call sites found.")
        if relationships:
            print("\n[relationships]")
            print("\n".join(relationships))
        print("\nStatic trace is heuristic; runtime DI/reflection requires logs or tests.")
        return 0
    if args.command == "history":
        found = False
        for repo in settings.repos(args.repo):
            result = git_history(repo, args.query)
            if result:
                found = True
                print(f"\n[{repo.name}]\n{result}")
        if not found:
            print("No matching history.")
        return 0
    if args.command == "start":
        synced: list[SyncResult] = []
        graphs: list[GraphIndexResult] = []
        if args.no_sync and args.branch:
            raise BrainError("--branch requires sync; remove --no-sync")
        if not args.no_sync:
            synced, graphs = _refresh_all(
                settings,
                fetch=True,
                branch_overrides=parse_branch_overrides(settings, args.branch),
            )
            if not args.json:
                _print_sync(synced)
                _print_graph(graphs)
        elif not (settings.generated_dir / "PROJECT_FACTS.md").exists():
            generate_map(settings)
            generate_relationship_map(settings)
        content, path = start_session(settings, args.ticket, _ticket_text(args))
        copy = args.copy if args.copy is not None else args.target == "claude"
        parts, current = deliver(settings, args.ticket, content, args.target, copy=copy)
        if args.json:
            print(json.dumps({
                "ticket": args.ticket,
                "path": str(path),
                "delivery": {"current": current, "total": len(parts), "parts": [str(part) for part in parts], "copied": copy},
                "sync": [asdict(item) for item in synced],
                "graph": [asdict(item) for item in graphs],
            }, indent=2))
        else:
            print(path)
            if args.target == "m365":
                print(f"M365 handoff: {parts[0]}")
            print(f"Delivery: {current}/{len(parts)}" + (" copied" if copy else ""))
        return 0
    if args.command == "ctx":
        content, path, number = create_context(settings, args.ticket, _request_text(args), args.include_diff)
        copy = args.copy if args.copy is not None else args.target == "claude"
        parts, current = deliver(settings, args.ticket, content, args.target, copy=copy)
        if args.json:
            print(json.dumps({
                "ticket": args.ticket,
                "request": number,
                "path": str(path),
                "delivery": {"current": current, "total": len(parts), "parts": [str(part) for part in parts], "copied": copy},
            }, indent=2))
        else:
            print(path)
            if args.target == "m365":
                print(f"M365 handoff: {parts[0]}")
            print(f"Request: {number:03d}; delivery: {current}/{len(parts)}" + (" copied" if copy else ""))
        return 0
    if args.command == "continue":
        text = _request_text(args)
        preview = response_preview(text, settings, args.ticket)
        kind = preview["kind"]
        if kind == "conversation":
            result = {"ticket": args.ticket, "kind": kind, "message": preview["message"]}
            print(json.dumps(result, indent=2) if args.json else preview["message"])
            return 0
        if kind == "final_solution":
            path = archive_final_solution(settings, args.ticket, text)
            if args.target == "m365":
                deliver(settings, args.ticket, text, args.target, copy=False)
            result = {"ticket": args.ticket, "kind": kind, "path": str(path)}
            print(json.dumps(result, indent=2) if args.json else f"Ready to implement: {path}")
            return 0
        content, path, number = create_context(settings, args.ticket, text, args.include_diff)
        copy = args.copy if args.copy is not None else args.target == "claude"
        parts, current = deliver(settings, args.ticket, content, args.target, copy=copy)
        result = {
            "ticket": args.ticket,
            "kind": kind,
            "request": number,
            "path": str(path),
            "delivery": {"current": current, "total": len(parts), "parts": [str(part) for part in parts], "copied": copy},
        }
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(path)
            if args.target == "m365":
                print(f"M365 handoff: {parts[0]}")
            print(f"Request: {number:03d}; delivery: {current}/{len(parts)}" + (" copied" if copy else ""))
        return 0
    if args.command == "preview":
        text = _request_text(args)
        try:
            plan = response_preview(text, settings, args.ticket)
        except BrainError as exc:
            if args.json:
                print(json.dumps({"valid": False, "error": str(exc), "repair_prompt": request_repair_prompt(str(exc))}, indent=2))
                return 2
            raise
        if args.json:
            print(json.dumps(plan, indent=2, ensure_ascii=False))
        elif plan["kind"] == "conversation":
            print(plan["message"])
        elif plan["kind"] == "final_solution":
            print("Ready to implement; no repository retrieval required.")
        else:
            print(f"Valid CONTEXT_REQUEST v{plan['protocol_version']}: {plan['operation_count']} operations")
            print(f"Objective: {plan['objective']}")
            for action in plan["actions"]:
                scope = ", ".join(action["repos"]) or "all repositories"
                print(f"- {action['kind']}: {action['value']} ({scope})")
        return 0
    if args.command == "agent-kit":
        kit = create_m365_agent_kit(settings)
        if args.json:
            print(json.dumps({key: value for key, value in kit.items() if key.endswith("path") or key == "directory"}, indent=2))
        else:
            print(kit["setup_path"])
            print(kit["instructions_path"])
            print(kit["knowledge_path"])
        return 0
    if args.command == "feedback":
        notes = _read_optional(args.notes, args.notes_file)
        test_output = _read_optional(None, args.test_output_file)
        content, path, number = create_feedback(
            settings,
            args.ticket,
            notes=notes,
            test_command=args.test_command,
            test_output=test_output,
            repos=args.repo,
            include_diff=not args.no_diff,
        )
        copy = args.copy if args.copy is not None else args.target == "claude"
        parts, current = deliver(settings, args.ticket, content, args.target, copy=copy)
        if args.json:
            print(json.dumps({
                "ticket": args.ticket,
                "feedback": number,
                "path": str(path),
                "delivery": {"current": current, "total": len(parts), "parts": [str(part) for part in parts], "copied": copy},
            }, indent=2))
        else:
            print(path)
            if args.target == "m365":
                print(f"M365 handoff: {parts[0]}")
            print(f"Feedback: {number:03d}; delivery: {current}/{len(parts)}" + (" copied" if copy else ""))
        return 0
    if args.command == "status":
        from .ui import project_status

        status = project_status(settings)
        if args.json:
            print(json.dumps(status, indent=2, ensure_ascii=False))
        else:
            print(f"{status['project']['name']}: {status['summary']['current']}/{status['summary']['repositories']} repositories current")
            for repo in status["repositories"]:
                print(f"- {repo['name']}: {repo['status']} {repo['sha'] or 'unknown'}")
            print(f"Investigations: {len(status['sessions'])}")
        return 0
    if args.command == "ui":
        if not 0 <= args.port <= 65535:
            raise BrainError("--port must be between 0 and 65535")
        from .ui import serve_ui

        serve_ui(settings, port=args.port, open_browser=not args.no_open)
        return 0
    if args.command in {"next", "prev"}:
        path, current, total = move_delivery(settings, args.ticket, 1 if args.command == "next" else -1)
        print(f"Copied {current}/{total}: {path}")
        return 0
    if args.command == "delivery-status":
        delivery = session_state(settings, args.ticket).get("delivery") or {}
        parts = delivery.get("parts") or []
        print(f"{delivery.get('current', 0)}/{len(parts)}")
        return 0
    if args.command == "learn":
        print(create_learning_template(settings, args.ticket))
        return 0
    raise BrainError(f"Unknown command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    try:
        return execute(_parser().parse_args(argv))
    except (BrainError, OSError) as exc:
        print(f"brain: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

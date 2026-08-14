from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .core import (
    BrainError,
    clipboard_read,
    create_context,
    create_learning_template,
    deliver,
    doctor,
    generate_map,
    git_history,
    load_settings,
    move_delivery,
    search,
    session_state,
    snapshot_indexes,
    start_session,
    symbol_hits,
    trace_symbol,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brain", description="Read-only multi-repository context for chat AIs")
    parser.add_argument("-c", "--config", help="brain.toml/config.yml path")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="create a portable Project Brain workspace")
    init.add_argument("repos", nargs="*", help="repository paths (defaults to current repo or child repos)")
    init.add_argument("--name", help="project name")
    init.add_argument("--force", action="store_true", help="replace an existing config")

    commands.add_parser("doctor", help="check configuration and local capabilities")
    commands.add_parser("index", help="record repository HEAD snapshots")
    commands.add_parser("refresh", help="snapshot changed repositories and regenerate facts")
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

    context = commands.add_parser("ctx", help="fulfil a CONTEXT_REQUEST")
    context.add_argument("ticket")
    source = context.add_mutually_exclusive_group()
    source.add_argument("--file")
    source.add_argument("--clipboard", action="store_true")
    context.add_argument("--target", choices=("claude", "m365"), default="claude")
    context.add_argument("--copy", action=argparse.BooleanOptionalAction, default=None)
    context.add_argument("--include-diff", action="store_true")

    for name, help_text in (("next", "copy the next Claude chunk"), ("prev", "copy the previous Claude chunk")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("ticket")
    status = commands.add_parser("delivery-status", help="show current delivery chunk")
    status.add_argument("ticket")
    learn = commands.add_parser("learn", help="create a concise solved-ticket knowledge template")
    learn.add_argument("ticket")
    return parser


def _discover_repos(values: list[str]) -> list[Path]:
    if values:
        paths = [Path(value).expanduser().resolve() for value in values]
    else:
        current = Path.cwd().resolve()
        if (current / ".git").exists():
            paths = [current]
        else:
            paths = sorted(path.parent for path in current.glob("*/.git"))
    invalid = [path for path in paths if not path.is_dir()]
    if invalid:
        raise BrainError("Repository paths do not exist: " + ", ".join(map(str, invalid)))
    if not paths:
        raise BrainError("No repositories found; pass one or more repository paths")
    return paths


def _init(args: argparse.Namespace) -> int:
    config = Path(args.config).expanduser().resolve() if args.config else Path.cwd() / "brain.toml"
    if config.exists() and not args.force:
        raise BrainError(f"Config already exists: {config} (use --force to replace it)")
    root = config.parent
    repos = _discover_repos(args.repos)
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
        "[knowledge]",
        'path = "knowledge"',
    ]
    for path in repos:
        name = path.name
        if name in names:
            raise BrainError(f"Repository names collide: {name}")
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
    print(f"Configured {len(repos)} repositories. Next: brain doctor && brain refresh")
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
    settings = load_settings(args.config)
    if args.command == "doctor":
        report, ok = doctor(settings)
        print(report, end="")
        return 0 if ok else 1
    if args.command == "index":
        _, updated = snapshot_indexes(settings)
        print("Snapshot updated: " + ", ".join(updated))
        return 0
    if args.command == "refresh":
        _, updated = snapshot_indexes(settings, changed_only=True)
        path = settings.generated_dir / "PROJECT_FACTS.md"
        generate_map(settings)
        print("Changed repositories: " + (", ".join(updated) if updated else "none"))
        print(f"Generated {path}")
        return 0
    if args.command == "map":
        generate_map(settings)
        print(settings.generated_dir / "PROJECT_FACTS.md")
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
        if not (settings.generated_dir / "PROJECT_FACTS.md").exists():
            generate_map(settings)
        content, path = start_session(settings, args.ticket, _ticket_text(args))
        copy = args.copy if args.copy is not None else args.target == "claude"
        parts, current = deliver(settings, args.ticket, content, args.target, copy=copy)
        print(path)
        print(f"Delivery: {current}/{len(parts)}" + (" copied" if copy else ""))
        return 0
    if args.command == "ctx":
        content, path, number = create_context(settings, args.ticket, _request_text(args), args.include_diff)
        copy = args.copy if args.copy is not None else args.target == "claude"
        parts, current = deliver(settings, args.ticket, content, args.target, copy=copy)
        print(path)
        print(f"Request: {number:03d}; delivery: {current}/{len(parts)}" + (" copied" if copy else ""))
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

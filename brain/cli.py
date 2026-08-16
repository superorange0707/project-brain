from __future__ import annotations

import argparse
import json
import os
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
from .relations import generate_relationship_map
from .sync import SyncResult, sync_repositories
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

    commands.add_parser("doctor", help="check configuration and local capabilities")
    commands.add_parser("index", help="index current source snapshots")
    sync = commands.add_parser("sync", help="fetch every repo and create read-only remote snapshots")
    sync.add_argument("--no-fetch", action="store_true", help="snapshot locally available commits only")
    refresh = commands.add_parser("refresh", help="sync repositories and regenerate project intelligence")
    refresh.add_argument("--no-fetch", action="store_true", help="refresh from locally available commits")
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


def _refresh_all(settings, *, fetch: bool) -> tuple[list[SyncResult], list[GraphIndexResult]]:
    results = sync_repositories(settings, fetch=fetch)
    snapshot_indexes(settings, changed_only=True)
    generate_map(settings)
    generate_relationship_map(settings)
    return results, index_graph(settings)


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
        _print_graph(index_graph(settings, changed_only=False))
        return 0
    if args.command == "sync":
        _print_sync(sync_repositories(settings, fetch=not args.no_fetch))
        return 0
    if args.command == "refresh":
        results, graphs = _refresh_all(settings, fetch=not args.no_fetch)
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
        if not args.no_sync:
            synced, graphs = _refresh_all(settings, fetch=True)
            _print_sync(synced)
            _print_graph(graphs)
        elif not (settings.generated_dir / "PROJECT_FACTS.md").exists():
            generate_map(settings)
            generate_relationship_map(settings)
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

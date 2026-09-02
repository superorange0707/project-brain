from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .agent import archive_final_solution, create_m365_agent_kit, response_preview
from .core import (
    BrainError,
    add_external_evidence,
    clipboard_read,
    create_feedback,
    create_context,
    create_learning_template,
    deliver,
    discover_and_configure_repositories,
    discover_git_repositories,
    doctor,
    generate_map,
    git_history,
    load_settings,
    move_delivery,
    request_repair_prompt,
    search,
    path_hits,
    session_dir,
    session_state,
    snapshot_indexes,
    start_session,
    symbol_hits,
    trace_symbol,
)
from .relations import generate_relationship_map
from .sync import SyncResult, parse_branch_overrides, sync_repositories
from .graph import GraphIndexResult, index_graph
from .experience import build_experience_index, evaluate_sessions, render_similar_cases, similar_cases
from .platforms import logical_path


MAX_CLI_INPUT_BYTES = 4 * 1024 * 1024


def _checked_input(value: str, label: str) -> str:
    if len(value.encode("utf-8")) > MAX_CLI_INPUT_BYTES:
        raise BrainError(f"{label} exceeds the {MAX_CLI_INPUT_BYTES}-byte input limit")
    return value


def _read_input_file(value: str, label: str) -> str:
    path = Path(value).expanduser()
    with path.open("rb") as source:
        raw = source.read(MAX_CLI_INPUT_BYTES + 1)
    if len(raw) > MAX_CLI_INPUT_BYTES:
        raise BrainError(f"{label} exceeds the {MAX_CLI_INPUT_BYTES}-byte input limit")
    return raw.decode("utf-8")


def _read_stdin() -> str:
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    value = stream.read(MAX_CLI_INPUT_BYTES + 1)
    if isinstance(value, bytes):
        if len(value) > MAX_CLI_INPUT_BYTES:
            raise BrainError(f"stdin exceeds the {MAX_CLI_INPUT_BYTES}-byte input limit")
        return value.decode("utf-8")
    return _checked_input(value, "stdin")


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
    index = commands.add_parser("index", help="build or inspect local index generations")
    index.add_argument("action", nargs="?", choices=("build", "status", "rebuild"), default="build")
    index.add_argument("--backend", choices=("lexical", "semantic", "all"), default="lexical")
    sync = commands.add_parser("sync", help="fetch every repo and create read-only remote snapshots")
    sync.add_argument("--no-fetch", action="store_true", help="snapshot locally available commits only")
    sync.add_argument("--branch", action="append", default=[], metavar="REPO=BRANCH", help="override one repository branch")
    refresh = commands.add_parser("refresh", help="sync repositories and regenerate project intelligence")
    refresh.add_argument("--no-fetch", action="store_true", help="refresh from locally available commits")
    refresh.add_argument("--no-discover", action="store_true", help="skip the newly cloned repository check")
    refresh.add_argument("--branch", action="append", default=[], metavar="REPO=BRANCH", help="override one repository branch")
    commands.add_parser("map", help="regenerate deterministic project facts")

    search_parser = commands.add_parser("search", help="exact/regex search across repositories")
    search_parser.add_argument("query")
    search_parser.add_argument("--repo", action="append", default=[])
    search_parser.add_argument("--fixed", action="store_true", help="treat query as literal text")
    search_parser.add_argument("--explain", action="store_true", help="show backend selection and deterministic query plan")

    paths = commands.add_parser("paths", help="find verified repository-relative file paths")
    paths.add_argument("query")
    paths.add_argument("--repo", action="append", default=[])
    paths.add_argument("--explain", action="store_true", help="show backend selection and deterministic query plan")

    symbol = commands.add_parser("symbol", help="find symbol declarations with lexical fallback")
    symbol.add_argument("name")
    symbol.add_argument("--repo", action="append", default=[])

    trace = commands.add_parser("trace", help="find static callers and likely outbound calls")
    trace.add_argument("name")
    trace.add_argument("--repo", action="append", default=[])

    history = commands.add_parser("history", help="search Git change history")
    history.add_argument("query")
    history.add_argument("--repo", action="append", default=[])

    experience = commands.add_parser("experience", help="inspect local ticket-labelled Git experience")
    experience.add_argument("query", nargs="?", help="ticket description or implementation concept")
    experience.add_argument("--rebuild", action="store_true", help="rescan configured Git history")
    experience.add_argument("--patches", action="store_true", help="include bounded historical patch excerpts")
    experience.add_argument("--json", action="store_true", help="print index or evaluation metadata")
    evaluate = commands.add_parser("evaluate", help="compare retrieval with historical commits or a local golden suite")
    evaluate.add_argument("--golden", help="local JSON/YAML hand-labelled replay suite")
    evaluate.add_argument("--split", choices=("calibration", "validation", "holdout"))
    evaluate.add_argument("--limit", type=int, default=20, help="maximum ranked files used for golden recall")
    benchmark = commands.add_parser("benchmark", help="summarize recorded local index and retrieval latency")
    benchmark.add_argument("--json", action="store_true", help="print machine-readable p50/p95 metrics")
    benchmark.add_argument("--machine", action="store_true", help="record a non-identifying local machine profile for later comparison")

    explain = commands.add_parser("explain", help="compile an investigation/context request without searching")
    explain.add_argument("ticket", nargs="?", help="optional ticket label for command ergonomics")
    explain_source = explain.add_mutually_exclusive_group(required=True)
    explain_source.add_argument("--file")
    explain_source.add_argument("--clipboard", action="store_true")
    explain.add_argument("--json", action="store_true")

    edition = commands.add_parser("edition", help="inspect or switch capability edition")
    edition.add_argument("action", choices=("current", "set"))
    edition.add_argument("value", nargs="?", choices=("core", "semantic", "precision"))
    commands.add_parser("capabilities", help="show locally available retrieval capabilities")

    model = commands.add_parser("model", help="manage auditable local model packs")
    model.add_argument("action", choices=("list", "install", "verify", "benchmark", "autotune", "remove", "status"))
    model.add_argument("value", nargs="?")
    model.add_argument("--sha256", help="required SHA-256 for an approved HTTPS model-pack release")
    model.add_argument("--samples", type=int, default=3, help="benchmark samples per public synthetic workload (1-10)")
    model.add_argument("--latency-budget-ms", type=int, default=3000, help="Precision candidate-pool p95 budget for autotune")

    commands.add_parser("freshness", help="compare pinned source snapshots and index state")
    commands.add_parser("storage", help="show local Brain storage usage")
    gc = commands.add_parser("gc", help="remove unpinned old index generations")
    gc.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True)
    gc.add_argument("--keep-recent", type=int, default=2)
    watch = commands.add_parser("watch", help="check freshness and refresh once the workspace is idle")
    watch.add_argument("--once", action="store_true", help="run one freshness check and exit")
    watch.add_argument("--interval", type=int, help="seconds between freshness checks")

    start = commands.add_parser("start", help="start a ticket investigation")
    start.add_argument("ticket")
    start.add_argument("--ticket-file")
    start.add_argument("--text")
    start.add_argument("--target", choices=("claude", "m365"), default="claude")
    start.add_argument("--copy", action=argparse.BooleanOptionalAction, default=None)
    start.add_argument("--no-sync", action="store_true", help="use the last source snapshots")
    start.add_argument("--no-discover", action="store_true", help="skip the newly cloned repository check during sync")
    start.add_argument("--branch", action="append", default=[], metavar="REPO=BRANCH", help="analyze a feature branch in one repository")
    start.add_argument("--json", action="store_true", help="print a stable machine-readable result")

    context = commands.add_parser("ctx", help="fulfil an investigation/context request")
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

    evidence = commands.add_parser("evidence", help="add a user-supplied document, log, note, or runtime artifact")
    evidence.add_argument("ticket")
    evidence.add_argument("file")
    evidence.add_argument("--kind", choices=("document", "log", "note", "runtime"), default="document")
    evidence.add_argument("--target", choices=("claude", "m365"), default="claude")
    evidence.add_argument("--copy", action=argparse.BooleanOptionalAction, default=None)
    evidence.add_argument("--json", action="store_true", help="print a stable machine-readable result")

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
    paths = set(discover_git_repositories(roots))
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


def _workspace_root(current: Path, repos: list[Path]) -> Path:
    """Choose a Brain-owned workspace outside every containing target repo."""
    containing = [path for path in repos if current == path or current.is_relative_to(path)]
    if not containing:
        return current
    outermost = min(containing, key=lambda path: len(path.parts))
    if outermost.parent == outermost:
        raise BrainError("Cannot create a Project Brain workspace outside a filesystem-root repository")
    return outermost.parent


def _refresh_all(
    settings,
    *,
    fetch: bool,
    branch_values: list[str] | None = None,
    discover: bool = True,
) -> tuple[list, list[SyncResult], list[GraphIndexResult]]:
    from .ops import refresh_brain

    outcome = refresh_brain(settings, fetch=fetch, branch_values=branch_values, discover=discover)
    return outcome.additions, outcome.sync, outcome.graph


def _init(args: argparse.Namespace) -> int:
    repos = _discover_repos(args.repos)
    if args.config:
        config = Path(args.config).expanduser().resolve()
    else:
        current = Path.cwd().resolve()
        root = _workspace_root(current, repos)
        config = root / "brain.toml"
    if any(config.is_relative_to(path) for path in repos):
        raise BrainError("Project Brain config and state must be outside target repositories")
    if config.exists() and not args.force:
        raise BrainError(f"Config already exists: {config} (use --force to replace it)")
    root = config.parent
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
        "path_result_limit = 12",
        "candidate_limit = 500",
        "",
        "[context]",
        "source_window_lines = 150",
        "full_file_lines = 350",
        "soft_target_chars = 120000",
        "hard_context_chars = 180000",
        "hydrate_limit = 18",
        "max_regions_per_file = 2",
        "max_regions_per_repo = 8",
        "",
        "[retrieval]",
        "max_concurrent_investigations = 2",
        "repo_workers = 4",
        "initial_repo_limit = 6",
        "widen_repo_limit = 16",
        "max_effective_operations = 15",
        "max_backend_operations = 200",
        "pre_rerank_candidate_limit = 200",
        "semantic_shard_workers = 4",
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
        "",
        "[storage]",
        "max_state_gb = 200",
        "minimum_free_disk_gb = 5",
        "",
        "[models]",
        "# Approved internal HTTPS hosts may be added here for one-time pack installation.",
        "approved_install_hosts = []",
        "# Optional PEM bundle added to model-download system trust. SSL_CERT_FILE also works.",
        "# ca_bundle = \"/path/to/enterprise-ca.pem\"",
        "",
        "[experience]",
        "enabled = true",
        'ticket_pattern = "(?<![A-Z0-9])([A-Z][A-Z0-9]+-[0-9]+)(?![A-Z0-9])"',
        "commit_limit = 1000",
        "similar_cases = 5",
        "patch_chars = 0",
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
            configured_path = logical_path(path.relative_to(root))
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
    _, results, graphs = _refresh_all(settings, fetch=not args.no_fetch, discover=False)
    print(f"Configured {len(repos)} repositories and built project intelligence.")
    _print_sync(results)
    _print_graph(graphs)
    print("Ready. Start a ticket with: brain start TICKET --ticket-file ticket.md")
    return 0


def _ticket_text(args: argparse.Namespace) -> str:
    if args.text:
        return _checked_input(args.text, "ticket text")
    if args.ticket_file:
        return _read_input_file(args.ticket_file, "ticket file")
    if not sys.stdin.isatty():
        value = _read_stdin()
        if value.strip():
            return value
    return f"# {args.ticket}\n\nPaste the ticket description here before sending this context to the AI."


def _demo(args: argparse.Namespace) -> int:
    from .demo import create_demo

    config = create_demo(Path(args.path))
    settings = load_settings(config)
    _, results, graphs = _refresh_all(settings, fetch=False, discover=False)
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
        return _read_input_file(args.file, "request file")
    if args.clipboard:
        return _checked_input(clipboard_read(), "clipboard request")
    if not sys.stdin.isatty():
        value = _read_stdin()
        if value.strip():
            return value
    raise BrainError("Provide a request using --file, --clipboard, or stdin")


def _read_optional(value: str | None, path: str | None) -> str:
    if path:
        return _read_input_file(path, "feedback input file")
    return _checked_input(value or "", "feedback input")


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
        if args.action == "status":
            from .ops import freshness

            print(json.dumps(freshness(settings), indent=2))
            return 0
        if args.action == "rebuild" and args.backend in {"semantic", "all"}:
            from .semantic import build_semantic_index
            from .atlas import build_atlas
            from .core import load_index_state
            from .locks import workspace_operation

            with workspace_operation(settings):
                atlas_payload = build_atlas(settings, load_index_state(settings))
                settings.atlas_cards = atlas_payload["cards"]
                try:
                    built = build_semantic_index(settings)
                finally:
                    settings.atlas_cards = None
                from .catalog import publish_current_components

                publish_current_components(settings, atlas_payload=atlas_payload)
            print(json.dumps(built, indent=2))
            if args.backend == "semantic":
                return 0
        from .locks import workspace_operation

        with workspace_operation(settings):
            _, updated = snapshot_indexes(settings)
            graphs = index_graph(settings, changed_only=False)
        print("Search index updated: " + ", ".join(updated))
        _print_graph(graphs)
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
        from .ops import format_refresh_progress, refresh_brain

        last_phase = ""
        last_second = -1

        def report_progress(event: dict[str, object]) -> None:
            nonlocal last_phase, last_second
            phase = str(event.get("phase") or "")
            second = int(event.get("elapsed_ms") or 0) // 1000
            if phase != last_phase or second != last_second or phase in {"complete", "semantic_reuse"}:
                print(format_refresh_progress(event))
                last_phase, last_second = phase, second

        outcome = refresh_brain(
            settings,
            fetch=not args.no_fetch,
            branch_values=args.branch,
            discover=not args.no_discover,
            progress=report_progress,
        )
        _print_sync(outcome.sync)
        _print_graph(outcome.graph)
        print(f"Generated {settings.generated_dir / 'PROJECT_FACTS.md'}")
        print(f"Generated {settings.generated_dir / 'PROJECT_RELATIONSHIPS.md'}")
        print(f"Generated {settings.generated_dir / 'EXPERIENCE_REPORT.md'}")
        if outcome.semantic["required"]:
            print("Semantic index: " + json.dumps(outcome.semantic, sort_keys=True))
        return 0
    if args.command == "map":
        generate_map(settings)
        generate_relationship_map(settings)
        print(settings.generated_dir / "PROJECT_FACTS.md")
        print(settings.generated_dir / "PROJECT_RELATIONSHIPS.md")
        return 0
    if args.command == "search":
        if args.explain:
            from .retrieval import compile_request, explain_plan

            print(json.dumps(explain_plan(compile_request({"objective": args.query, "searches": [{"query": args.query, "repos": args.repo}]})), indent=2))
        _print_hits(search(settings, args.query, args.repo, fixed=args.fixed))
        return 0
    if args.command == "paths":
        if args.explain:
            from .retrieval import compile_request, explain_plan

            print(json.dumps(explain_plan(compile_request({"objective": args.query, "paths": [{"query": args.query, "repos": args.repo}]})), indent=2))
        _print_hits(path_hits(settings, args.query, args.repo), "No matching repository paths.")
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
    if args.command == "benchmark":
        from .metrics import benchmark_report, write_machine_profile

        report = benchmark_report(settings)
        if args.machine:
            profile = write_machine_profile(settings)
            if args.json:
                print(json.dumps({"machine": profile, "benchmark": report}, indent=2))
            else:
                print("Recorded local machine profile (no hostname, serial number, or paths):")
                print(json.dumps(profile, indent=2))
            return 0
        if args.json:
            print(json.dumps(report, indent=2))
            return 0
        if not report["samples"]:
            print("No benchmark samples yet. Run brain index or complete a brain ctx request.")
            return 0
        print(f"Benchmark samples: {report['samples']}")
        for event, values in report["events"].items():
            print(f"\n[{event}] {values['samples']} samples")
            for metric, timing in values["timings"].items():
                print(f"{metric}: p50 {timing['p50']:.3f} ms, p95 {timing['p95']:.3f} ms, max {timing['max']:.3f} ms")
        return 0
    if args.command == "explain":
        from .retrieval.planner import route_repositories

        plan = request_preview(_request_text(args), settings)
        routed = route_repositories(
            settings.repositories,
            plan["request"],
            limit=min(settings.initial_repo_limit, len(settings.repositories)),
        )
        report = {
            **plan["planner"],
            "repo_routing": {"initial_scope": routed, "workspace_repositories": len(settings.repositories)},
            "physical_backend_budget": settings.max_backend_operations,
            "pre_rerank_candidate_limit": settings.pre_rerank_candidate_limit,
            "request_signature": plan["signature"],
        }
        print(json.dumps(report, indent=2) if args.json else "\n".join(f"L{item['tier']} {item['kind']}: {item['value']} — {item['reason']}" for item in report["operations"]))
        return 0
    if args.command == "edition":
        from .editions import current_edition
        from .ops import change_edition

        if args.action == "set":
            if not args.value:
                raise BrainError("brain edition set requires core, semantic, or precision")
            print(change_edition(settings, args.value)["edition"])
        else:
            print(current_edition(settings))
        return 0
    if args.command == "capabilities":
        from .editions import capabilities

        print(json.dumps(capabilities(settings), indent=2))
        return 0
    if args.command == "model":
        from .ops import model_operation

        result = model_operation(
            settings,
            args.action,
            args.value,
            samples=args.samples,
            latency_budget_ms=args.latency_budget_ms,
            expected_sha256=args.sha256,
        )
        if args.action == "remove":
            print(f"Removed local model pack {result['pack_id']}")
        else:
            print(json.dumps(result, indent=2))
        return 0
    if args.command in {"freshness", "storage", "gc"}:
        from .ops import freshness, gc, storage

        if args.command == "freshness":
            print(json.dumps(freshness(settings), indent=2))
        elif args.command == "storage":
            print(json.dumps(storage(settings), indent=2))
        else:
            print(json.dumps(gc(settings, dry_run=args.dry_run, keep_recent=max(1, args.keep_recent)), indent=2))
        return 0
    if args.command == "watch":
        from .auto_refresh import AutoRefreshService

        interval = max(10, args.interval or settings.watch_interval_seconds)
        service = AutoRefreshService(
            settings,
            mode="when_idle",
            persist=False,
            interval_seconds=interval,
            debounce_seconds=0 if args.once else 5,
        )
        while True:
            state = service.poll(force_check=not service.status()["pending"])
            print(json.dumps(state, indent=2))
            if args.once:
                return 0
            try:
                time.sleep(0.5 if state["pending"] else interval)
            except KeyboardInterrupt:
                return 0
    if args.command == "experience":
        index = build_experience_index(settings, changed_only=not args.rebuild)
        if args.query:
            if args.json:
                print(json.dumps({"query": args.query, "matches": similar_cases(settings, args.query)}, indent=2))
            else:
                rendered = render_similar_cases(
                    settings,
                    args.query,
                    include_patches=args.patches,
                    patch_chars=40_000 if args.patches and settings.experience_patch_chars == 0 else None,
                )
                print(rendered or "No similar ticket-labelled Git history found.")
        elif args.json:
            print(json.dumps({"cases": len(index.get("cases") or []), "generated_at": index.get("generated_at")}, indent=2))
        else:
            print(f"Indexed {len(index.get('cases') or [])} ticket cases from local Git history.")
        return 0
    if args.command == "evaluate":
        if args.golden:
            from .evaluation import evaluate_golden

            print(json.dumps(evaluate_golden(settings, args.golden, split=args.split, limit=max(1, args.limit)), indent=2))
            return 0
        report = evaluate_sessions(settings)
        print(settings.generated_dir / "EXPERIENCE_REPORT.md")
        print(f"Evaluated {report['evaluated_sessions']} sessions against {report['indexed_cases']} indexed ticket cases.")
        return 0
    if args.command == "start":
        ticket_text = _ticket_text(args)
        additions = []
        synced: list[SyncResult] = []
        graphs: list[GraphIndexResult] = []
        if args.no_sync and args.branch:
            raise BrainError("--branch requires sync; remove --no-sync")
        if not args.no_sync:
            additions, synced, graphs = _refresh_all(
                settings,
                fetch=True,
                branch_values=args.branch,
                discover=not args.no_discover,
            )
            if not args.json:
                _print_sync(synced)
                _print_graph(graphs)
        elif not (settings.generated_dir / "PROJECT_FACTS.md").exists():
            generate_map(settings)
            generate_relationship_map(settings)
        content, path = start_session(settings, args.ticket, ticket_text)
        copy = args.copy if args.copy is not None else args.target == "claude"
        parts, current = deliver(settings, args.ticket, content, args.target, copy=copy)
        checkpoint = session_state(settings, args.ticket).get("progressive_checkpoint") or {}
        if args.json:
            print(json.dumps({
                "ticket": args.ticket,
                "path": str(path),
                "delivery": {"current": current, "total": len(parts), "parts": [str(part) for part in parts], "copied": copy},
                "discovered": [repo.name for repo in additions],
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
        def checkpoint_progress(event: dict[str, object]) -> None:
            if event.get("phase") != "first_useful_checkpoint":
                return
            checkpoint = (session_state(settings, args.ticket).get("progressive_checkpoint") or {})
            payload = {
                "event": "first_useful_checkpoint",
                "checkpoint_id": checkpoint.get("checkpoint_id"),
                "path": checkpoint.get("handoff_artifact") or str(
                    session_dir(settings, args.ticket) / str(event.get("checkpoint_artifact") or "")
                ),
            }
            print(
                json.dumps(payload, separators=(",", ":")) if args.json
                else f"First useful checkpoint: {payload['path']}",
                file=sys.stderr,
                flush=True,
            )

        content, path, number = create_context(
            settings, args.ticket, _request_text(args), args.include_diff, progress=checkpoint_progress,
        )
        copy = args.copy if args.copy is not None else args.target == "claude"
        parts, current = deliver(settings, args.ticket, content, args.target, copy=copy)
        checkpoint = session_state(settings, args.ticket).get("progressive_checkpoint") or {}
        if args.json:
            print(json.dumps({
                "ticket": args.ticket,
                "request": number,
                "path": str(path),
                "delivery": {"current": current, "total": len(parts), "parts": [str(part) for part in parts], "copied": copy},
                "progressive_delivery": {
                    "checkpoint": checkpoint.get("handoff_artifact"),
                    "continuation": checkpoint.get("continuation_handoff_artifact"),
                } if checkpoint.get("continuation_status") == "published" else None,
            }, indent=2))
        else:
            print(path)
            if args.target == "m365":
                print(f"M365 handoff: {parts[0]}")
                if checkpoint.get("continuation_handoff_artifact"):
                    print(f"M365 checkpoint continuation: {checkpoint['continuation_handoff_artifact']}")
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
        def checkpoint_progress(event: dict[str, object]) -> None:
            if event.get("phase") != "first_useful_checkpoint":
                return
            checkpoint = (session_state(settings, args.ticket).get("progressive_checkpoint") or {})
            payload = {
                "event": "first_useful_checkpoint",
                "checkpoint_id": checkpoint.get("checkpoint_id"),
                "path": checkpoint.get("handoff_artifact") or str(
                    session_dir(settings, args.ticket) / str(event.get("checkpoint_artifact") or "")
                ),
            }
            print(
                json.dumps(payload, separators=(",", ":")) if args.json
                else f"First useful checkpoint: {payload['path']}",
                file=sys.stderr,
                flush=True,
            )

        content, path, number = create_context(
            settings, args.ticket, text, args.include_diff, progress=checkpoint_progress,
        )
        copy = args.copy if args.copy is not None else args.target == "claude"
        parts, current = deliver(settings, args.ticket, content, args.target, copy=copy)
        checkpoint = session_state(settings, args.ticket).get("progressive_checkpoint") or {}
        result = {
            "ticket": args.ticket,
            "kind": kind,
            "request": number,
            "path": str(path),
            "delivery": {"current": current, "total": len(parts), "parts": [str(part) for part in parts], "copied": copy},
            "progressive_delivery": {
                "checkpoint": checkpoint.get("handoff_artifact"),
                "continuation": checkpoint.get("continuation_handoff_artifact"),
            } if checkpoint.get("continuation_status") == "published" else None,
        }
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(path)
            if args.target == "m365":
                print(f"M365 handoff: {parts[0]}")
                if checkpoint.get("continuation_handoff_artifact"):
                    print(f"M365 checkpoint continuation: {checkpoint['continuation_handoff_artifact']}")
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
            label = "INVESTIGATION_REQUEST" if plan["protocol_version"] in {4, 5} else "CONTEXT_REQUEST"
            print(f"Valid {label} v{plan['protocol_version']}: {plan['operation_count']} operations")
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
    if args.command == "evidence":
        content, path, number, stored = add_external_evidence(
            settings,
            args.ticket,
            Path(args.file),
            kind=args.kind,
        )
        copy = args.copy if args.copy is not None else args.target == "claude"
        parts, current = deliver(settings, args.ticket, content, args.target, copy=copy)
        result = {
            "ticket": args.ticket,
            "evidence": number,
            "path": str(path),
            "stored": str(stored),
            "delivery": {"current": current, "total": len(parts), "parts": [str(part) for part in parts], "copied": copy},
        }
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(path)
            print(f"Stored source: {stored}")
            if args.target == "m365":
                print(f"M365 handoff: {parts[0]}")
            print(f"Evidence: {number:03d}; delivery: {current}/{len(parts)}" + (" copied" if copy else ""))
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
            print(f"Ticket memory: {status['summary']['experience_cases']} committed cases")
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
    except (BrainError, OSError, ValueError, RuntimeError) as exc:
        print(f"brain: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.resources import files as package_files
from pathlib import Path
from typing import Any, Iterable


IGNORED_DIRS = {".git", ".idea", ".venv", "node_modules", "target", "build", "dist"}
DISCOVERY_IGNORED_DIRS = IGNORED_DIRS | {".runs", ".codex", ".agents", "state", "generated", "knowledge"}
PROTOCOL_VERSION = 1
CODE_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cs", ".go", ".h", ".hpp", ".java", ".js",
    ".jsx", ".kt", ".kts", ".php", ".py", ".rb", ".rs", ".scala",
    ".sh", ".sql", ".swift", ".ts", ".tsx", ".vue", ".xml", ".yml",
    ".yaml", ".toml", ".properties", ".gradle",
}


class BrainError(RuntimeError):
    pass


@dataclass
class Repository:
    name: str
    path: Path
    description: str = ""
    tags: list[str] = field(default_factory=list)
    branch: str | None = None
    source_path: Path | None = None
    source_ref: str | None = None
    source_sha: str | None = None
    source_status: str = "working tree"
    source_fetched: bool = False
    source_warning: str | None = None

    @property
    def scan_path(self) -> Path:
        """The immutable snapshot used for evidence, or the working tree fallback."""
        return self.source_path if self.source_path and self.source_path.is_dir() else self.path


@dataclass
class Settings:
    name: str
    root: Path
    config_path: Path
    repositories: list[Repository]
    knowledge_dir: Path
    runs_dir: Path
    state_dir: Path
    generated_dir: Path
    max_results: int = 100
    source_window_lines: int = 150
    full_file_lines: int = 350
    soft_target_chars: int = 500_000
    clipboard_chunk_chars: int = 180_000
    graph_enabled: bool = True
    graph_lazy: bool = True
    branch_priority: list[str] = field(default_factory=lambda: ["develop", "development"])

    def repos(self, names: Iterable[str] | None = None) -> list[Repository]:
        wanted = set(names or [])
        if not wanted:
            return self.repositories
        known = {repo.name for repo in self.repositories}
        missing = wanted - known
        if missing:
            raise BrainError(f"Unknown repositories: {', '.join(sorted(missing))}")
        return [repo for repo in self.repositories if repo.name in wanted]

    def repo(self, name: str) -> Repository:
        return self.repos([name])[0]


def discover_git_repositories(roots: Iterable[Path]) -> list[Path]:
    """Find repository roots without walking every file inside each repository."""
    paths: set[Path] = set()
    for root in roots:
        for directory, dirs, _ in os.walk(root):
            path = Path(directory)
            if (path / ".git").exists():
                paths.add(path.resolve())
                dirs[:] = []
                continue
            dirs[:] = [name for name in dirs if name not in DISCOVERY_IGNORED_DIRS]
    return sorted(paths)


def discover_and_configure_repositories(settings: Settings) -> list[Repository]:
    """Append newly cloned repositories to brain.toml while preserving existing config."""
    configured_paths = {repo.path.resolve() for repo in settings.repositories}
    new_paths = [path for path in discover_git_repositories([settings.root]) if path not in configured_paths]
    if not new_paths:
        return []
    if settings.config_path.suffix.lower() != ".toml":
        raise BrainError(
            "New Git repositories were found, but automatic config updates require brain.toml; "
            "migrate the legacy YAML config or add them manually."
        )

    all_paths = [repo.path.resolve() for repo in settings.repositories] + new_paths
    used_names = {repo.name for repo in settings.repositories}
    additions: list[Repository] = []
    rows: list[str] = []
    for path in new_paths:
        candidate = path.name
        if sum(other.name == path.name for other in all_paths) > 1 or candidate in used_names:
            candidate = "-".join(path.relative_to(settings.root).parts)
        name = candidate
        counter = 2
        while name in used_names:
            name = f"{candidate}-{counter}"
            counter += 1
        used_names.add(name)
        relative = str(path.relative_to(settings.root))
        rows.extend([
            "[[repositories]]",
            f"name = {json.dumps(name)}",
            f"path = {json.dumps(relative)}",
            'description = ""',
            "tags = []",
            "",
        ])
        additions.append(Repository(name=name, path=path))

    existing = settings.config_path.read_text(encoding="utf-8")
    separator = "\n" if existing.endswith("\n") else "\n\n"
    temporary = settings.config_path.with_suffix(settings.config_path.suffix + ".tmp")
    temporary.write_text(existing + separator + "\n".join(rows), encoding="utf-8")
    shutil.copymode(settings.config_path, temporary)
    temporary.replace(settings.config_path)
    settings.repositories.extend(additions)
    return additions


@dataclass
class SearchHit:
    repo: str
    path: str
    line: int
    text: str
    kind: str = "code"
    score: int = 40
    found_by: list[str] = field(default_factory=list)


@dataclass
class Evidence:
    repo: str
    path: str
    line_start: int
    line_end: int
    content: str
    kind: str
    score: int
    found_by: list[str] = field(default_factory=list)


@dataclass
class ContextBundle:
    objective: str
    evidence: list[Evidence] = field(default_factory=list)
    relationships: list[str] = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def run(args: list[str], cwd: Path | None = None, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            cwd=cwd,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise BrainError(f"Could not run {args[0]}: {exc}") from exc


def _scalar(value: str) -> Any:
    value = value.strip()
    if value == "":
        return None
    lowered = value.lower()
    if lowered in {"null", "~"}:
        return None
    if lowered in {"true", "false"}:
        return lowered == "true"
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_scalar(part.strip()) for part in inner.split(",")]
    try:
        return ast.literal_eval(value)
    except (ValueError, SyntaxError):
        return value


def simple_yaml_load(text: str) -> Any:
    """Parse the small, indentation-based YAML subset used by Project Brain.

    PyYAML is deliberately unnecessary for a fresh install. JSON-style lists,
    mappings, quoted/plain scalars, and `>`/`|` blocks are supported.
    """
    raw = text.replace("\t", "    ").splitlines()
    tokens: list[tuple[int, str]] = []
    index = 0
    while index < len(raw):
        line = raw[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("```") or stripped == "---":
            index += 1
            continue
        indent = len(line) - len(line.lstrip(" "))
        block = re.match(r"^([^:#][^:]*):\s*([>|])\s*$", stripped)
        if block:
            block_lines: list[str] = []
            index += 1
            while index < len(raw):
                child = raw[index]
                child_indent = len(child) - len(child.lstrip(" "))
                if child.strip() and child_indent <= indent:
                    break
                if child.strip():
                    block_lines.append(child.strip())
                elif block_lines:
                    block_lines.append("")
                index += 1
            separator = " " if block.group(2) == ">" else "\n"
            tokens.append((indent, f"{block.group(1)}: {json.dumps(separator.join(block_lines))}"))
            continue
        tokens.append((indent, stripped))
        index += 1

    if not tokens:
        return {}

    def split_pair(value: str) -> tuple[str, str]:
        match = re.match(r"^([^:]+):(?:\s*(.*))?$", value)
        if not match:
            raise BrainError(f"Invalid YAML line: {value}")
        return match.group(1).strip(), (match.group(2) or "").strip()

    def parse(position: int, indent: int) -> tuple[Any, int]:
        is_list = tokens[position][1].startswith("-")
        if is_list:
            result: list[Any] = []
            while position < len(tokens) and tokens[position][0] == indent and tokens[position][1].startswith("-"):
                rest = tokens[position][1][1:].strip()
                position += 1
                if not rest:
                    if position < len(tokens) and tokens[position][0] > indent:
                        value, position = parse(position, tokens[position][0])
                    else:
                        value = None
                    result.append(value)
                    continue
                if re.match(r"^[^:]+:", rest):
                    key, raw_value = split_pair(rest)
                    item: dict[str, Any] = {key: _scalar(raw_value)}
                    if not raw_value and position < len(tokens) and tokens[position][0] > indent:
                        nested, position = parse(position, tokens[position][0])
                        item[key] = nested
                    if position < len(tokens) and tokens[position][0] > indent:
                        extra, position = parse(position, tokens[position][0])
                        if not isinstance(extra, dict):
                            raise BrainError(f"Expected mapping below list item: {rest}")
                        item.update(extra)
                    result.append(item)
                else:
                    result.append(_scalar(rest))
            return result, position

        result_map: dict[str, Any] = {}
        while position < len(tokens) and tokens[position][0] == indent and not tokens[position][1].startswith("-"):
            key, raw_value = split_pair(tokens[position][1])
            position += 1
            if raw_value:
                result_map[key] = _scalar(raw_value)
            elif position < len(tokens) and tokens[position][0] > indent:
                result_map[key], position = parse(position, tokens[position][0])
            else:
                result_map[key] = None
        return result_map, position

    result, end = parse(0, tokens[0][0])
    if end != len(tokens):
        raise BrainError(f"Could not parse YAML near: {tokens[end][1]}")
    return result


def _load_data(path: Path) -> dict[str, Any]:
    try:
        if path.suffix == ".toml":
            with path.open("rb") as handle:
                return tomllib.load(handle)
        text = path.read_text(encoding="utf-8")
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError:
            return simple_yaml_load(text)
        loaded = yaml.safe_load(text)
        return loaded or {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise BrainError(f"Invalid config {path}: {exc}") from exc


def find_config(explicit: str | None = None, start: Path | None = None) -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise BrainError(f"Config not found: {path}")
        return path
    current = (start or Path.cwd()).resolve()
    for directory in [current, *current.parents]:
        for name in ("brain.toml", "config.yml", "config.yaml"):
            candidate = directory / name
            if candidate.is_file():
                return candidate
    raise BrainError("No brain.toml/config.yml found. Run `brain init` first.")


def load_settings(path: str | Path | None = None) -> Settings:
    config_path = find_config(str(path) if path else None)
    data = _load_data(config_path)
    root = config_path.parent.resolve()
    project = data.get("project") or {}
    repo_values = data.get("repositories") or []
    if not isinstance(repo_values, list) or not repo_values:
        raise BrainError("Config must contain at least one [[repositories]] entry")
    repositories: list[Repository] = []
    seen: set[str] = set()
    for value in repo_values:
        if not isinstance(value, dict) or not value.get("name") or not value.get("path"):
            raise BrainError("Every repository needs name and path")
        name = str(value["name"])
        if name in seen:
            raise BrainError(f"Duplicate repository name: {name}")
        seen.add(name)
        repo_path = Path(os.path.expandvars(str(value["path"]))).expanduser()
        if not repo_path.is_absolute():
            repo_path = root / repo_path
        repositories.append(
            Repository(
                name,
                repo_path.resolve(),
                str(value.get("description") or ""),
                list(value.get("tags") or []),
                str(value.get("branch") or "").strip() or None,
            )
        )
    knowledge = data.get("knowledge") or {}
    context = data.get("context") or {}
    search = data.get("search") or {}
    delivery = data.get("delivery") or {}
    graph = data.get("graph") or {}
    sources = data.get("sources") or {}
    branch_priority = sources.get("branch_priority", ["develop", "development"])
    if not isinstance(branch_priority, list):
        raise BrainError("sources.branch_priority must be a list")
    graph_mode = str(graph.get("mode") or "lazy")
    if graph_mode not in {"lazy", "eager"}:
        raise BrainError("graph.mode must be lazy or eager")

    def local(value: str) -> Path:
        candidate = Path(value).expanduser()
        return (candidate if candidate.is_absolute() else root / candidate).resolve()

    settings = Settings(
        name=str(project.get("name") or root.name),
        root=root,
        config_path=config_path,
        repositories=repositories,
        knowledge_dir=local(str(knowledge.get("path") or "knowledge")),
        runs_dir=local(str(project.get("runs_dir") or ".runs")),
        state_dir=local(str(project.get("state_dir") or "state")),
        generated_dir=local(str(project.get("generated_dir") or "generated")),
        max_results=int(search.get("max_results") or 100),
        source_window_lines=int(context.get("source_window_lines") or 150),
        full_file_lines=int(context.get("full_file_lines") or 350),
        soft_target_chars=int(context.get("soft_target_chars") or 500_000),
        clipboard_chunk_chars=int(delivery.get("clipboard_chunk_chars") or 180_000),
        graph_enabled=bool(graph.get("enabled", True)),
        graph_lazy=graph_mode == "lazy",
        branch_priority=[str(value).strip() for value in branch_priority if str(value).strip()],
    )
    _attach_source_snapshots(settings)
    return settings


def load_source_state(settings: Settings) -> dict[str, Any]:
    path = settings.state_dir / "sources.json"
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _attach_source_snapshots(settings: Settings) -> None:
    state = load_source_state(settings)
    for repo in settings.repositories:
        item = state.get(repo.name) or {}
        snapshot = Path(str(item.get("snapshot") or ""))
        if snapshot.is_dir() and snapshot.is_relative_to(settings.state_dir):
            repo.source_path = snapshot
            repo.source_ref = str(item.get("ref") or "") or None
            repo.source_sha = str(item.get("sha") or "") or None
            repo.source_status = str(item.get("status") or "snapshot")
            repo.source_fetched = bool(item.get("fetched"))
            repo.source_warning = str(item.get("warning") or "") or None


def git_head(repo: Repository) -> str | None:
    result = run(["git", "rev-parse", "HEAD"], cwd=repo.path)
    return result.stdout.strip() if result.returncode == 0 else None


def _walk_files(root: Path) -> Iterable[Path]:
    for directory, dirs, names in os.walk(root):
        dirs[:] = [name for name in dirs if name not in IGNORED_DIRS]
        base = Path(directory)
        for name in names:
            path = base / name
            if path.suffix.lower() in CODE_SUFFIXES or name in {"Dockerfile", "Makefile", "pom.xml", "build.gradle"}:
                yield path


def _python_search(repo: Repository, pattern: str, fixed: bool, max_results: int) -> list[SearchHit]:
    try:
        regex = re.compile(re.escape(pattern) if fixed else pattern)
    except re.error as exc:
        raise BrainError(f"Invalid search regex: {exc}") from exc
    hits: list[SearchHit] = []
    root = repo.scan_path
    for path in _walk_files(root):
        try:
            if path.stat().st_size > 3_000_000:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if regex.search(line):
                    hits.append(SearchHit(repo.name, str(path.relative_to(root)), number, line, score=95, found_by=["python exact search"]))
                    if len(hits) >= max_results:
                        return hits
        except OSError:
            continue
    return hits


def search_repo(repo: Repository, pattern: str, *, fixed: bool = False, max_results: int = 100) -> list[SearchHit]:
    root = repo.scan_path
    if not root.is_dir():
        return []
    if not shutil.which("rg"):
        return _python_search(repo, pattern, fixed, max_results)
    args = ["rg", "--json", "--line-number", "--color", "never"]
    if fixed:
        args.append("--fixed-strings")
    args.extend(["-e", pattern, str(root)])
    result = run(args)
    if result.returncode not in {0, 1}:
        raise BrainError(f"rg failed in {repo.name}: {result.stderr.strip()}")
    hits: list[SearchHit] = []
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "match":
            continue
        data = event["data"]
        absolute = Path(data["path"]["text"])
        try:
            relative = absolute.relative_to(root)
        except ValueError:
            relative = absolute
        hits.append(SearchHit(
            repo=repo.name,
            path=str(relative),
            line=int(data["line_number"]),
            text=data["lines"]["text"].rstrip("\n"),
            score=95 if fixed else 80,
            found_by=["ripgrep literal" if fixed else "ripgrep regex"],
        ))
        if len(hits) >= max_results:
            break
    return hits


def search(settings: Settings, pattern: str, repos: Iterable[str] | None = None, *, fixed: bool = False) -> list[SearchHit]:
    selected = settings.repos(repos)
    hits: list[SearchHit] = []
    for repo in selected:
        # A busy first repository must not hide evidence in later repositories.
        hits.extend(search_repo(repo, pattern, fixed=fixed, max_results=settings.max_results))
    return hits[: settings.max_results * max(1, len(selected))]


def symbol_hits(settings: Settings, query: str, repos: Iterable[str] | None = None) -> list[SearchHit]:
    from .graph import graph_symbol_hits

    scope = list(repos or [])
    name = query.rsplit(".", 1)[-1]
    escaped = re.escape(name)
    declaration = (
        rf"\b(?:class|interface|enum|record|trait|struct|type|object|def|fn|func|function|fun)\s+{escaped}\b"
        rf"|\b{escaped}\s*[:=]\s*(?:async\s+)?(?:function|\([^)]*\)\s*=>)"
        rf"|\b(?:public|protected|private|static|final|abstract|synchronized|native\s+)*[A-Za-z_$][\w$<>, ?\[\].]*\s+{escaped}\s*\("
    )
    hits = search(settings, declaration, scope)
    for hit in hits:
        hit.kind = "definition"
        hit.score = 100
        hit.found_by.append("symbol declaration")
    graph_scope = scope or sorted({hit.repo for hit in hits})
    graph_hits = graph_symbol_hits(settings, query, graph_scope)
    if graph_hits or hits:
        merged: dict[tuple[str, str, int], SearchHit] = {}
        for hit in graph_hits + hits:
            key = hit.repo, hit.path, hit.line
            existing = merged.get(key)
            if existing:
                existing.score = max(existing.score, hit.score)
                existing.found_by = sorted(set(existing.found_by + hit.found_by))
            else:
                merged[key] = hit
        return list(merged.values())
    fallback = search(settings, rf"\b{escaped}\b", scope)
    for hit in fallback:
        hit.kind = "symbol reference"
        hit.score = 60
        hit.found_by.append("symbol fallback")
    return fallback


def implementation_hits(settings: Settings, name: str, repos: Iterable[str] | None = None) -> list[SearchHit]:
    short = name.rsplit(".", 1)[-1]
    pattern = rf"\b(?:implements|extends)\s+[^{{\n]*\b{re.escape(short)}\b|:\s*[^{{=\n]*\b{re.escape(short)}\b"
    hits = search(settings, pattern, repos)
    for hit in hits:
        hit.kind = "implementation"
        hit.score = 90
        hit.found_by.append("implementation fallback")
    return hits


def test_hits(settings: Settings, name: str, repos: Iterable[str] | None = None) -> list[SearchHit]:
    candidates = search(settings, rf"\b{re.escape(name.rsplit('.', 1)[-1])}\b", repos)
    tests = [hit for hit in candidates if re.search(r"(^|/)(test|tests|src/test)/|(?:Test|Tests|IT|Spec)\.", hit.path, re.I)]
    for hit in tests:
        hit.kind = "test"
        hit.score = 85
        hit.found_by.append("test discovery")
    return tests


def read_source(settings: Settings, hit: SearchHit, *, full: bool = False, lines: tuple[int, int] | None = None) -> Evidence:
    repo = settings.repo(hit.repo)
    root = repo.scan_path.resolve()
    path = (root / hit.path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise BrainError(f"Unsafe or missing file: {hit.repo}:{hit.path}")
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if lines:
        start, end = max(1, lines[0]), min(len(content), lines[1])
    elif full or len(content) <= settings.full_file_lines:
        start, end = 1, len(content)
    else:
        radius = max(10, settings.source_window_lines // 2)
        start, end = max(1, hit.line - radius), min(len(content), hit.line + radius)
    return Evidence(hit.repo, hit.path, start, end, "\n".join(content[start - 1:end]), hit.kind, hit.score, list(hit.found_by))


def trace_symbol(settings: Settings, query: str, repos: Iterable[str] | None = None) -> tuple[list[SearchHit], list[str]]:
    from .graph import graph_trace

    scope = list(repos or [])
    name = query.rsplit(".", 1)[-1]
    uses = search(settings, rf"\b{re.escape(name)}\s*\(", scope)
    inbound: list[SearchHit] = []
    definitions: list[SearchHit] = []
    declaration = re.compile(rf"\b(?:def|fn|func|function|fun|[A-Za-z_$][\w$<>, ?\[\]]+)\s+{re.escape(name)}\s*\(")
    for hit in uses:
        if declaration.search(hit.text):
            hit.kind = "definition"
            hit.score = 100
            definitions.append(hit)
        else:
            hit.kind = "caller"
            hit.score = 90
            inbound.append(hit)
    graph_scope = scope or sorted({hit.repo for hit in definitions + inbound})
    graph_hits, graph_relationships = graph_trace(settings, query, graph_scope)
    relationships = graph_relationships + [f"{hit.repo}:{hit.path}:{hit.line}  CALLS  {query}" for hit in inbound]
    call_names: set[str] = set()
    ignored = {"if", "for", "while", "switch", "catch", "return", "new", "throw", "super", "this", name}
    for definition in definitions[:5]:
        source = read_source(settings, definition).content
        for match in re.finditer(r"\b([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)?)\s*\(", source):
            called = match.group(1)
            if called.rsplit(".", 1)[-1] not in ignored:
                call_names.add(called)
    relationships.extend(f"{query}  CALLS  {called}" for called in sorted(call_names)[:80])
    combined = graph_hits + definitions + inbound
    return list({(hit.repo, hit.path, hit.line, hit.kind): hit for hit in combined}.values()), relationships


def git_history(repo: Repository, query: str, limit: int = 20) -> str:
    if not (repo.path / ".git").exists():
        return ""
    fmt = "%h %ad %s"
    revision = repo.source_ref or "HEAD"
    result = run(["git", "log", revision, f"-n{limit}", "--date=short", f"--pretty=format:{fmt}", "-S", query], cwd=repo.path)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    result = run(["git", "log", revision, f"-n{limit}", "--date=short", f"--pretty=format:{fmt}", "-G", re.escape(query)], cwd=repo.path)
    return result.stdout.strip() if result.returncode == 0 else ""


def knowledge_hits(settings: Settings, query: str, limit: int = 30) -> list[Evidence]:
    if not settings.knowledge_dir.is_dir():
        return []
    try:
        regex = re.compile(query, re.I)
    except re.error:
        regex = re.compile(re.escape(query), re.I)
    results: list[Evidence] = []
    for path in sorted(settings.knowledge_dir.rglob("*.md")):
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for number, line in enumerate(lines, 1):
            if regex.search(line):
                start, end = max(1, number - 20), min(len(lines), number + 20)
                results.append(Evidence("knowledge", str(path.relative_to(settings.knowledge_dir)), start, end, "\n".join(lines[start - 1:end]), "knowledge", 70, ["knowledge search"]))
                break
        if len(results) >= limit:
            break
    return results


def _pom_dependencies(path: Path) -> list[str]:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return []
    dependencies: list[str] = []
    for dependency in root.findall(".//{*}dependency"):
        group = dependency.find("{*}groupId")
        artifact = dependency.find("{*}artifactId")
        if artifact is not None and artifact.text:
            dependencies.append(f"{group.text if group is not None else '?'}:{artifact.text}")
    return dependencies


def generate_map(settings: Settings) -> str:
    output = ["# Generated Project Facts", "", f"Generated: {datetime.now(UTC).isoformat()}", ""]
    annotation = r"@(RestController|Controller|Service|Repository|FeignClient|KafkaListener|Scheduled|Entity|Table|RequestMapping|GetMapping|PostMapping|PutMapping|PatchMapping|DeleteMapping)\b"
    for repo in settings.repositories:
        output.extend(
            [
                f"## {repo.name}",
                "",
                f"Source: `{repo.source_ref or 'working tree'}` at "
                f"`{(repo.source_sha or git_head(repo) or 'unknown')[:12]}` ({repo.source_status})",
            ]
        )
        if repo.source_warning:
            output.append(f"Freshness warning: {repo.source_warning}")
        output.append("")
        if repo.description:
            output.extend([repo.description, ""])
        facts = search_repo(repo, annotation, max_results=300) if repo.scan_path.is_dir() else []
        output.append("### Framework facts")
        output.append("")
        if facts:
            output.extend(f"- `{hit.path}:{hit.line}` — `{hit.text.strip()}`" for hit in facts)
        else:
            output.append("- None detected")
        dependencies: list[str] = []
        for pom in repo.scan_path.rglob("pom.xml") if repo.scan_path.is_dir() else []:
            if not any(part in IGNORED_DIRS for part in pom.parts):
                dependencies.extend(_pom_dependencies(pom))
        output.extend(["", "### Maven dependencies", ""])
        output.extend(f"- `{item}`" for item in sorted(set(dependencies))) if dependencies else output.append("- None detected")
        output.append("")
    text = "\n".join(output).rstrip() + "\n"
    settings.generated_dir.mkdir(parents=True, exist_ok=True)
    (settings.generated_dir / "PROJECT_FACTS.md").write_text(text, encoding="utf-8")
    return text


def _request_body(text: str) -> dict[str, Any]:
    """Extract a versioned request from a whole chat response or request file."""
    stripped = text.strip()
    if not stripped:
        raise BrainError("The AI response is empty")

    loaded: Any = None
    if stripped.startswith("{"):
        try:
            loaded = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise BrainError(f"Invalid CONTEXT_REQUEST JSON: line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc

    if loaded is None:
        marker = text.find("CONTEXT_REQUEST:")
        if marker < 0:
            raise BrainError(
                "Input does not contain CONTEXT_REQUEST:. Copy the AI's complete response, "
                "or ask it to return a Project Brain CONTEXT_REQUEST YAML block."
            )
        payload = text[marker:]
        closing_fence = payload.find("\n```")
        if closing_fence >= 0:
            payload = payload[:closing_fence]
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError:
            loaded = simple_yaml_load(payload)
        else:
            try:
                loaded = yaml.safe_load(payload)
            except Exception as exc:
                raise BrainError(f"Invalid CONTEXT_REQUEST YAML: {exc}") from exc

    if isinstance(loaded, dict) and "CONTEXT_REQUEST" not in loaded and "objective" in loaded:
        loaded = {"CONTEXT_REQUEST": loaded}
    if not isinstance(loaded, dict) or not isinstance(loaded.get("CONTEXT_REQUEST"), dict):
        raise BrainError("CONTEXT_REQUEST must be a YAML mapping or JSON object")
    request = loaded["CONTEXT_REQUEST"]
    version = request.get("version", loaded.get("version", PROTOCOL_VERSION))
    if version != PROTOCOL_VERSION:
        raise BrainError(f"Unsupported CONTEXT_REQUEST version {version!r}; this build supports version {PROTOCOL_VERSION}")
    request["version"] = PROTOCOL_VERSION
    for key in ("searches", "symbols", "files", "history"):
        value = request.get(key, [])
        if value is None:
            request[key] = []
        elif not isinstance(value, list):
            raise BrainError(f"{key} must be a list")
    if not str(request.get("objective") or "").strip():
        raise BrainError("objective is required")
    return request


def parse_context_request(text: str) -> dict[str, Any]:
    request = _request_body(text)
    for index, item in enumerate(request["searches"]):
        if not isinstance(item, dict) or not str(item.get("query") or "").strip():
            raise BrainError(f"searches[{index}].query is required")
        _requested_repos(item)
    allowed = {"definition", "callers", "callees", "implementations", "tests"}
    for index, item in enumerate(request["symbols"]):
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            raise BrainError(f"symbols[{index}].name is required")
        include = item.get("include") or ["definition"]
        if not isinstance(include, list):
            raise BrainError(f"symbols[{index}].include must be a list")
        unknown = set(include) - allowed
        if unknown:
            raise BrainError(f"symbols[{index}].include has unknown values: {', '.join(sorted(unknown))}")
        item["include"] = include
        _requested_repos(item)
    for index, item in enumerate(request["files"]):
        if not isinstance(item, dict) or not item.get("repo") or not item.get("path"):
            raise BrainError(f"files[{index}] requires repo and path")
        if item.get("lines") and not re.fullmatch(r"\s*\d+\s*[-:]\s*\d+\s*", str(item["lines"])):
            raise BrainError(f"files[{index}].lines must look like 10-40")
    for index, item in enumerate(request["history"]):
        if not isinstance(item, dict) or not str(item.get("query") or "").strip():
            raise BrainError(f"history[{index}].query is required")
        _requested_repos(item)
    return request


def request_preview(text: str, settings: Settings | None = None) -> dict[str, Any]:
    """Return the deterministic execution plan without touching repositories."""
    request = parse_context_request(text)
    actions: list[dict[str, Any]] = []

    def repos_for(item: dict[str, Any]) -> list[str]:
        repos = _requested_repos(item)
        if settings:
            settings.repos(repos)
        return repos

    for item in request["searches"]:
        actions.append({"kind": "search", "value": str(item["query"]), "repos": repos_for(item)})
    for item in request["symbols"]:
        repos = repos_for(item)
        for operation in item["include"]:
            actions.append({"kind": str(operation), "value": str(item["name"]), "repos": repos})
    for item in request["files"]:
        if settings:
            settings.repo(str(item["repo"]))
        value = f"{item['repo']}:{item['path']}"
        if item.get("lines"):
            value += f":{item['lines']}"
        actions.append({"kind": "file", "value": value, "repos": [str(item["repo"]) ]})
    for item in request["history"]:
        actions.append({"kind": "history", "value": str(item["query"]), "repos": repos_for(item)})

    if not actions:
        raise BrainError("CONTEXT_REQUEST contains no repository operations")
    signature = hashlib.sha256(
        json.dumps(sorted(actions, key=lambda item: json.dumps(item, sort_keys=True)), sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "valid": True,
        "protocol_version": PROTOCOL_VERSION,
        "objective": str(request["objective"]).strip(),
        "request": request,
        "actions": actions,
        "operation_count": len(actions),
        "signature": signature,
        "counts": {
            "searches": len(request["searches"]),
            "symbols": len(request["symbols"]),
            "files": len(request["files"]),
            "history": len(request["history"]),
        },
        "normalized_json": json.dumps({"CONTEXT_REQUEST": request}, indent=2, ensure_ascii=False) + "\n",
    }


def request_repair_prompt(error: str) -> str:
    """Build a safe prompt the user can copy back when the model broke protocol."""
    return (
        "Your previous response could not be executed by Project Brain.\n\n"
        f"Validation error: {error}\n\n"
        "Return only one fenced YAML block using this exact schema. Do not invent repository names.\n\n"
        "```yaml\n"
        "CONTEXT_REQUEST:\n"
        f"  version: {PROTOCOL_VERSION}\n"
        "  objective: Explain the next fact that must be established.\n"
        "  searches: []\n"
        "  symbols: []\n"
        "  files: []\n"
        "  history: []\n"
        "```\n"
    )


def _requested_repos(item: dict[str, Any]) -> list[str]:
    repos = item.get("repos") or []
    if not isinstance(repos, list):
        raise BrainError("repos must be a list")
    return [str(repo) for repo in repos]


def _direct_file(settings: Settings, item: dict[str, Any]) -> Evidence:
    repo = settings.repo(str(item["repo"]))
    relative = str(item["path"])
    root = repo.scan_path.resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise BrainError(f"Unsafe or missing file: {repo.name}:{relative}")
    requested = item.get("lines")
    line_range: tuple[int, int] | None = None
    if requested:
        match = re.fullmatch(r"\s*(\d+)\s*[-:]\s*(\d+)\s*", str(requested))
        if not match:
            raise BrainError(f"Invalid line range `{requested}`; use 10-40")
        line_range = int(match.group(1)), int(match.group(2))
    hit = SearchHit(repo.name, str(path.relative_to(root)), line_range[0] if line_range else 1, "", "requested file", 100, ["direct file request"])
    return read_source(settings, hit, full=line_range is None, lines=line_range)


def _deduplicate(evidence: list[Evidence]) -> list[Evidence]:
    merged: dict[tuple[str, str, int, int], Evidence] = {}
    for item in evidence:
        key = item.repo, item.path, item.line_start, item.line_end
        existing = merged.get(key)
        if existing:
            existing.score = max(existing.score, item.score)
            existing.found_by = sorted(set(existing.found_by + item.found_by))
            if item.kind not in existing.kind:
                existing.kind += f", {item.kind}"
        else:
            merged[key] = item
    return sorted(merged.values(), key=lambda item: (-item.score, item.repo, item.path, item.line_start))


def working_tree_diffs(settings: Settings, repos: Iterable[str] | None = None) -> list[Evidence]:
    """Read tracked working-tree diffs without modifying or staging anything."""
    evidence: list[Evidence] = []
    for repo in settings.repos(repos):
        if not (repo.path / ".git").exists():
            continue
        unstaged = run(["git", "diff", "--no-ext-diff"], cwd=repo.path)
        staged = run(["git", "diff", "--cached", "--no-ext-diff"], cwd=repo.path)
        content = "\n".join(part for part in [unstaged.stdout.strip(), staged.stdout.strip()] if part)
        if content:
            evidence.append(
                Evidence(
                    repo.name,
                    "(working tree diff)",
                    1,
                    content.count("\n") + 1,
                    content,
                    "local diff",
                    100,
                    ["working tree review"],
                )
            )
    return evidence


def retrieve_context(settings: Settings, request: dict[str, Any], *, include_diff: bool = False) -> ContextBundle:
    bundle = ContextBundle(str(request["objective"]).strip())
    for item in request["searches"]:
        query = str(item["query"])
        repos = _requested_repos(item)
        hits = search(settings, query, repos, fixed=True)
        if not hits:
            try:
                hits = search(settings, query, repos)
            except BrainError:
                hits = []
        if not hits:
            bundle.unresolved.append(f"Search `{query}` returned no code matches in {repos or ['all repositories']}")
        bundle.evidence.extend(read_source(settings, hit) for hit in hits)
        bundle.evidence.extend(knowledge_hits(settings, query))

    for item in request["symbols"]:
        name = str(item["name"])
        repos = _requested_repos(item)
        include = set(item["include"])
        definitions = symbol_hits(settings, name, repos)
        if "definition" in include:
            if definitions:
                bundle.evidence.extend(read_source(settings, hit) for hit in definitions)
            else:
                bundle.unresolved.append(f"Definition for `{name}` was not found")
        if include & {"callers", "callees"}:
            traced, relationships = trace_symbol(settings, name, repos)
            if traced:
                bundle.evidence.extend(read_source(settings, hit) for hit in traced)
            if relationships:
                bundle.relationships.extend(relationships)
            else:
                bundle.unresolved.append(f"No static call evidence found for `{name}`")
        if "implementations" in include:
            implementations = implementation_hits(settings, name, repos)
            if implementations:
                bundle.evidence.extend(read_source(settings, hit) for hit in implementations)
            else:
                bundle.unresolved.append(f"No implementations found for `{name}`")
        if "tests" in include:
            tests = test_hits(settings, name, repos)
            if tests:
                bundle.evidence.extend(read_source(settings, hit) for hit in tests)
            else:
                bundle.unresolved.append(f"No tests referencing `{name}` were found")

    for item in request["files"]:
        bundle.evidence.append(_direct_file(settings, item))

    for item in request["history"]:
        if not isinstance(item, dict) or not item.get("query"):
            raise BrainError("Every history request requires query")
        repos = settings.repos(_requested_repos(item))
        query = str(item["query"])
        found = False
        for repo in repos:
            result = git_history(repo, query)
            if result:
                found = True
                bundle.history.append(f"## {repo.name}: `{query}`\n\n```text\n{result}\n```")
        if not found:
            bundle.unresolved.append(f"No Git history found for `{query}`")

    if include_diff:
        bundle.evidence.extend(working_tree_diffs(settings))

    bundle.evidence = _deduplicate(bundle.evidence)
    state = load_index_state(settings)
    for repo in settings.repositories:
        current = repo.source_sha or git_head(repo)
        indexed = (state.get(repo.name) or {}).get("sha")
        if indexed and current and indexed != current:
            bundle.warnings.append(f"Index for {repo.name} is stale: indexed {indexed[:12]}, source {current[:12]}.")
    return bundle


def _language(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return {
        ".java": "java", ".kt": "kotlin", ".py": "python", ".js": "javascript",
        ".jsx": "jsx", ".ts": "typescript", ".tsx": "tsx", ".go": "go",
        ".rs": "rust", ".rb": "ruby", ".xml": "xml", ".yml": "yaml",
        ".yaml": "yaml", ".toml": "toml", ".sql": "sql", ".sh": "bash",
    }.get(suffix, "text")


def pack_context(
    settings: Settings,
    ticket: str,
    request_number: int,
    bundle: ContextBundle,
    progress: dict[str, Any] | None = None,
) -> str:
    output = [
        "# PROJECT BRAIN CONTEXT", "", f"Ticket: `{ticket}`", f"Request: `{request_number:03d}`", "",
        "## Objective", "", bundle.objective, "", "## Repository state", "",
    ]
    warnings = list(bundle.warnings)
    for repo in settings.repositories:
        local = git_head(repo)
        source = repo.source_sha or local
        output.append(
            f"- `{repo.name}` — analyzed `{(source or 'not a Git repository')[:12]}` "
            f"from `{repo.source_ref or 'working tree'}` ({repo.source_status}); "
            f"local HEAD `{(local or 'n/a')[:12]}`"
        )
        if repo.source_warning:
            warnings.append(f"{repo.name}: {repo.source_warning}")
    if warnings:
        output.extend(["", "## Warnings", ""])
        output.extend(f"- {warning}" for warning in warnings)
    if progress:
        output.extend([
            "",
            "## Investigation progress",
            "",
            f"- Retrieval requests completed: {request_number}",
            f"- Operations in this request: {progress['operations']}",
            f"- New unique evidence regions: {progress['new_evidence']}",
            f"- Previously seen evidence regions: {progress['known_evidence']}",
            f"- Consecutive requests with no new evidence: {progress['no_progress_rounds']}",
        ])
        history = progress.get("history") or []
        if history:
            output.extend(["", "Earlier retrieval objectives:", ""])
            output.extend(
                f"- {int(item.get('number') or 0):03d}: {item.get('objective')} "
                f"({item.get('new_evidence', 0)} new evidence regions)"
                for item in history[-8:]
            )
        if progress["no_progress_rounds"]:
            output.append(
                "- This request added no new repository evidence. Do not repeat open-ended retrieval; "
                "either ask the user for the specific external/runtime fact that blocks the decision or produce FINAL_SOLUTION."
            )
    if bundle.relationships:
        output.extend(["", "## Static execution relationships", "", "```text", *sorted(set(bundle.relationships)), "```"])
    output.extend(["", "## Source evidence", ""])
    if not bundle.evidence:
        output.append("No source evidence was retrieved.")
    for index, item in enumerate(bundle.evidence, 1):
        found = ", ".join(item.found_by)
        output.extend([
            f"### {index}. {item.repo} — `{item.path}:{item.line_start}-{item.line_end}`",
            "", f"Kind: {item.kind}  ", f"Found by: {found}", "",
            f"```{_language(item.path)}", item.content, "```", "",
        ])
    if bundle.history:
        output.extend(["## Git history", "", *bundle.history, ""])
    output.extend(["## Unresolved", ""])
    output.extend(f"- {item}" for item in bundle.unresolved) if bundle.unresolved else output.append("- None")
    text = "\n".join(output).rstrip() + "\n"
    if len(text) > settings.soft_target_chars:
        text += f"\n> Context size warning: {len(text):,} characters exceeds the soft target of {settings.soft_target_chars:,}. No evidence was discarded.\n"
    return text


def load_index_state(settings: Settings) -> dict[str, Any]:
    path = settings.state_dir / "indexes.json"
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def snapshot_indexes(settings: Settings, changed_only: bool = False) -> tuple[dict[str, Any], list[str]]:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    state = load_index_state(settings)
    updated: list[str] = []
    for repo in settings.repositories:
        sha = repo.source_sha or git_head(repo)
        previous = (state.get(repo.name) or {}).get("sha")
        if not changed_only or repo.name not in state or sha != previous:
            state[repo.name] = {
                "sha": sha,
                "ref": repo.source_ref,
                "indexed_at": datetime.now(UTC).isoformat(),
                "backend": "deterministic multi-repo scanner",
            }
            updated.append(repo.name)
    (settings.state_dir / "indexes.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return state, updated


def doctor(settings: Settings) -> tuple[str, bool]:
    from .graph import TESTED_BACKEND_VERSION, backend_version

    output = ["PROJECT BRAIN", "", "Dependencies", ""]
    ok = True
    for command, required in (("python", True), ("git", False), ("rg", False)):
        present = sys.executable if command == "python" else shutil.which(command)
        status = "OK" if present else ("MISSING" if required else "OPTIONAL — built-in fallback active")
        output.append(f"{command:<24}{status}")
        ok = ok and (bool(present) or not required)
    output.extend(["", "Repositories", ""])
    for repo in settings.repositories:
        exists = repo.path.is_dir()
        status = "OK" if exists else "MISSING"
        if exists and not (repo.path / ".git").exists():
            status = "OK (not Git)"
        output.append(f"{repo.name:<24}{status}  {repo.path}")
        ok = ok and exists
    state = load_index_state(settings)
    output.extend(["", "Freshness snapshots", ""])
    for repo in settings.repositories:
        current = repo.source_sha or git_head(repo)
        indexed = (state.get(repo.name) or {}).get("sha")
        status = "NOT SNAPSHOTTED" if repo.name not in state else ("CURRENT" if current == indexed else "STALE")
        output.append(f"{repo.name:<24}{status}")
    output.extend(["", "Source snapshots", ""])
    source_state = load_source_state(settings)
    for repo in settings.repositories:
        item = source_state.get(repo.name) or {}
        source = (repo.source_sha or git_head(repo) or "")[:12]
        output.append(f"{repo.name:<24}{item.get('status', repo.source_status).upper()}  {source or 'unknown'}")
    version = backend_version() if settings.graph_enabled else None
    graph_status = "DISABLED — lexical analysis active" if not settings.graph_enabled else (
        f"codebase-memory-mcp {version}" if version else "OPTIONAL MISSING — lexical fallback active"
    )
    if settings.graph_enabled and version and version != TESTED_BACKEND_VERSION:
        graph_status += f" (tested with {TESTED_BACKEND_VERSION})"
    if settings.graph_enabled and settings.graph_lazy:
        graph_status += " — lazy per relevant repository"
    output.extend(["", f"Config: {settings.config_path}", f"Structural backend: {graph_status}"])
    return "\n".join(output) + "\n", ok


def session_dir(settings: Settings, ticket: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", ticket).strip(".-")
    if not safe:
        raise BrainError("Ticket identifier is empty")
    return settings.runs_dir / safe


def session_state(settings: Settings, ticket: str) -> dict[str, Any]:
    path = session_dir(settings, ticket) / "session.json"
    if not path.is_file():
        return {"ticket": ticket, "requests": 0, "feedbacks": 0, "delivery": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BrainError(f"Invalid session state: {path}: {exc}") from exc


def save_session(settings: Settings, ticket: str, state: dict[str, Any]) -> None:
    directory = session_dir(settings, ticket)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "session.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def start_session(settings: Settings, ticket: str, ticket_text: str) -> tuple[str, Path]:
    directory = session_dir(settings, ticket)
    directory.mkdir(parents=True, exist_ok=True)
    ticket_path = directory / "ticket.md"
    ticket_path.write_text(ticket_text.rstrip() + "\n", encoding="utf-8")
    prompt = package_files("brain").joinpath("prompt.md").read_text(encoding="utf-8")
    sections = ["# PROJECT BRAIN — START", "", f"Project: `{settings.name}`", f"Ticket: `{ticket}`", ""]
    sections.extend(["## Repository snapshot manifest", ""])
    for repo in settings.repositories:
        source = repo.source_sha or git_head(repo)
        sections.append(
            f"- `{repo.name}` — `{repo.source_ref or 'working tree'}` at "
            f"`{(source or 'unknown')[:12]}` ({repo.source_status})"
        )
        if repo.source_warning:
            sections.append(f"  - Freshness warning: {repo.source_warning}")
    sections.extend(["", "## Operating protocol", "", prompt, "", "## Ticket", "", ticket_text.strip(), ""])
    for title, path in (
        ("Human project map", settings.knowledge_dir / "PROJECT_MAP.md"),
        ("Generated project facts", settings.generated_dir / "PROJECT_FACTS.md"),
        ("Generated cross-repository relationships", settings.generated_dir / "PROJECT_RELATIONSHIPS.md"),
        ("Glossary", settings.knowledge_dir / "glossary.md"),
    ):
        if path.is_file():
            sections.extend([f"## {title}", "", path.read_text(encoding="utf-8", errors="replace").strip(), ""])
    content = "\n".join(sections).rstrip() + "\n"
    start_path = directory / "start.md"
    start_path.write_text(content, encoding="utf-8")
    state = session_state(settings, ticket)
    source_signature = hashlib.sha256(
        json.dumps(
            [(repo.name, repo.source_ref, repo.source_sha or git_head(repo)) for repo in settings.repositories],
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    state.update(
        {
            "ticket": ticket,
            "started_at": datetime.now(UTC).isoformat(),
            "status": "waiting_for_ai",
            "source_signature": source_signature,
            "requests": state.get("requests", 0),
            "feedbacks": state.get("feedbacks", 0),
            "sources": {
                repo.name: {
                    "snapshot": str(repo.source_path) if repo.source_path else None,
                    "ref": repo.source_ref,
                    "sha": repo.source_sha,
                    "status": repo.source_status,
                    "fetched": repo.source_fetched,
                    "warning": repo.source_warning,
                }
                for repo in settings.repositories
            },
        }
    )
    save_session(settings, ticket, state)
    return content, start_path


def create_context(settings: Settings, ticket: str, request_text: str, include_diff: bool = False) -> tuple[str, Path, int]:
    directory = session_dir(settings, ticket)
    if not directory.is_dir():
        raise BrainError(f"Session {ticket} does not exist. Run `brain start {ticket}` first.")
    plan = request_preview(request_text, settings)
    request = plan["request"]
    state = session_state(settings, ticket)
    for previous in state.get("request_history") or []:
        if previous.get("signature") == plan["signature"] and previous.get("source_signature") == state.get("source_signature"):
            raise BrainError(
                f"This retrieval plan already ran as request {int(previous.get('number') or 0):03d}. "
                "Return that result to the AI instead of repeating it."
            )
    for repo in settings.repositories:
        source = (state.get("sources") or {}).get(repo.name) or {}
        snapshot = Path(str(source.get("snapshot") or ""))
        if snapshot.is_dir() and snapshot.is_relative_to(settings.state_dir):
            repo.source_path = snapshot
            repo.source_ref = str(source.get("ref") or "") or None
            repo.source_sha = str(source.get("sha") or "") or None
            repo.source_status = str(source.get("status") or "session snapshot")
            repo.source_fetched = bool(source.get("fetched"))
            repo.source_warning = str(source.get("warning") or "") or None
    number = int(state.get("requests") or 0) + 1
    (directory / f"request-{number:03d}.yml").write_text(request_text.rstrip() + "\n", encoding="utf-8")
    bundle = retrieve_context(settings, request, include_diff=include_diff)
    evidence_keys = {
        hashlib.sha256(
            f"{item.repo}\0{item.path}\0{item.line_start}\0{item.line_end}\0{item.content}".encode("utf-8")
        ).hexdigest()
        for item in bundle.evidence
    }
    known_keys = set(state.get("evidence_keys") or [])
    new_evidence = evidence_keys - known_keys
    no_progress_rounds = 0 if new_evidence else int(state.get("no_progress_rounds") or 0) + 1
    progress = {
        "operations": plan["operation_count"],
        "new_evidence": len(new_evidence),
        "known_evidence": len(evidence_keys & known_keys),
        "no_progress_rounds": no_progress_rounds,
        "history": list(state.get("request_history") or []),
    }
    content = pack_context(settings, ticket, number, bundle, progress)
    path = directory / f"context-{number:03d}.md"
    path.write_text(content, encoding="utf-8")
    state["requests"] = number
    state["status"] = "waiting_for_ai"
    state["no_progress_rounds"] = no_progress_rounds
    state["evidence_keys"] = sorted(known_keys | evidence_keys)
    history = list(state.get("request_history") or [])
    history.append({
        "number": number,
        "objective": plan["objective"],
        "signature": plan["signature"],
        "source_signature": state.get("source_signature"),
        "operations": plan["operation_count"],
        "new_evidence": len(new_evidence),
        "unresolved": len(bundle.unresolved),
        "created_at": datetime.now(UTC).isoformat(),
    })
    state["request_history"] = history
    save_session(settings, ticket, state)
    return content, path, number


def create_feedback(
    settings: Settings,
    ticket: str,
    *,
    notes: str = "",
    test_command: str = "",
    test_output: str = "",
    repos: Iterable[str] | None = None,
    include_diff: bool = True,
) -> tuple[str, Path, int]:
    """Package human implementation and test results for a chat AI review."""
    directory = session_dir(settings, ticket)
    if not directory.is_dir():
        raise BrainError(f"Session {ticket} does not exist. Run `brain start {ticket}` first.")
    selected = settings.repos(repos)
    state = session_state(settings, ticket)
    number = int(state.get("feedbacks") or 0) + 1
    sections = [
        "# PROJECT BRAIN — IMPLEMENTATION FEEDBACK",
        "",
        f"Ticket: `{ticket}`",
        f"Feedback: `{number:03d}`",
        "",
        "Review the developer's implementation against the ticket, prior evidence, and proposed solution. "
        "Identify correctness gaps, missed callers, compatibility risks, and missing tests. Do not invent "
        "runtime results. If more source evidence is required, return a new CONTEXT_REQUEST.",
        "",
        "## Repository state",
        "",
    ]
    for repo in selected:
        source = (state.get("sources") or {}).get(repo.name) or {}
        sections.append(
            f"- `{repo.name}` — investigation source `{str(source.get('sha') or 'unknown')[:12]}`; "
            f"current local HEAD `{(git_head(repo) or 'unknown')[:12]}`"
        )
    sections.extend(["", "## Developer notes", "", notes.strip() or "No notes supplied.", ""])
    sections.extend(["## Test execution", ""])
    if test_command.strip():
        sections.extend(["Command:", "", "```text", test_command.strip(), "```", ""])
    if test_output.strip():
        sections.extend(["Observed output:", "", "```text", test_output.rstrip(), "```", ""])
    if not test_command.strip() and not test_output.strip():
        sections.extend(["No test result supplied.", ""])
    sections.extend(["## Working-tree changes", ""])
    diffs = working_tree_diffs(settings, [repo.name for repo in selected]) if include_diff else []
    if not include_diff:
        sections.extend(["Diff inclusion was disabled.", ""])
    elif not diffs:
        sections.extend(["No tracked staged or unstaged changes were found in the selected repositories.", ""])
    else:
        for item in diffs:
            sections.extend([f"### {item.repo}", "", "```diff", item.content, "```", ""])
    content = "\n".join(sections).rstrip() + "\n"
    path = directory / f"feedback-{number:03d}.md"
    path.write_text(content, encoding="utf-8")
    state["feedbacks"] = number
    state["status"] = "reviewing_implementation"
    save_session(settings, ticket, state)
    return content, path, number


def chunk_text(text: str, size: int) -> list[str]:
    if size < 1:
        raise BrainError("Chunk size must be positive")
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        split = min(size, len(remaining))
        if split < len(remaining):
            newline = remaining.rfind("\n", 0, split)
            if newline >= size // 2:
                split = newline + 1
        chunks.append(remaining[:split])
        remaining = remaining[split:]
    return chunks


def _clipboard_command(write: bool) -> list[str] | None:
    if sys.platform == "darwin":
        return ["pbcopy" if write else "pbpaste"]
    if shutil.which("wl-copy"):
        return ["wl-copy" if write else "wl-paste", *( [] if write else ["--no-newline"] )]
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard", "-in" if write else "-out"]
    return None


def clipboard_read() -> str:
    command = _clipboard_command(False)
    if not command:
        raise BrainError("No clipboard command found; use --file or stdin")
    result = run(command)
    if result.returncode != 0:
        raise BrainError(f"Clipboard read failed: {result.stderr.strip()}")
    return result.stdout


def clipboard_write(text: str) -> None:
    command = _clipboard_command(True)
    if not command:
        raise BrainError("No clipboard command found; use the generated file")
    result = run(command, input_text=text)
    if result.returncode != 0:
        raise BrainError(f"Clipboard write failed: {result.stderr.strip()}")


def deliver(settings: Settings, ticket: str, text: str, target: str, *, copy: bool) -> tuple[list[Path], int]:
    directory = session_dir(settings, ticket)
    state = session_state(settings, ticket)
    if target == "m365":
        internal_handoff = directory / "current-handoff.md"
        internal_handoff.write_text(text, encoding="utf-8")
        handoff_directory = settings.generated_dir / "handoffs"
        handoff_directory.mkdir(parents=True, exist_ok=True)
        handoff = handoff_directory / f"{directory.name}-current.md"
        handoff.write_text(text, encoding="utf-8")
        paths = [handoff]
        state["delivery"] = {"target": target, "parts": [str(handoff)], "current": 1, "handoff": str(handoff)}
        save_session(settings, ticket, state)
        if copy:
            clipboard_write(text)
        return paths, 1
    parts = chunk_text(text, settings.clipboard_chunk_chars)
    delivery_dir = directory / "delivery"
    delivery_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    total = len(parts)
    for index, part in enumerate(parts, 1):
        header = f"PROJECT BRAIN CONTEXT — PART {index} OF {total}\n\n" if total > 1 else ""
        path = delivery_dir / f"part-{index:03d}.txt"
        path.write_text(header + part, encoding="utf-8")
        paths.append(path)
    state["delivery"] = {"target": target, "parts": [str(path) for path in paths], "current": 1}
    save_session(settings, ticket, state)
    if copy:
        clipboard_write(paths[0].read_text(encoding="utf-8"))
    return paths, 1


def move_delivery(settings: Settings, ticket: str, delta: int) -> tuple[Path, int, int]:
    state = session_state(settings, ticket)
    delivery = state.get("delivery") or {}
    parts = delivery.get("parts") or []
    if not parts:
        raise BrainError(f"No delivery exists for {ticket}")
    current = max(1, min(len(parts), int(delivery.get("current") or 1) + delta))
    delivery["current"] = current
    state["delivery"] = delivery
    save_session(settings, ticket, state)
    path = Path(parts[current - 1])
    clipboard_write(path.read_text(encoding="utf-8"))
    return path, current, len(parts)


def create_learning_template(settings: Settings, ticket: str) -> Path:
    target = settings.knowledge_dir / "tickets" / f"{re.sub(r'[^A-Za-z0-9._-]+', '-', ticket)}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text(
            f"# {ticket}\n\n## Problem\n\n\n## Repositories\n\n\n## Execution Flow\n\n\n## Root Cause\n\n\n## Solution\n\n\n## Tests\n\n\n## Gotchas\n",
            encoding="utf-8",
        )
    return target

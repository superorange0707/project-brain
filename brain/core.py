from __future__ import annotations

import ast
import contextvars
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import tomllib
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from importlib.resources import files as package_files
from pathlib import Path
from typing import Any, Iterable

from .locks import ticket_exclusive, ticket_retrieval_exclusive, ticket_snapshot_exclusive, workspace_exclusive


IGNORED_DIRS = {".git", ".idea", ".venv", "node_modules", "target", "build", "dist"}
SENSITIVE_FILE_NAMES = {".env", ".envrc", "credentials", "credentials.json", "service-account.json", "id_rsa", "id_ed25519", "keystore"}
SENSITIVE_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".jks"}
DISCOVERY_IGNORED_DIRS = IGNORED_DIRS | {".runs", ".codex", ".agents", "state", "generated", "knowledge"}
PROTOCOL_VERSION = 4
LEGACY_DEFAULT_PROTOCOL_VERSION = 1
MAX_REQUEST_ITEMS = 50
MAX_REQUEST_TEXT_CHARS = 100_000
CODE_SUFFIXES = {
    ".adoc", ".avsc", ".bash", ".c", ".cc", ".cfg", ".conf", ".cpp", ".cs", ".csv",
    ".gql", ".go", ".gradle", ".graphql", ".graphqls", ".groovy", ".h", ".hcl", ".hpp",
    ".ini", ".java", ".js", ".json", ".jsx", ".kt", ".kts", ".md", ".mustache", ".php",
    ".properties", ".proto", ".py", ".rb", ".rs", ".rst", ".scala", ".sh", ".sql",
    ".swift", ".tf", ".tfvars", ".toml", ".tpl", ".ts", ".tsx", ".vue", ".xml", ".yaml",
    ".yml", ".zsh",
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
    soft_target_chars: int = 120_000
    hard_context_chars: int = 180_000
    clipboard_chunk_chars: int = 180_000
    graph_enabled: bool = True
    graph_lazy: bool = True
    branch_priority: list[str] = field(default_factory=lambda: ["develop", "development"])
    sync_fetch_scope: str = "selected"
    watch_interval_seconds: int = 180
    path_result_limit: int = 12
    candidate_limit: int = 500
    hydrate_limit: int = 18
    max_regions_per_file: int = 2
    max_regions_per_repo: int = 8
    max_state_gb: int = 200
    minimum_free_disk_gb: int = 5
    experience_enabled: bool = True
    ticket_pattern: str = r"(?<![A-Z0-9])([A-Z][A-Z0-9]+-[0-9]+)(?![A-Z0-9])"
    experience_commit_limit: int = 1000
    experience_similar_cases: int = 5
    experience_patch_chars: int = 0
    model_install_hosts: list[str] = field(default_factory=list)
    model_ca_bundle: Path | None = None
    max_concurrent_investigations: int = 2
    repo_workers: int = 4
    initial_repo_limit: int = 6
    widen_repo_limit: int = 16
    max_effective_operations: int = 15
    max_backend_operations: int = 200
    pre_rerank_candidate_limit: int = 200
    semantic_shard_workers: int = 4
    ticket_prefetch_enabled: bool = True
    context_checkpoint_interval: int = 5
    atlas_generation: Any | None = None
    atlas_generation_mode: str = "current"
    atlas_cards: list[dict[str, Any]] | None = None

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
    experience: str = ""
    unresolved: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    additional_candidates: list[SearchHit] = field(default_factory=list)
    metrics: dict[str, int | float] = field(default_factory=dict)
    trace: dict[str, Any] = field(default_factory=dict)
    atlas_generation: Any | None = None


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


_ACTIVE_RETRIEVAL_TRACE: contextvars.ContextVar[Any | None] = contextvars.ContextVar("brain_retrieval_trace", default=None)
_ACTIVE_RETRIEVAL_CACHE: contextvars.ContextVar[dict[tuple[Any, ...], Any] | None] = contextvars.ContextVar("brain_retrieval_cache", default=None)
_REPO_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="brain-repo")


def _record_backend(name: str, elapsed_ms: float, **values: int | bool) -> None:
    trace = _ACTIVE_RETRIEVAL_TRACE.get()
    if trace is not None:
        trace.add_backend(name, elapsed_ms, **values)


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


def ensure_private_directory(path: Path) -> None:
    """Create Brain-owned state with owner-only permissions on POSIX hosts."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        path.chmod(0o700)


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
    storage = data.get("storage") or {}
    experience = data.get("experience") or {}
    models = data.get("models") or {}
    retrieval = data.get("retrieval") or {}
    branch_priority = sources.get("branch_priority", ["develop", "development"])
    if not isinstance(branch_priority, list):
        raise BrainError("sources.branch_priority must be a list")
    graph_mode = str(graph.get("mode") or "lazy")
    if graph_mode not in {"lazy", "eager"}:
        raise BrainError("graph.mode must be lazy or eager")
    fetch_scope = str(sources.get("fetch_scope") or "selected")
    if fetch_scope not in {"selected", "tracked", "all-branches"}:
        raise BrainError("sources.fetch_scope must be selected, tracked, or all-branches")
    install_hosts = models.get("approved_install_hosts", [])
    if not isinstance(install_hosts, list) or not all(isinstance(host, str) and host.strip() for host in install_hosts):
        raise BrainError("models.approved_install_hosts must be a list of host names")
    ca_bundle = models.get("ca_bundle")
    if ca_bundle is not None and (not isinstance(ca_bundle, str) or not ca_bundle.strip()):
        raise BrainError("models.ca_bundle must be a non-empty CA bundle path when configured")

    def bounded_retrieval(name: str, default: int, maximum: int) -> int:
        try:
            value = int(retrieval.get(name) or default)
        except (TypeError, ValueError) as exc:
            raise BrainError(f"retrieval.{name} must be an integer") from exc
        if not 1 <= value <= maximum:
            raise BrainError(f"retrieval.{name} must be between 1 and {maximum}")
        return value

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
        soft_target_chars=int(context.get("soft_target_chars") or 120_000),
        hard_context_chars=max(10_000, int(context.get("hard_context_chars") or 180_000)),
        clipboard_chunk_chars=int(delivery.get("clipboard_chunk_chars") or 180_000),
        graph_enabled=bool(graph.get("enabled", True)),
        graph_lazy=graph_mode == "lazy",
        branch_priority=[str(value).strip() for value in branch_priority if str(value).strip()],
        sync_fetch_scope=fetch_scope,
        watch_interval_seconds=max(10, int(sources.get("watch_interval_seconds") or 180)),
        path_result_limit=max(1, int(search.get("path_result_limit") or 12)),
        candidate_limit=max(1, int(search.get("candidate_limit") or 500)),
        hydrate_limit=max(1, int(context.get("hydrate_limit") or 18)),
        max_regions_per_file=max(1, int(context.get("max_regions_per_file") or 2)),
        max_regions_per_repo=max(1, int(context.get("max_regions_per_repo") or 8)),
        max_state_gb=max(0, int(storage["max_state_gb"])) if "max_state_gb" in storage else 200,
        minimum_free_disk_gb=max(0, int(storage["minimum_free_disk_gb"])) if "minimum_free_disk_gb" in storage else 5,
        experience_enabled=bool(experience.get("enabled", True)),
        ticket_pattern=str(experience.get("ticket_pattern") or r"(?<![A-Z0-9])([A-Z][A-Z0-9]+-[0-9]+)(?![A-Z0-9])"),
        experience_commit_limit=max(1, int(experience.get("commit_limit") or 1000)),
        experience_similar_cases=max(1, int(experience.get("similar_cases") or 5)),
        experience_patch_chars=max(0, int(experience.get("patch_chars") or 0)),
        model_install_hosts=[host.lower().strip() for host in install_hosts],
        model_ca_bundle=local(ca_bundle.strip()) if isinstance(ca_bundle, str) else None,
        max_concurrent_investigations=bounded_retrieval("max_concurrent_investigations", 2, 8),
        repo_workers=bounded_retrieval("repo_workers", 4, 4),
        initial_repo_limit=bounded_retrieval("initial_repo_limit", 6, 50),
        widen_repo_limit=bounded_retrieval("widen_repo_limit", 16, 100),
        max_effective_operations=bounded_retrieval("max_effective_operations", 15, 100),
        max_backend_operations=bounded_retrieval("max_backend_operations", 200, 500),
        pre_rerank_candidate_limit=bounded_retrieval("pre_rerank_candidate_limit", 200, 500),
        semantic_shard_workers=bounded_retrieval("semantic_shard_workers", 4, 4),
        ticket_prefetch_enabled=bool(retrieval.get("ticket_prefetch_enabled", True)),
        context_checkpoint_interval=bounded_retrieval("context_checkpoint_interval", 5, 100),
    )
    try:
        re.compile(settings.ticket_pattern)
    except re.error as exc:
        raise BrainError(f"Invalid experience.ticket_pattern: {exc}") from exc
    for directory in (settings.state_dir, settings.runs_dir, settings.generated_dir):
        ensure_private_directory(directory)
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
            if name.lower() in SENSITIVE_FILE_NAMES or path.suffix.lower() in SENSITIVE_SUFFIXES:
                continue
            if path.suffix.lower() in CODE_SUFFIXES or name in {
                "Dockerfile", "Jenkinsfile", "Makefile", "Procfile", "build.gradle", "gradlew", "mvnw", "pom.xml"
            }:
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
    from .backends.ripgrep import search as ripgrep_search

    result = ripgrep_search(root, pattern, fixed=fixed, max_results=max_results)
    if result is None:
        started = time.perf_counter()
        hits = _python_search(repo, pattern, fixed, max_results)
        _record_backend("python-fallback", (time.perf_counter() - started) * 1000, files=len(hits), raw_hits=len(hits))
        return hits
    rows, stats = result
    _record_backend(
        "ripgrep",
        float(stats["elapsed_ms"]),
        subprocesses=int(stats["subprocesses"]),
        bytes_scanned=int(stats["bytes_scanned"]),
        files=len({path for path, _, _ in rows}),
        raw_hits=int(stats["raw_hits"]),
    )
    return [
        SearchHit(repo.name, path, line, text, score=95 if fixed else 80, found_by=["ripgrep literal" if fixed else "ripgrep regex"])
        for path, line, text in rows
    ]


def _clone_hits(hits: Iterable[SearchHit]) -> list[SearchHit]:
    return [replace(hit, found_by=list(hit.found_by)) for hit in hits]


def _cached_hits(key: tuple[Any, ...]) -> list[SearchHit] | None:
    cache = _ACTIVE_RETRIEVAL_CACHE.get()
    if cache is None or key not in cache:
        return None
    trace = _ACTIVE_RETRIEVAL_TRACE.get()
    if trace is not None:
        trace.add_cache_hit()
    return _clone_hits(cache[key])


def _store_hits(key: tuple[Any, ...], hits: list[SearchHit]) -> None:
    cache = _ACTIVE_RETRIEVAL_CACHE.get()
    if cache is not None and len(cache) < 256:
        cache[key] = _clone_hits(hits)


def _parallel_repositories(settings: Settings, repositories: list[Repository], operation: Any) -> list[Any]:
    """Use the one shared bounded repository worker pool and preserve input order."""
    if len(repositories) <= 1 or settings.repo_workers <= 1:
        return [operation(repo) for repo in repositories]
    results: list[Any] = []
    for offset in range(0, len(repositories), settings.repo_workers):
        futures = [
            _REPO_EXECUTOR.submit(contextvars.copy_context().run, operation, repo)
            for repo in repositories[offset:offset + settings.repo_workers]
        ]
        results.extend(future.result() for future in futures)
    return results


def search(settings: Settings, pattern: str, repos: Iterable[str] | None = None, *, fixed: bool = False) -> list[SearchHit]:
    from .index import query_index
    from .backends.zoekt import search as zoekt_search

    selected = settings.repos(repos)
    key = ("search", pattern, fixed, tuple(repo.name for repo in selected))
    cached = _cached_hits(key)
    if cached is not None:
        return cached
    trace = _ACTIVE_RETRIEVAL_TRACE.get()
    if trace is not None:
        selected = selected[: trace.physical_budget_remaining]
        if not selected:
            trace.stop_reason = "physical_budget"
            return []

    def one(repo: Repository) -> list[SearchHit]:
        # A busy first repository must not hide evidence in later repositories.
        generation = settings.atlas_generation
        zoekt = (
            zoekt_search(settings, repo, pattern, fixed=fixed, max_results=settings.max_results)
            if generation is None or generation.component("zoekt").get("status") == "ready"
            else None
        )
        if zoekt is not None:
            rows, stats = zoekt
            _record_backend("zoekt", float(stats["elapsed_ms"]), raw_hits=int(stats["raw_hits"]), cache_hit=True)
            return [
                SearchHit(repo.name, path, line, text, score=90 + min(9, score), found_by=["zoekt local shard"])
                for path, line, text, score in rows
            ]
        indexed_started = time.perf_counter()
        indexed = (
            query_index(settings, repo, pattern, max_results=settings.max_results, snapshot_sha=repo.source_sha)
            if fixed and (generation is None or generation.component("lexical").get("status") == "ready")
            else None
        )
        if indexed is None:
            if settings.atlas_generation_mode == "pinned" and repo.source_sha and repo.source_path is None:
                return []
            return search_repo(repo, pattern, fixed=fixed, max_results=settings.max_results)
        _record_backend("sqlite-fts5", (time.perf_counter() - indexed_started) * 1000, raw_hits=len(indexed), cache_hit=True)
        return [
            SearchHit(repo.name, path, line, text, score=95, found_by=["sqlite trigram index"])
            for path, line, text in indexed
        ]

    hits = [hit for rows in _parallel_repositories(settings, selected, one) for hit in rows]
    hits = hits[: settings.max_results * max(1, len(selected))]
    _store_hits(key, hits)
    return _clone_hits(hits)


def path_hits(settings: Settings, query: str, repos: Iterable[str] | None = None) -> list[SearchHit]:
    """Find verified repository-relative paths without searching file contents."""
    needle = query.strip().lower().replace("\\", "/")
    if not needle:
        raise BrainError("Path query is empty")
    tokens = [value for value in re.findall(r"[a-z0-9_.-]+", needle) if len(value) > 1]
    from .index import query_paths

    selected = settings.repos(repos)
    key = ("path", needle, tuple(repo.name for repo in selected))
    cached = _cached_hits(key)
    if cached is not None:
        return cached
    trace = _ACTIVE_RETRIEVAL_TRACE.get()
    if trace is not None:
        selected = selected[: trace.physical_budget_remaining]
        if not selected:
            trace.stop_reason = "physical_budget"
            return []

    def one(repo: Repository) -> list[SearchHit]:
        root = repo.scan_path
        matches: list[SearchHit] = []
        generation = settings.atlas_generation
        indexed_started = time.perf_counter()
        indexed = (
            query_paths(settings, repo, needle, limit=settings.path_result_limit, snapshot_sha=repo.source_sha)
            if generation is None or generation.component("lexical").get("status") == "ready"
            else None
        )
        _record_backend(
            "path-index" if indexed is not None else "path-scan",
            (time.perf_counter() - indexed_started) * 1000,
            raw_hits=len(indexed or []), cache_hit=indexed is not None,
        )
        paths = indexed if indexed is not None else (
            [] if settings.atlas_generation_mode == "pinned" and repo.source_sha and repo.source_path is None
            else (str(path.relative_to(root)) for path in _walk_files(root)) if root.is_dir() else []
        )
        for relative in paths:
            lowered = relative.lower()
            basename = Path(relative).name.lower()
            stem = Path(relative).stem.lower()
            if needle in {lowered, basename, stem}:
                score = 100
            elif needle in lowered:
                score = 95
            elif tokens and all(token in lowered for token in tokens):
                score = 85
            else:
                continue
            matches.append(SearchHit(repo.name, relative, 1, relative, "verified path", score, ["repository path index"]))
        return sorted(matches, key=lambda item: (-item.score, len(item.path), item.path))[: settings.path_result_limit]

    hits = [hit for rows in _parallel_repositories(settings, selected, one) for hit in rows]
    _store_hits(key, hits)
    return _clone_hits(hits)


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
    declaration_re = re.compile(declaration)
    hits = [hit for hit in search(settings, name, scope, fixed=True) if declaration_re.search(hit.text)]
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
    matcher = re.compile(pattern)
    hits = [hit for hit in search(settings, short, repos, fixed=True) if matcher.search(hit.text)]
    for hit in hits:
        hit.kind = "implementation"
        hit.score = 98
        hit.found_by.append("implementation fallback")
    return hits


def test_hits(settings: Settings, name: str, repos: Iterable[str] | None = None) -> list[SearchHit]:
    candidates = search(settings, name.rsplit('.', 1)[-1], repos, fixed=True)
    tests = [hit for hit in candidates if re.search(r"(^|/)(test|tests|src/test)/|(?:Test|Tests|IT|Spec)\.", hit.path, re.I)]
    for hit in tests:
        hit.kind = "test"
        hit.score = 97
        hit.found_by.append("test discovery")
    return tests


def read_source(settings: Settings, hit: SearchHit, *, full: bool = False, lines: tuple[int, int] | None = None) -> Evidence:
    repo = settings.repo(hit.repo)
    root = repo.scan_path.resolve()
    path = (root / hit.path).resolve()
    if not path.is_relative_to(root):
        raise BrainError(f"Unsafe or missing file: {hit.repo}:{hit.path}")
    if path.name.lower() in SENSITIVE_FILE_NAMES or path.suffix.lower() in SENSITIVE_SUFFIXES:
        raise BrainError(f"Sensitive source path is excluded from automatic retrieval: {hit.repo}:{hit.path}")
    indexed_only = settings.atlas_generation_mode == "pinned" and repo.source_sha and repo.source_path is None
    if indexed_only:
        from .index import read_indexed_file

        source = read_indexed_file(settings, repo, hit.path, snapshot_sha=repo.source_sha)
        if source is None:
            raise BrainError(f"Pinned indexed source is unavailable: {hit.repo}:{hit.path}")
    elif path.is_file():
        source = path.read_text(encoding="utf-8", errors="replace")
    else:
        from .index import read_indexed_file

        source = read_indexed_file(settings, repo, hit.path, snapshot_sha=repo.source_sha)
        if source is None:
            raise BrainError(f"Unsafe or missing file: {hit.repo}:{hit.path}")
    content = source.splitlines()
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
    invocation = re.compile(rf"\b{re.escape(name)}\s*\(")
    uses = [hit for hit in search(settings, name, scope, fixed=True) if invocation.search(hit.text)]
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
            hit.score = 96
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


@workspace_exclusive
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
    if len(text) > MAX_REQUEST_TEXT_CHARS:
        raise BrainError(f"Project Brain request exceeds the {MAX_REQUEST_TEXT_CHARS:,}-character input limit")

    loaded: Any = None
    if stripped.startswith("{"):
        try:
            loaded = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise BrainError(f"Invalid CONTEXT_REQUEST JSON: line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc

    if loaded is None:
        # A textarea or copied chat can contain an earlier request followed by
        # the AI's new one. The newest directive is the one to execute.
        markers = [(text.rfind(name + ":"), name) for name in ("CONTEXT_REQUEST", "INVESTIGATION_REQUEST")]
        marker, marker_name = max(markers)
        if marker < 0:
            raise BrainError(
                "Input does not contain CONTEXT_REQUEST: or INVESTIGATION_REQUEST:. Copy the AI's complete response, "
                "or ask it to return one Project Brain request block."
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

    if isinstance(loaded, dict) and not ({"CONTEXT_REQUEST", "INVESTIGATION_REQUEST"} & set(loaded)) and "objective" in loaded:
        wrapper = "INVESTIGATION_REQUEST" if loaded.get("version") == 4 else "CONTEXT_REQUEST"
        loaded = {wrapper: loaded}
    if not isinstance(loaded, dict):
        raise BrainError("Project Brain request must be a YAML mapping or JSON object")
    wrappers = [name for name in ("CONTEXT_REQUEST", "INVESTIGATION_REQUEST") if isinstance(loaded.get(name), dict)]
    if len(wrappers) != 1:
        raise BrainError("Provide exactly one CONTEXT_REQUEST or INVESTIGATION_REQUEST mapping")
    wrapper = wrappers[0]
    unknown_wrapper = sorted(set(loaded) - {wrapper, "version"})
    if unknown_wrapper:
        raise BrainError(f"CONTEXT_REQUEST wrapper has unknown keys: {', '.join(unknown_wrapper)}")
    request = dict(loaded[wrapper])
    version = request.get("version", loaded.get("version", LEGACY_DEFAULT_PROTOCOL_VERSION))
    if version not in {1, 2, 3, 4}:
        raise BrainError(f"Unsupported Project Brain request version {version!r}; this build supports versions 1, 2, 3, and 4")
    if version == 4 and wrapper != "INVESTIGATION_REQUEST":
        raise BrainError("version 4 must use the INVESTIGATION_REQUEST wrapper")
    request["version"] = version
    allowed = {
        1: {"version", "objective", "searches", "paths", "symbols", "files", "history", "expand"},
        2: {"version", "objective", "searches", "paths", "symbols", "files", "history", "expand"},
        3: {"version", "objective", "hints", "coverage", "expand"},
        4: {"version", "objective", "runtime_facts", "hypotheses", "required", "resolve", "base_context_id", "checkpoint"},
    }[version]
    unknown = sorted(set(request) - allowed)
    if unknown:
        raise BrainError(f"CONTEXT_REQUEST has unknown keys: {', '.join(unknown)}")
    objective = str(request.get("objective") or "").strip()
    if not objective:
        raise BrainError("objective is required")
    if len(objective) > 4_000:
        raise BrainError("objective exceeds the 4,000-character limit")
    request["objective"] = objective

    if version == 4:
        for key in ("runtime_facts", "hypotheses", "required", "resolve"):
            value = request.get(key) or []
            if not isinstance(value, list) or len(value) > MAX_REQUEST_ITEMS:
                raise BrainError(f"{key} must be a list of at most {MAX_REQUEST_ITEMS} items")
            normalized: list[str] = []
            for index, item in enumerate(value):
                text_value = str(item).strip()
                if not text_value or len(text_value) > 500:
                    raise BrainError(f"{key}[{index}] must be a non-empty value up to 500 characters")
                normalized.append(text_value)
            request[key] = list(dict.fromkeys(normalized))
        base_context_id = str(request.get("base_context_id") or "").strip()
        if len(base_context_id) > 200:
            raise BrainError("base_context_id exceeds 200 characters")
        request["base_context_id"] = base_context_id or None
        if not isinstance(request.get("checkpoint", False), bool):
            raise BrainError("checkpoint must be true or false")
        request["checkpoint"] = bool(request.get("checkpoint"))
        from .retrieval.planner import objective_terms

        resolve_terms = [*request["resolve"], *objective_terms(objective, limit=8)]
        request["searches"] = [{"query": value, "repos": []} for value in list(dict.fromkeys(resolve_terms))[:12]]
        request["paths"] = []
        request["symbols"] = []
        request["files"] = []
        request["history"] = [
            {"query": value, "repos": []} for value in request["resolve"]
            if any(token in value.lower() for token in ("history", "commit", "change", "ticket"))
        ][:4]
        request["expand"] = []
        required_text = " ".join(request["required"]).lower()
        request["coverage"] = {
            "production": "required",
            "tests": "required" if "test" in required_text else "auto",
            "relationships": "required" if any(value in required_text for value in ("flow", "integration", "relationship", "graph")) else "auto",
            "configuration": "required" if "config" in required_text else "auto",
            "history": "required" if any(value in required_text for value in ("history", "change", "commit")) else "auto",
        }
    elif version == 3:
        hints = request.get("hints") or {}
        coverage = request.get("coverage") or {}
        if not isinstance(hints, dict):
            raise BrainError("hints must be a mapping")
        if not isinstance(coverage, dict):
            raise BrainError("coverage must be a mapping")
        hint_keys = {"repos", "literals", "symbols", "paths", "files", "history"}
        coverage_keys = {"production", "tests", "relationships", "configuration", "history"}
        unknown_hints = sorted(set(hints) - hint_keys)
        unknown_coverage = sorted(set(coverage) - coverage_keys)
        if unknown_hints:
            raise BrainError(f"hints has unknown keys: {', '.join(unknown_hints)}")
        if unknown_coverage:
            raise BrainError(f"coverage has unknown keys: {', '.join(unknown_coverage)}")
        normalized_hints: dict[str, list[Any]] = {}
        for key in sorted(hint_keys):
            value = hints.get(key, [])
            if value is None:
                value = []
            if not isinstance(value, list):
                raise BrainError(f"hints.{key} must be a list")
            if len(value) > MAX_REQUEST_ITEMS:
                raise BrainError(f"hints.{key} exceeds the {MAX_REQUEST_ITEMS}-item limit")
            normalized_hints[key] = value
        normalized_coverage = {key: str(coverage.get(key) or ("required" if key == "production" else "auto")) for key in coverage_keys}
        for key, value in normalized_coverage.items():
            if value not in {"required", "auto", "omit"}:
                raise BrainError(f"coverage.{key} must be required, auto, or omit")
        repos = [str(value).strip() for value in normalized_hints["repos"]]
        if any(not value or len(value) > 200 for value in repos):
            raise BrainError("hints.repos entries must be non-empty repository names")
        repos = list(dict.fromkeys(repos))

        def text_hints(key: str) -> list[str]:
            values: list[str] = []
            for index, value in enumerate(normalized_hints[key]):
                if not isinstance(value, str) or not value.strip() or len(value) > 500:
                    raise BrainError(f"hints.{key}[{index}] must be a non-empty string up to 500 characters")
                values.append(value.strip())
            return list(dict.fromkeys(values))

        from .retrieval.planner import objective_terms

        literals = text_hints("literals")
        if not literals:
            literals = objective_terms(objective)
        symbols = text_hints("symbols")
        paths = text_hints("paths")
        histories = text_hints("history")
        files: list[dict[str, Any]] = []
        for index, value in enumerate(normalized_hints["files"]):
            if not isinstance(value, dict) or not value.get("repo") or not value.get("path"):
                raise BrainError(f"hints.files[{index}] requires repo and path")
            if set(value) - {"repo", "path", "lines"}:
                raise BrainError(f"hints.files[{index}] has unknown keys")
            files.append({key: value[key] for key in ("repo", "path", "lines") if key in value})
        includes = ["definition"]
        if normalized_coverage["relationships"] != "omit":
            includes.extend(["callers", "callees"])
        if normalized_coverage["tests"] != "omit":
            includes.append("tests")
        request["hints"] = {**normalized_hints, "repos": repos}
        request["coverage"] = normalized_coverage
        request["searches"] = [{"query": value, "repos": repos} for value in literals[:8]]
        request["paths"] = [{"query": value, "repos": repos} for value in paths]
        request["symbols"] = [{"name": value, "repos": repos, "include": includes} for value in symbols]
        request["files"] = files
        request["history"] = [{"query": value, "repos": repos} for value in histories] if normalized_coverage["history"] != "omit" else []

    for key in ("searches", "paths", "symbols", "files", "history", "expand"):
        value = request.get(key, [])
        if value is None:
            request[key] = []
        elif not isinstance(value, list):
            raise BrainError(f"{key} must be a list")
        else:
            if len(value) > MAX_REQUEST_ITEMS:
                raise BrainError(f"{key} exceeds the {MAX_REQUEST_ITEMS}-item limit")
            request[key] = value
    return request


def parse_context_request(text: str) -> dict[str, Any]:
    request = _request_body(text)
    for index, item in enumerate(request["searches"]):
        if not isinstance(item, dict) or not str(item.get("query") or "").strip():
            raise BrainError(f"searches[{index}].query is required")
        if set(item) - {"query", "repos"}:
            raise BrainError(f"searches[{index}] has unknown keys")
        if len(str(item["query"])) > 500:
            raise BrainError(f"searches[{index}].query exceeds 500 characters")
        _requested_repos(item)
    for index, item in enumerate(request["paths"]):
        if not isinstance(item, dict) or not str(item.get("query") or "").strip():
            raise BrainError(f"paths[{index}].query is required")
        if set(item) - {"query", "repos"}:
            raise BrainError(f"paths[{index}] has unknown keys")
        _requested_repos(item)
    allowed = {"definition", "callers", "callees", "implementations", "tests"}
    for index, item in enumerate(request["symbols"]):
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            raise BrainError(f"symbols[{index}].name is required")
        if set(item) - {"name", "repos", "include"}:
            raise BrainError(f"symbols[{index}] has unknown keys")
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
        if set(item) - {"repo", "path", "lines"}:
            raise BrainError(f"files[{index}] has unknown keys")
        if item.get("lines") and not re.fullmatch(r"\s*\d+\s*[-:]\s*\d+\s*", str(item["lines"])):
            raise BrainError(f"files[{index}].lines must look like 10-40")
        if item.get("lines"):
            start, end = (int(value) for value in re.split(r"[-:]", str(item["lines"])))
            if start < 1 or end < start or end - start > 2_000:
                raise BrainError(f"files[{index}].lines is outside the safe range")
        relative = str(item["path"])
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise BrainError(f"Unsafe file path: {item['repo']}:{relative}")
    for index, item in enumerate(request["history"]):
        if not isinstance(item, dict) or not str(item.get("query") or "").strip():
            raise BrainError(f"history[{index}].query is required")
        if set(item) - {"query", "repos"}:
            raise BrainError(f"history[{index}] has unknown keys")
        _requested_repos(item)
    for index, item in enumerate(request["expand"]):
        if not re.fullmatch(r"C[1-9][0-9]*", str(item)):
            raise BrainError(f"expand[{index}] must be a candidate id such as C12")
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

    if settings and request["version"] == 3:
        settings.repos((request.get("hints") or {}).get("repos") or [])

    for item in request["searches"]:
        actions.append({"kind": "search", "value": str(item["query"]), "repos": repos_for(item)})
    for item in request["paths"]:
        actions.append({"kind": "path", "value": str(item["query"]), "repos": repos_for(item)})
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
    for item in request["expand"]:
        actions.append({"kind": "expand", "value": str(item), "repos": []})

    if not actions and request["version"] in {1, 2}:
        raise BrainError("CONTEXT_REQUEST contains no repository operations")
    signature = hashlib.sha256(
        json.dumps(
            {"objective": request["objective"], "coverage": request.get("coverage"), "actions": sorted(actions, key=lambda item: json.dumps(item, sort_keys=True))},
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    from .retrieval import compile_request, explain_plan

    planner = explain_plan(compile_request(request, max_effective_operations=settings.max_effective_operations if settings else 15))
    public_request = (
        {key: request[key] for key in ("version", "objective", "hints", "coverage", "expand") if key in request}
        if request["version"] == 3
        else {key: request[key] for key in ("version", "objective", "runtime_facts", "hypotheses", "required", "resolve", "base_context_id", "checkpoint") if key in request}
        if request["version"] == 4
        else {key: request[key] for key in ("version", "objective", "searches", "paths", "symbols", "files", "history", "expand") if key in request}
    )
    return {
        "valid": True,
        "protocol_version": request["version"],
        "objective": str(request["objective"]).strip(),
        "request": request,
        "actions": actions,
        "operation_count": len(actions),
        "effective_operation_count": int(planner["effective_operations"]),
        "signature": signature,
        "counts": {
            "searches": len(request["searches"]),
            "paths": len(request["paths"]),
            "symbols": len(request["symbols"]),
            "files": len(request["files"]),
            "history": len(request["history"]),
        },
        "planner": planner,
        "normalized_json": json.dumps({"INVESTIGATION_REQUEST" if request["version"] == 4 else "CONTEXT_REQUEST": public_request}, indent=2, ensure_ascii=False) + "\n",
    }


def request_repair_prompt(error: str) -> str:
    """Build a safe prompt the user can copy back when the model broke protocol."""
    return (
        "Your previous response could not be executed by Project Brain.\n\n"
        f"Validation error: {error}\n\n"
        "Return only one minimal fenced YAML block. State the repository fact to establish; do not invent repository names or enumerate command matrices.\n\n"
        "Legacy CONTEXT_REQUEST version: 1, version: 2, and version: 3 remain supported; new repairs use investigation protocol v4.\n\n"
        "```yaml\n"
        "INVESTIGATION_REQUEST:\n"
        f"  version: {PROTOCOL_VERSION}\n"
        "  objective: State the next repository fact that must be established.\n"
        "```\n"
    )


def _requested_repos(item: dict[str, Any]) -> list[str]:
    repos = item.get("repos") or []
    if not isinstance(repos, list):
        raise BrainError("repos must be a list")
    if len(repos) > MAX_REQUEST_ITEMS:
        raise BrainError(f"repos exceeds the {MAX_REQUEST_ITEMS}-item limit")
    values = [str(repo).strip() for repo in repos]
    if any(not value or len(value) > 200 for value in values):
        raise BrainError("repos entries must be non-empty repository names")
    return list(dict.fromkeys(values))


def _direct_file(settings: Settings, item: dict[str, Any]) -> Evidence | None:
    repo = settings.repo(str(item["repo"]))
    relative = str(item["path"])
    root = repo.scan_path.resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise BrainError(f"Unsafe file path: {repo.name}:{relative}")
    requested = item.get("lines")
    line_range: tuple[int, int] | None = None
    if requested:
        match = re.fullmatch(r"\s*(\d+)\s*[-:]\s*(\d+)\s*", str(requested))
        if not match:
            raise BrainError(f"Invalid line range `{requested}`; use 10-40")
        line_range = int(match.group(1)), int(match.group(2))
    hit = SearchHit(repo.name, str(path.relative_to(root)), line_range[0] if line_range else 1, "", "requested file", 100, ["direct file request"])
    try:
        return read_source(settings, hit, full=line_range is None, lines=line_range)
    except BrainError:
        return None


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


def retrieve_context(
    settings: Settings,
    request: dict[str, Any],
    *,
    include_diff: bool = False,
    progress: Any | None = None,
) -> ContextBundle:
    """Run a routed, fused, budgeted retrieval while preserving exact-source hydration."""
    started = time.perf_counter()
    catalog_started = time.perf_counter()
    if settings.atlas_generation is None and settings.atlas_generation_mode == "current":
        from .catalog import current_generation_ref

        settings = replace(settings, atlas_generation=current_generation_ref(settings))
    catalog_open_ms = (time.perf_counter() - catalog_started) * 1000
    bundle = ContextBundle(str(request["objective"]).strip(), atlas_generation=settings.atlas_generation)
    from .retrieval import compile_request
    from .retrieval.models import RetrievalTrace
    from .retrieval.planner import route_repositories

    trace = RetrievalTrace(max_physical_backend_operations=settings.max_backend_operations)
    trace_token = _ACTIVE_RETRIEVAL_TRACE.set(trace)
    cache_token = _ACTIVE_RETRIEVAL_CACHE.set({})

    def emit(phase: str, **details: Any) -> None:
        if progress is not None:
            progress({"phase": phase, "elapsed_ms": (time.perf_counter() - started) * 1000, **details})

    try:
        emit("planning")
        stage = time.perf_counter()
        compiled_plan = compile_request(request, max_effective_operations=settings.max_effective_operations)
        trace.requested_operations = compiled_plan.requested_operations
        trace.effective_operations = len(compiled_plan.operations)
        trace.add_stage("planning_ms", (time.perf_counter() - stage) * 1000)
        if compiled_plan.deferred_operations:
            trace.stop_reason = "operation_budget"

        candidates: list[SearchHit] = []
        from .atlas import route as route_atlas

        atlas_started = time.perf_counter()
        atlas_route = route_atlas(
            settings,
            bundle.objective,
            request,
            bundle.atlas_generation,
            repo_limit=max(settings.widen_repo_limit, settings.initial_repo_limit),
            entity_limit=settings.pre_rerank_candidate_limit,
        )
        atlas_route_ms = (time.perf_counter() - atlas_started) * 1000
        trace.add_stage("atlas_route_ms", atlas_route_ms)
        first_repo_ms = (time.perf_counter() - started) * 1000 if atlas_route.get("repos") else None
        first_entity_ms = (time.perf_counter() - started) * 1000 if atlas_route.get("candidates") else None
        first_verified_evidence_ms: float | None = None
        candidates.extend(
            SearchHit(
                str(item["repo"]), str(item["path"]), int(item["line"]), "",
                f"Atlas {item.get('kind') or 'entity'} candidate", round(float(item.get("score") or 0), 3),
                list(item.get("found_by") or ["Atlas hierarchical router"]),
            )
            for item in atlas_route.get("candidates") or []
        )
        bundle.relationships.extend(
            f"{item.get('source_repo') or item['repo']}:{item['source_id']}  {item['edge_type']}  "
            f"{item['repo']}:{item['target_id']} | provenance {item['repo']}:{item['path']}:{item['line']} | confidence {item['confidence']}"
            for item in atlas_route.get("graph_edges") or []
        )
        cross_repo_relationships = any(
            str(item.get("source_repo") or item.get("repo")) != str(item.get("repo"))
            for item in atlas_route.get("graph_edges") or []
        )
        atlas_repo_scope = [str(name) for name in atlas_route.get("repos") or []]
        emit("global_discovery", requested_operations=trace.requested_operations, effective_operations=trace.effective_operations)
        discovery_started = time.perf_counter()
        search_operations = [item for item in compiled_plan.operations if item.kind == "search"]
        lexical_started = time.perf_counter()
        for operation in search_operations:
            repos = list(operation.repos) or atlas_repo_scope[: settings.initial_repo_limit]
            hits = search(settings, operation.value, repos, fixed=True)
            if not hits:
                try:
                    hits = search(settings, operation.value, repos)
                except BrainError:
                    hits = []
            if not hits and not operation.repos and repos and len(repos) < len(settings.repositories):
                hits = search(settings, operation.value, atlas_repo_scope[: settings.widen_repo_limit], fixed=True)
            if not hits and not operation.repos and len(atlas_repo_scope) < len(settings.repositories):
                hits = search(settings, operation.value, [], fixed=True)
            if not hits:
                bundle.unresolved.append(f"Search `{operation.value}` returned no code matches in {repos or ['all repositories']}")
            candidates.extend(hits)
            bundle.evidence.extend(knowledge_hits(settings, operation.value))
        trace.add_stage("exact_lexical_ms", (time.perf_counter() - lexical_started) * 1000)

        semantic_started = time.perf_counter()
        semantic_repo_scores: dict[str, float] = {}
        try:
            from .editions import current_edition

            edition = current_edition(settings)
            if edition in {"semantic", "precision"}:
                emit("semantic", candidate_count=len(candidates))
                from .semantic import search_semantic

                semantic = search_semantic(
                    settings,
                    bundle.objective,
                    repos={repo.name for repo in settings.repositories},
                    trace=trace,
                    generation=bundle.atlas_generation,
                )
                for item in semantic:
                    semantic_repo_scores[str(item["repo"])] = max(
                        semantic_repo_scores.get(str(item["repo"]), -1.0), float(item.get("score") or 0)
                    )
                candidates.extend(
                    SearchHit(
                        str(item["repo"]), str(item["path"]), int(item["line"]), "", "semantic candidate",
                        round(50 + float(item.get("score") or 0) * 50, 3), ["local semantic index"],
                    )
                    for item in semantic
                    if str(item.get("path") or "")
                    and (not str(item.get("kind") or "").startswith("atlas_") or item.get("kind") == "atlas_entity_card")
                )
                if not semantic:
                    bundle.warnings.append("Semantic index is unavailable or stale; used Core retrieval only.")
        except (OSError, ValueError, RuntimeError):
            bundle.warnings.append("Semantic runtime failed; used Core retrieval only.")
            trace.fallback_reasons.append("semantic_runtime")
        trace.add_stage("semantic_ms", (time.perf_counter() - semantic_started) * 1000)
        trace.add_stage("candidate_discovery_ms", (time.perf_counter() - discovery_started) * 1000)

        emit("repo_routing", candidate_count=len(candidates))
        routing_started = time.perf_counter()
        fallback_repos = route_repositories(settings.repositories, request, candidates, limit=len(settings.repositories))
        semantic_repos = sorted(semantic_repo_scores, key=lambda name: (-semantic_repo_scores[name], name))
        explicit_repos = list(dict.fromkeys(
            str(repo) for operation in compiled_plan.operations for repo in operation.repos
        ))
        ordered_repos = list(dict.fromkeys([*explicit_repos, *semantic_repos, *atlas_repo_scope, *fallback_repos]))
        if first_repo_ms is None and ordered_repos:
            first_repo_ms = (time.perf_counter() - started) * 1000
        trace.repo_candidates = len(ordered_repos)
        initial_count = min(settings.initial_repo_limit, len(ordered_repos))
        widen_count = min(max(initial_count, settings.widen_repo_limit), len(ordered_repos))
        scopes = [ordered_repos[:initial_count]]
        if widen_count > initial_count:
            scopes.append(ordered_repos[:widen_count])
        if len(ordered_repos) > widen_count:
            scopes.append(ordered_repos)
        trace.initial_repo_scope = list(scopes[0])
        trace.final_repo_scope = list(scopes[0])
        trace.add_stage("repo_routing_ms", (time.perf_counter() - routing_started) * 1000)

        targeted = [item for item in compiled_plan.operations if item.kind not in {"search", "file"}]
        unscoped = any(not item.repos for item in targeted)
        seen_wave_repos: set[str] = set()

        def scope_for(operation: Any, wave: list[str]) -> list[str]:
            return list(operation.repos) if operation.repos else [name for name in wave if name not in seen_wave_repos]

        def needs_widening() -> bool:
            coverage = request.get("coverage") or {}
            production = any("test" not in item.kind.lower() for item in candidates)
            tests = any("test" in item.kind.lower() or re.search(r"(^|/)(test|tests|src/test)/", item.path, re.I) for item in candidates)
            relationships = bool(bundle.relationships)
            return (
                not production
                or (coverage.get("tests") == "required" and not tests)
                or (coverage.get("relationships") == "required" and not relationships)
            )

        for wave_number, wave in enumerate(scopes):
            if wave_number and (not unscoped or not needs_widening()):
                break
            if trace.physical_budget_remaining <= 0:
                trace.stop_reason = "physical_budget"
                break
            if wave_number:
                trace.widening_rounds += 1
            trace.final_repo_scope = list(wave)
            emit("targeted_retrieval", repo_current=0, repo_total=len(wave), candidate_count=len(candidates))
            for operation in targeted:
                repos = scope_for(operation, wave)
                if not repos:
                    continue
                if operation.kind == "path":
                    operation_started = time.perf_counter()
                    hits = path_hits(settings, operation.value, repos)
                    trace.add_stage("path_ms", (time.perf_counter() - operation_started) * 1000)
                    candidates.extend(hits)
                    if not hits:
                        bundle.unresolved.append(f"Path search `{operation.value}` returned no matches in {repos}")
                elif operation.kind == "symbol":
                    operation_started = time.perf_counter()
                    include = set(operation.includes)
                    definitions = symbol_hits(settings, operation.value, repos)
                    if "definition" in include:
                        candidates.extend(definitions)
                        if not definitions:
                            bundle.unresolved.append(f"Definition for `{operation.value}` was not found")
                    if include & {"callers", "callees"}:
                        traced, relationships = trace_symbol(settings, operation.value, repos)
                        candidates.extend(traced)
                        bundle.relationships.extend(relationships)
                        if not relationships:
                            bundle.unresolved.append(f"No static call evidence found for `{operation.value}`")
                    if "implementations" in include:
                        implementations = implementation_hits(settings, operation.value, repos)
                        candidates.extend(implementations)
                        if not implementations:
                            bundle.unresolved.append(f"No implementations found for `{operation.value}`")
                    if "tests" in include:
                        tests = test_hits(settings, operation.value, repos)
                        candidates.extend(tests)
                        if not tests:
                            bundle.unresolved.append(f"No tests referencing `{operation.value}` were found")
                    trace.add_stage("symbol_ms", (time.perf_counter() - operation_started) * 1000)
                elif operation.kind == "history":
                    operation_started = time.perf_counter()
                    selected_repos = settings.repos(repos)[: trace.physical_budget_remaining]

                    def history_one(repo: Repository) -> tuple[str, str]:
                        history_started = time.perf_counter()
                        result = git_history(repo, operation.value)
                        _record_backend("history", (time.perf_counter() - history_started) * 1000, subprocesses=1)
                        return repo.name, result

                    rows = _parallel_repositories(settings, selected_repos, history_one)
                    found = False
                    for repo_name, result in rows:
                        if result:
                            found = True
                            bundle.history.append(f"## {repo_name}: `{operation.value}`\n\n```text\n{result}\n```")
                    if not found:
                        bundle.unresolved.append(f"No Git history found for `{operation.value}`")
                    trace.add_stage("history_ms", (time.perf_counter() - operation_started) * 1000)
            seen_wave_repos.update(wave)
            emit(
                "targeted_retrieval",
                repo_current=len(wave), repo_total=len(wave), candidate_count=len(candidates),
                physical_operations_completed=trace.physical_backend_operations,
            )

        file_values = {item.value for item in compiled_plan.operations if item.kind == "file"}
        for item in request["files"]:
            value = f"{item['repo']}:{item['path']}" + (f":{item['lines']}" if item.get("lines") else "")
            if value not in file_values:
                continue
            evidence = _direct_file(settings, item)
            if evidence:
                bundle.evidence.append(evidence)
                trace.bytes_read += len(evidence.content.encode("utf-8", errors="replace"))
                if first_verified_evidence_ms is None:
                    first_verified_evidence_ms = (time.perf_counter() - started) * 1000
            else:
                bundle.unresolved.append(f"Requested file `{item['repo']}:{item['path']}` was not found")

        if include_diff:
            bundle.evidence.extend(working_tree_diffs(settings))

        experience_started = time.perf_counter()
        if settings.experience_enabled:
            from .experience import render_similar_cases

            bundle.experience = render_similar_cases(
                settings,
                bundle.objective,
                generation=bundle.atlas_generation,
            )
        trace.add_stage("experience_ms", (time.perf_counter() - experience_started) * 1000)

        relation_started = time.perf_counter()
        from .relations import related_relationships

        relationship_queries = [bundle.objective]
        relationship_queries.extend(str(item["query"]) for item in request["searches"])
        relationship_queries.extend(str(item["query"]) for item in request["paths"])
        relationship_queries.extend(str(item["name"]) for item in request["symbols"])
        related = related_relationships(
            settings,
            relationship_queries,
            {
                *((item.repo, item.path) for item in candidates),
                *((item.repo, item.path) for item in bundle.evidence if item.repo not in {"external", "knowledge"}),
            },
            generation=bundle.atlas_generation,
        )
        cross_repo_relationships = cross_repo_relationships or any(
            relationship.source != relationship.target for relationship in related
        )
        for relationship in related:
            bundle.relationships.append(
                f"{relationship.summary()} | source {relationship.source_evidence} | target {relationship.target_evidence}"
            )
            for location in (relationship.source_evidence, relationship.target_evidence):
                match = re.fullmatch(r"([^:]+):(.+):(\d+)", location)
                if match:
                    candidates.append(SearchHit(match.group(1), match.group(2), int(match.group(3)), "", "contract relationship", 92, [f"{relationship.kind} contract graph"]))
        bundle.relationships = list(dict.fromkeys(bundle.relationships))
        trace.add_stage("relationship_ms", (time.perf_counter() - relation_started) * 1000)
        trace.add_stage("graph_ms", 0.0)

        emit("candidate_pruning", candidate_count=len(candidates))
        from .query import merge_evidence, prune_candidates, select_candidates

        prune_started = time.perf_counter()
        trace.unique_candidates_before_prune = len({(item.repo, item.path, item.line) for item in candidates})
        raw_candidates = len(candidates)
        candidates, early_omitted = prune_candidates(settings, candidates, settings.pre_rerank_candidate_limit)
        trace.candidates_after_prune = len(candidates)
        if first_entity_ms is None and candidates:
            first_entity_ms = (time.perf_counter() - started) * 1000
        trace.add_stage("candidate_pruning_ms", (time.perf_counter() - prune_started) * 1000)
        trace.add_stage("dedup_fusion_ms", 0.0)

        emit("reranking", pruned_candidate_count=len(candidates))
        rerank_started = time.perf_counter()
        try:
            from .editions import current_edition

            if current_edition(settings) == "precision":
                from .models import rerank_candidates

                trace.rerank_input_count = len(candidates)
                requested = [name for name in ("searches", "paths", "symbols", "files", "history") if request.get(name)]
                rerank_query = bundle.objective + ("\nRequested evidence: " + ", ".join(requested) if requested else "")
                candidates = rerank_candidates(settings, rerank_query, candidates, trace=trace)
        except (OSError, ValueError, RuntimeError):
            bundle.warnings.append("Local reranker failed; used semantic/lexical candidate ranking.")
            trace.fallback_reasons.append("reranker_runtime")
        trace.add_stage("rerank_ms", (time.perf_counter() - rerank_started) * 1000)

        selection_started = time.perf_counter()
        selected, omitted = select_candidates(settings, candidates)
        omitted.extend(early_omitted)
        trace.add_stage("selection_ms", (time.perf_counter() - selection_started) * 1000)

        emit("hydrating", evidence_count=len(bundle.evidence), candidate_count=len(selected))
        hydrate_started = time.perf_counter()
        source_budget = max(10_000, settings.hard_context_chars - 40_000)
        source_chars = sum(len(item.content) for item in bundle.evidence)
        for hit in selected:
            try:
                evidence = read_source(settings, hit)
            except BrainError:
                bundle.warnings.append(f"Candidate source disappeared before hydration: {hit.repo}:{hit.path}")
                continue
            if source_chars and source_chars + len(evidence.content) > source_budget:
                omitted.append(hit)
                trace.stop_reason = "context_budget"
                continue
            bundle.evidence.append(evidence)
            source_chars += len(evidence.content)
            trace.bytes_read += len(evidence.content.encode("utf-8", errors="replace"))
            if first_verified_evidence_ms is None:
                first_verified_evidence_ms = (time.perf_counter() - started) * 1000
        trace.add_stage("source_hydration_ms", (time.perf_counter() - hydrate_started) * 1000)
        bundle.additional_candidates = sorted(omitted, key=lambda item: (-item.score, item.repo, item.path, item.line))
        bundle.evidence = merge_evidence(bundle.evidence)

        if bundle.atlas_generation is None:
            state = load_index_state(settings)
            for repo in settings.repositories:
                current = repo.source_sha or git_head(repo)
                indexed = (state.get(repo.name) or {}).get("sha")
                if indexed and current and indexed != current:
                    bundle.warnings.append(f"Index for {repo.name} is stale: indexed {indexed[:12]}, source {current[:12]}.")

        trace.unique_candidates = len(candidates)
        trace.hydrated_regions = len(bundle.evidence)
        trace.deferred_candidates = len(bundle.additional_candidates)
        trace.stage_ms["candidate_discovery_ms"] = round(sum(
            trace.stage_ms.get(name, 0.0)
            for name in ("exact_lexical_ms", "semantic_ms", "path_ms", "symbol_ms", "history_ms", "relationship_ms", "experience_ms")
        ), 3)
        total_ms = (time.perf_counter() - started) * 1000
        bundle.metrics = {
            "candidate_ms": round(trace.stage_ms.get("candidate_discovery_ms", 0.0), 3),
            "hydrate_ms": round(trace.stage_ms.get("source_hydration_ms", 0.0), 3),
            "planning_ms": trace.stage_ms.get("planning_ms", 0.0),
            "catalog_open_ms": round(catalog_open_ms, 3),
            "atlas_route_ms": trace.stage_ms.get("atlas_route_ms", 0.0),
            "repo_routing_ms": trace.stage_ms.get("repo_routing_ms", 0.0),
            "candidate_pruning_ms": trace.stage_ms.get("candidate_pruning_ms", 0.0),
            "rerank_ms": trace.stage_ms.get("rerank_ms", 0.0),
            "selection_ms": trace.stage_ms.get("selection_ms", 0.0),
            "source_hydration_ms": trace.stage_ms.get("source_hydration_ms", 0.0),
            "total_ms": round(total_ms, 3),
            "candidates": len(candidates),
            "raw_candidates": raw_candidates,
            "hydrated_regions": len(bundle.evidence),
            "deferred_candidates": len(bundle.additional_candidates),
            "late_candidates": len(bundle.additional_candidates),
            "rerank_input_count": trace.rerank_input_count,
            "time_to_first_repo_ms": round(first_repo_ms, 3) if first_repo_ms is not None else None,
            "time_to_first_entity_ms": round(first_entity_ms, 3) if first_entity_ms is not None else None,
            "time_to_first_verified_evidence_ms": (
                round(first_verified_evidence_ms, 3) if first_verified_evidence_ms is not None else None
            ),
            "requested_operations": trace.requested_operations,
            "effective_operations": trace.effective_operations,
            "physical_backend_operations": trace.physical_backend_operations,
        }
        bundle.trace = trace.as_dict()
        bundle.trace["cross_repo_relationships"] = cross_repo_relationships
        bundle.trace["atlas_generation"] = (
            bundle.atlas_generation.generation if bundle.atlas_generation is not None else None
        )
        bundle.trace["atlas_generation_id"] = (
            bundle.atlas_generation.identity if bundle.atlas_generation is not None else None
        )
        bundle.trace["atlas_components"] = (
            {name: value.get("status") for name, value in bundle.atlas_generation.components.items()}
            if bundle.atlas_generation is not None
            else {}
        )
        bundle.trace["atlas_route"] = {
            "cache_hit": bool(atlas_route.get("cache_hit")),
            "prefetch_reused": int(atlas_route.get("prefetch_reused") or 0),
            "investigation_reused": int(atlas_route.get("investigation_reused") or 0),
            "repositories": list(atlas_route.get("repos") or []),
            "modules": list(atlas_route.get("modules") or []),
            "entities": len(atlas_route.get("entities") or []),
            "entity_ids": [str(item.get("entity_id")) for item in atlas_route.get("entities") or [] if item.get("entity_id")],
            "graph_edges": len(atlas_route.get("graph_edges") or []),
            "schema": atlas_route.get("schema"),
            "evaluation_ablation": list(atlas_route.get("evaluation_ablation") or []),
        }
        bundle.trace["planner"] = {
            "requested_protocol": compiled_plan.protocol_version,
            "requested_operations": compiled_plan.requested_operations,
            "effective_operations": len(compiled_plan.operations),
            "operations": len(compiled_plan.operations),
            "deferred_operations": compiled_plan.deferred_operations,
            "stop_reason": trace.stop_reason,
        }
        emit("complete", evidence_count=len(bundle.evidence), pruned_candidate_count=len(candidates), physical_operations_completed=trace.physical_backend_operations)
        return bundle
    finally:
        _ACTIVE_RETRIEVAL_CACHE.reset(cache_token)
        _ACTIVE_RETRIEVAL_TRACE.reset(trace_token)


def _coverage(bundle: ContextBundle) -> dict[str, Any]:
    config_suffixes = {".conf", ".gradle", ".json", ".properties", ".toml", ".xml", ".yaml", ".yml"}
    tests = [item for item in bundle.evidence if item.kind == "test" or re.search(r"(^|/)(test|tests|src/test)/|(?:Test|Tests|IT|Spec)\.", item.path, re.I)]
    configs = [item for item in bundle.evidence if Path(item.path).suffix.lower() in config_suffixes]
    production = [
        item for item in bundle.evidence
        if item.repo not in {"external", "knowledge"} and item.kind != "local diff" and item not in tests and item not in configs
    ]
    return {
        "production_source": bool(production),
        "tests": bool(tests),
        "configuration": bool(configs),
        "relationships": bool(bundle.relationships),
        "git_history": bool(bundle.history),
        "similar_tickets": bool(bundle.experience),
    }


def _evidence_id(item: Evidence) -> str:
    digest = hashlib.sha256(
        f"{item.repo}\0{item.path}\0{item.line_start}\0{item.line_end}\0{item.content}".encode("utf-8")
    ).hexdigest()
    return f"E-{digest[:24]}"


def _candidate_id(generation: Any | None, item: SearchHit) -> str:
    identity = generation.identity if generation is not None else "legacy"
    return "K-" + hashlib.sha256(
        f"{identity}\0{item.repo}\0{item.path}\0{item.line}".encode("utf-8")
    ).hexdigest()[:24]


def _restore_checkpoint_evidence(
    settings: Settings,
    records: Iterable[dict[str, Any]],
    *,
    max_chars: int | None = None,
) -> tuple[list[Evidence], int]:
    """Rehydrate prior verified regions when a client needs a full checkpoint."""
    restored: list[Evidence] = []
    missed = 0
    restored_chars = 0
    for record in records:
        try:
            repo_name = str(record["repo"])
            relative = str(record["path"])
            start = max(1, int(record["line_start"]))
            end = max(start, int(record["line_end"]))
            if repo_name == "external":
                continue  # External evidence is loaded afresh on every request.
            if repo_name == "knowledge":
                root = settings.knowledge_dir.resolve()
                source_path = (root / relative).resolve()
                if not source_path.is_relative_to(root) or not source_path.is_file():
                    missed += 1
                    continue
                lines = source_path.read_text(encoding="utf-8", errors="replace").splitlines()
                evidence = Evidence(
                    "knowledge", relative, start, min(end, len(lines)),
                    "\n".join(lines[start - 1:end]), "checkpoint recovery", 70,
                    ["checkpoint lineage recovery"],
                )
            else:
                evidence = read_source(
                    settings,
                    SearchHit(repo_name, relative, start, "", "checkpoint recovery", 90,
                              ["checkpoint lineage recovery"]),
                    lines=(start, end),
                )
            if hashlib.sha256(evidence.content.encode("utf-8")).hexdigest() != record.get("content_hash"):
                missed += 1
                continue
            if max_chars is not None and restored_chars + len(evidence.content) > max_chars:
                missed += 1
                continue
            restored.append(evidence)
            restored_chars += len(evidence.content)
        except (BrainError, KeyError, OSError, TypeError, ValueError):
            missed += 1
    return restored, missed


def pack_delta_context(
    settings: Settings,
    ticket: str,
    request_number: int,
    bundle: ContextBundle,
    progress: dict[str, Any],
    new_evidence_ids: set[str],
) -> str:
    """Render only newly verified source plus deterministic investigation deltas."""
    generation = bundle.atlas_generation
    output = [
        "# PROJECT BRAIN CONTEXT DELTA", "", f"Ticket: `{ticket}`", f"Request: `{request_number:03d}`",
        f"Context ID: `{progress['context_id']}`", f"Base context ID: `{progress.get('base_context_id')}`", "",
        "## Retrieval contract", "",
        f"- Atlas generation: `{generation.generation if generation is not None else 'legacy_source_pin'}`",
        f"- Atlas identity: `{generation.identity if generation is not None else 'unresolved legacy source pin'}`",
        f"- Source signature: `{generation.source_signature if generation is not None else 'legacy'}`",
        "- This is a delta. Candidate metadata is routing intelligence; only the source regions below are verified evidence.",
        "", "## Coverage Map changes", "",
    ]
    changes = progress.get("coverage_changes") or {}
    output.extend(f"- `{key}`: `{value.get('before')}` → `{value.get('after')}`" for key, value in sorted(changes.items()))
    if not changes:
        output.append("- None")
    output.extend(["", "## Investigation Memory changes", ""])
    memory_changes = progress.get("memory_changes") or {}
    for key, value in sorted(memory_changes.items()):
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
        output.append(f"- `{key}`: {rendered[:2_000]}")
    if not memory_changes:
        output.append("- None")
    output.extend(["", "## Evidence lineage", ""])
    output.append(f"- New evidence: `{len(new_evidence_ids)}`")
    superseded = progress.get("superseded_evidence_ids") or []
    output.append(f"- Invalidated/superseded evidence: `{', '.join(superseded) if superseded else 'none'}`")
    output.extend(["", "## New source evidence", ""])
    new_items = [item for item in bundle.evidence if _evidence_id(item) in new_evidence_ids]
    if not new_items:
        output.append("- None")
    for item in new_items:
        output.extend([
            f"### {_evidence_id(item)} — {item.repo} — `{item.path}:{item.line_start}-{item.line_end}`", "",
            f"Kind: {item.kind}  ", f"Found by: {', '.join(item.found_by)}", "",
            f"```{_language(item.path)}", item.content, "```", "",
        ])
    output.extend(["## Stable candidate changes", ""])
    for item in bundle.additional_candidates[:50]:
        output.append(f"- `{_candidate_id(generation, item)}` `{item.repo}:{item.path}:{item.line}` — {item.kind} — score {item.score}")
    if not bundle.additional_candidates:
        output.append("- None")
    output.extend(["", "## Unresolved", ""])
    output.extend(f"- {item}" for item in bundle.unresolved) if bundle.unresolved else output.append("- None")
    output.extend(["", "## Next-Best-Evidence", "", f"```json\n{json.dumps(progress.get('next_best_evidence') or {}, indent=2, sort_keys=True)}\n```", ""])
    return "\n".join(output).rstrip() + "\n"


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
    if progress and progress.get("context_id"):
        output[5:5] = [
            f"Context ID: `{progress['context_id']}`",
            f"Base context ID: `{progress.get('base_context_id') or 'none'}`",
            "Context kind: `full checkpoint`",
            "",
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
    try:
        from .editions import current_edition

        generation = bundle.atlas_generation
        output.extend([
            "",
            "## Retrieval contract",
            "",
            f"- Edition: `{current_edition(settings)}`",
            f"- Generation: `{generation.generation if generation is not None else 'legacy_source_pin'}`",
            f"- Atlas identity: `{generation.identity if generation is not None else 'unresolved legacy source pin'}`",
            f"- Source signature: `{generation.source_signature if generation is not None else 'legacy'}`",
            "- Evidence is read and verified from the pinned source snapshot; indexes and models only supply candidates or rank signals.",
            f"- Candidate planner: `{bundle.trace.get('planner', {}).get('operations', 0)}` operations; `{bundle.trace.get('planner', {}).get('stop_reason', 'fixed safe plan')}`.",
            "",
            "## Retrieval transparency",
            "",
            f"- Requested protocol: `v{bundle.trace.get('planner', {}).get('requested_protocol', 1)}`",
            f"- Requested / effective / physical operations: `{bundle.trace.get('requested_operations', 0)}` / `{bundle.trace.get('effective_operations', 0)}` / `{bundle.trace.get('physical_backend_operations', 0)}`",
            f"- Initial / final repository scope: `{len(bundle.trace.get('initial_repo_scope') or [])}` / `{len(bundle.trace.get('final_repo_scope') or [])}`",
            f"- Candidates before / after prune: `{bundle.trace.get('unique_candidates_before_prune', 0)}` / `{bundle.trace.get('candidates_after_prune', 0)}`",
            f"- Stop reason: `{bundle.trace.get('stop_reason', 'coverage_satisfied')}`",
            f"- Safe timing: planning `{bundle.trace.get('planning_ms', 0)}` ms; routing `{bundle.trace.get('repo_routing_ms', 0)}` ms; discovery `{bundle.trace.get('candidate_discovery_ms', 0)}` ms; pruning `{bundle.trace.get('candidate_pruning_ms', 0)}` ms; rerank `{bundle.trace.get('rerank_ms', 0)}` ms; hydration `{bundle.trace.get('source_hydration_ms', 0)}` ms.",
        ])
    except OSError:
        pass
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
        coverage = progress.get("coverage") or {}
        output.extend(
            [
                "",
                "## Implementation readiness",
                "",
                "This is deterministic evidence coverage, not a claim that implementation is safe or complete.",
                "",
                f"- Production source: {'VERIFIED' if coverage.get('production_source') else 'MISSING'}",
                f"- Tests: {'VERIFIED' if coverage.get('tests') else 'NOT YET FOUND'}",
                f"- Configuration: {'VERIFIED' if coverage.get('configuration') else 'NOT SHOWN / MAY BE IRRELEVANT'}",
                f"- Static or contract relationships: {'VERIFIED' if coverage.get('relationships') else 'NOT YET FOUND'}",
                f"- Git change history: {'VERIFIED' if coverage.get('git_history') else 'NOT REQUESTED / NOT FOUND'}",
                f"- Similar ticket history: {'FOUND' if coverage.get('similar_tickets') else 'NONE MATCHED'}",
                f"- Unresolved operations in this request: {len(bundle.unresolved)}",
            ]
        )
        if not coverage.get("production_source"):
            output.append("- Suggested next action: continue repository retrieval with a more specific symbol, literal, or path query.")
        elif progress["no_progress_rounds"]:
            output.append("- Suggested next action: ask for the external/runtime blocker or produce FINAL_SOLUTION; more identical searching will not help.")
        else:
            output.append("- Suggested next action: the AI must decide whether remaining unknowns can change the implementation; if not, produce FINAL_SOLUTION.")
        coverage_map = progress.get("coverage_map") or {}
        if coverage_map:
            output.extend(["", "## Coverage Map", ""])
            output.extend(f"- `{key}`: `{value}`" for key, value in sorted(coverage_map.items()))
        investigation_memory = progress.get("investigation_memory") or {}
        if investigation_memory:
            output.extend([
                "", "## Investigation Memory", "",
                "```json",
                json.dumps(investigation_memory, ensure_ascii=False, indent=2, sort_keys=True),
                "```",
            ])
        if progress.get("next_best_evidence"):
            output.extend([
                "", "## Next-Best-Evidence", "",
                f"```json\n{json.dumps(progress['next_best_evidence'], indent=2, sort_keys=True)}\n```",
            ])
        output.extend([
            "", "## Evidence lineage", "",
            f"- New stable evidence IDs: `{', '.join(progress.get('new_evidence_ids') or []) or 'none'}`",
            f"- Invalidated/superseded evidence IDs: `{', '.join(progress.get('superseded_evidence_ids') or []) or 'none'}`",
        ])
        memory_changes = progress.get("memory_changes") or {}
        if memory_changes:
            output.extend(["", "## Investigation Memory changes", ""])
            output.extend(
                f"- `{key}`: {json.dumps(value, ensure_ascii=False, sort_keys=True)[:2_000]}"
                for key, value in sorted(memory_changes.items())
            )
    if bundle.relationships:
        output.extend(["", "## Static execution relationships", "", "```text", *sorted(set(bundle.relationships)), "```"])
    if bundle.experience:
        output.extend(["", bundle.experience.rstrip(), ""])
    output.extend(["", "## Source evidence", ""])
    if not bundle.evidence:
        output.append("No source evidence was retrieved.")
    for index, item in enumerate(bundle.evidence, 1):
        found = ", ".join(item.found_by)
        output.extend([
            f"### {index}. {_evidence_id(item)} — {item.repo} — `{item.path}:{item.line_start}-{item.line_end}`",
            "", f"Kind: {item.kind}  ", f"Found by: {found}", "",
            f"```{_language(item.path)}", item.content, "```", "",
        ])
    if bundle.additional_candidates:
        output.extend([
            "## Additional verified candidates",
            "",
            f"{len(bundle.additional_candidates)} ranked candidates were kept as metadata instead of hydrating more source.",
            "Request a candidate path directly if its source is needed.",
            "",
        ])
        output.extend(
            f"- `C{index}` / `{_candidate_id(bundle.atlas_generation, item)}` `{item.repo}:{item.path}:{item.line}` — {item.kind} — score {item.score}"
            for index, item in enumerate(bundle.additional_candidates[:50], 1)
        )
        if len(bundle.additional_candidates) > 50:
            output.append(f"- {len(bundle.additional_candidates) - 50} lower-ranked candidates remain in the local index.")
        output.append("")
    if bundle.history:
        output.extend(["## Git history", "", *bundle.history, ""])
    output.extend(["## Unresolved", ""])
    output.extend(f"- {item}" for item in bundle.unresolved) if bundle.unresolved else output.append("- None")
    text = "\n".join(output).rstrip() + "\n"
    if len(text) > settings.soft_target_chars:
        text += f"\n> Context size warning: {len(text):,} characters exceeds the soft target of {settings.soft_target_chars:,}. Lower-ranked source candidates were not hydrated.\n"
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


@workspace_exclusive
def snapshot_indexes(
    settings: Settings,
    changed_only: bool = False,
    *,
    publish: bool = True,
) -> tuple[dict[str, Any], list[str]]:
    """Build the real local search index; kept as the public name for compatibility."""
    from .index import build_index_generation, write_state
    from .ops import ensure_write_capacity

    ensure_write_capacity(settings)
    started = time.perf_counter()
    try:
        state, updated = build_index_generation(
            settings,
            changed_only=changed_only,
            suffixes=CODE_SUFFIXES,
            ignored_dirs=IGNORED_DIRS,
        )
    except sqlite3.Error as exc:
        previous = load_index_state(settings)
        state = {}
        updated = []
        for repo in settings.repositories:
            sha = repo.source_sha or git_head(repo)
            old_sha = (previous.get(repo.name) or {}).get("sha")
            if not changed_only or repo.name not in previous or old_sha != sha:
                updated.append(repo.name)
            state[repo.name] = {
                "sha": sha,
                "indexed_at": datetime.now(UTC).isoformat(),
                "backend": "scanner fallback",
                "warning": f"SQLite search index unavailable ({type(exc).__name__})",
                "files": 0,
            }
    for repo in settings.repositories:
        item = state.get(repo.name)
        if isinstance(item, dict):
            item["ref"] = repo.source_ref
    try:
        from .backends.zoekt import build as build_zoekt

        zoekt = build_zoekt(settings, [settings.repo(name) for name in updated])
        for name, details in zoekt.items():
            if isinstance(state.get(name), dict):
                state[name]["zoekt"] = details
    except OSError:
        zoekt = {}
    existing_generation = None
    try:
        from .catalog import collect_generation_components, current_generation_ref, publish_generation
        from .catalog import record_index_catalog

        existing_generation = current_generation_ref(settings)
        backends = ["sqlite-fts5"] + (["zoekt"] if zoekt else [])
        snapshots = {
            name: str(item.get("sha") or "working-tree")
            for name, item in state.items()
            if isinstance(item, dict)
        }
        should_publish = publish and (
            updated
            or existing_generation is None
            or not existing_generation.identity
            or existing_generation.snapshots != snapshots
            or existing_generation.component("lexical").get("status") != "ready"
        )
        if updated or existing_generation is None or not existing_generation.identity:
            record_index_catalog(settings, state)
        if should_publish:
            from .atlas import build_atlas

            atlas_payload = build_atlas(settings, state)
            generation = publish_generation(
                settings,
                state,
                backends=backends,
                components=collect_generation_components(settings, state, atlas_payload=atlas_payload),
                atlas_payload=atlas_payload,
            )
        else:
            generation = existing_generation.manifest if existing_generation is not None else None
        if generation is not None:
            for item in state.values():
                if isinstance(item, dict):
                    item["generation"] = generation["generation"]
    except (OSError, sqlite3.Error) as exc:
        for item in state.values():
            if isinstance(item, dict):
                item.setdefault("warning", f"Catalog generation unavailable ({type(exc).__name__})")
        if publish and existing_generation is not None:
            raise
    write_state(settings, state)
    from .metrics import record_metric

    record_metric(
        settings,
        "index",
        total_ms=round((time.perf_counter() - started) * 1000, 3),
        updated_repos=len(updated),
        indexed_files=sum(
            int(state[name].get("files") or 0)
            for name in updated
            if isinstance(state.get(name), dict)
        ),
        changed_blobs=sum(
            int(state[name].get("changed_blobs") or 0)
            for name in updated
            if isinstance(state.get(name), dict)
        ),
        bytes_indexed=sum(
            int(state[name].get("bytes_indexed") or 0)
            for name in updated
            if isinstance(state.get(name), dict)
        ),
    )
    return state, updated


def doctor(settings: Settings) -> tuple[str, bool]:
    from .graph import TESTED_BACKEND_VERSION, backend_version
    from .models import managed_runtime_loopback_status, model_download_trust_status

    output = ["PROJECT BRAIN", "", "Dependencies", ""]
    ok = True
    for command, required in (("python", True), ("git", False), ("rg", False)):
        present = sys.executable if command == "python" else shutil.which(command)
        status = "OK" if present else ("MISSING" if required else "OPTIONAL — built-in fallback active")
        output.append(f"{command:<24}{status}")
        ok = ok and (bool(present) or not required)
    trust_status, trust_ok = model_download_trust_status(settings)
    output.extend(["", "Model-download TLS", "", f"trust store{'':<13}{trust_status}"])
    ok = ok and trust_ok
    output.extend([
        "", "Pack-owned model runtime", "",
        f"loopback transport{'':<7}{managed_runtime_loopback_status()}",
    ])
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
    path = directory / "session.json"
    temporary = path.with_suffix(".writing")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _session_snapshots(state: dict[str, Any]) -> dict[str, str]:
    return {
        str(name): str(value.get("sha") or "working-tree")
        for name, value in (state.get("sources") or {}).items()
        if isinstance(value, dict)
    }


def _resolve_session_generation(settings: Settings, state: dict[str, Any]) -> tuple[Any | None, bool]:
    """Resolve a ticket once; ambiguous legacy state stays source-only instead of using current."""
    from .catalog import matching_generations, resolve_generation

    snapshots = _session_snapshots(state)
    generation = None
    identity = str(state.get("atlas_generation_id") or "")
    if identity:
        generation = resolve_generation(settings, identity=identity)
    elif state.get("generation") is not None:
        try:
            generation = resolve_generation(settings, generation=int(state["generation"]))
        except (TypeError, ValueError):
            generation = None
    if generation is not None and generation.snapshots != snapshots:
        generation = None
    if identity and generation is None:
        raise BrainError(
            "This ticket's pinned Atlas generation is unavailable; restore the retained generation "
            "or start a new ticket instead of mixing repository generations"
        )
    if generation is None and not identity:
        matches = matching_generations(settings, snapshots)
        generation = matches[0] if len(matches) == 1 else None
    before = json.dumps(state, sort_keys=True)
    state["session_schema_version"] = 2
    if generation is None:
        state["generation_mode"] = "legacy_source_pin"
        state["atlas_generation_id"] = None
        state["generation"] = None
    else:
        state.update({
            "generation_mode": "atlas",
            "atlas_generation_id": generation.identity,
            "generation": generation.generation,
            "source_signature": generation.source_signature,
        })
    return generation, before != json.dumps(state, sort_keys=True)


@workspace_exclusive
@ticket_exclusive
def start_session(settings: Settings, ticket: str, ticket_text: str) -> tuple[str, Path]:
    from .experience import build_experience_index, load_experience_index, render_similar_cases
    from .catalog import current_generation_ref, source_signature
    from .atlas import initial_coverage_map, initial_investigation_memory, similar_investigations

    directory = session_dir(settings, ticket)
    directory.mkdir(parents=True, exist_ok=True)
    sources = {
        repo.name: {
            "snapshot": str(repo.source_path) if repo.source_path else None,
            "ref": repo.source_ref,
            "sha": repo.source_sha,
            "status": repo.source_status,
            "fetched": repo.source_fetched,
            "warning": repo.source_warning,
        }
        for repo in settings.repositories
    }
    snapshots = {
        name: str(value.get("sha") or "working-tree") for name, value in sources.items()
    }
    current_atlas = current_generation_ref(settings)
    pinned_atlas = current_atlas if current_atlas is not None and current_atlas.snapshots == snapshots else None
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
    if settings.experience_enabled:
        if not load_experience_index(settings):
            build_experience_index(settings, changed_only=True)
        historical = render_similar_cases(
            settings,
            f"{ticket}\n{ticket_text}",
            include_patches=True,
            generation=pinned_atlas,
        )
        if historical:
            sections.extend([historical.rstrip(), ""])
    ticket_knowledge = settings.knowledge_dir / "tickets" / f"{directory.name}.md"
    if ticket_knowledge.is_file():
        sections.extend(["## Human-maintained knowledge for this ticket", "", ticket_knowledge.read_text(encoding="utf-8", errors="replace").strip(), ""])
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
    state.update(
        {
            "ticket": ticket,
            "started_at": datetime.now(UTC).isoformat(),
            "status": "waiting_for_ai",
            "session_schema_version": 2,
            "generation_mode": "atlas" if pinned_atlas is not None else "legacy_source_pin",
            "atlas_generation_id": pinned_atlas.identity if pinned_atlas is not None else None,
            "generation": pinned_atlas.generation if pinned_atlas is not None else None,
            "source_signature": pinned_atlas.source_signature if pinned_atlas is not None else source_signature(snapshots),
            "requests": state.get("requests", 0),
            "feedbacks": state.get("feedbacks", 0),
            "sources": sources,
            "investigation_memory": initial_investigation_memory(ticket_text.strip()),
            "coverage_map": initial_coverage_map(),
            "similar_investigations": similar_investigations(settings, ticket_text, limit=settings.experience_similar_cases),
            "context_lineage": [],
        }
    )
    save_session(settings, ticket, state)
    if settings.ticket_prefetch_enabled and pinned_atlas is not None:
        try:
            prefetch_ticket(settings, ticket)
        except (OSError, sqlite3.Error, BrainError):
            state = session_state(settings, ticket)
            state["prefetch"] = {"status": "failed", "generation": pinned_atlas.generation}
            save_session(settings, ticket, state)
    return content, start_path


@ticket_retrieval_exclusive
def prefetch_ticket(settings: Settings, ticket: str) -> dict[str, Any]:
    """Warm generation routing only; never create evidence or a request round."""
    from .atlas import route as route_atlas

    state = session_state(settings, ticket)
    generation, migrated = _resolve_session_generation(settings, state)
    if migrated:
        save_session(settings, ticket, state)
    if generation is None:
        result = {"status": "unavailable", "generation": None, "candidate_ids": []}
    else:
        objective = str((state.get("investigation_memory") or {}).get("objective") or ticket)
        from .retrieval.planner import objective_terms

        routed = route_atlas(settings, objective, {
            "version": 4, "objective": objective,
            "searches": [{"query": value, "repos": []} for value in objective_terms(objective, limit=8)],
            "paths": [], "symbols": [], "history": [], "required": [], "resolve": [],
        }, generation,
                             repo_limit=settings.widen_repo_limit, entity_limit=settings.pre_rerank_candidate_limit)
        result = {
            "status": "ready", "generation": generation.generation, "atlas_generation_id": generation.identity,
            "cache_hit": bool(routed.get("cache_hit")), "repos": list(routed.get("repos") or []),
            "modules": list(routed.get("modules") or []),
            "candidate_ids": [str(item.get("entity_id")) for item in routed.get("entities") or []],
            "created_at": datetime.now(UTC).isoformat(),
        }
    state["prefetch"] = result
    save_session(settings, ticket, state)
    return result


@ticket_retrieval_exclusive
def create_context(
    settings: Settings,
    ticket: str,
    request_text: str,
    include_diff: bool = False,
    progress: Any | None = None,
) -> tuple[str, Path, int]:
    progress_callback = progress
    directory = session_dir(settings, ticket)
    if not directory.is_dir():
        raise BrainError(f"Session {ticket} does not exist. Run `brain start {ticket}` first.")
    plan = request_preview(request_text, settings)
    request = plan["request"]
    state = session_state(settings, ticket)
    atlas_generation, migrated = _resolve_session_generation(settings, state)
    if migrated:
        save_session(settings, ticket, state)
    from .atlas import initial_coverage_map, initial_investigation_memory

    memory = dict(state.get("investigation_memory") or initial_investigation_memory(plan["objective"]))
    coverage_map = dict(state.get("coverage_map") or initial_coverage_map())
    memory_before = json.loads(json.dumps(memory))
    coverage_before = dict(coverage_map)
    if request.get("version") == 4:
        memory["objective"] = str(request["objective"])
        memory["hypotheses"] = list(dict.fromkeys([*(memory.get("hypotheses") or []), *request.get("hypotheses", [])]))[-100:]
        memory["runtime_facts"] = list(dict.fromkeys([*(memory.get("runtime_facts") or []), *request.get("runtime_facts", [])]))[-100:]
        for required in request.get("required") or []:
            coverage_map["explicit_requested"] = "candidate"
            memory["blocking_unknowns"] = list(dict.fromkeys([*(memory.get("blocking_unknowns") or []), str(required)]))[-100:]
    request["_prefetch"] = state.get("prefetch") or {}
    request["_prior_entity_ids"] = [
        *(str(entity_id) for entity_id in state.get("atlas_entity_ids") or []),
        *(str(entity_id)
          for prior in state.get("similar_investigations") or [] if isinstance(prior, dict)
          for entity_id in prior.get("entity_ids") or []),
    ]
    for previous in state.get("request_history") or []:
        if previous.get("signature") == plan["signature"] and previous.get("source_signature") == state.get("source_signature"):
            raise BrainError(
                f"This retrieval plan already ran as request {int(previous.get('number') or 0):03d}. "
                "Clear any old reply and paste only the AI's latest complete response. If the latest reply "
                "is a human question, answer it directly in the AI chat; Brain should not create a new request."
            )
    retrieval_settings = replace(
        settings,
        repositories=[replace(repo) for repo in settings.repositories],
        atlas_generation=atlas_generation,
        atlas_generation_mode="pinned" if atlas_generation is not None else "legacy_source_pin",
    )
    for repo in retrieval_settings.repositories:
        source = (state.get("sources") or {}).get(repo.name) or {}
        raw_snapshot = str(source.get("snapshot") or "")
        snapshot = Path(raw_snapshot)
        if raw_snapshot and (not snapshot.is_dir() or not snapshot.is_relative_to(settings.state_dir)):
            raise BrainError(f"Pinned source snapshot for {repo.name} is unavailable; refresh/start a new ticket instead of mixing commits")
        repo.source_path = snapshot if raw_snapshot else None
        repo.source_ref = str(source.get("ref") or "") or None
        repo.source_sha = str(source.get("sha") or "") or None
        repo.source_status = str(source.get("status") or "session snapshot")
        repo.source_fetched = bool(source.get("fetched"))
        repo.source_warning = str(source.get("warning") or "") or None
    if request.get("version") == 3 and request.get("files"):
        established = {
            (str(item.get("repo") or ""), str(item.get("path") or ""))
            for item in state.get("evidence_manifest") or []
            if isinstance(item, dict)
        }
        for item in request["files"]:
            if (str(item["repo"]), str(item["path"])) not in established:
                raise BrainError(
                    f"v3 hints.files path is not established by prior Brain evidence: {item['repo']}:{item['path']}"
                )
    if request.get("expand"):
        manifest = state.get("candidate_manifest") or {}
        for candidate_id in request["expand"]:
            candidate = manifest.get(candidate_id)
            if not isinstance(candidate, dict):
                raise BrainError(f"Candidate {candidate_id} is not available in this pinned session")
            request["files"].append({
                "repo": candidate["repo"],
                "path": candidate["path"],
                "lines": f"{candidate['line']}-{candidate['line']}",
            })
    number = int(state.get("requests") or 0) + 1
    requested_base = str(request.get("base_context_id") or "") or None
    current_base = str(state.get("last_context_id") or "") or None
    checkpoint_reason = None
    full_checkpoint = request.get("version") != 4 or bool(request.get("checkpoint"))
    if request.get("version") == 4:
        if not requested_base:
            full_checkpoint = True
            checkpoint_reason = "base_missing"
        elif requested_base != current_base:
            full_checkpoint = True
            checkpoint_reason = "base_mismatch"
        elif number % settings.context_checkpoint_interval == 0:
            full_checkpoint = True
            checkpoint_reason = "checkpoint_interval"
    request_path = directory / f"request-{number:03d}.yml"
    path = directory / f"context-{number:03d}.md"
    trace_path = directory / f"trace-{number:03d}.json"
    try:
        bundle = retrieve_context(retrieval_settings, request, include_diff=include_diff, progress=progress_callback)
        from .query import merge_evidence

        bundle.evidence = merge_evidence(bundle.evidence + _external_evidence(settings, ticket))
        if full_checkpoint and state.get("evidence_records"):
            checkpoint_source_budget = max(
                0,
                settings.hard_context_chars
                - 40_000
                - sum(len(item.content) for item in bundle.evidence),
            )
            restored, missed = _restore_checkpoint_evidence(
                retrieval_settings,
                (item for item in state.get("evidence_records") or [] if isinstance(item, dict)),
                max_chars=checkpoint_source_budget,
            )
            bundle.evidence = merge_evidence(bundle.evidence + restored)
            if missed:
                bundle.warnings.append(
                    f"{missed} prior evidence region(s) could not be restored into this full checkpoint."
                )
        evidence_keys = {
            hashlib.sha256(
                f"{item.repo}\0{item.path}\0{item.line_start}\0{item.line_end}\0{item.content}".encode("utf-8")
            ).hexdigest()
            for item in bundle.evidence
        }
        known_keys = set(state.get("evidence_keys") or [])
        new_evidence = evidence_keys - known_keys
        no_progress_rounds = 0 if new_evidence else int(state.get("no_progress_rounds") or 0) + 1
        if no_progress_rounds:
            bundle.trace["stop_reason"] = "no_progress"
            if isinstance(bundle.trace.get("planner"), dict):
                bundle.trace["planner"]["stop_reason"] = "no_progress"
        coverage = dict(state.get("coverage") or {})
        coverage.update({key: bool(coverage.get(key) or value) for key, value in _coverage(bundle).items()})
        investigation_progress = {
            "operations": plan["operation_count"],
            "new_evidence": len(new_evidence),
            "known_evidence": len(evidence_keys & known_keys),
            "no_progress_rounds": no_progress_rounds,
            "history": list(state.get("request_history") or []),
            "coverage": coverage,
        }
        evidence_records: dict[str, dict[str, Any]] = {}
        for item in bundle.evidence:
            identifier = _evidence_id(item)
            evidence_records[identifier] = {
                "evidence_id": identifier, "repo": item.repo, "path": item.path,
                "line_start": item.line_start, "line_end": item.line_end,
                "content_hash": hashlib.sha256(item.content.encode("utf-8")).hexdigest(),
            }
        previous_records = {
            str(item.get("evidence_id")): item for item in state.get("evidence_records") or []
            if isinstance(item, dict) and item.get("evidence_id")
        }
        new_evidence_ids = set(evidence_records) - set(previous_records)
        superseded = sorted(
            identifier for identifier, old in previous_records.items()
            if identifier not in evidence_records and any(
                current["repo"] == old.get("repo") and current["path"] == old.get("path")
                for current in evidence_records.values()
            )
        )
        lineage_records = {**previous_records, **evidence_records}
        for identifier in superseded:
            lineage_records.pop(identifier, None)
        if len(lineage_records) > 500:
            current_ids = set(evidence_records)
            retained_ids = [*sorted(current_ids), *sorted(set(lineage_records) - current_ids)][:500]
            lineage_records = {identifier: lineage_records[identifier] for identifier in retained_ids}
        generation_identity = atlas_generation.identity if atlas_generation is not None else str(state.get("source_signature") or "legacy")
        context_id = "ctx-" + hashlib.sha256(
            json.dumps({"generation": generation_identity, "request": plan["signature"], "evidence": sorted(lineage_records)},
                       sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        from .atlas import next_best_evidence, update_investigation

        update_investigation(memory, coverage_map, bundle, context_id)
        next_evidence = next_best_evidence(coverage_map, request, no_progress_rounds)
        bundle.trace["next_best_evidence"] = next_evidence
        bundle.trace["investigation_state"] = {
            "verified_facts": len(memory.get("verified_facts") or []),
            "blocking_unknowns": len(memory.get("blocking_unknowns") or []),
            "verified_references": len(memory.get("verified_references") or []),
            "coverage": dict(coverage_map),
        }
        coverage_changes = {
            key: {"before": coverage_before.get(key), "after": value}
            for key, value in coverage_map.items() if coverage_before.get(key) != value
        }
        memory_changes = {
            key: value for key, value in memory.items() if memory_before.get(key) != value
        }
        investigation_progress.update({
            "context_id": context_id, "base_context_id": requested_base, "checkpoint": full_checkpoint,
            "checkpoint_reason": checkpoint_reason, "coverage_map": coverage_map, "coverage_changes": coverage_changes,
            "memory_changes": memory_changes, "new_evidence_ids": sorted(new_evidence_ids),
            "superseded_evidence_ids": superseded, "next_best_evidence": next_evidence,
            "investigation_memory": memory,
        })
        if progress_callback is not None:
            progress_callback({"phase": "packing_context", "elapsed_ms": bundle.metrics.get("total_ms", 0), "evidence_count": len(bundle.evidence)})
        pack_started = time.perf_counter()
        content = (
            pack_context(retrieval_settings, ticket, number, bundle, investigation_progress)
            if full_checkpoint
            else pack_delta_context(retrieval_settings, ticket, number, bundle, investigation_progress, new_evidence_ids)
        )
        context_pack_ms = round((time.perf_counter() - pack_started) * 1000, 3)
        bundle.metrics["context_pack_ms"] = context_pack_ms
        bundle.metrics["total_ms"] = round(float(bundle.metrics.get("total_ms") or 0) + context_pack_ms, 3)
        bundle.trace["context_pack_ms"] = context_pack_ms
        bundle.trace["queue_wait_ms"] = float(bundle.trace.get("queue_wait_ms") or 0)
        bundle.trace["wall_ms"] = round(float(bundle.trace.get("wall_ms") or 0) + context_pack_ms, 3)
        bundle.trace["total_ms"] = bundle.trace["wall_ms"]
        from .metrics import record_metric, record_trace

        record_metric(settings, "retrieve", **bundle.metrics, context_chars=len(content))
        bundle.trace["context_chars"] = len(content)
        record_trace(settings, ticket, number, bundle.trace)
        request_path.write_text(request_text.rstrip() + "\n", encoding="utf-8")
        path.write_text(content, encoding="utf-8")
        state["requests"] = number
        state["status"] = "waiting_for_ai"
        state["no_progress_rounds"] = no_progress_rounds
        retained_evidence_keys = [*sorted(evidence_keys), *sorted(known_keys - evidence_keys)][:1_000]
        state["evidence_keys"] = retained_evidence_keys
        state["coverage"] = coverage
        state["investigation_memory"] = memory
        state["coverage_map"] = coverage_map
        state["last_context_id"] = context_id
        state["context_lineage"] = [*(state.get("context_lineage") or []), {
            "context_id": context_id, "base_context_id": requested_base, "number": number,
            "kind": "checkpoint" if full_checkpoint else "delta",
        }][-100:]
        state["evidence_records"] = sorted(lineage_records.values(), key=lambda item: item["evidence_id"])
        state["candidate_manifest"] = {
            f"C{index}": {"repo": item.repo, "path": item.path, "line": item.line,
                           "candidate_id": _candidate_id(atlas_generation, item)}
            for index, item in enumerate(bundle.additional_candidates[:50], 1)
        }
        state["atlas_entity_ids"] = list(dict.fromkeys(
            str(entity_id) for entity_id in (bundle.trace.get("atlas_route") or {}).get("entity_ids", [])
            if entity_id
        ))
        state["evidence_manifest"] = sorted(
            ({"repo": item.repo, "path": item.path} for item in bundle.evidence if item.repo not in {"external", "knowledge"}),
            key=lambda item: (item["repo"], item["path"]),
        )
        from .editions import current_edition

        found_by = {source for item in bundle.evidence for source in item.found_by}
        requested_edition = current_edition(retrieval_settings)
        semantic_used = "local semantic index" in found_by
        reranker_used = "local reranker" in found_by
        if requested_edition == "precision" and reranker_used and semantic_used:
            effective_edition = "Precision"
        elif requested_edition in {"semantic", "precision"} and semantic_used:
            effective_edition = "Semantic"
        elif requested_edition == "core":
            effective_edition = "Core"
        else:
            effective_edition = "Degraded Core"
        retrieval = {
            "requested_edition": requested_edition,
            "effective_edition": effective_edition,
            "semantic_recall_used": semantic_used,
            "reranker_used": reranker_used,
            "candidate_count": int(bundle.metrics.get("candidates") or 0),
            "evidence_count": len(bundle.evidence),
            "generation": state.get("generation"),
            "atlas_generation_id": state.get("atlas_generation_id"),
            "generation_mode": state.get("generation_mode", "legacy_source_pin"),
            "snapshots": sorted((state.get("sources") or {}).keys()),
            "timing_ms": bundle.metrics,
            "trace": bundle.trace,
            "requested_operations": int(bundle.trace.get("requested_operations") or 0),
            "effective_operations": int(bundle.trace.get("effective_operations") or 0),
            "physical_backend_operations": int(bundle.trace.get("physical_backend_operations") or 0),
            "initial_repo_scope": list(bundle.trace.get("initial_repo_scope") or []),
            "final_repo_scope": list(bundle.trace.get("final_repo_scope") or []),
            "stop_reason": str(bundle.trace.get("stop_reason") or "coverage_satisfied"),
            "warnings": list(bundle.warnings),
            "context_id": context_id,
            "base_context_id": requested_base,
            "context_kind": "checkpoint" if full_checkpoint else "delta",
            "checkpoint_reason": checkpoint_reason,
            "next_best_evidence": next_evidence,
        }
        history = list(state.get("request_history") or [])
        history.append({
            "number": number,
            "objective": plan["objective"],
            "signature": plan["signature"],
            "source_signature": state.get("source_signature"),
            "operations": plan["operation_count"],
            "new_evidence": len(new_evidence),
            "unresolved": len(bundle.unresolved),
            "retrieval": retrieval,
            "created_at": datetime.now(UTC).isoformat(),
        })
        state["request_history"] = history[-500:]
        from .atlas import record_investigation

        record_investigation(settings, ticket, state)
        save_session(settings, ticket, state)
    except Exception:
        request_path.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        trace_path.unlink(missing_ok=True)
        raise
    return content, path, number


@ticket_snapshot_exclusive
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
        "runtime results. If more source evidence is required, return a new INVESTIGATION_REQUEST v4.",
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
    if settings.experience_enabled:
        from .experience import evaluate_sessions

        evaluate_sessions(settings)
    return content, path, number


@ticket_exclusive
def add_external_evidence(
    settings: Settings,
    ticket: str,
    source: Path,
    *,
    kind: str = "document",
) -> tuple[str, Path, int, Path]:
    """Archive user-supplied evidence locally; include text verbatim and never claim to parse binaries."""
    directory = session_dir(settings, ticket)
    if not directory.is_dir():
        raise BrainError(f"Session {ticket} does not exist. Run `brain start {ticket}` first.")
    supplied = source.expanduser()
    if supplied.is_symlink():
        raise BrainError(f"Evidence file must not be a symlink: {supplied}")
    source = supplied.resolve()
    if not source.is_file():
        raise BrainError(f"Evidence file does not exist or is a symlink: {source}")
    size = source.stat().st_size
    if size > 20 * 1024 * 1024:
        raise BrainError("Evidence files are limited to 20 MB; extract or split the relevant content first")
    if kind not in {"document", "log", "note", "runtime"}:
        raise BrainError("Evidence kind must be document, log, note, or runtime")
    state = session_state(settings, ticket)
    number = int(state.get("external_evidence") or 0) + 1
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", source.name).strip(".-") or f"evidence-{number:03d}"
    stored_dir = directory / "external"
    stored_dir.mkdir(parents=True, exist_ok=True)
    stored = stored_dir / f"{number:03d}-{safe_name}"
    shutil.copy2(source, stored)
    digest = hashlib.sha256(stored.read_bytes()).hexdigest()
    text_suffixes = {".conf", ".csv", ".html", ".htm", ".json", ".log", ".md", ".properties", ".txt", ".xml", ".yaml", ".yml"}
    display_name = source.name.replace("`", "'").replace("\n", " ").replace("\r", " ")
    sections = [
        "# PROJECT BRAIN — EXTERNAL EVIDENCE",
        "",
        f"Ticket: `{ticket}`",
        f"Evidence: `{number:03d}`",
        f"Kind: `{kind}`",
        f"Original filename: `{display_name}`",
        f"SHA-256: `{digest}`",
        "",
        "This evidence was explicitly supplied by the user. It is not repository proof and may describe runtime or external state.",
        "",
    ]
    if source.suffix.lower() in text_suffixes:
        sections.extend(["## Content", "", "```text", stored.read_text(encoding="utf-8", errors="replace").rstrip(), "```", ""])
    else:
        sections.extend(
            [
                "## Binary attachment",
                "",
                "Project Brain archived this file but did not parse it. Attach the stored binary directly to an AI that can read this format.",
                f"Stored file: `{stored.relative_to(settings.root) if stored.is_relative_to(settings.root) else stored.name}`",
                "",
            ]
        )
    content = "\n".join(sections).rstrip() + "\n"
    artifact = directory / f"external-{number:03d}.md"
    artifact.write_text(content, encoding="utf-8")
    state["external_evidence"] = number
    state["status"] = "waiting_for_ai"
    save_session(settings, ticket, state)
    return content, artifact, number, stored


def _external_evidence(settings: Settings, ticket: str) -> list[Evidence]:
    directory = session_dir(settings, ticket)
    evidence: list[Evidence] = []
    for path in sorted(directory.glob("external-*.md")):
        content = path.read_text(encoding="utf-8", errors="replace")
        evidence.append(
            Evidence(
                "external",
                path.name,
                1,
                content.count("\n") + 1,
                content,
                "user-supplied external evidence",
                100,
                ["explicit ticket evidence"],
            )
        )
    return evidence


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


@ticket_exclusive
def deliver(settings: Settings, ticket: str, text: str, target: str, *, copy: bool) -> tuple[list[Path], int]:
    directory = session_dir(settings, ticket)
    state = session_state(settings, ticket)
    if target == "m365":
        internal_handoff = directory / "current-handoff.md"
        internal_handoff.write_text(text, encoding="utf-8")
        handoff_directory = settings.generated_dir / "handoffs"
        handoff_directory.mkdir(parents=True, exist_ok=True)
        current_handoff = handoff_directory / f"{directory.name}-current.md"
        current_handoff.write_text(text, encoding="utf-8")
        if text.startswith("# PROJECT BRAIN — START"):
            label = "start"
        elif text.startswith("# PROJECT BRAIN — EXTERNAL EVIDENCE"):
            match = re.search(r"(?m)^Evidence: `(\d+)`", text)
            label = f"evidence-{int(match.group(1)):03d}" if match else "evidence"
        elif text.startswith("# PROJECT BRAIN — IMPLEMENTATION FEEDBACK"):
            match = re.search(r"(?m)^Feedback: `(\d+)`", text)
            label = f"feedback-{int(match.group(1)):03d}" if match else "feedback"
        elif re.search(r"(?im)^\s*(?:#+\s*)?FINAL_SOLUTION\b", text):
            label = "final"
        else:
            match = re.search(r"(?m)^Request: `(\d+)`", text)
            label = f"context-{int(match.group(1)):03d}" if match else "update"
        handoff = handoff_directory / f"{directory.name}-{label}.md"
        handoff.write_text(text, encoding="utf-8")
        paths = [handoff]
        state["delivery"] = {
            "target": target,
            "parts": [str(handoff)],
            "current": 1,
            "handoff": str(current_handoff),
            "latest": str(handoff),
        }
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

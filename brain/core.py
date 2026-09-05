from __future__ import annotations

import ast
import copy
import contextvars
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
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
from .platforms import (
    atomic_managed_bytes_write,
    atomic_managed_text_write,
    filesystem_component,
    is_test_path,
    logical_path,
    native_command,
    read_direct_file_bytes,
    read_managed_bytes,
    read_managed_text,
    run_bounded_process,
    trusted_path_executable,
    windows_system_executable,
)


IGNORED_DIRS = {".git", ".idea", ".venv", "node_modules", "target", "build", "dist"}
SENSITIVE_FILE_NAMES = {".env", ".envrc", "credentials", "credentials.json", "service-account.json", "id_rsa", "id_ed25519", "keystore"}
SENSITIVE_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".jks"}
DISCOVERY_IGNORED_DIRS = IGNORED_DIRS | {".runs", ".codex", ".agents", "state", "generated", "knowledge"}
MAX_REPOSITORY_DISCOVERY_ENTRIES = 100_000
MAX_REPOSITORY_DISCOVERY_DEPTH = 12
MAX_REPOSITORY_DISCOVERY_SECONDS = 1.0
MAX_FALLBACK_SCAN_ENTRIES = 100_000
MAX_FALLBACK_SCAN_DEPTH = 64
MAX_FALLBACK_SCAN_SECONDS = 2.0
MAX_FALLBACK_SEARCH_BYTES = 64 * 1024 * 1024
PROTOCOL_VERSION = 5
LEGACY_DEFAULT_PROTOCOL_VERSION = 1
CURRENT_SESSION_SCHEMA_VERSION = 3
MAX_REQUEST_ITEMS = 50
MAX_REQUEST_TEXT_CHARS = 100_000
MAX_REQUEST_TEXT_BYTES = 100_000
MAX_SESSION_STATE_BYTES = 4 * 1024 * 1024
MAX_DELIVERY_ARTIFACT_BYTES = 4 * 1024 * 1024
MAX_WORKING_TREE_DIFF_COMMAND_BYTES = 512 * 1024
MAX_WORKING_TREE_DIFF_TOTAL_BYTES = 2 * 1024 * 1024
MAX_WORKING_TREE_DIFF_COMMAND_SECONDS = 10.0
MAX_WORKING_TREE_DIFF_TOTAL_SECONDS = 30.0
WORKING_TREE_DIFF_OMISSION = "[Project Brain omitted the remaining working-tree diff at its retrieval limit.]"
MAX_EXTERNAL_CONTEXT_ITEMS = 32
MAX_EXTERNAL_CONTEXT_ITEM_BYTES = 1024 * 1024
MAX_EXTERNAL_CONTEXT_TOTAL_BYTES = 2 * 1024 * 1024
EXTERNAL_CONTEXT_OMISSION = "External evidence was omitted because its managed artifact failed a safety or size check."
MAX_START_TICKET_BYTES = 1024 * 1024
MAX_START_KNOWLEDGE_ITEM_BYTES = 256 * 1024
MAX_START_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_CLIPBOARD_BYTES = 4 * 1024 * 1024
MAX_CLIPBOARD_SECONDS = 10.0
MAX_CHECKPOINT_ARTIFACT_BYTES = 24_000
MAX_EXTERNAL_EVIDENCE_SOURCE_BYTES = 20 * 1024 * 1024
MAX_KNOWLEDGE_SCAN_ENTRIES = 2_048
MAX_KNOWLEDGE_FILES = 256
MAX_KNOWLEDGE_ITEM_BYTES = 512 * 1024
MAX_KNOWLEDGE_TOTAL_BYTES = 8 * 1024 * 1024
CODE_SUFFIXES = {
    ".adoc", ".avsc", ".bash", ".c", ".cc", ".cfg", ".conf", ".cpp", ".cs", ".csv",
    ".gql", ".go", ".gradle", ".graphql", ".graphqls", ".groovy", ".h", ".hcl", ".hpp",
    ".ini", ".java", ".js", ".json", ".jsx", ".kt", ".kts", ".md", ".mustache", ".php",
    ".properties", ".proto", ".py", ".rb", ".rs", ".rst", ".scala", ".sh", ".sql",
    ".swift", ".tf", ".tfvars", ".toml", ".tpl", ".ts", ".tsx", ".vue", ".xml", ".yaml",
    ".yml", ".zsh",
}
MAX_PROJECT_MAP_REPOSITORIES = 100
MAX_PROJECT_MAP_DOCUMENTS = 50_000
MAX_PROJECT_MAP_SOURCE_BYTES = 256 * 1024 * 1024
MAX_PROJECT_MAP_FILE_BYTES = 3 * 1024 * 1024
MAX_PROJECT_MAP_SOURCE_SECONDS = 30.0
MAX_PROJECT_MAP_DEPENDENCIES_PER_REPO = 2_000
MAX_PROJECT_MAP_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_CONFIG_CONTEXT_BYTES = 512 * 1024
MAX_CONFIG_CLIPBOARD_CHARS = 512 * 1024
MAX_CONFIG_REPOSITORIES = 100
MAX_CONFIG_BYTES = 4 * 1024 * 1024
MAX_SOURCE_FILE_BYTES = 3_000_000
MAX_PINNED_QUERY_CANDIDATE_FILES = 2_000
MAX_PINNED_QUERY_BYTES = 64 * 1024 * 1024
MAX_PINNED_QUERY_SECONDS = 2.0
MAX_PINNED_PATH_CANDIDATES = 20_000
MAX_PINNED_PATH_SECONDS = 2.0
MAX_PINNED_HYDRATION_BYTES = 32 * 1024 * 1024
MAX_PINNED_HYDRATION_SECONDS = 2.0


class BrainError(RuntimeError):
    pass


def _bounded_utf8_text(text: str, max_bytes: int, marker: str) -> tuple[str, bool]:
    """Bound generated/model input by encoded bytes without splitting UTF-8."""
    payload = text.encode("utf-8")
    if len(payload) <= max_bytes:
        return text, False
    marker_bytes = marker.encode("utf-8")
    if len(marker_bytes) > max_bytes:
        return marker_bytes[:max_bytes].decode("utf-8", errors="ignore"), True
    prefix = payload[:max_bytes - len(marker_bytes)].decode("utf-8", errors="ignore")
    return prefix + marker, True


def _bounded_text_file(path: Path, max_bytes: int) -> tuple[str, bool]:
    """Read at most max_bytes from a regular non-symlink text artifact."""
    try:
        raw, exceeded = read_direct_file_bytes(path, max_bytes=max_bytes)
    except (OSError, ValueError):
        return "", True
    return raw[:max_bytes].decode("utf-8", errors="ignore"), exceeded


def _bounded_regular_file_bytes(path: Path, max_bytes: int) -> bytes:
    """Read one direct regular file without following a symbolic-link artifact."""
    try:
        raw, exceeded = read_direct_file_bytes(path, max_bytes=max_bytes)
    except (OSError, ValueError) as error:
        raise BrainError(f"Managed artifact is unavailable: {path.name}") from error
    if exceeded:
        raise BrainError(f"Managed artifact exceeds its byte limit: {path.name}")
    return raw


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
    evaluation_ablations: frozenset[str] = frozenset()
    persist_investigation_records: bool = True

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
    deadline = time.monotonic() + MAX_REPOSITORY_DISCOVERY_SECONDS
    remaining = MAX_REPOSITORY_DISCOVERY_ENTRIES
    for root in roots:
        pending = [(root.resolve(), 0)]
        while pending:
            if remaining <= 0 or time.monotonic() >= deadline:
                raise BrainError("Git repository discovery exceeded its bounded scope")
            directory, depth = pending.pop()
            children: list[Path] = []
            found_git = False
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        remaining -= 1
                        if remaining < 0 or time.monotonic() >= deadline:
                            raise BrainError("Git repository discovery exceeded its bounded scope")
                        if entry.name == ".git" and not entry.is_symlink():
                            found_git = entry.is_dir(follow_symlinks=False) or entry.is_file(follow_symlinks=False)
                            continue
                        if (
                            depth < MAX_REPOSITORY_DISCOVERY_DEPTH
                            and entry.name not in DISCOVERY_IGNORED_DIRS
                            and not entry.is_symlink()
                            and entry.is_dir(follow_symlinks=False)
                        ):
                            children.append(Path(entry.path))
            except OSError:
                continue
            if found_git:
                paths.add(directory)
            else:
                pending.extend((child, depth + 1) for child in children)
    return sorted(paths)


@workspace_exclusive
def discover_and_configure_repositories(settings: Settings) -> list[Repository]:
    """Safely append newly cloned repositories to the authoritative brain.toml."""
    configured_paths = {repo.path.resolve() for repo in settings.repositories}
    new_paths = [path for path in discover_git_repositories([settings.root]) if path not in configured_paths]
    if not new_paths:
        return []
    if settings.config_path.suffix.lower() != ".toml":
        raise BrainError(
            "New Git repositories were found, but automatic config updates require brain.toml; "
            "migrate the legacy YAML config or add them manually."
        )

    try:
        expected = settings.config_path.lstat()
        raw, exceeded = read_direct_file_bytes(settings.config_path, max_bytes=MAX_CONFIG_BYTES)
        current = settings.config_path.lstat()
    except (OSError, ValueError) as error:
        raise BrainError("Could not safely read brain.toml for repository discovery") from error
    if (
        exceeded
        or settings.config_path.is_symlink()
        or not stat.S_ISREG(current.st_mode)
        or (expected.st_dev, expected.st_ino, expected.st_size, expected.st_mtime_ns, expected.st_ctime_ns)
        != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns)
    ):
        raise BrainError("brain.toml changed during repository discovery; retry refresh")
    try:
        data = tomllib.loads(raw.decode("utf-8"))
        repo_values = data.get("repositories") or []
        configured_names = {str(value["name"]) for value in repo_values}
        current_paths = set()
        for value in repo_values:
            candidate = Path(os.path.expandvars(str(value["path"]))).expanduser()
            current_paths.add((candidate if candidate.is_absolute() else settings.root / candidate).resolve())
    except (KeyError, OSError, TypeError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise BrainError("brain.toml changed or became invalid during repository discovery") from error

    new_paths = [path for path in new_paths if path not in current_paths]
    if not new_paths:
        settings.repositories[:] = load_settings(settings.config_path).repositories
        return []
    if len(repo_values) + len(new_paths) > MAX_CONFIG_REPOSITORIES:
        raise BrainError(f"Config supports at most {MAX_CONFIG_REPOSITORIES} repositories")

    all_paths = [*current_paths, *new_paths]
    rows: list[str] = []
    for path in new_paths:
        candidate = path.name
        if sum(other.name == path.name for other in all_paths) > 1 or candidate in configured_names:
            candidate = "-".join(path.relative_to(settings.root).parts)
        name = candidate
        counter = 2
        while name in configured_names:
            name = f"{candidate}-{counter}"
            counter += 1
        configured_names.add(name)
        rows.extend([
            "[[repositories]]",
            f"name = {json.dumps(name)}",
            f"path = {json.dumps(str(path.relative_to(settings.root)))}",
            'description = ""',
            "tags = []",
            "",
        ])

    separator = b"\n" if raw.endswith(b"\n") else b"\n\n"
    payload = separator + "\n".join(rows).encode("utf-8")
    if len(raw) + len(payload) > MAX_CONFIG_BYTES:
        raise BrainError(f"Config exceeds the {MAX_CONFIG_BYTES:,}-byte direct-file limit: {settings.config_path}")

    descriptor = -1
    appended = 0
    try:
        flags = (
            os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(settings.config_path, flags)
        opened = os.fstat(descriptor)
        path_metadata = settings.config_path.lstat()
        if (
            settings.config_path.is_symlink()
            or not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (path_metadata.st_dev, path_metadata.st_ino)
            or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
            != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns)
        ):
            raise OSError("brain.toml identity changed while opening")
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("brain.toml append made no progress")
            appended += written
            view = view[written:]
        os.fsync(descriptor)
    except OSError as error:
        if descriptor >= 0 and appended:
            try:
                changed = os.fstat(descriptor)
                if (
                    (changed.st_dev, changed.st_ino) == (current.st_dev, current.st_ino)
                    and changed.st_size == current.st_size + appended
                ):
                    os.ftruncate(descriptor, current.st_size)
                    os.fsync(descriptor)
            except OSError:
                pass
        raise BrainError("brain.toml changed during repository discovery; retry refresh") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    published = load_settings(settings.config_path)
    published_paths = {repo.path.resolve() for repo in published.repositories}
    if any(path not in published_paths for path in new_paths):
        raise BrainError("brain.toml changed during repository discovery; retry refresh")
    settings.repositories[:] = published.repositories
    return [repo for repo in settings.repositories if repo.path.resolve() in set(new_paths)]


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
    verification_content: str | None = None


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


def run(
    args: list[str], cwd: Path | None = None, input_text: str | None = None,
    *, timeout: float = 30.0, max_stdout_bytes: int = 8 * 1024 * 1024,
) -> subprocess.CompletedProcess[str]:
    try:
        environment = os.environ.copy()
        if Path(args[0]).name == "git":
            environment.update({"GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"})
        result = run_bounded_process(
            args, cwd or Path.cwd(),
            input_bytes=input_text.encode("utf-8") if input_text is not None else None,
            environment=environment,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=256 * 1024,
            timeout=timeout,
        )
        returncode = 124 if getattr(result, "timed_out", False) else 125 if getattr(result, "output_truncated", False) else result.returncode
        return subprocess.CompletedProcess(result.args, returncode, result.stdout, result.stderr)
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
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_CONFIG_BYTES:
            raise BrainError(f"Config exceeds the {MAX_CONFIG_BYTES:,}-byte direct-file limit: {path}")
        raw, exceeded = read_direct_file_bytes(path, max_bytes=MAX_CONFIG_BYTES)
        if exceeded:
            raise BrainError(f"Config exceeds the {MAX_CONFIG_BYTES:,}-byte direct-file limit: {path}")
        if path.suffix == ".toml":
            return tomllib.loads(raw.decode("utf-8"))
        text = raw.decode("utf-8")
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError:
            return simple_yaml_load(text)
        loaded = yaml.safe_load(text)
        return loaded or {}
    except (OSError, ValueError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
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
    try:
        configured = path.absolute()
        resolved_before = path.resolve(strict=False)
    except OSError as error:
        raise BrainError(f"Could not validate Brain-owned state directory: {error}") from error
    if path.is_symlink() or resolved_before != configured:
        raise BrainError("Brain-owned state directory escapes its configured location")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.resolve() != configured:
        raise BrainError("Brain-owned state directory escapes its configured location")
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
    if len(repo_values) > MAX_CONFIG_REPOSITORIES:
        raise BrainError(f"Config supports at most {MAX_CONFIG_REPOSITORIES} repositories")
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

    def bounded_config(
        section: dict[str, Any], label: str, name: str, default: int, minimum: int, maximum: int,
    ) -> int:
        raw = section.get(name, default)
        if isinstance(raw, bool):
            raise BrainError(f"{label}.{name} must be an integer")
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise BrainError(f"{label}.{name} must be an integer") from exc
        if not minimum <= value <= maximum:
            raise BrainError(f"{label}.{name} must be between {minimum} and {maximum}")
        return value

    def local(value: str) -> Path:
        candidate = Path(value).expanduser()
        return (candidate if candidate.is_absolute() else root / candidate).resolve()

    def managed(value: str) -> Path:
        candidate = Path(value).expanduser()
        return Path(os.path.abspath(candidate if candidate.is_absolute() else root / candidate))

    settings = Settings(
        name=str(project.get("name") or root.name),
        root=root,
        config_path=config_path,
        repositories=repositories,
        knowledge_dir=local(str(knowledge.get("path") or "knowledge")),
        runs_dir=managed(str(project.get("runs_dir") or ".runs")),
        state_dir=managed(str(project.get("state_dir") or "state")),
        generated_dir=managed(str(project.get("generated_dir") or "generated")),
        max_results=bounded_config(search, "search", "max_results", 100, 1, 5_000),
        hard_context_chars=bounded_config(context, "context", "hard_context_chars", 180_000, 10_000, MAX_CONFIG_CONTEXT_BYTES),
        source_window_lines=bounded_config(context, "context", "source_window_lines", 150, 10, 2_000),
        full_file_lines=bounded_config(context, "context", "full_file_lines", 350, 10, 5_000),
        soft_target_chars=bounded_config(context, "context", "soft_target_chars", 120_000, 10_000, MAX_CONFIG_CONTEXT_BYTES),
        clipboard_chunk_chars=bounded_config(delivery, "delivery", "clipboard_chunk_chars", 180_000, 1_000, MAX_CONFIG_CLIPBOARD_CHARS),
        graph_enabled=bool(graph.get("enabled", True)),
        graph_lazy=graph_mode == "lazy",
        branch_priority=[str(value).strip() for value in branch_priority if str(value).strip()],
        sync_fetch_scope=fetch_scope,
        watch_interval_seconds=bounded_config(sources, "sources", "watch_interval_seconds", 180, 10, 86_400),
        path_result_limit=bounded_config(search, "search", "path_result_limit", 12, 1, 100),
        candidate_limit=bounded_config(search, "search", "candidate_limit", 500, 1, 2_000),
        hydrate_limit=bounded_config(context, "context", "hydrate_limit", 18, 1, 100),
        max_regions_per_file=bounded_config(context, "context", "max_regions_per_file", 2, 1, 20),
        max_regions_per_repo=bounded_config(context, "context", "max_regions_per_repo", 8, 1, 200),
        max_state_gb=max(0, int(storage["max_state_gb"])) if "max_state_gb" in storage else 200,
        minimum_free_disk_gb=max(0, int(storage["minimum_free_disk_gb"])) if "minimum_free_disk_gb" in storage else 5,
        experience_enabled=bool(experience.get("enabled", True)),
        ticket_pattern=str(experience.get("ticket_pattern") or r"(?<![A-Z0-9])([A-Z][A-Z0-9]+-[0-9]+)(?![A-Z0-9])"),
        experience_commit_limit=bounded_config(experience, "experience", "commit_limit", 1000, 1, 5_000),
        experience_similar_cases=bounded_config(experience, "experience", "similar_cases", 5, 1, 50),
        experience_patch_chars=bounded_config(experience, "experience", "patch_chars", 0, 0, 1_000_000),
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
        loaded = json.loads(read_managed_text(
            settings.state_dir, path, max_bytes=16 * 1024 * 1024,
        ))
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _attach_source_snapshots(settings: Settings) -> None:
    state = load_source_state(settings)
    snapshot_root = settings.state_dir / "snapshots"
    for repo in settings.repositories:
        item = state.get(repo.name) or {}
        raw_snapshot = str(item.get("snapshot") or "")
        sha = str(item.get("sha") or "")
        snapshot = Path(raw_snapshot) if raw_snapshot else Path()
        try:
            relative = snapshot.relative_to(snapshot_root)
            legacy_repo = re.sub(r"[^A-Za-z0-9._-]+", "-", repo.name).strip(".-") or "repo"
            expected_repositories = {filesystem_component(repo.name), legacy_repo}
            direct = (
                snapshot.is_absolute()
                and len(relative.parts) == 2
                and relative.parts[0] in expected_repositories
                and relative.parts[1] == filesystem_component(sha)
                and not snapshot_root.is_symlink()
                and not (snapshot_root / relative.parts[0]).is_symlink()
                and not snapshot.is_symlink()
                and snapshot.resolve() == snapshot
                and snapshot.is_dir()
            )
            seal = snapshot.parent / f".{filesystem_component(sha)}.brain-snapshot.json"
            seal_state = json.loads(read_managed_text(
                settings.state_dir, seal, max_bytes=256 * 1024 * 1024,
            )) if direct else {}
            sealed = (
                isinstance(seal_state, dict)
                and seal_state.get("sha") == sha
                and seal_state.get("version") in {2, 3}
                and isinstance(seal_state.get("files"), dict)
            )
        except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            direct = False
            sealed = False
        if direct and sealed:
            repo.source_path = snapshot
            repo.source_ref = str(item.get("ref") or "") or None
            repo.source_sha = sha or None
            repo.source_status = str(item.get("status") or "snapshot")
            repo.source_fetched = bool(item.get("fetched"))
            repo.source_warning = str(item.get("warning") or "") or None
        elif raw_snapshot:
            repo.source_warning = "Ignored unsafe or invalid stored source snapshot; refresh is required"


def git_head(repo: Repository, ref: str = "HEAD", *, timeout: float = 30.0) -> str | None:
    result = run(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=repo.path, timeout=timeout)
    return result.stdout.strip() if result.returncode == 0 else None


def _walk_files(
    root: Path,
    *,
    max_entries: int = MAX_FALLBACK_SCAN_ENTRIES,
    deadline: float | None = None,
    max_depth: int = MAX_FALLBACK_SCAN_DEPTH,
) -> Iterable[Path]:
    deadline = deadline if deadline is not None else time.monotonic() + MAX_FALLBACK_SCAN_SECONDS
    pending = [(root, 0)]
    scanned = 0
    while pending:
        if scanned >= max_entries or time.monotonic() >= deadline:
            raise BrainError("repository fallback scan exceeded its bounded scope")
        directory, depth = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    scanned += 1
                    if scanned > max_entries or time.monotonic() >= deadline:
                        raise BrainError("repository fallback scan exceeded its bounded scope")
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name not in IGNORED_DIRS and depth < max_depth:
                            pending.append((Path(entry.path), depth + 1))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    path = Path(entry.path)
                    if entry.name.lower() in SENSITIVE_FILE_NAMES or path.suffix.lower() in SENSITIVE_SUFFIXES:
                        continue
                    if path.suffix.lower() in CODE_SUFFIXES or entry.name in {
                        "Dockerfile", "Jenkinsfile", "Makefile", "Procfile", "build.gradle", "gradlew", "mvnw", "pom.xml"
                    }:
                        yield path
        except OSError:
            continue


def _python_search(repo: Repository, pattern: str, fixed: bool, max_results: int) -> list[SearchHit]:
    # Python's backtracking regular-expression engine cannot be interrupted by
    # our wall-clock deadline.  When the bounded ripgrep backend is absent,
    # preserve the fallback only for escaped literals so its time budget is
    # enforceable even for adversarial request text.
    if not fixed:
        return []
    try:
        regex = re.compile(re.escape(pattern))
    except re.error as exc:
        raise BrainError(f"Invalid search regex: {exc}") from exc
    hits: list[SearchHit] = []
    root = repo.scan_path
    deadline = time.monotonic() + MAX_FALLBACK_SCAN_SECONDS
    remaining_bytes = MAX_FALLBACK_SEARCH_BYTES
    for path in _walk_files(root, deadline=deadline):
        try:
            limit = min(MAX_SOURCE_FILE_BYTES, remaining_bytes)
            raw, exceeded = read_direct_file_bytes(path, max_bytes=limit)
            if exceeded and remaining_bytes < MAX_SOURCE_FILE_BYTES:
                raise BrainError("Python lexical fallback exceeded its byte budget")
            if exceeded:
                continue
            remaining_bytes -= len(raw)
            for number, raw_line in enumerate(raw.splitlines(), 1):
                if time.monotonic() >= deadline:
                    raise BrainError("Python lexical fallback exceeded its time budget")
                line = raw_line.decode("utf-8", errors="replace")
                if regex.search(line):
                    hits.append(SearchHit(repo.name, logical_path(path.relative_to(root)), number, line, score=95, found_by=["python exact search"]))
                    if len(hits) >= max_results:
                        return hits
        except (OSError, ValueError):
            continue
    return hits


def search_repo(
    repo: Repository,
    pattern: str,
    *,
    fixed: bool = False,
    max_results: int = 100,
    reserve_backend: Any | None = None,
    complete_backend: Any | None = None,
) -> list[SearchHit]:
    root = repo.scan_path
    if not root.is_dir():
        return []
    from .backends.ripgrep import search as ripgrep_search

    ripgrep_reserved = False

    def reserve_ripgrep() -> bool:
        nonlocal ripgrep_reserved
        ripgrep_reserved = reserve_backend is None or bool(reserve_backend())
        return ripgrep_reserved

    result = ripgrep_search(
        root, pattern, fixed=fixed, max_results=max_results,
        reserve=reserve_ripgrep if reserve_backend is not None else None,
    )
    if result is None:
        if ripgrep_reserved and complete_backend is not None:
            complete_backend("ripgrep", 0.0, subprocesses=1, raw_hits=0)
        if reserve_backend is not None and not reserve_backend():
            return []
        started = time.perf_counter()
        hits = _python_search(repo, pattern, fixed, max_results)
        elapsed = (time.perf_counter() - started) * 1000
        if complete_backend is not None:
            complete_backend("python-fallback", elapsed, files=len(hits), raw_hits=len(hits))
        else:
            _record_backend("python-fallback", elapsed, files=len(hits), raw_hits=len(hits))
        return hits
    rows, stats = result
    recorder = complete_backend or _record_backend
    recorder(
        "ripgrep", float(stats["elapsed_ms"]), subprocesses=int(stats["subprocesses"]),
        bytes_scanned=int(stats["bytes_scanned"]), files=len({path for path, _, _ in rows}),
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


def _lexical_generation_ready(settings: Settings, repo: Repository | None = None) -> bool:
    generation = settings.atlas_generation
    if generation is None:
        return True
    key = ("component-validation", "lexical", generation.generation, generation.identity, repo.name if repo else "all")
    cache = _ACTIVE_RETRIEVAL_CACHE.get()
    if cache is not None and key in cache:
        return bool(cache[key])
    component = generation.component("lexical")
    from .index import LEXICAL_COMPONENT_SCHEMA_VERSION

    expected_snapshots = {
        str(name): str(value) for name, value in (component.get("details") or {}).get("snapshots", {}).items()
    }
    valid = False
    if (
        component.get("status") == "ready"
        and component.get("schema_version") == str(LEXICAL_COMPONENT_SCHEMA_VERSION)
        and expected_snapshots == generation.snapshots
    ):
        details = component.get("details") if isinstance(component.get("details"), dict) else {}
        repository_hashes = details.get("repository_hashes") if isinstance(details.get("repository_hashes"), dict) else {}
        repository_files = details.get("repository_files") if isinstance(details.get("repository_files"), dict) else {}
        if repo is not None and repo.name in repository_hashes:
            from .index import lexical_repository_identity

            identity = lexical_repository_identity(settings, repo.name, generation.snapshots.get(repo.name, ""))
            valid = bool(
                identity and identity[0] == repository_hashes.get(repo.name)
                and identity[1] == int(repository_files.get(repo.name, -1))
            )
        else:
            from .index import lexical_membership_identity

            identity = lexical_membership_identity(settings, generation.snapshots)
            valid = bool(identity and identity[0] == component.get("content_hash"))
    if cache is not None and len(cache) < 256:
        cache[key] = valid
    return valid


def _zoekt_manifest_hash(settings: Settings, repo: Repository) -> str | None:
    generation = settings.atlas_generation
    if generation is None:
        return None
    rows = (generation.component("zoekt").get("details") or {}).get("shards") or []
    return next((
        str(item.get("manifest_hash")) for item in rows
        if isinstance(item, dict)
        and item.get("repo") == repo.name
        and item.get("snapshot") == repo.source_sha
    ), None)


def search(settings: Settings, pattern: str, repos: Iterable[str] | None = None, *, fixed: bool = False) -> list[SearchHit]:
    from .index import query_generation_indexes, query_index
    from .backends.zoekt import search as zoekt_search

    if settings.atlas_generation is None and settings.atlas_generation_mode == "current":
        from .catalog import current_generation_ref

        settings = replace(settings, atlas_generation=current_generation_ref(settings))
    selected = settings.repos(repos)
    key = ("search", pattern, fixed, tuple(repo.name for repo in selected))
    cached = _cached_hits(key)
    if cached is not None:
        return cached
    trace = _ACTIVE_RETRIEVAL_TRACE.get()
    generation = settings.atlas_generation
    if (
        fixed
        and generation is not None
        and all(_zoekt_manifest_hash(settings, repo) is None for repo in selected)
        and (trace is None or trace.try_reserve_backend())
    ):
        indexed_started = time.perf_counter()
        indexed_stats: dict[str, object] = {}
        indexed = query_generation_indexes(
            settings,
            generation,
            selected,
            pattern,
            max_results=settings.max_results,
            max_candidate_files=min(
                MAX_PINNED_QUERY_CANDIDATE_FILES,
                max(len(selected), settings.candidate_limit),
            ),
            max_hits=min(settings.candidate_limit, settings.max_results * max(1, len(selected))),
            max_bytes=MAX_PINNED_QUERY_BYTES,
            max_seconds=MAX_PINNED_QUERY_SECONDS,
            stats=indexed_stats,
        )
        elapsed = (time.perf_counter() - indexed_started) * 1000
        if trace is not None:
            trace.complete_reserved_backend(
                "sqlite-fts5-batch", elapsed,
                raw_hits=sum(len(rows) for rows in indexed.values()) if indexed is not None else 0,
                cache_hit=indexed is not None and not indexed_stats.get("budget_exhausted"),
            )
        elif indexed is not None:
            _record_backend(
                "sqlite-fts5-batch", elapsed,
                raw_hits=sum(len(rows) for rows in indexed.values()),
                cache_hit=not bool(indexed_stats.get("budget_exhausted")),
            )
        if indexed is not None:
            hits = [
                SearchHit(repo.name, path, line, text, score=95, found_by=["sqlite trigram index"])
                for repo in selected for path, line, text in indexed[repo.name]
            ]
            hits = hits[: settings.max_results * max(1, len(selected))]
            if indexed_stats.get("budget_exhausted"):
                if trace is not None:
                    trace.fallback_reasons.append(
                        f"lexical_batch_budget:{indexed_stats.get('reason') or 'unknown'}"
                    )
                    trace.stop_reason = "lexical_batch_budget"
            else:
                _store_hits(key, hits)
            return _clone_hits(hits)
    if trace is not None:
        selected = selected[: trace.physical_budget_remaining]
        if not selected:
            trace.stop_reason = "physical_budget"
            return []

    def one(repo: Repository) -> list[SearchHit]:
        # A busy first repository must not hide evidence in later repositories.
        generation = settings.atlas_generation
        zoekt_hash = _zoekt_manifest_hash(settings, repo)
        local_trace = _ACTIVE_RETRIEVAL_TRACE.get()
        zoekt_reserved = False

        def reserve_zoekt() -> bool:
            nonlocal zoekt_reserved
            zoekt_reserved = local_trace is None or local_trace.try_reserve_backend()
            return zoekt_reserved

        zoekt_started = time.perf_counter()
        zoekt = (
            zoekt_search(
                settings, repo, pattern, fixed=fixed, max_results=settings.max_results,
                expected_manifest_hash=zoekt_hash,
                reserve=reserve_zoekt if local_trace is not None else None,
            )
            if generation is None or (
                generation.component("zoekt").get("status") == "ready" and zoekt_hash is not None
            )
            else None
        )
        if zoekt is not None:
            rows, stats = zoekt
            if zoekt_reserved and local_trace is not None:
                local_trace.complete_reserved_backend(
                    "zoekt", float(stats["elapsed_ms"]), subprocesses=1,
                    raw_hits=int(stats["raw_hits"]), cache_hit=True,
                )
            else:
                _record_backend("zoekt", float(stats["elapsed_ms"]), subprocesses=1, raw_hits=int(stats["raw_hits"]), cache_hit=True)
            return [
                SearchHit(repo.name, path, line, text, score=90 + min(9, score), found_by=["zoekt local shard"])
                for path, line, text, score in rows
            ]
        if zoekt_reserved and local_trace is not None:
            local_trace.complete_reserved_backend(
                "zoekt", (time.perf_counter() - zoekt_started) * 1000,
                subprocesses=1, raw_hits=0,
            )
        if local_trace is not None and local_trace.physical_budget_remaining <= 0:
            local_trace.stop_reason = "physical_budget"
            return []
        indexed_started = time.perf_counter()
        indexed_reserved = False
        should_query_index = (
            fixed and _lexical_generation_ready(settings, repo)
            and (settings.state_dir / "search.sqlite3").is_file()
        )
        if should_query_index and local_trace is not None:
            indexed_reserved = local_trace.try_reserve_backend()
            if not indexed_reserved:
                local_trace.stop_reason = "physical_budget"
                return []
        indexed = query_index(
            settings, repo, pattern, max_results=settings.max_results, snapshot_sha=repo.source_sha,
        ) if should_query_index else None
        if indexed_reserved and local_trace is not None:
            local_trace.complete_reserved_backend(
                "sqlite-fts5", (time.perf_counter() - indexed_started) * 1000,
                raw_hits=len(indexed or []), cache_hit=indexed is not None,
            )
        if indexed is None:
            if settings.atlas_generation_mode == "pinned":
                return []
            return search_repo(
                repo, pattern, fixed=fixed, max_results=settings.max_results,
                reserve_backend=local_trace.try_reserve_backend if local_trace is not None else None,
                complete_backend=local_trace.complete_reserved_backend if local_trace is not None else None,
            )
        if not indexed_reserved:
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
    from .index import query_generation_paths, query_paths

    if settings.atlas_generation is None and settings.atlas_generation_mode == "current":
        from .catalog import current_generation_ref

        settings = replace(settings, atlas_generation=current_generation_ref(settings))
    selected = settings.repos(repos)
    key = ("path", needle, tuple(repo.name for repo in selected))
    cached = _cached_hits(key)
    if cached is not None:
        return cached

    def ranked(repo: Repository, paths: Iterable[str]) -> list[SearchHit]:
        matches: list[SearchHit] = []
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
            matches.append(SearchHit(
                repo.name, relative, 1, relative, "verified path", score, ["repository path index"],
            ))
        return sorted(matches, key=lambda item: (-item.score, len(item.path), item.path))[:settings.path_result_limit]

    trace = _ACTIVE_RETRIEVAL_TRACE.get()
    generation = settings.atlas_generation
    if (
        generation is not None
        and (trace is None or trace.try_reserve_backend())
    ):
        indexed_started = time.perf_counter()
        indexed_stats: dict[str, object] = {}
        indexed = query_generation_paths(
            settings,
            generation,
            selected,
            needle,
            limit=settings.path_result_limit,
            max_candidate_paths=min(
                MAX_PINNED_PATH_CANDIDATES,
                max(len(selected), settings.path_result_limit * 20 * len(selected)),
            ),
            max_seconds=MAX_PINNED_PATH_SECONDS,
            stats=indexed_stats,
        )
        elapsed = (time.perf_counter() - indexed_started) * 1000
        raw_hits = sum(len(rows) for rows in indexed.values()) if indexed is not None else 0
        if trace is not None:
            trace.complete_reserved_backend(
                "path-index-batch", elapsed, raw_hits=raw_hits,
                cache_hit=indexed is not None and not indexed_stats.get("budget_exhausted"),
            )
        elif indexed is not None:
            _record_backend(
                "path-index-batch", elapsed, raw_hits=raw_hits,
                cache_hit=not bool(indexed_stats.get("budget_exhausted")),
            )
        if indexed is not None:
            hits = [hit for repo in selected for hit in ranked(repo, indexed[repo.name])]
            if indexed_stats.get("budget_exhausted"):
                if trace is not None:
                    trace.fallback_reasons.append(
                        f"path_batch_budget:{indexed_stats.get('reason') or 'unknown'}"
                    )
                    trace.stop_reason = "path_batch_budget"
            else:
                _store_hits(key, hits)
            return _clone_hits(hits)
    if trace is not None:
        selected = selected[: trace.physical_budget_remaining]
        if not selected:
            trace.stop_reason = "physical_budget"
            return []

    def one(repo: Repository) -> list[SearchHit]:
        root = repo.scan_path
        indexed_started = time.perf_counter()
        indexed = (
            query_paths(settings, repo, needle, limit=settings.path_result_limit, snapshot_sha=repo.source_sha)
            if _lexical_generation_ready(settings, repo)
            else None
        )
        _record_backend(
            "path-index" if indexed is not None else "path-scan",
            (time.perf_counter() - indexed_started) * 1000,
            raw_hits=len(indexed or []), cache_hit=indexed is not None,
        )
        paths = indexed if indexed is not None else (
            [] if settings.atlas_generation_mode == "pinned"
            else (logical_path(path.relative_to(root)) for path in _walk_files(root)) if root.is_dir() else []
        )
        return ranked(repo, paths)

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
    tests = [hit for hit in candidates if is_test_path(hit.path)]
    for hit in tests:
        hit.kind = "test"
        hit.score = 97
        hit.found_by.append("test discovery")
    return tests


_INDEXED_SOURCE_UNSET = object()


def read_source(
    settings: Settings,
    hit: SearchHit,
    *,
    full: bool = False,
    lines: tuple[int, int] | None = None,
    _indexed_source: object = _INDEXED_SOURCE_UNSET,
) -> Evidence:
    repo = settings.repo(hit.repo)
    root = repo.scan_path.resolve()
    path = (root / hit.path).resolve()
    if not path.is_relative_to(root):
        raise BrainError(f"Unsafe or missing file: {hit.repo}:{hit.path}")
    if path.name.lower() in SENSITIVE_FILE_NAMES or path.suffix.lower() in SENSITIVE_SUFFIXES:
        raise BrainError(f"Sensitive source path is excluded from automatic retrieval: {hit.repo}:{hit.path}")
    indexed_only = settings.atlas_generation_mode == "pinned"
    if indexed_only:
        if _indexed_source is not _INDEXED_SOURCE_UNSET:
            source = _indexed_source if isinstance(_indexed_source, str) else None
        else:
            from .index import read_indexed_file

            source = (
                read_indexed_file(settings, repo, hit.path, snapshot_sha=repo.source_sha)
                if _lexical_generation_ready(settings, repo) else None
            )
        if source is None:
            raise BrainError(f"Pinned indexed source is unavailable: {hit.repo}:{hit.path}")
    elif path.is_file():
        from .index import _read_source_bytes

        try:
            raw = _read_source_bytes(path, max_bytes=MAX_SOURCE_FILE_BYTES)
        except OSError as error:
            raise BrainError(f"Unsafe or missing file: {hit.repo}:{hit.path}") from error
        if len(raw) > MAX_SOURCE_FILE_BYTES:
            raise BrainError(f"Source file exceeds retrieval byte limit: {hit.repo}:{hit.path}")
        source = raw.decode("utf-8", errors="replace")
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
    verification_content = source if len(source.encode("utf-8")) <= 1_000_000 else None
    return Evidence(
        hit.repo, hit.path, start, end, "\n".join(content[start - 1:end]), hit.kind, hit.score,
        list(hit.found_by), verification_content,
    )


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
    # History is evidence too: a pinned ticket must never follow a movable ref
    # after a later refresh publishes a new source generation.
    revision = repo.source_sha or repo.source_ref or "HEAD"
    result = run(["git", "log", revision, f"-n{limit}", "--date=short", f"--pretty=format:{fmt}", "-S", query], cwd=repo.path)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    result = run(["git", "log", revision, f"-n{limit}", "--date=short", f"--pretty=format:{fmt}", "-G", re.escape(query)], cwd=repo.path)
    return result.stdout.strip() if result.returncode == 0 else ""


def _knowledge_corpus(settings: Settings, deadline: float | None = None) -> list[tuple[str, list[str]]]:
    """Load a bounded, direct-file knowledge corpus once per retrieval."""
    root = settings.knowledge_dir
    try:
        if root.is_symlink() or not root.is_dir():
            return []
        resolved_root = root.resolve()
    except OSError:
        return []
    pending = [resolved_root]
    corpus: list[tuple[str, list[str]]] = []
    scanned = 0
    total_bytes = 0
    while pending and scanned < MAX_KNOWLEDGE_SCAN_ENTRIES and len(corpus) < MAX_KNOWLEDGE_FILES:
        if deadline is not None and time.perf_counter() >= deadline:
            break
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = []
                for entry in iterator:
                    scanned += 1
                    if scanned > MAX_KNOWLEDGE_SCAN_ENTRIES:
                        break
                    entries.append(entry)
        except OSError:
            continue
        for entry in reversed(sorted(entries, key=lambda item: item.name)):
            try:
                if entry.is_symlink():
                    continue
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                    continue
                if not entry.is_file(follow_symlinks=False) or path.suffix.lower() != ".md":
                    continue
                metadata = entry.stat(follow_symlinks=False)
                if metadata.st_size > MAX_KNOWLEDGE_ITEM_BYTES:
                    continue
                raw, exceeded = read_direct_file_bytes(
                    path, max_bytes=MAX_KNOWLEDGE_ITEM_BYTES,
                )
                if exceeded or total_bytes + len(raw) > MAX_KNOWLEDGE_TOTAL_BYTES:
                    continue
                relative = path.relative_to(resolved_root)
            except (OSError, ValueError):
                continue
            total_bytes += len(raw)
            corpus.append((logical_path(relative), raw.decode("utf-8", errors="replace").splitlines()))
            if len(corpus) >= MAX_KNOWLEDGE_FILES or total_bytes >= MAX_KNOWLEDGE_TOTAL_BYTES:
                break
    corpus.sort(key=lambda item: item[0])
    return corpus


def knowledge_hits(
    settings: Settings,
    query: str,
    limit: int = 30,
    *,
    deadline: float | None = None,
) -> list[Evidence]:
    cache = _ACTIVE_RETRIEVAL_CACHE.get()
    cache_key = ("knowledge-corpus", str(settings.knowledge_dir))
    if cache is not None and cache_key in cache:
        corpus = cache[cache_key]
    else:
        corpus = _knowledge_corpus(settings, deadline)
        if cache is not None:
            cache[cache_key] = corpus
    regex = re.compile(re.escape(query[:MAX_REQUEST_TEXT_CHARS]), re.I)
    results: list[Evidence] = []
    for relative, lines in corpus:
        if deadline is not None and time.perf_counter() >= deadline:
            break
        for number, line in enumerate(lines, 1):
            if regex.search(line):
                start, end = max(1, number - 20), min(len(lines), number + 20)
                results.append(Evidence("knowledge", relative, start, end, "\n".join(lines[start - 1:end]), "knowledge", 70, ["knowledge search"]))
                break
        if len(results) >= limit:
            break
    return results


def _pom_dependencies(path: Path) -> list[str]:
    try:
        return _pom_dependencies_content(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []


def _pom_dependencies_content(content: str) -> list[str]:
    try:
        root = ET.fromstring(content)
    except (ET.ParseError, OSError):
        return []
    dependencies: list[str] = []
    for dependency in root.findall(".//{*}dependency"):
        if len(dependencies) >= MAX_PROJECT_MAP_DEPENDENCIES_PER_REPO:
            break
        group = dependency.find("{*}groupId")
        artifact = dependency.find("{*}artifactId")
        if artifact is not None and artifact.text:
            dependencies.append(f"{group.text if group is not None else '?'}:{artifact.text}"[:500])
    return dependencies


@workspace_exclusive
def generate_map(settings: Settings) -> str:
    output = ["# Generated Project Facts", "", f"Generated: {datetime.now(UTC).isoformat()}", ""]
    annotation = r"@(RestController|Controller|Service|Repository|FeignClient|KafkaListener|Scheduled|Entity|Table|RequestMapping|GetMapping|PostMapping|PutMapping|PatchMapping|DeleteMapping)\b"
    snapshots = {repo.name: str(repo.source_sha or "working-tree") for repo in settings.repositories}
    facts_by_repo: dict[str, list[tuple[str, int, str]]] = {repo.name: [] for repo in settings.repositories}
    dependencies_by_repo: dict[str, list[str]] = {repo.name: [] for repo in settings.repositories}
    matcher = re.compile(annotation)
    try:
        from .index import indexed_snapshot_documents

        documents = indexed_snapshot_documents(
            settings, snapshots, CODE_SUFFIXES | {".xml"},
            max_repositories=MAX_PROJECT_MAP_REPOSITORIES,
            max_items=MAX_PROJECT_MAP_DOCUMENTS,
            max_bytes=MAX_PROJECT_MAP_SOURCE_BYTES,
            max_file_bytes=MAX_PROJECT_MAP_FILE_BYTES,
            max_seconds=MAX_PROJECT_MAP_SOURCE_SECONDS,
        )
        manifest = True
        for repo, path, content in documents:
            if Path(path).suffix.lower() not in CODE_SUFFIXES and Path(path).name != "pom.xml":
                continue
            facts = facts_by_repo[repo]
            if len(facts) < 300:
                for number, line in enumerate(content.splitlines(), 1):
                    if matcher.search(line):
                        facts.append((path, number, line[:500]))
                        if len(facts) >= 300:
                            break
            if Path(path).name == "pom.xml":
                remaining = MAX_PROJECT_MAP_DEPENDENCIES_PER_REPO - len(dependencies_by_repo[repo])
                dependencies_by_repo[repo].extend(_pom_dependencies_content(content)[:max(0, remaining)])
    except sqlite3.DataError as error:
        raise BrainError("project facts authoritative source budget exceeded") from error
    except sqlite3.Error as error:
        if any(snapshot != "working-tree" for snapshot in snapshots.values()):
            raise BrainError("project facts are unavailable from the authoritative lexical snapshot") from error
        # Preserve the pre-Atlas direct-map behavior only for an explicitly
        # unpinned working tree. Published generations never use this branch.
        manifest = False
        deadline = time.monotonic() + MAX_PROJECT_MAP_SOURCE_SECONDS
        document_count = 0
        source_bytes = 0
        for repo in settings.repositories:
            for path in _walk_files(repo.scan_path, deadline=deadline):
                if Path(path).suffix.lower() not in CODE_SUFFIXES and path.name != "pom.xml":
                    continue
                document_count += 1
                if document_count > MAX_PROJECT_MAP_DOCUMENTS:
                    raise BrainError("project facts working-tree document budget exceeded")
                raw = _bounded_regular_file_bytes(path, MAX_PROJECT_MAP_FILE_BYTES)
                source_bytes += len(raw)
                if source_bytes > MAX_PROJECT_MAP_SOURCE_BYTES:
                    raise BrainError("project facts working-tree source budget exceeded")
                content = raw.decode("utf-8", errors="replace")
                relative = logical_path(path.relative_to(repo.scan_path))
                facts = facts_by_repo[repo.name]
                if len(facts) < 300:
                    for number, line in enumerate(content.splitlines(), 1):
                        if matcher.search(line):
                            facts.append((relative, number, line[:500]))
                            if len(facts) >= 300:
                                break
                if path.name == "pom.xml":
                    remaining = MAX_PROJECT_MAP_DEPENDENCIES_PER_REPO - len(dependencies_by_repo[repo.name])
                    dependencies_by_repo[repo.name].extend(
                        _pom_dependencies_content(content)[:max(0, remaining)]
                    )
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
        facts = (
            facts_by_repo[repo.name]
            if manifest is not None else []
        )
        output.append("### Framework facts")
        output.append("")
        if facts:
            output.extend(f"- `{path}:{line}` — `{text.strip()}`" for path, line, text in facts)
        else:
            output.append("- None detected")
        dependencies = dependencies_by_repo[repo.name] if manifest is not None else []
        output.extend(["", "### Maven dependencies", ""])
        output.extend(f"- `{item}`" for item in sorted(set(dependencies))) if dependencies else output.append("- None detected")
        output.append("")
    text = "\n".join(output).rstrip() + "\n"
    if len(text.encode("utf-8")) > MAX_PROJECT_MAP_ARTIFACT_BYTES:
        raise BrainError("project facts artifact budget exceeded")
    _atomic_generated_text_write(settings, settings.generated_dir / "PROJECT_FACTS.md", text)
    return text


def _request_body(text: str) -> dict[str, Any]:
    """Extract a versioned request from a whole chat response or request file."""
    # Windows PowerShell 5.1 writes ``Set-Content -Encoding UTF8`` files with
    # a UTF-8 BOM.  Treat that transport marker as encoding metadata, not as
    # part of the protocol document, for JSON, YAML, stdin, and clipboard use.
    text = text.removeprefix("\ufeff")
    stripped = text.strip()
    if not stripped:
        raise BrainError("The AI response is empty")
    if len(text) > MAX_REQUEST_TEXT_CHARS or len(text.encode("utf-8")) > MAX_REQUEST_TEXT_BYTES:
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
        wrapper = "INVESTIGATION_REQUEST" if loaded.get("version") in {4, 5} else "CONTEXT_REQUEST"
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
    if version not in {1, 2, 3, 4, 5}:
        raise BrainError(f"Unsupported Project Brain request version {version!r}; this build supports versions 1, 2, 3, 4, and 5")
    if version in {4, 5} and wrapper != "INVESTIGATION_REQUEST":
        raise BrainError(f"version {version} must use the INVESTIGATION_REQUEST wrapper")
    request["version"] = version
    allowed = {
        1: {"version", "objective", "searches", "paths", "symbols", "files", "history", "expand"},
        2: {"version", "objective", "searches", "paths", "symbols", "files", "history", "expand"},
        3: {"version", "objective", "hints", "coverage", "expand"},
        4: {"version", "objective", "runtime_facts", "hypotheses", "required", "resolve", "base_context_id", "checkpoint"},
        5: {"version", "mode", "objective", "runtime_facts", "hypotheses", "required", "resolve", "anchors", "base_context_id", "checkpoint", "wave"},
    }[version]
    unknown = sorted(set(request) - allowed)
    if unknown:
        raise BrainError(f"CONTEXT_REQUEST has unknown keys: {', '.join(unknown)}")
    objective = str(request.get("objective") or "").strip()
    if not objective:
        raise BrainError("objective is required")
    if len(objective) > 4_000 or len(objective.encode("utf-8")) > 4_000:
        raise BrainError("objective exceeds the 4,000-character / UTF-8 byte limit")
    request["objective"] = objective

    if version in {4, 5}:
        for key in ("runtime_facts", "required", "resolve"):
            value = request.get(key) or []
            if not isinstance(value, list) or len(value) > MAX_REQUEST_ITEMS:
                raise BrainError(f"{key} must be a list of at most {MAX_REQUEST_ITEMS} items")
            normalized: list[str] = []
            for index, item in enumerate(value):
                text_value = str(item).strip()
                if not text_value or len(text_value) > 500 or len(text_value.encode("utf-8")) > 500:
                    raise BrainError(
                        f"{key}[{index}] must be a non-empty value up to 500 characters and UTF-8 bytes"
                    )
                normalized.append(text_value)
            request[key] = list(dict.fromkeys(normalized))
        raw_hypotheses = request.get("hypotheses") or []
        if not isinstance(raw_hypotheses, list) or len(raw_hypotheses) > MAX_REQUEST_ITEMS:
            raise BrainError(f"hypotheses must be a list of at most {MAX_REQUEST_ITEMS} items")
        hypotheses: list[str] = []
        for index, item in enumerate(raw_hypotheses):
            if version == 5 and isinstance(item, dict):
                if set(item) - {"id", "statement"}:
                    raise BrainError(f"hypotheses[{index}] must contain only id and statement")
                external_id = str(item.get("id") or "").strip()
                if len(external_id.encode("utf-8")) > 100:
                    raise BrainError(f"hypotheses[{index}].id exceeds 100 bytes")
                text_value = str(item.get("statement") or "").strip()
            else:
                text_value = str(item).strip()
            if not text_value or len(text_value.encode("utf-8")) > 500:
                raise BrainError(
                    f"hypotheses[{index}] must be a non-empty value up to 500 characters (and 500 UTF-8 bytes)"
                )
            hypotheses.append(text_value)
        request["hypotheses"] = list(dict.fromkeys(hypotheses))
        base_context_id = str(request.get("base_context_id") or "").strip()
        if len(base_context_id) > 200 or len(base_context_id.encode("utf-8")) > 200:
            raise BrainError("base_context_id exceeds 200 characters or UTF-8 bytes")
        request["base_context_id"] = base_context_id or None
        if not isinstance(request.get("checkpoint", False), bool):
            raise BrainError("checkpoint must be true or false")
        request["checkpoint"] = bool(request.get("checkpoint"))
        if version == 5:
            mode = str(request.get("mode") or "").strip()
            modes = {"root_cause", "implementation_plan", "impact_analysis", "test_surface", "flow_trace", "history"}
            if mode not in modes:
                raise BrainError(f"mode must be one of: {', '.join(sorted(modes))}")
            request["mode"] = mode
            wave = request.get("wave")
            if wave is not None and (not isinstance(wave, int) or isinstance(wave, bool) or not 1 <= wave <= 4):
                raise BrainError("wave must be an integer from 1 through 4")
            request["wave"] = wave
            raw_anchors = request.get("anchors") or []
            anchors: list[dict[str, str]] = []
            allowed_anchor_kinds = {
                "symbol", "stack_frame", "exception", "log_literal", "error_code", "endpoint", "topic",
                "event", "queue", "config_key", "feature_flag", "schema", "table", "field", "constant",
                "package", "file_hint",
            }
            if isinstance(raw_anchors, dict):
                aliases = {kind: kind for kind in allowed_anchor_kinds}
                aliases.update({f"{kind}s": kind for kind in allowed_anchor_kinds})
                aliases.update({"symbols": "symbol", "stack_frames": "stack_frame", "exceptions": "exception"})
                unknown_anchor_groups = sorted(set(raw_anchors) - set(aliases))
                if unknown_anchor_groups:
                    raise BrainError(f"anchors has unknown groups: {', '.join(unknown_anchor_groups)}")
                grouped: list[dict[str, str]] = []
                for group, raw_values in raw_anchors.items():
                    if not isinstance(raw_values, list):
                        raise BrainError(f"anchors.{group} must be a list")
                    grouped.extend({"kind": aliases[group], "value": str(value)} for value in raw_values)
                raw_anchors = grouped
            if not isinstance(raw_anchors, list) or len(raw_anchors) > MAX_REQUEST_ITEMS:
                raise BrainError(f"anchors must contain at most {MAX_REQUEST_ITEMS} items")
            for index, item in enumerate(raw_anchors):
                if not isinstance(item, dict) or set(item) - {"kind", "value"}:
                    raise BrainError(f"anchors[{index}] must contain only kind and value")
                kind = str(item.get("kind") or "").strip()
                value = str(item.get("value") or "").strip()
                if kind not in allowed_anchor_kinds or not value or len(value.encode("utf-8")) > 1_000:
                    raise BrainError(f"anchors[{index}] has an invalid kind or value")
                anchors.append({"kind": kind, "value": value})
            request["anchors"] = anchors
        from .retrieval.planner import objective_terms

        exact_anchor_terms = [
            str(item.get("value") or "")
            for item in request.get("anchors") or [] if isinstance(item, dict) and item.get("value")
        ] if version == 5 else []
        derived_anchor_terms = [
            term
            for value in exact_anchor_terms
            for term in objective_terms(value, limit=4)
            if term != value
        ]
        resolve_terms = [
            *exact_anchor_terms, *request["resolve"], *derived_anchor_terms,
            *objective_terms(objective, limit=8),
        ]
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
        else {key: request[key] for key in ("version", "mode", "objective", "runtime_facts", "hypotheses", "required", "resolve", "anchors", "base_context_id", "checkpoint", "wave") if key in request}
        if request["version"] == 5
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
        "normalized_json": json.dumps({"INVESTIGATION_REQUEST" if request["version"] in {4, 5} else "CONTEXT_REQUEST": public_request}, indent=2, ensure_ascii=False) + "\n",
    }


def protocol_request_signature(plan: dict[str, Any], ticket: str, state: dict[str, Any]) -> str:
    """Bind v5 replay identity to the ticket and its pinned serving state."""
    if int(plan.get("protocol_version") or 1) != 5:
        return str(plan["signature"])
    payload = {
        "protocol": 5,
        "ticket": ticket,
        "generation": state.get("generation"),
        "atlas_generation_id": state.get("atlas_generation_id"),
        "base_context_id": (plan.get("request") or {}).get("base_context_id"),
        "request": json.loads(str(plan["normalized_json"])),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def request_repair_prompt(error: str) -> str:
    """Build a safe prompt the user can copy back when the model broke protocol."""
    return (
        "Your previous response could not be executed by Project Brain.\n\n"
        f"Validation error: {error}\n\n"
        "Return only one minimal fenced YAML block. State the repository fact to establish; do not invent repository names or enumerate command matrices.\n\n"
        "Legacy CONTEXT_REQUEST forms with version: 1, version: 2, or version: 3 and INVESTIGATION_REQUEST version: 4 remain supported; new repairs use investigation protocol v5.\n\n"
        "```yaml\n"
        "INVESTIGATION_REQUEST:\n"
        f"  version: {PROTOCOL_VERSION}\n"
        "  mode: root_cause\n"
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
    hit = SearchHit(repo.name, logical_path(path.relative_to(root)), line_range[0] if line_range else 1, "", "requested file", 100, ["direct file request"])
    try:
        return read_source(settings, hit, full=line_range is None, lines=line_range)
    except BrainError:
        return None


def working_tree_diffs(settings: Settings, repos: Iterable[str] | None = None) -> list[Evidence]:
    """Read tracked working-tree diffs without modifying or staging anything."""
    evidence: list[Evidence] = []
    remaining = MAX_WORKING_TREE_DIFF_TOTAL_BYTES
    deadline = time.monotonic() + MAX_WORKING_TREE_DIFF_TOTAL_SECONDS
    omission_bytes = WORKING_TREE_DIFF_OMISSION.encode("utf-8")
    for repo in settings.repos(repos):
        if not (repo.path / ".git").exists():
            continue
        parts: list[str] = []
        omitted = False
        for args in (["git", "diff", "--no-ext-diff"], ["git", "diff", "--cached", "--no-ext-diff"]):
            seconds = min(MAX_WORKING_TREE_DIFF_COMMAND_SECONDS, deadline - time.monotonic())
            if remaining <= len(omission_bytes) or seconds <= 0:
                omitted = True
                break
            limit = min(MAX_WORKING_TREE_DIFF_COMMAND_BYTES, remaining - len(omission_bytes))
            try:
                completed = run_bounded_process(
                    args,
                    repo.path,
                    max_stdout_bytes=limit,
                    timeout=seconds,
                )
            except OSError:
                continue
            raw = completed.stdout.encode("utf-8")[:limit]
            remaining -= len(raw)
            if raw:
                parts.append(raw.decode("utf-8", errors="ignore").strip())
            omitted = omitted or bool(
                getattr(completed, "output_truncated", False)
                or getattr(completed, "timed_out", False)
            )
            if omitted:
                break
        if omitted:
            marker = omission_bytes[:max(0, remaining)]
            if marker:
                parts.append(marker.decode("utf-8"))
                remaining -= len(marker)
        content = "\n".join(part for part in parts if part)
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
        if remaining <= 0 or time.monotonic() >= deadline:
            break
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
        deadline = started + max(0, compiled_plan.timeout_ms) / 1000

        def time_budget_exhausted() -> bool:
            exhausted = time.perf_counter() >= deadline
            if exhausted:
                trace.stop_reason = "time_budget"
            return exhausted

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
                str(item["repo"]), str(item["path"]), int(item["line"]), str(item.get("text") or ""),
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
            if time_budget_exhausted():
                break
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
            bundle.evidence.extend(knowledge_hits(settings, operation.value, deadline=deadline))
        trace.add_stage("exact_lexical_ms", (time.perf_counter() - lexical_started) * 1000)

        semantic_started = time.perf_counter()
        semantic_repo_scores: dict[str, float] = {}
        try:
            from .editions import current_edition

            edition = current_edition(settings)
            if edition in {"semantic", "precision"}:
                if time_budget_exhausted():
                    from .semantic import semantic_component_available

                    available = semantic_component_available(settings, bundle.atlas_generation)
                    trace.semantic_status = "degraded" if available else "unavailable"
                    bundle.warnings.append(
                        "Semantic retrieval was skipped after the query time budget expired; used Core retrieval only."
                        if available else
                        "Semantic index is unavailable or stale; used Core retrieval only."
                    )
                else:
                    emit("semantic", candidate_count=len(candidates))
                    from .semantic import search_semantic

                    semantic_status: dict[str, str] = {}
                    explicit_semantic_repos = [
                        str(repo) for operation in compiled_plan.operations for repo in operation.repos
                    ]
                    semantic_repo_scope = list(dict.fromkeys([
                        *explicit_semantic_repos,
                        *atlas_repo_scope,
                        *(repo.name for repo in settings.repositories),
                    ]))[:max(1, settings.widen_repo_limit)]
                    trace.semantic_repo_scope = semantic_repo_scope
                    semantic = search_semantic(
                        settings,
                        bundle.objective,
                        repos=set(semantic_repo_scope),
                        trace=trace,
                        generation=bundle.atlas_generation,
                        serving_status=semantic_status,
                    )
                    trace.semantic_status = semantic_status.get("status", "unavailable")
                    for item in semantic:
                        semantic_repo_scores[str(item["repo"])] = max(
                            semantic_repo_scores.get(str(item["repo"]), -1.0), float(item.get("score") or 0)
                        )
                    candidates.extend(
                        SearchHit(
                            str(item["repo"]), str(item["path"]), int(item["line"]),
                            str(item.get("symbol") or item.get("target_id") or ""), "semantic candidate",
                            round(50 + float(item.get("score") or 0) * 50, 3), ["local semantic index"],
                        )
                        for item in semantic
                        if str(item.get("path") or "")
                        and (not str(item.get("kind") or "").startswith("atlas_") or item.get("kind") == "atlas_entity_card")
                    )
                    if trace.semantic_status == "degraded":
                        bundle.warnings.append(
                            "Semantic serving is degraded; healthy-shard candidates may be used, but the effective edition is degraded."
                        )
                    elif trace.semantic_status != "ready":
                        bundle.warnings.append("Semantic index is unavailable or stale; used Core retrieval only.")
        except (OSError, ValueError, RuntimeError):
            bundle.warnings.append("Semantic runtime failed; used Core retrieval only.")
            trace.semantic_status = "failed"
            trace.fallback_reasons.append("semantic_runtime")
        trace.add_stage("semantic_ms", (time.perf_counter() - semantic_started) * 1000)
        trace.add_stage("candidate_discovery_ms", (time.perf_counter() - discovery_started) * 1000)

        emit("repo_routing", candidate_count=len(candidates))
        routing_started = time.perf_counter()
        repo_scope_limit = max(settings.initial_repo_limit, settings.widen_repo_limit)
        fallback_repos = route_repositories(settings.repositories, request, candidates, limit=repo_scope_limit)
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
            if time_budget_exhausted():
                break
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
                if time_budget_exhausted():
                    break
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
                            history_block = "\n".join(_source_markdown_block(result, "text"))
                            bundle.history.append(f"## {repo_name}: `{operation.value}`\n\n{history_block}")
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
            if time_budget_exhausted():
                break
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

        if include_diff and not time_budget_exhausted():
            bundle.evidence.extend(working_tree_diffs(settings))

        experience_started = time.perf_counter()
        if settings.experience_enabled and not time_budget_exhausted():
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
        related = [] if time_budget_exhausted() else related_relationships(
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

            if current_edition(settings) == "precision" and not time_budget_exhausted():
                from .models import rerank_candidates

                requested = [name for name in ("searches", "paths", "symbols", "files", "history") if request.get(name)]
                rerank_query = bundle.objective + ("\nRequested evidence: " + ", ".join(requested) if requested else "")
                candidates = rerank_candidates(settings, rerank_query, candidates, trace=trace)
        except (OSError, ValueError, RuntimeError):
            bundle.warnings.append("Local reranker failed; used semantic/lexical candidate ranking.")
            trace.fallback_reasons.append("reranker_runtime")
        trace.add_stage("rerank_ms", (time.perf_counter() - rerank_started) * 1000)

        selection_started = time.perf_counter()
        selected, omitted = select_candidates(settings, candidates, already_fused=True)
        omitted.extend(early_omitted)
        trace.add_stage("selection_ms", (time.perf_counter() - selection_started) * 1000)

        emit("hydrating", evidence_count=len(bundle.evidence), candidate_count=len(selected))
        hydrate_started = time.perf_counter()
        indexed_sources: dict[tuple[str, str], str] | None = None
        if settings.atlas_generation_mode == "pinned" and bundle.atlas_generation is not None:
            from .index import read_generation_files

            indexed_sources = read_generation_files(
                settings, bundle.atlas_generation, ((hit.repo, hit.path) for hit in selected),
                max_bytes=MAX_PINNED_HYDRATION_BYTES,
                max_seconds=MAX_PINNED_HYDRATION_SECONDS,
            )
        source_budget = max(10_000, settings.hard_context_chars - 40_000)
        source_chars = sum(len(item.content) for item in bundle.evidence)
        for index, hit in enumerate(selected):
            # An optional model call can cross the soft query deadline after it
            # starts.  Preserve exact-source authority by hydrating the first
            # surviving source candidate before stopping later reads.
            if time_budget_exhausted() and first_verified_evidence_ms is not None:
                omitted.extend(selected[index:])
                break
            try:
                evidence = read_source(
                    settings,
                    hit,
                    _indexed_source=(
                        indexed_sources.get((hit.repo, hit.path)) if indexed_sources is not None else None
                    ) if settings.atlas_generation_mode == "pinned" else _INDEXED_SOURCE_UNSET,
                )
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
            "subprocess_count": trace.subprocess_count,
            "bytes_scanned": trace.bytes_scanned,
            "bytes_read": trace.bytes_read,
            "repo_scope_count": len(trace.final_repo_scope),
            "repo_scope_limit": repo_scope_limit,
            "semantic_repo_count": len(trace.semantic_repo_scope),
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
            "routing_index": atlas_route.get("routing_index"),
            "routing_terms": int(atlas_route.get("routing_terms") or 0),
            "cards_considered": int(atlas_route.get("cards_considered") or 0),
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
    generation = bundle.atlas_generation

    def authoritative(item: Evidence) -> bool:
        if item.repo in {"external", "knowledge"} or item.kind in {
            "knowledge", "local diff", "user-supplied external evidence",
        } or item.path == "(working tree diff)":
            return False
        return generation is None or item.repo in generation.snapshots

    repository_evidence = [item for item in bundle.evidence if authoritative(item)]
    tests = [item for item in repository_evidence if item.kind == "test" or is_test_path(item.path)]
    configs = [item for item in repository_evidence if Path(item.path).suffix.lower() in config_suffixes]
    production = [
        item for item in repository_evidence if item not in tests and item not in configs
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


def _effective_retrieval_edition(
    requested: str, *, semantic_used: bool, reranker_used: bool, semantic_status: str,
) -> str:
    semantic_ready = semantic_status == "ready"
    if requested == "precision" and reranker_used and semantic_used and semantic_ready:
        return "Precision"
    if requested in {"semantic", "precision"} and semantic_used and semantic_ready:
        return "Semantic"
    if requested == "core":
        return "Core"
    return "Degraded Core"


def _public_evidence_id(progress: dict[str, Any] | None, item: Evidence) -> str:
    internal = _evidence_id(item)
    if progress:
        return str((progress.get("evidence_public_ids") or {}).get(internal) or internal)
    return internal


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
                configured_root = settings.knowledge_dir
                root = configured_root.resolve()
                candidate = configured_root / relative
                source_path = candidate.resolve()
                relative_candidate = candidate.relative_to(configured_root)
                parents = [configured_root, *(configured_root / Path(*relative_candidate.parts[:index]) for index in range(1, len(relative_candidate.parts)))]
                if (
                    configured_root.is_symlink()
                    or candidate.is_symlink()
                    or any(parent.is_symlink() for parent in parents)
                    or not source_path.is_relative_to(root)
                ):
                    missed += 1
                    continue
                lines = _bounded_regular_file_bytes(
                    source_path, MAX_KNOWLEDGE_ITEM_BYTES,
                ).decode("utf-8", errors="replace").splitlines()
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
    new_items = [item for item in bundle.evidence if _evidence_id(item) in new_evidence_ids]
    new_public_ids = [_public_evidence_id(progress, item) for item in new_items]
    output.append(f"- Embedded evidence IDs: `{', '.join(new_public_ids) or 'none'}`")
    output.append("- Omitted evidence IDs due to byte limit: `none`")
    output.extend(["", "## New source evidence", ""])
    if progress.get("protocol_version") == 5 and progress.get("investigation_runtime"):
        from .investigation import render_protocol_v5

        output.extend([render_protocol_v5(progress["investigation_runtime"], delta=True), ""])
    if not new_items:
        output.append("- None")
    for item in new_items:
        source_block = _source_markdown_block(item.content, _language(item.path))
        output.extend([
            f"### {_public_evidence_id(progress, item)} — {item.repo} — `{item.path}:{item.line_start}-{item.line_end}`", "",
            f"Kind: {item.kind}  ", f"Found by: {', '.join(item.found_by)}", "",
            *source_block, "",
        ])
    output.extend(["## Stable candidate changes", ""])
    for item in bundle.additional_candidates[:50]:
        output.append(f"- `{_candidate_id(generation, item)}` `{item.repo}:{item.path}:{item.line}` — {item.kind} — score {item.score}")
    if not bundle.additional_candidates:
        output.append("- None")
    output.extend(["", "## Unresolved", ""])
    output.extend(f"- {item}" for item in bundle.unresolved) if bundle.unresolved else output.append("- None")
    output.extend([
        "", "## Next-Best-Evidence", "",
        *_source_markdown_block(json.dumps(progress.get("next_best_evidence") or {}, indent=2, sort_keys=True), "json"),
        "",
    ])
    return _bounded_protocol_context(
        "\n".join(output).rstrip() + "\n",
        settings.hard_context_chars,
        new_public_ids,
    )


def _language(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return {
        ".java": "java", ".kt": "kotlin", ".py": "python", ".js": "javascript",
        ".jsx": "jsx", ".ts": "typescript", ".tsx": "tsx", ".go": "go",
        ".rs": "rust", ".rb": "ruby", ".xml": "xml", ".yml": "yaml",
        ".yaml": "yaml", ".toml": "toml", ".sql": "sql", ".sh": "bash",
    }.get(suffix, "text")


def _source_markdown_block(content: str, language: str) -> list[str]:
    """Fence untrusted source with a delimiter it cannot terminate."""
    backticks = max((len(match.group(0)) for match in re.finditer(r"`+", content)), default=0) + 1
    tildes = max((len(match.group(0)) for match in re.finditer(r"~+", content)), default=0) + 1
    marker, length = ("`", max(3, backticks)) if backticks <= tildes else ("~", max(3, tildes))
    if length <= 64:
        fence = marker * length
        return [f"{fence}{language}", content, fence]
    return [f'<pre data-language="{html.escape(language, quote=True)}"><code>', html.escape(content), "</code></pre>"]


def _render_first_useful_checkpoint(
    settings: Settings,
    ticket: str,
    number: int,
    checkpoint_id: str,
    context_id: str,
    requested_base: str | None,
    generation: Any,
    evidence_rows: list[tuple[str, Evidence]],
) -> str:
    output = [
        "# PROJECT BRAIN FIRST USEFUL CHECKPOINT", "", f"Ticket: `{ticket}`",
        f"Request: `{number:03d}`", f"Checkpoint ID: `{checkpoint_id}`",
        f"Continuation context ID: `{context_id}`",
        f"Prior context ID: `{requested_base or 'none'}`", "",
        "## Evidence contract", "",
        f"- Atlas generation: `{generation.generation}`",
        f"- Atlas identity: `{generation.identity}`",
        "- This is an early, durable checkpoint. Later investigation state is delivered as a lineage-linked delta.",
        "- Only the exact pinned source regions below are evidence authority.", "",
        "## Exact pinned evidence", "",
    ]
    for public_id, item in evidence_rows:
        output.extend([
            f"### {public_id} — `{item.repo}:{item.path}:{item.line_start}-{item.line_end}`", "",
            *_source_markdown_block(item.content, _language(item.path)), "",
        ])
    return _bounded_markdown("\n".join(output).rstrip() + "\n", min(settings.hard_context_chars, 24_000))


def _publish_first_useful_checkpoint(
    settings: Settings,
    ticket: str,
    number: int,
    context_id: str,
    requested_base: str | None,
    bundle: ContextBundle,
    request: dict[str, Any],
    request_signature: str,
    state: dict[str, Any],
    directory: Path,
    progress: Any | None,
) -> dict[str, Any] | None:
    """Durably expose bounded pinned evidence before later v5 flow construction."""
    from .investigation import (
        _exact_evidence_anchors,
        _is_runtime_entry_anchor,
        _java_file_intelligence,
        _verified_value_location,
        resolve_runtime_anchors,
        stable_evidence_id,
    )

    generation = bundle.atlas_generation
    existing_checkpoint = state.get("progressive_checkpoint")
    if isinstance(existing_checkpoint, dict) and existing_checkpoint.get("status") == "published":
        if (
            existing_checkpoint.get("continuation_status") in {"pending", "failed"}
            and existing_checkpoint.get("request_signature") == request_signature
        ):
            expected_hash = str(existing_checkpoint.get("content_hash") or "")
            artifact_name = str(existing_checkpoint.get("artifact") or "")
            handoff_name = str(existing_checkpoint.get("handoff_artifact") or "")
            artifact_path = (directory / artifact_name).resolve()
            handoff_candidate = Path(handoff_name)
            try:
                handoff_path = _validated_generated_artifact(
                    settings, handoff_candidate, create_parents=False,
                ).resolve()
                if (
                    not artifact_name or Path(artifact_name).name != artifact_name
                    or not artifact_path.is_relative_to(directory.resolve())
                    or not artifact_path.is_file() or artifact_path.is_symlink()
                    or not handoff_path.is_file() or handoff_path.is_symlink()
                ):
                    raise BrainError("Published first-useful checkpoint artifact is unavailable")
                try:
                    artifact_content = _bounded_regular_file_bytes(
                        artifact_path, MAX_CHECKPOINT_ARTIFACT_BYTES,
                    )
                    handoff_content = _bounded_regular_file_bytes(
                        handoff_path, MAX_CHECKPOINT_ARTIFACT_BYTES,
                    )
                except BrainError as error:
                    raise BrainError("Published first-useful checkpoint artifact is corrupt") from error
                if (
                    expected_hash != "sha256:" + hashlib.sha256(artifact_content).hexdigest()
                    or handoff_content != artifact_content
                ):
                    raise BrainError("Published first-useful checkpoint artifact is corrupt")
                proofs = existing_checkpoint.get("evidence_proofs")
                if (
                    generation is None
                    or int(existing_checkpoint.get("generation") or -1) != generation.generation
                    or not isinstance(proofs, list) or not 1 <= len(proofs) <= 3
                ):
                    raise BrainError("Published first-useful checkpoint evidence proof is invalid")
                proof_rows: list[tuple[str, Evidence]] = []
                for proof in proofs:
                    if not isinstance(proof, dict):
                        raise BrainError("Published first-useful checkpoint evidence proof is invalid")
                    match = next((
                        item for item in bundle.evidence
                        if _evidence_id(item) == proof.get("internal_evidence_id")
                        and item.repo == proof.get("repo") and item.path == proof.get("path")
                        and item.line_start == proof.get("line_start") and item.line_end == proof.get("line_end")
                        and hashlib.sha256(item.content.encode("utf-8")).hexdigest() == proof.get("content_sha256")
                        and item.repo in generation.snapshots
                        and item.kind not in {"knowledge", "local diff", "user-supplied external evidence"}
                        and item.path != "(working tree diff)"
                    ), None)
                    if match is None and generation is not None:
                        from .index import read_indexed_file

                        repo_name = str(proof.get("repo") or "")
                        path = str(proof.get("path") or "")
                        try:
                            start = int(proof.get("line_start") or 0)
                            end = int(proof.get("line_end") or 0)
                            pinned = read_indexed_file(
                                settings, settings.repo(repo_name), path,
                                snapshot_sha=generation.snapshots.get(repo_name),
                            )
                        except (KeyError, TypeError, ValueError):
                            pinned = None
                            start = end = 0
                        pinned_lines = pinned.splitlines() if pinned is not None else []
                        content = "\n".join(pinned_lines[start - 1:end])
                        candidate = Evidence(
                            repo_name, path, start, end, content, "code", 100,
                            ["pinned checkpoint revalidation"], pinned,
                        )
                        if (
                            1 <= start <= end <= len(pinned_lines)
                            and _evidence_id(candidate) == proof.get("internal_evidence_id")
                            and hashlib.sha256(content.encode("utf-8")).hexdigest() == proof.get("content_sha256")
                        ):
                            match = candidate
                    public_id = str(proof.get("public_id") or "")
                    if match is None or stable_evidence_id(state, match) != public_id:
                        raise BrainError("Published first-useful checkpoint evidence proof is invalid")
                    proof_rows.append((public_id, match))
                expected_content = _render_first_useful_checkpoint(
                    settings, ticket, number, str(existing_checkpoint.get("checkpoint_id") or ""),
                    context_id, requested_base, generation, proof_rows,
                ).encode("utf-8")
                if expected_content != artifact_content:
                    raise BrainError("Published first-useful checkpoint does not match pinned evidence")
            except OSError as exc:
                raise BrainError(f"Published first-useful checkpoint artifact is unavailable: {exc}") from exc
            return existing_checkpoint
        return None
    if generation is None:
        return None

    entry_anchors: list[dict[str, Any]] = [
        item for item in _exact_evidence_anchors(request, bundle, generation)
        if item.get("evidence_authority") == "exact_source"
        and float(item.get("confidence") or 0) >= .9
        and _is_runtime_entry_anchor(item)
        and not is_test_path(str(item.get("path") or ""))
    ]
    resolver_inputs: list[object] = [
        str(request.get("objective") or ""),
        *(request.get("runtime_facts") or []),
        *(request.get("resolve") or []),
        *(request.get("anchors") or []),
    ]
    resolved = resolve_runtime_anchors(settings, generation, resolver_inputs, use_cache=True)
    for item in resolved.get("candidates") or []:
        if (
            _is_runtime_entry_anchor(item)
            and not is_test_path(str(item.get("path") or ""))
            and _verified_value_location(
                bundle, str(item.get("repo")), str(item.get("path")), int(item.get("line") or 1),
                item.get("value"), kind=str(item.get("kind") or ""),
            )
        ):
            entry_anchors.append({**item, "evidence_authority": "exact_source", "confidence": 1.0})
    for evidence in bundle.evidence[:50]:
        if (
            evidence.repo not in generation.snapshots
            or is_test_path(evidence.path)
            or Path(evidence.path).suffix.lower() not in {".java", ".kt", ".kts", ".groovy"}
        ):
            continue
        structural_source = evidence.verification_content or (
            evidence.content if evidence.line_start == 1 else None
        )
        if structural_source is None:
            continue
        extracted, _ = _java_file_intelligence(
            evidence.repo, evidence.path, _evidence_id(evidence), None, structural_source,
        )
        entry_anchors.extend(
            {
                **item,
                "identity": item.get("anchor_id"),
                "line": int(item.get("line") or 1),
                "evidence_authority": "exact_source",
            }
            for item in extracted
            if _is_runtime_entry_anchor(item)
            and bool((item.get("provenance") or {}).get("exact_source"))
        )
    if not entry_anchors:
        return None
    candidates = [
        item for item in bundle.evidence
        if item.repo in generation.snapshots
        and item.kind not in {"knowledge", "local diff", "user-supplied external evidence"}
        and item.path != "(working tree diff)"
        and len(item.content.encode("utf-8")) <= 8_192
        and any(
            item.repo == anchor.get("repo") and item.path == anchor.get("path")
            and item.line_start <= int(anchor.get("line") or 1) <= item.line_end
            for anchor in entry_anchors
        )
    ][:12]
    if not candidates:
        return None
    checkpoint_id = f"{context_id}-P1"
    evidence_rows: list[tuple[str, Evidence]] = []
    for item in candidates:
        public_id = stable_evidence_id(state, item)
        proposed = _render_first_useful_checkpoint(
            settings, ticket, number, checkpoint_id, context_id, requested_base,
            generation, [*evidence_rows, (public_id, item)],
        )
        if len(proposed.encode("utf-8")) > 22_000:
            continue
        evidence_rows.append((public_id, item))
        if len(evidence_rows) >= 3:
            break
    if not evidence_rows:
        return None
    content = _render_first_useful_checkpoint(
        settings, ticket, number, checkpoint_id, context_id, requested_base, generation, evidence_rows,
    )
    evidence_rows = [
        (public_id, item) for public_id, item in evidence_rows
        if f"### {public_id} —" in content
    ]
    if not evidence_rows:
        return None
    artifact = directory / f"checkpoint-{number:03d}.md"
    handoff = handoff_dir(settings, ticket) / f"checkpoint-{number:03d}.md"
    internal_ids = [_evidence_id(item) for _, item in evidence_rows]
    checkpoint = {
        "schema_version": "first-useful-checkpoint-v1",
        "status": "published",
        "continuation_status": "pending",
        "checkpoint_id": checkpoint_id,
        "context_id": context_id,
        "base_context_id": requested_base,
        "generation": generation.generation,
        "artifact": artifact.name,
        "handoff_artifact": str(handoff),
        "content_hash": "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "request_signature": request_signature,
        "wave": int(request.get("wave") or number),
        "evidence_ids": [public_id for public_id, _ in evidence_rows],
        "internal_evidence_ids": internal_ids,
        "evidence_proofs": [
            {
                "public_id": public_id, "internal_evidence_id": _evidence_id(item),
                "repo": item.repo, "path": item.path, "line_start": item.line_start,
                "line_end": item.line_end,
                "content_sha256": hashlib.sha256(item.content.encode("utf-8")).hexdigest(),
            }
            for public_id, item in evidence_rows
        ],
        "created_at": datetime.now(UTC).isoformat(),
    }
    state_before = copy.deepcopy(state)
    try:
        _atomic_session_text_write(settings, ticket, artifact, content)
        _atomic_generated_text_write(settings, handoff, content)
        state["progressive_checkpoint"] = checkpoint
        state["status"] = "retrieving"
        lineage = list(state.get("context_lineage") or [])
        lineage = [item for item in lineage if item.get("context_id") != checkpoint_id]
        lineage.append({
            "context_id": checkpoint_id,
            "base_context_id": str(state.get("last_context_id") or "") or None,
            "number": number,
            "kind": "first_useful_checkpoint", "content_hash": checkpoint["content_hash"],
            "protocol_version": 5, "generation": generation.generation,
        })
        state["context_lineage"] = lineage[-100:]
        mark_active_artifacts(state, artifact)
        save_session(settings, ticket, state)
    except Exception:
        artifact.unlink(missing_ok=True)
        handoff.unlink(missing_ok=True)
        state.clear()
        state.update(state_before)
        raise
    if progress is not None:
        progress({
            "phase": "first_useful_checkpoint", "wave": checkpoint["wave"],
            "context_id": checkpoint_id, "checkpoint_artifact": artifact.name,
            "evidence_count": len(evidence_rows),
        })
    return checkpoint


def _bounded_markdown_details(text: str, max_bytes: int) -> tuple[str, set[str]]:
    """Apply the final byte ceiling and report whole evidence regions that were omitted."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, set()
    omitted_ids: list[str] = []
    heading = re.compile(r"(?m)^### (?:\d+\. )?(E(?:-|[0-9])[A-Za-z0-9-]*)\s+—")
    while len(text.encode("utf-8")) > max_bytes and (matches := list(heading.finditer(text))):
        match = matches[-1]
        following = re.search(r"(?m)^#{2,3}\s", text[match.end():])
        end = match.end() + following.start() if following else len(text)
        omitted_ids.append(match.group(1))
        text = text[:match.start()].rstrip() + "\n\n" + text[end:].lstrip()
    if omitted_ids:
        text += (
            "\n\n## Omitted evidence IDs\n\n"
            + "\n".join(f"- `{identifier}` — omitted as a whole bounded region; no partial evidence was emitted."
                        for identifier in reversed(omitted_ids))
            + "\n"
        )
        if len(text.encode("utf-8")) <= max_bytes:
            return text, set(omitted_ids)
    suffix = (
        "\n\n## Bounded omission manifest\n\n"
        "- The remaining lower-priority tail was omitted to satisfy the protocol UTF-8 byte limit.\n"
        "- Exact pinned source remains authoritative; request a narrower checkpoint for omitted evidence.\n"
    )
    reserve = len(suffix.encode("utf-8")) + 128
    budget = max(0, max_bytes - reserve)
    prefix = text.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
    prefix = prefix.rsplit("\n", 1)[0]

    def close_fence(value: str) -> str:
        open_fence: str | None = None
        for line in value.splitlines():
            match = re.match(r"^(`{3,}|~{3,})(.*)$", line)
            if not match:
                continue
            marker = match.group(1)
            if open_fence is None:
                open_fence = marker
            elif marker[0] == open_fence[0] and len(marker) >= len(open_fence) and not match.group(2).strip():
                open_fence = None
        return f"\n{open_fence}" if open_fence else ""

    while True:
        result = prefix.rstrip() + close_fence(prefix) + suffix
        if len(result.encode("utf-8")) <= max_bytes:
            return (result if result.endswith("\n") else result + "\n"), set(omitted_ids)
        if "\n" not in prefix:
            prefix = ""
        else:
            prefix = prefix.rsplit("\n", 1)[0]


def _bounded_markdown(text: str, max_bytes: int) -> str:
    """Apply the final protocol byte ceiling without emitting invalid UTF-8 or an open fence."""
    return _bounded_markdown_details(text, max_bytes)[0]


def _bounded_protocol_context(text: str, max_bytes: int, evidence_ids: Iterable[str]) -> str:
    """Keep protocol delivery metadata consistent with the evidence that survived bounding."""
    known = {str(identifier) for identifier in evidence_ids if identifier}
    omitted: set[str] = set()
    bounded = text
    for _ in range(len(known) + 2):
        embedded = sorted(known - omitted)
        adjusted = re.sub(
            r"(?m)^- Embedded evidence IDs: `[^`]*`$",
            f"- Embedded evidence IDs: `{', '.join(embedded) or 'none'}`",
            text,
        )
        adjusted = re.sub(
            r"(?m)^- Omitted evidence IDs due to byte limit: `[^`]*`$",
            f"- Omitted evidence IDs due to byte limit: `{', '.join(sorted(omitted)) or 'none'}`",
            adjusted,
        )
        if omitted:
            adjusted = adjusted.replace(
                "- Replacement status: `complete_replacement`",
                "- Replacement status: `incomplete_non_replacing`",
            )
            lines = []
            for line in adjusted.splitlines():
                if any(line.startswith(f"- `{identifier}` ") for identifier in omitted):
                    line = re.sub(r"`included`$", "`omitted_by_byte_limit`", line)
                lines.append(line)
            adjusted = "\n".join(lines) + ("\n" if adjusted.endswith("\n") else "")
        bounded, observed = _bounded_markdown_details(adjusted, max_bytes)
        expanded = omitted | (observed & known)
        if expanded == omitted:
            return bounded
        omitted = expanded
    return bounded


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
        pinned = bundle.atlas_generation is not None
        # Live HEAD is not evidence for a pinned investigation. Avoid an
        # all-repository subprocess sweep every time a wave is packaged.
        local = None if pinned else git_head(repo, timeout=1.0)
        source = bundle.atlas_generation.snapshots.get(repo.name) if pinned else repo.source_sha or local
        output.append(
            f"- `{repo.name}` — analyzed `{(source or 'not a Git repository')[:12]}` "
            f"from `{repo.source_ref or 'working tree'}` ({repo.source_status}); "
            f"local HEAD `{('not probed (pinned)' if pinned else (local or 'n/a')[:12])}`"
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
                f"- Static or contract relationships: {'FOUND (NAVIGATION ONLY)' if coverage.get('relationships') else 'NOT YET FOUND'}",
                f"- Git change history: {'FOUND (PINNED NAVIGATION)' if coverage.get('git_history') else 'NOT REQUESTED / NOT FOUND'}",
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
            memory_block = _source_markdown_block(
                json.dumps(investigation_memory, ensure_ascii=False, indent=2, sort_keys=True), "json"
            )
            output.extend([
                "", "## Investigation Memory", "",
                *memory_block,
            ])
        if progress.get("next_best_evidence"):
            next_block = _source_markdown_block(
                json.dumps(progress["next_best_evidence"], indent=2, sort_keys=True), "json"
            )
            output.extend([
                "", "## Next-Best-Evidence", "",
                *next_block,
            ])
        if progress.get("protocol_version") == 5 and progress.get("investigation_runtime"):
            from .investigation import render_protocol_v5

            output.extend(["", render_protocol_v5(progress["investigation_runtime"]), ""])
        output.extend([
            "", "## Evidence lineage", "",
            f"- New stable evidence IDs: `{', '.join(progress.get('new_evidence_ids') or []) or 'none'}`",
            f"- Invalidated/superseded evidence IDs: `{', '.join(progress.get('superseded_evidence_ids') or []) or 'none'}`",
            f"- Embedded evidence IDs: `{', '.join(_public_evidence_id(progress, item) for item in bundle.evidence) or 'none'}`",
            "- Omitted evidence IDs due to byte limit: `none`",
        ])
        if progress.get("checkpoint"):
            replacement = str(progress.get("checkpoint_replacement") or "complete_replacement")
            output.extend([
                "", "## Checkpoint replacement contract", "",
                f"- Replacement status: `{replacement}`",
            ])
            if replacement == "incomplete_non_replacing":
                output.append(
                    "- Do not replace accumulated client evidence with this checkpoint: some retained source regions "
                    "were not embedded within the bounded context. Preserve prior IDs and request a bounded recovery "
                    "for any omitted region needed for the decision."
                )
            manifest = progress.get("retained_evidence_manifest") or []
            output.extend(["", "### Retained evidence manifest", ""])
            output.extend(
                f"- `{item.get('evidence_id')}` `{item.get('repo')}:{item.get('path')}:{item.get('line_start')}-{item.get('line_end')}` — `{item.get('status')}`"
                for item in manifest
            )
            if not manifest:
                output.append("- None")
        memory_changes = progress.get("memory_changes") or {}
        if memory_changes:
            output.extend(["", "## Investigation Memory changes", ""])
            output.extend(
                f"- `{key}`: {json.dumps(value, ensure_ascii=False, sort_keys=True)[:2_000]}"
                for key, value in sorted(memory_changes.items())
            )
    if bundle.relationships:
        output.extend([
            "", "## Static execution relationships", "",
            *_source_markdown_block("\n".join(sorted(set(bundle.relationships))), "text"),
        ])
    if bundle.experience:
        output.extend(["", bundle.experience.rstrip(), ""])
    output.extend(["", "## Source evidence", ""])
    if not bundle.evidence:
        output.append("No source evidence was retrieved.")
    for index, item in enumerate(bundle.evidence, 1):
        found = ", ".join(item.found_by)
        source_block = _source_markdown_block(item.content, _language(item.path))
        output.extend([
            f"### {index}. {_public_evidence_id(progress, item)} — {item.repo} — `{item.path}:{item.line_start}-{item.line_end}`",
            "", f"Kind: {item.kind}  ", f"Found by: {found}", "",
            *source_block, "",
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
    text_bytes = len(text.encode("utf-8"))
    if text_bytes > settings.soft_target_chars:
        text += (
            f"\n> Context size warning: {text_bytes:,} UTF-8 bytes exceeds the soft target of "
            f"{settings.soft_target_chars:,}. Lower-ranked source candidates were not hydrated.\n"
        )
    return _bounded_protocol_context(
        text,
        settings.hard_context_chars,
        (_public_evidence_id(progress, item) for item in bundle.evidence),
    )


def load_index_state(settings: Settings) -> dict[str, Any]:
    path = settings.state_dir / "indexes.json"
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(read_managed_text(
            settings.state_dir, path, max_bytes=16 * 1024 * 1024,
        ))
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


@workspace_exclusive
def snapshot_indexes(
    settings: Settings,
    changed_only: bool = False,
    *,
    publish: bool = True,
) -> tuple[dict[str, Any], list[str]]:
    """Build the real local search index; kept as the public name for compatibility."""
    from .index import build_index_generation, prepare_working_tree_snapshots, write_state
    from .ops import ensure_write_capacity

    ensure_write_capacity(settings)
    prepare_working_tree_snapshots(settings, suffixes=CODE_SUFFIXES, ignored_dirs=IGNORED_DIRS)
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
    zoekt_repaired = False
    try:
        from .backends.zoekt import (
            build as build_zoekt,
            immutable_snapshot_available,
            shard_path,
            valid_shard_manifest,
        )

        zoekt_targets = set(updated)
        for repo in settings.repositories:
            sha = repo.source_sha or "working-tree"
            if immutable_snapshot_available(repo) and not valid_shard_manifest(
                shard_path(settings.state_dir, repo.name, sha), sha,
            ):
                zoekt_targets.add(repo.name)
        zoekt = build_zoekt(settings, [settings.repo(name) for name in sorted(zoekt_targets)])
        zoekt_repaired = any(
            name not in updated and details.get("status") == "built"
            for name, details in zoekt.items()
        )
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
            or zoekt_repaired
        )
        # A previous process may have committed the lexical generation and
        # crashed before mirroring it into the Atlas catalog.  Any refresh that
        # is about to publish must therefore rebuild the sealed projection even
        # when the lexical builder correctly reports no new update on retry.
        if should_publish or updated or existing_generation is None or not existing_generation.identity:
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
        graph_status += " — deferred; run brain index for an explicit build"
    output.extend(["", f"Config: {settings.config_path}", f"Structural backend: {graph_status}"])
    return "\n".join(output) + "\n", ok


def _read_session_json(path: Path) -> dict[str, Any]:
    try:
        raw, exceeded = read_direct_file_bytes(path, max_bytes=MAX_SESSION_STATE_BYTES)
    except ValueError as error:
        if path.is_symlink():
            raise ValueError("session state must not be a symbolic link") from error
        raise
    if exceeded:
        raise ValueError("session state exceeds its byte limit")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("session state is not an object")
    return value


def _validated_runs_root(settings: Settings) -> Path:
    """Return the direct managed session root, never a substituted directory."""
    runs_root = settings.runs_dir
    try:
        configured_root = runs_root.absolute()
        resolved_root = runs_root.resolve()
    except OSError as error:
        raise BrainError("Session state path cannot be resolved safely") from error
    if (
        runs_root.is_symlink()
        or not runs_root.is_dir()
        or resolved_root != configured_root
    ):
        raise BrainError("Session state root must be a direct managed directory")
    return runs_root


def _validated_session_directory(settings: Settings, candidate: Path) -> Path:
    runs_root = _validated_runs_root(settings)
    try:
        resolved_root = runs_root.resolve()
        resolved_candidate = candidate.resolve(strict=False)
    except OSError as error:
        raise BrainError("Session state path cannot be resolved safely") from error
    if candidate.is_symlink() or resolved_candidate.parent != resolved_root:
        raise BrainError("Session state path escapes the managed runs directory")
    if candidate.exists() and not candidate.is_dir():
        raise BrainError("Session state path is not a directory")
    return candidate


def _validated_session_artifact(
    settings: Settings, ticket: str, path: Path,
) -> Path:
    directory = session_dir(settings, ticket)
    directory.mkdir(parents=True, exist_ok=True)
    directory = _validated_session_directory(settings, directory)
    try:
        relative = path.relative_to(directory)
    except ValueError as error:
        raise BrainError("Session artifact escapes the managed ticket directory") from error
    if not relative.parts:
        raise BrainError("Session artifact path is invalid")
    parent = directory
    for part in relative.parts[:-1]:
        parent = parent / part
        if parent.is_symlink():
            raise BrainError("Session artifact parent must not be a symbolic link")
        if parent.exists() and not parent.is_dir():
            raise BrainError("Session artifact parent is not a directory")
        parent.mkdir(exist_ok=True)
    if not parent.resolve().is_relative_to(directory.resolve()):
        raise BrainError("Session artifact parent escapes the managed ticket directory")
    if path.exists() and path.is_dir():
        raise BrainError("Session artifact path is a directory")
    return path


def _atomic_session_bytes_write(
    settings: Settings, ticket: str, path: Path, payload: bytes,
) -> None:
    artifact = _validated_session_artifact(settings, ticket, path)
    try:
        atomic_managed_bytes_write(session_dir(settings, ticket), artifact, payload)
    except ValueError as error:
        raise BrainError("Session artifact path changed during publication") from error


def _atomic_session_text_write(
    settings: Settings, ticket: str, path: Path, content: str,
) -> None:
    _atomic_session_bytes_write(settings, ticket, path, content.encode("utf-8"))


def _read_session_artifact(
    settings: Settings, ticket: str, path: Path, max_bytes: int,
) -> str:
    """Read one direct managed ticket artifact without following substitutions."""
    directory = session_dir(settings, ticket)
    try:
        raw = read_managed_bytes(directory, path, max_bytes=max_bytes)
    except (OSError, ValueError) as error:
        if "exceeds its byte limit" in str(error):
            raise BrainError("Managed session artifact exceeds its byte limit") from error
        raise BrainError("Invalid managed session artifact") from error
    return raw.decode("utf-8", errors="replace")


def _validated_generated_artifact(
    settings: Settings, path: Path, *, create_parents: bool = True,
) -> Path:
    root = settings.generated_dir
    if root.is_symlink() or root.resolve() != root:
        raise BrainError("Generated artifact root escapes its configured location")
    if create_parents:
        root.mkdir(parents=True, exist_ok=True)
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise BrainError("Generated artifact escapes managed Brain state") from error
    if not relative.parts:
        raise BrainError("Generated artifact path is invalid")
    parent = root
    for part in relative.parts[:-1]:
        parent = parent / part
        if parent.is_symlink():
            raise BrainError("Generated artifact parent must not be a symbolic link")
        if parent.exists() and not parent.is_dir():
            raise BrainError("Generated artifact parent is not a directory")
        if create_parents:
            parent.mkdir(exist_ok=True)
    if not parent.resolve().is_relative_to(root.resolve()):
        raise BrainError("Generated artifact parent escapes managed Brain state")
    if path.exists() and path.is_dir():
        raise BrainError("Generated artifact path is a directory")
    return path


def _atomic_generated_text_write(settings: Settings, path: Path, content: str) -> None:
    artifact = _validated_generated_artifact(settings, path)
    try:
        atomic_managed_bytes_write(settings.generated_dir, artifact, content.encode("utf-8"))
    except ValueError as error:
        raise BrainError("Generated artifact path changed during publication") from error


def handoff_dir(settings: Settings, ticket: str) -> Path:
    """Return the user-facing handoff directory for one managed ticket."""
    directory = settings.generated_dir / "handoffs" / session_dir(settings, ticket).name
    _validated_generated_artifact(settings, directory / ".handoff-root")
    return directory


def session_dir(settings: Settings, ticket: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", ticket).strip(".-")
    if not safe:
        raise BrainError("Ticket identifier is empty")
    reserved = {
        "con", "prn", "aux", "nul",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
    }
    portable_legacy = (
        len(safe.encode("utf-8")) <= 128
        and safe.rstrip(" .") == safe
        and safe.split(".", 1)[0].casefold() not in reserved
    )
    legacy = settings.runs_dir / safe
    if portable_legacy:
        legacy = _validated_session_directory(settings, legacy)
    digest = hashlib.sha256(ticket.encode("utf-8")).hexdigest()[:12]
    old_hashed = _validated_session_directory(
        settings, settings.runs_dir / f"{safe[:80]}--{digest}",
    )
    canonical = _validated_session_directory(
        settings, settings.runs_dir / filesystem_component(ticket),
    )
    identity_collision = False
    candidates = (legacy, old_hashed, canonical) if portable_legacy else (old_hashed, canonical)
    for candidate in dict.fromkeys(candidates):
        state_path = candidate / "session.json"
        if not state_path.is_file():
            continue
        if state_path.is_symlink():
            raise BrainError("Session state must not be a symbolic link")
        try:
            stored_ticket = str(_read_session_json(state_path).get("ticket"))
            if stored_ticket == ticket:
                return candidate
            if candidate == legacy and stored_ticket:
                identity_collision = True
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError, ValueError):
            pass
    # Keep the long-standing readable directory for ordinary ticket IDs.  On
    # a case-insensitive filesystem an existing case-colliding ticket is found
    # above but deliberately not reused, so the new identity falls through to
    # the collision-resistant canonical encoding.  Windows device names and
    # overlong components always use that portable encoding immediately.
    if identity_collision:
        return old_hashed
    if portable_legacy:
        return legacy
    return canonical


def session_state(settings: Settings, ticket: str) -> dict[str, Any]:
    path = session_dir(settings, ticket) / "session.json"
    if not path.is_file():
        return {"ticket": ticket, "requests": 0, "feedbacks": 0, "delivery": {}}
    try:
        state = _read_session_json(path)
        _validate_session_schema(state)
        return state
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise BrainError(f"Invalid session state: {path}: {exc}") from exc


def save_session(settings: Settings, ticket: str, state: dict[str, Any]) -> None:
    _validate_session_schema(state)
    directory = session_dir(settings, ticket)
    path = directory / "session.json"
    if path.exists():
        try:
            existing = _read_session_json(path)
            _validate_session_schema(existing)
        except BrainError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise BrainError(f"Invalid existing session state: {path}: {error}") from error
    payload = (json.dumps(state, indent=2) + "\n").encode("utf-8")
    if len(payload) > MAX_SESSION_STATE_BYTES:
        raise BrainError("Session state exceeds its byte limit")
    _atomic_session_bytes_write(settings, ticket, path, payload)


def _validate_session_schema(state: dict[str, Any]) -> int:
    """Reject forward session schemas before an older binary can rewrite them."""
    raw = state.get("session_schema_version")
    if raw is None:
        return 0
    if isinstance(raw, bool):
        raise BrainError("Session schema version is invalid")
    try:
        version = int(raw)
    except (TypeError, ValueError) as error:
        raise BrainError("Session schema version is invalid") from error
    if version < 0 or str(raw).strip() != str(version):
        raise BrainError("Session schema version is invalid")
    if version > CURRENT_SESSION_SCHEMA_VERSION:
        raise BrainError(
            "This ticket uses a newer session schema; upgrade Project Brain before modifying it"
        )
    return version


def mark_active_artifacts(state: dict[str, Any], *paths: Path | str) -> None:
    """Record managed artifacts that belong to the current run of a ticket."""
    if "active_artifacts" not in state:
        return
    active = [
        str(item)
        for item in state.get("active_artifacts") or []
        if isinstance(item, str) and item
    ]
    for path in paths:
        name = Path(path).name
        if name and name not in active:
            active.append(name)
    state["active_artifacts"] = active


def _session_snapshots(state: dict[str, Any]) -> dict[str, str]:
    return {
        str(name): str(value.get("sha") or "working-tree")
        for name, value in (state.get("sources") or {}).items()
        if isinstance(value, dict)
    }


def _validated_session_snapshot_paths(
    settings: Settings, state: dict[str, Any], generation: Any | None,
) -> dict[str, Path | None]:
    """Resolve session source pins without permitting traversal or symlink substitution."""
    sources = state.get("sources") or {}
    if not isinstance(sources, dict) or any(not isinstance(item, dict) for item in sources.values()):
        raise BrainError("Ticket session source pins are invalid; start a new ticket")
    snapshot_root = (settings.state_dir / "snapshots").resolve()
    resolved: dict[str, Path | None] = {}
    expected = generation.snapshots if generation is not None else _session_snapshots(state)
    lexical_without_exports = False
    if generation is not None and any(
        sha != "working-tree" and (sources.get(repo) or {}).get("snapshot") is None
        for repo, sha in expected.items()
    ):
        from .index import lexical_membership_identity

        lexical = generation.component("lexical")
        lexical_identity = lexical_membership_identity(settings, expected)
        lexical_without_exports = bool(
            lexical.get("status") == "ready"
            and lexical_identity is not None
            and lexical.get("content_hash") == lexical_identity[0]
            and int((lexical.get("details") or {}).get("files") or -1) == lexical_identity[1]
        )
    for repo, sha in expected.items():
        item = sources.get(repo)
        if not isinstance(item, dict):
            raise BrainError(f"Pinned source snapshot for {repo} is unavailable; refresh/start a new ticket instead of mixing commits")
        raw = item.get("snapshot")
        if raw is None:
            if sha != "working-tree" and not lexical_without_exports:
                raise BrainError(f"Pinned source snapshot for {repo} is unavailable; refresh/start a new ticket instead of mixing commits")
            resolved[repo] = None
            continue
        if not isinstance(raw, str) or not raw.strip():
            raise BrainError(f"Pinned source snapshot for {repo} is unavailable; refresh/start a new ticket instead of mixing commits")
        raw_path = Path(raw)
        candidate = raw_path.resolve()
        if (
            not raw_path.is_absolute()
            or raw_path != candidate
            or not candidate.is_relative_to(snapshot_root)
            or not candidate.is_dir()
            or candidate.is_symlink()
        ):
            raise BrainError(f"Pinned source snapshot for {repo} is unavailable; refresh/start a new ticket instead of mixing commits")
        if generation is not None:
            canonical = (
                snapshot_root / filesystem_component(repo) / filesystem_component(sha)
            ).resolve()
            legacy_repo = re.sub(r"[^A-Za-z0-9._-]+", "-", repo).strip(".-") or "repo"
            legacy = (snapshot_root / legacy_repo / sha).resolve()
            legacy_valid = False
            if candidate == legacy and legacy != canonical:
                from .sync import _sealed_snapshot_is_intact, _snapshot_seal_path

                legacy_valid = _sealed_snapshot_is_intact(
                    legacy, _snapshot_seal_path(legacy.parent, sha), sha,
                )
            if (
                (candidate != canonical and not legacy_valid)
                or not candidate.is_relative_to(snapshot_root)
            ):
                raise BrainError(f"Pinned source snapshot for {repo} does not match its Atlas generation")
        resolved[repo] = candidate
    return resolved


def _resolve_session_generation(settings: Settings, state: dict[str, Any]) -> tuple[Any | None, bool]:
    """Resolve a ticket once; ambiguous legacy state stays source-only instead of using current."""
    from .catalog import matching_generations, resolve_generation

    _validate_session_schema(state)
    snapshots = _session_snapshots(state)
    generation = None
    identity = str(state.get("atlas_generation_id") or "")
    raw_number = state.get("generation")
    explicit_number = raw_number is not None
    mode = state.get("generation_mode")

    def unavailable() -> BrainError:
        return BrainError(
            "This ticket's pinned Atlas generation is unavailable; restore the retained generation "
            "or start a new ticket instead of mixing repository generations"
        )

    number: int | None = None
    if explicit_number:
        if isinstance(raw_number, bool):
            raise unavailable()
        try:
            number = int(raw_number)
        except (TypeError, ValueError) as error:
            raise unavailable() from error
        if number < 1:
            raise unavailable()
    if (identity or explicit_number) and mode not in {None, "atlas"}:
        raise unavailable()
    if not identity and not explicit_number and mode not in {None, "legacy_source_pin"}:
        raise unavailable()
    if identity:
        generation = resolve_generation(settings, identity=identity)
        if generation is not None and number is not None and generation.generation != number:
            raise unavailable()
    elif number is not None:
        generation = resolve_generation(settings, generation=number)
    if (identity or explicit_number) and (
        generation is None or generation.snapshots != snapshots
    ):
        raise unavailable()
    if generation is None:
        matches = matching_generations(settings, snapshots)
        generation = matches[0] if len(matches) == 1 else None
    _validated_session_snapshot_paths(settings, state, generation)
    before = json.dumps(state, sort_keys=True)
    state["session_schema_version"] = CURRENT_SESSION_SCHEMA_VERSION
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

    if len(ticket_text.encode("utf-8")) > MAX_START_TICKET_BYTES:
        raise BrainError("Ticket text exceeds the start-package byte limit")
    directory = session_dir(settings, ticket)
    directory_existed = directory.is_dir()
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
        text, omitted = _bounded_text_file(ticket_knowledge, MAX_START_KNOWLEDGE_ITEM_BYTES)
        sections.extend(["## Human-maintained knowledge for this ticket", "", text.strip()])
        if omitted:
            sections.append("[Project Brain omitted unsafe or excess bytes from this knowledge section.]")
        sections.append("")
    for title, path in (
        ("Human project map", settings.knowledge_dir / "PROJECT_MAP.md"),
        ("Generated project facts", settings.generated_dir / "PROJECT_FACTS.md"),
        ("Generated cross-repository relationships", settings.generated_dir / "PROJECT_RELATIONSHIPS.md"),
        ("Glossary", settings.knowledge_dir / "glossary.md"),
    ):
        if path.is_file():
            text, omitted = _bounded_text_file(path, MAX_START_KNOWLEDGE_ITEM_BYTES)
            sections.extend([f"## {title}", "", text.strip()])
            if omitted:
                sections.append("[Project Brain omitted unsafe or excess bytes from this knowledge section.]")
            sections.append("")
    content, _ = _bounded_utf8_text(
        "\n".join(sections).rstrip() + "\n",
        MAX_START_ARTIFACT_BYTES,
        "\n\n[Project Brain omitted remaining start-package sections at the byte limit.]\n",
    )
    start_path = directory / "start.md"
    previous_state = session_state(settings, ticket)
    external_evidence_baseline = int(previous_state.get("external_evidence") or 0)
    state = {
            "ticket": ticket,
            "started_at": datetime.now(UTC).isoformat(),
            "status": "waiting_for_ai",
            "session_schema_version": CURRENT_SESSION_SCHEMA_VERSION,
            "generation_mode": "atlas" if pinned_atlas is not None else "legacy_source_pin",
            "atlas_generation_id": pinned_atlas.identity if pinned_atlas is not None else None,
            "generation": pinned_atlas.generation if pinned_atlas is not None else None,
            "source_signature": pinned_atlas.source_signature if pinned_atlas is not None else source_signature(snapshots),
            "requests": 0,
            "feedbacks": 0,
            "external_evidence": external_evidence_baseline,
            "external_evidence_baseline": external_evidence_baseline,
            "sources": sources,
            "investigation_memory": initial_investigation_memory(ticket_text.strip()),
            "coverage_map": initial_coverage_map(),
            "similar_investigations": similar_investigations(settings, ticket_text, limit=settings.experience_similar_cases),
            "context_lineage": [],
            "stable_identities": {},
            "active_artifacts": [ticket_path.name, start_path.name],
        }
    previous_artifacts: dict[Path, bytes | None] = {}
    for artifact in (ticket_path, start_path):
        prior: bytes | None = None
        if artifact.is_file() and not artifact.is_symlink():
            try:
                prior = read_managed_bytes(
                    directory, artifact, max_bytes=MAX_START_ARTIFACT_BYTES,
                )
            except ValueError as error:
                if "exceeds its byte limit" not in str(error):
                    raise BrainError("Existing start-session artifact is unsafe") from error
                raise BrainError("Existing start-session artifact exceeds its safe restart limit")
        previous_artifacts[artifact] = prior
    try:
        _atomic_session_text_write(settings, ticket, ticket_path, ticket_text.rstrip() + "\n")
        _atomic_session_text_write(settings, ticket, start_path, content)
        save_session(settings, ticket, state)
    except Exception:
        for artifact, prior in previous_artifacts.items():
            if prior is None:
                artifact.unlink(missing_ok=True)
            else:
                _atomic_session_bytes_write(settings, ticket, artifact, prior)
        if not directory_existed:
            try:
                directory.rmdir()
            except OSError:
                pass
        raise
    if (
        settings.ticket_prefetch_enabled
        and "prefetch" not in settings.evaluation_ablations
        and pinned_atlas is not None
    ):
        try:
            prefetch_ticket(settings, ticket)
        except (OSError, sqlite3.Error, BrainError):
            state = session_state(settings, ticket)
            from .investigation import PREFETCH_SCHEMA_VERSION

            state["prefetch"] = {
                "status": "failed", "generation": pinned_atlas.generation,
                "atlas_generation_id": pinned_atlas.identity,
                "schema_version": PREFETCH_SCHEMA_VERSION,
                "compatibility_identity": "sha256:" + hashlib.sha256(
                    f"{PREFETCH_SCHEMA_VERSION}\0{pinned_atlas.identity}\0failed".encode("utf-8")
                ).hexdigest(),
            }
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
        result = {"status": "unavailable", "generation": None, "candidate_ids": [],
                  "schema_version": "ticket-prefetch-v1", "compatibility_identity": None}
    else:
        objective = str((state.get("investigation_memory") or {}).get("objective") or ticket)
        from .retrieval.planner import objective_terms

        routed = route_atlas(settings, objective, {
            "version": 4, "objective": objective,
            "searches": [{"query": value, "repos": []} for value in objective_terms(objective, limit=8)],
            "paths": [], "symbols": [], "history": [], "required": [], "resolve": [],
            "_evaluation_ablation": sorted(settings.evaluation_ablations),
        }, generation,
                             repo_limit=settings.widen_repo_limit, entity_limit=settings.pre_rerank_candidate_limit)
        from .investigation import (
            PREFETCH_SCHEMA_VERSION,
            _prefetch_compatibility_identity,
            resolve_runtime_anchors,
        )

        resolved = resolve_runtime_anchors(
            settings, generation, objective_terms(objective, limit=8),
            use_cache="generation_cache" not in settings.evaluation_ablations,
        )
        result = {
            "status": "ready", "generation": generation.generation, "atlas_generation_id": generation.identity,
            "objective": objective,
            "cache_hit": bool(routed.get("cache_hit")), "repos": list(routed.get("repos") or []),
            "modules": list(routed.get("modules") or []),
            "candidate_ids": [str(item.get("entity_id")) for item in routed.get("entities") or []],
            "anchor_ids": [str(item.get("identity")) for item in resolved.get("candidates") or []],
            "anchor_status": resolved.get("status"),
            "schema_version": PREFETCH_SCHEMA_VERSION,
            "created_at": datetime.now(UTC).isoformat(),
        }
        result["compatibility_identity"] = _prefetch_compatibility_identity(generation, result)
    state["prefetch"] = result
    save_session(settings, ticket, state)
    return result


def _required_coverage_key(value: str) -> str | None:
    normalized = re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
    mappings = (
        (("test",), "tests"), (("cross repo", "integration"), "cross_repo_integration"),
        (("main flow", "execution flow", "production flow"), "main_execution_flow"),
        (("entry point", "production entry"), "production_entry_point"),
        (("config",), "configuration"), (("data", "schema", "persistence"), "data_schema"),
        (("history",), "history"), (("impact",), "impact_surface"), (("contract",), "contract_surface"),
    )
    return next((key for terms, key in mappings if any(term in normalized for term in terms)), None)


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
    if settings.evaluation_ablations:
        request["_evaluation_ablation"] = sorted(settings.evaluation_ablations)
    state = session_state(settings, ticket)
    atlas_generation, migrated = _resolve_session_generation(settings, state)
    plan["signature"] = protocol_request_signature(plan, ticket, state)
    if migrated:
        save_session(settings, ticket, state)
    pre_wave_state = json.loads(json.dumps(state))
    from .atlas import initial_coverage_map, initial_investigation_memory

    memory = dict(state.get("investigation_memory") or initial_investigation_memory(plan["objective"]))
    coverage_map = dict(state.get("coverage_map") or initial_coverage_map())
    coverage_map.pop("explicit_requested", None)
    memory_before = json.loads(json.dumps(memory))
    coverage_before = dict(coverage_map)
    if request.get("version") in {4, 5}:
        memory["objective"] = str(request["objective"])
        memory["hypotheses"] = list(dict.fromkeys([*(memory.get("hypotheses") or []), *request.get("hypotheses", [])]))[-100:]
        memory["runtime_facts"] = list(dict.fromkeys([*(memory.get("runtime_facts") or []), *request.get("runtime_facts", [])]))[-100:]
        required_coverage = {
            str(key): str(value) for key, value in (state.get("required_coverage") or {}).items()
        }
        for required in request.get("required") or []:
            required = str(required)
            key = _required_coverage_key(required)
            if key is not None:
                required_coverage[required] = key
                if coverage_map.get(key) != "verified":
                    coverage_map[key] = "candidate"
            if key is None or coverage_map.get(key) != "verified":
                memory["blocking_unknowns"] = list(dict.fromkeys([
                    *(memory.get("blocking_unknowns") or []), required,
                ]))[-100:]
        state["required_coverage"] = required_coverage
    if request.get("version") == 5:
        from .investigation import validate_stable_identity_registry

        try:
            validate_stable_identity_registry(state)
        except ValueError as error:
            raise BrainError("Protocol v5 stable identity registry is corrupt; start a new ticket instead of reusing lineage") from error
        if atlas_generation is None:
            raise BrainError("Protocol v5 requires an available pinned Atlas generation")
        prior_physical_operations = sum(
            int((item.get("retrieval") or {}).get("physical_backend_operations") or 0)
            for item in state.get("request_history") or [] if isinstance(item, dict)
        )
        global_physical_limit = settings.max_backend_operations * 4
        if prior_physical_operations >= global_physical_limit:
            raise BrainError("Protocol v5 investigation reached its global physical-operation budget")
        previous_runtime = state.get("investigation_runtime") or {}
        completed_wave = int(previous_runtime.get("wave") or 0)
        if completed_wave >= 4:
            raise BrainError("Protocol v5 investigation reached the hard four-wave limit")
        prior_stop_reason = str(previous_runtime.get("stop_reason") or "")
        if prior_stop_reason in {"coverage_satisfied", "no_progress"}:
            raise BrainError(
                f"Protocol v5 investigation is terminal ({prior_stop_reason}); do not run another retrieval wave"
            )
        expected_wave = completed_wave + 1
        if request.get("wave") is not None and int(request["wave"]) != expected_wave:
            raise BrainError(f"Protocol v5 wave must be {expected_wave} for this ticket")
        previous_frontier = (previous_runtime.get("evidence_frontier") or {}).get("items") or []
        prior_hypotheses = (previous_runtime.get("hypothesis_ledger") or {}).get("items") or []
        wave_four_justified = any(
            isinstance(item, dict) and item.get("status") == "contradicted" for item in prior_hypotheses
        ) or any(
            isinstance(item, dict) and item.get("status") == "unresolved" and item.get("priority") == "high"
            for item in previous_frontier
        )
        if expected_wave == 4 and not wave_four_justified:
            raise BrainError("Protocol v5 wave 4 requires a contradiction or high-value unresolved blocker")
        if progress_callback is not None:
            progress_callback({"phase": "wave_started", "wave": expected_wave, "generation": atlas_generation.generation})
        remaining_physical_operations = global_physical_limit - prior_physical_operations
    else:
        remaining_physical_operations = settings.max_backend_operations
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
        max_backend_operations=min(settings.max_backend_operations, remaining_physical_operations),
    )
    pinned_paths = _validated_session_snapshot_paths(settings, state, atlas_generation)
    for repo in retrieval_settings.repositories:
        source = (state.get("sources") or {}).get(repo.name) or {}
        repo.source_path = pinned_paths.get(repo.name)
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
    full_checkpoint = request.get("version") not in {4, 5} or bool(request.get("checkpoint"))
    if request.get("version") in {4, 5}:
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
    failed_checkpoint = state.get("progressive_checkpoint")
    if (
        request.get("version") == 5
        and isinstance(failed_checkpoint, dict)
        and failed_checkpoint.get("continuation_status") in {"pending", "failed"}
        and failed_checkpoint.get("request_signature") != plan["signature"]
    ):
        raise BrainError(
            "The prior first-useful checkpoint has a pending or failed continuation; retry that same request before changing the plan"
        )
    progressive_checkpoint: dict[str, Any] | None = None
    durable_checkpoint_state: dict[str, Any] | None = None
    continuation_path: Path | None = None
    continuation_handoff: Path | None = None
    continuation_event: dict[str, Any] | None = None
    completion_event: dict[str, Any] | None = None
    try:
        bundle = retrieve_context(retrieval_settings, request, include_diff=include_diff, progress=progress_callback)
        if request.get("version") == 5 and int(bundle.trace.get("physical_backend_operations") or 0) > remaining_physical_operations:
            raise BrainError("Protocol v5 wave exceeded the remaining global physical-operation budget")
        from .query import merge_evidence

        bundle.evidence = merge_evidence(bundle.evidence + _external_evidence(settings, ticket))
        checkpoint_restore_missed = 0
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
                checkpoint_restore_missed = missed
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
                "generation": atlas_generation.generation if atlas_generation is not None else None,
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
                and current["line_start"] == old.get("line_start")
                and current["line_end"] == old.get("line_end")
                and current["content_hash"] != old.get("content_hash")
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
        context_hash = "sha256:" + hashlib.sha256(
            json.dumps({"generation": generation_identity, "request": plan["signature"], "evidence": sorted(lineage_records)},
                       sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if request.get("version") == 5:
            registry = state.setdefault("stable_identities", {}).setdefault("contexts", {})
            from .investigation import _allocate

            context_id = _allocate({"contexts": registry}, "contexts", context_hash, "CTX-", 3)
        else:
            context_id = "ctx-" + context_hash.removeprefix("sha256:")
        from .atlas import next_best_evidence, update_investigation

        validated_prior_evidence_ids: set[str] | None = None
        if request.get("version") == 5:
            from .investigation import _validated_prior_evidence_ids

            evidence_identity_registry = state.setdefault("stable_identities", {}).setdefault("evidence", {})
            validated_prior_evidence_ids = _validated_prior_evidence_ids(
                settings,
                atlas_generation,
                state,
                bundle,
                {"evidence": evidence_identity_registry},
            )
            valid_records = [
                record for record in state.get("evidence_records") or []
                if isinstance(record, dict) and str(record.get("public_id") or "") in validated_prior_evidence_ids
            ]
            memory["verified_facts"] = [
                {
                    "evidence_id": str(record["public_id"]),
                    "reference": f"{record.get('repo')}:{record.get('path')}:{record.get('line_start')}-{record.get('line_end')}",
                    "kind": "retained exact source",
                    "verified_by": ["pinned evidence revalidation"],
                }
                for record in valid_records
            ]
            memory["verified_references"] = [str(item["reference"]) for item in memory["verified_facts"]]
            memory["implementation_surface"] = sorted({
                f"{record.get('repo')}:{record.get('path')}" for record in valid_records
                if not is_test_path(str(record.get("path") or ""))
            })
            memory["test_surface"] = sorted({
                f"{record.get('repo')}:{record.get('path')}" for record in valid_records
                if is_test_path(str(record.get("path") or ""))
            })
        update_investigation(memory, coverage_map, bundle, context_id)
        next_evidence = next_best_evidence(coverage_map, request, no_progress_rounds)
        bundle.trace["next_best_evidence"] = next_evidence
        bundle.trace["investigation_state"] = {
            "verified_facts": len(memory.get("verified_facts") or []),
            "blocking_unknowns": len(memory.get("blocking_unknowns") or []),
            "verified_references": len(memory.get("verified_references") or []),
            "coverage": dict(coverage_map),
        }
        runtime: dict[str, Any] | None = None
        if request.get("version") == 5:
            from .investigation import build_ticket_runtime, stable_evidence_id

            state["no_progress_rounds"] = no_progress_rounds
            state["coverage_map"] = dict(coverage_map)
            progressive_checkpoint = _publish_first_useful_checkpoint(
                settings, ticket, number, context_id, requested_base, bundle, request, plan["signature"],
                state, directory,
                progress_callback,
            )
            if progressive_checkpoint is not None:
                durable_checkpoint_state = json.loads(json.dumps(state))
            runtime_started = time.perf_counter()
            runtime = build_ticket_runtime(
                settings, atlas_generation, request, bundle, state, context_id=context_id,
                next_best_evidence=next_evidence,
                validated_prior_evidence_ids=validated_prior_evidence_ids,
            )
            from .editions import current_edition as runtime_edition

            semantic_serving = str((runtime.get("serving_state") or {}).get("semantic") or "unavailable")
            if (
                runtime_edition(retrieval_settings) in {"semantic", "precision"}
                and semantic_serving != "ready"
                and not any("semantic" in warning.casefold() for warning in bundle.warnings)
            ):
                bundle.warnings.append(
                    "Semantic component for the pinned generation is unavailable; exact Core evidence was used."
                )
            runtime_ms = round((time.perf_counter() - runtime_started) * 1000, 3)
            next_evidence = dict(runtime.get("next_best_evidence") or next_evidence)
            bundle.trace["next_best_evidence"] = next_evidence
            runtime["runtime_ms"] = runtime_ms
            bundle.metrics["investigation_runtime_ms"] = runtime_ms
            bundle.metrics["investigation_db_operations"] = int(
                (runtime.get("bounds") or {}).get("database_operations") or 0
            )
            bundle.trace["investigation_runtime_ms"] = runtime_ms
            bundle.trace["investigation_db_operations"] = bundle.metrics["investigation_db_operations"]
            if progressive_checkpoint is not None:
                runtime["first_useful_checkpoint"] = {
                    key: value for key, value in progressive_checkpoint.items()
                    if key != "internal_evidence_ids"
                }
                progressive = runtime.get("progressive_checkpoint")
                if isinstance(progressive, dict):
                    progressive["first_useful"] = runtime["first_useful_checkpoint"]
            coverage_map.update({
                str(key): str(value) for key, value in (runtime.get("coverage") or {}).items()
            })
            resolved_requirements = {
                requirement for requirement, key in (state.get("required_coverage") or {}).items()
                if coverage_map.get(str(key)) == "verified"
            }
            memory["blocking_unknowns"] = [
                value for value in memory.get("blocking_unknowns") or []
                if str(value) not in resolved_requirements
            ]
            bundle.trace["investigation_state"]["coverage"] = dict(coverage_map)
            state["investigation_runtime"] = runtime
            evidence_public_ids = {
                _evidence_id(item): stable_evidence_id(state, item) for item in bundle.evidence
            }
            if progress_callback is not None:
                progress_callback({
                    "phase": "anchors_resolved", "wave": runtime["wave"],
                    "candidate_count": len((runtime.get("anchors") or {}).get("candidates") or []),
                })
                progress_callback({
                    "phase": "flow_built", "wave": runtime["wave"],
                    "execution_steps": len((runtime.get("execution_flow") or {}).get("steps") or []),
                    "integration_steps": len((runtime.get("integration_flow") or {}).get("steps") or []),
                })
                progress_callback({"phase": "evidence_verified", "wave": runtime["wave"], "evidence_count": len(bundle.evidence)})
        else:
            evidence_public_ids = {}
        if request.get("version") == 5:
            evidence_registry = state.setdefault("stable_identities", {}).setdefault("evidence", {})

            def retained_public_id(identifier: str, record: dict[str, Any]) -> str:
                public = str(record.get("public_id") or "")
                if re.fullmatch(r"E[0-9]{4,}", public):
                    return public
                identity = "retained:" + identifier
                return _allocate({"evidence": evidence_registry}, "evidence", identity, "E", 4)

            for identifier, record in previous_records.items():
                evidence_public_ids.setdefault(identifier, retained_public_id(identifier, record))
                record["public_id"] = evidence_public_ids[identifier]
            for identifier, record in evidence_records.items():
                record["public_id"] = evidence_public_ids[identifier]
            for identifier, record in lineage_records.items():
                record["public_id"] = evidence_public_ids.get(identifier) or retained_public_id(identifier, record)
            rendered_new_evidence_ids = sorted(evidence_public_ids[item] for item in new_evidence_ids)
            rendered_superseded_evidence_ids = sorted(
                evidence_public_ids[item] for item in superseded if item in evidence_public_ids
            )
            for fact in memory.get("verified_facts") or []:
                if isinstance(fact, dict) and str(fact.get("evidence_id") or "") in evidence_public_ids:
                    fact["evidence_id"] = evidence_public_ids[str(fact["evidence_id"])]
        else:
            rendered_new_evidence_ids = sorted(new_evidence_ids)
            rendered_superseded_evidence_ids = superseded
        retained_evidence_manifest = [
            {
                "evidence_id": str(record.get("public_id") or identifier),
                "repo": record.get("repo"), "path": record.get("path"),
                "line_start": record.get("line_start"), "line_end": record.get("line_end"),
                "status": "included" if identifier in evidence_records else "retained_not_embedded",
            }
            for identifier, record in sorted(lineage_records.items())
        ]
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
            "memory_changes": memory_changes, "new_evidence_ids": rendered_new_evidence_ids,
            "superseded_evidence_ids": rendered_superseded_evidence_ids, "next_best_evidence": next_evidence,
            "investigation_memory": memory,
            "protocol_version": request.get("version"), "context_hash": context_hash,
            "investigation_runtime": runtime, "evidence_public_ids": evidence_public_ids,
            "progressive_checkpoint_id": (
                progressive_checkpoint.get("checkpoint_id") if progressive_checkpoint else None
            ),
            "retained_evidence_manifest": retained_evidence_manifest,
            "checkpoint_replacement": (
                "incomplete_non_replacing" if full_checkpoint and checkpoint_restore_missed
                else "complete_replacement" if full_checkpoint else "delta"
            ),
            "checkpoint_restore_missed": checkpoint_restore_missed,
        })
        if progress_callback is not None:
            progress_callback({"phase": "packing_context", "elapsed_ms": bundle.metrics.get("total_ms", 0), "evidence_count": len(bundle.evidence)})
        pack_started = time.perf_counter()
        content = (
            pack_context(retrieval_settings, ticket, number, bundle, investigation_progress)
            if full_checkpoint
            else pack_delta_context(retrieval_settings, ticket, number, bundle, investigation_progress, new_evidence_ids)
        )
        if progressive_checkpoint is not None:
            progressive_delta = dict(investigation_progress)
            progressive_delta["base_context_id"] = progressive_checkpoint["checkpoint_id"]
            checkpoint_evidence = set(progressive_checkpoint.get("internal_evidence_ids") or [])
            continuation = pack_delta_context(
                retrieval_settings, ticket, number, bundle, progressive_delta,
                set(evidence_records) - checkpoint_evidence,
            )
            continuation_path = directory / f"checkpoint-delta-{number:03d}.md"
            _atomic_session_text_write(settings, ticket, continuation_path, continuation)
            continuation_handoff = handoff_dir(settings, ticket) / f"checkpoint-delta-{number:03d}.md"
            _atomic_generated_text_write(settings, continuation_handoff, continuation)
            progressive_checkpoint.update({
                "continuation_status": "published",
                "continuation_artifact": continuation_path.name,
                "continuation_handoff_artifact": str(continuation_handoff),
                "continuation_content_hash": "sha256:" + hashlib.sha256(
                    continuation.encode("utf-8")
                ).hexdigest(),
            })
            progressive_checkpoint.pop("continuation_failure", None)
            state["progressive_checkpoint"] = progressive_checkpoint
            if progress_callback is not None:
                continuation_event = {
                    "phase": "continuation_published",
                    "wave": runtime.get("wave") if runtime else int(request.get("wave") or number),
                    "context_id": progressive_checkpoint["checkpoint_id"],
                    "checkpoint_artifact": progressive_checkpoint["artifact"],
                    "continuation_artifact": progressive_checkpoint["continuation_artifact"],
                    "continuation_handoff_artifact": progressive_checkpoint["continuation_handoff_artifact"],
                }
            if runtime is not None and isinstance(runtime.get("first_useful_checkpoint"), dict):
                runtime["first_useful_checkpoint"].update({
                    "continuation_status": progressive_checkpoint["continuation_status"],
                    "continuation_artifact": progressive_checkpoint["continuation_artifact"],
                    "continuation_content_hash": progressive_checkpoint["continuation_content_hash"],
                })
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
        _atomic_session_text_write(settings, ticket, request_path, request_text.rstrip() + "\n")
        _atomic_session_text_write(settings, ticket, path, content)
        state["requests"] = number
        state["status"] = "waiting_for_ai"
        state["no_progress_rounds"] = no_progress_rounds
        retained_evidence_keys = [*sorted(evidence_keys), *sorted(known_keys - evidence_keys)][:1_000]
        state["evidence_keys"] = retained_evidence_keys
        state["coverage"] = coverage
        state["investigation_memory"] = memory
        state["coverage_map"] = coverage_map
        state["last_context_id"] = context_id
        context_lineage = [
            item for item in (state.get("context_lineage") or [])
            if item.get("context_id") != context_id
        ]
        state["context_lineage"] = [*context_lineage, {
            "context_id": context_id, "base_context_id": None if full_checkpoint else requested_base, "number": number,
            "kind": "checkpoint" if full_checkpoint else "delta", "content_hash": context_hash,
            "protocol_version": request.get("version"), "generation": state.get("generation"),
            "progressive_parent_id": (
                progressive_checkpoint.get("checkpoint_id") if progressive_checkpoint else None
            ),
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
        effective_edition = _effective_retrieval_edition(
            requested_edition,
            semantic_used=semantic_used,
            reranker_used=reranker_used,
            semantic_status=str(bundle.trace.get("semantic_status") or "unavailable"),
        )
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
            "wave": runtime.get("wave") if runtime else None,
            "first_useful_checkpoint": runtime.get("first_useful_checkpoint") if runtime else None,
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
        if runtime is not None and progress_callback is not None:
            phase = "investigation_complete" if runtime.get("stop_reason") != "continue" else "wave_complete"
            completion_event = {
                "phase": phase, "wave": runtime["wave"], "context_id": context_id,
                "stop_reason": runtime.get("stop_reason"),
            }
        from .atlas import record_investigation

        mark_active_artifacts(
            state, request_path, path, trace_path,
            *( [directory / str(progressive_checkpoint["artifact"])] if progressive_checkpoint else [] ),
            *( [continuation_path] if continuation_path is not None else [] ),
        )
        if request.get("version") == 5:
            from .investigation import validate_stable_identity_registry

            validate_stable_identity_registry(state)
        save_session(settings, ticket, state)
        if progress_callback is not None:
            if continuation_event is not None:
                progress_callback(continuation_event)
            if completion_event is not None:
                progress_callback(completion_event)
        if settings.persist_investigation_records:
            try:
                record_investigation(settings, ticket, state)
            except (OSError, sqlite3.Error):
                # The ticket session is authoritative. A derived cross-ticket prior
                # must never make an otherwise durable context fail.
                pass
    except Exception:
        request_path.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        trace_path.unlink(missing_ok=True)
        if continuation_path is not None:
            continuation_path.unlink(missing_ok=True)
        if continuation_handoff is not None:
            continuation_handoff.unlink(missing_ok=True)
        if progressive_checkpoint is not None:
            published_state = durable_checkpoint_state or state
            published_checkpoint = json.loads(json.dumps(
                published_state.get("progressive_checkpoint") or progressive_checkpoint
            ))
            published_checkpoint.update({
                "continuation_status": "failed",
                "continuation_failure": "retryable internal continuation failure",
            })
            stable_identities = json.loads(json.dumps(published_state.get("stable_identities") or {}))
            checkpoint_lineage = json.loads(json.dumps(published_state.get("context_lineage") or []))
            active_artifacts = json.loads(json.dumps(published_state.get("active_artifacts") or []))
            state.clear()
            state.update(json.loads(json.dumps(pre_wave_state)))
            state["stable_identities"] = stable_identities
            state["context_lineage"] = checkpoint_lineage
            if active_artifacts:
                state["active_artifacts"] = active_artifacts
            state["progressive_checkpoint"] = published_checkpoint
            state["status"] = "waiting_for_ai"
            failures = list(state.get("continuation_failures") or [])
            failures.append({
                "event_id": f"continuation-failure:{published_checkpoint.get('checkpoint_id')}:{len(failures) + 1}",
                "checkpoint_id": published_checkpoint.get("checkpoint_id"),
                "number": number, "kind": "progressive_continuation_failed",
                "content_hash": None, "protocol_version": 5,
                "generation": published_checkpoint.get("generation"),
            })
            state["continuation_failures"] = failures[-100:]
            try:
                save_session(settings, ticket, state)
            except OSError:
                pass
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
        "runtime results. If more source evidence is required, return a new INVESTIGATION_REQUEST v5.",
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
    _atomic_session_text_write(settings, ticket, path, content)
    state["feedbacks"] = number
    state["status"] = "reviewing_implementation"
    mark_active_artifacts(state, path)
    save_session(settings, ticket, state)
    if settings.experience_enabled:
        from .experience import evaluate_sessions

        evaluate_sessions(settings, tickets={ticket})
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
    try:
        expected_metadata = source.lstat()
    except OSError as error:
        raise BrainError(f"Evidence file is unavailable: {source}") from error
    if not stat.S_ISREG(expected_metadata.st_mode):
        raise BrainError(f"Evidence file must be a regular non-symlink file: {source}")
    if expected_metadata.st_size > MAX_EXTERNAL_EVIDENCE_SOURCE_BYTES:
        raise BrainError("Evidence files are limited to 20 MB; extract or split the relevant content first")
    if kind not in {"document", "log", "note", "runtime"}:
        raise BrainError("Evidence kind must be document, log, note, or runtime")
    try:
        supplied_bytes, exceeded = read_direct_file_bytes(
            source, max_bytes=MAX_EXTERNAL_EVIDENCE_SOURCE_BYTES,
        )
    except (OSError, ValueError) as error:
        raise BrainError(f"Evidence file is unavailable: {source}") from error
    if exceeded:
        raise BrainError("Evidence files are limited to 20 MB; extract or split the relevant content first")
    state = session_state(settings, ticket)
    number = int(state.get("external_evidence") or 0) + 1
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", source.name).strip(".-") or f"evidence-{number:03d}"
    stored_dir = directory / "external"
    stored = stored_dir / f"{number:03d}-{safe_name}"
    _atomic_session_bytes_write(settings, ticket, stored, supplied_bytes)
    digest = hashlib.sha256(supplied_bytes).hexdigest()
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
        sections.extend(["## Content", "", "```text", supplied_bytes.decode("utf-8", errors="replace").rstrip(), "```", ""])
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
    _atomic_session_text_write(settings, ticket, artifact, content)
    state["external_evidence"] = number
    state["status"] = "waiting_for_ai"
    mark_active_artifacts(state, artifact)
    save_session(settings, ticket, state)
    return content, artifact, number, stored


def _external_evidence(settings: Settings, ticket: str) -> list[Evidence]:
    directory = session_dir(settings, ticket)
    state = session_state(settings, ticket)
    baseline = max(0, int(state.get("external_evidence_baseline") or 0))
    current = max(baseline, int(state.get("external_evidence") or 0))
    evidence: list[Evidence] = []
    remaining = MAX_EXTERNAL_CONTEXT_TOTAL_BYTES
    omitted = current - baseline > MAX_EXTERNAL_CONTEXT_ITEMS
    end = min(current, baseline + MAX_EXTERNAL_CONTEXT_ITEMS)
    for number in range(baseline + 1, end + 1):
        path = directory / f"external-{number:03d}.md"
        limit = min(MAX_EXTERNAL_CONTEXT_ITEM_BYTES, remaining)
        if limit <= 0:
            omitted = True
            break
        try:
            raw = read_managed_bytes(directory, path, max_bytes=limit)
        except (OSError, ValueError):
            omitted = True
            continue
        remaining -= len(raw)
        content = raw.decode("utf-8", errors="replace")
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
    if omitted:
        evidence.append(
            Evidence(
                "external", "(external evidence omitted)", 1, 1,
                EXTERNAL_CONTEXT_OMISSION, "external evidence safety warning", 100,
                ["bounded ticket evidence validation"],
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
        command = trusted_path_executable("pbcopy" if write else "pbpaste")
        return [str(command)] if command else None
    if os.name == "nt":
        script = "Set-Clipboard -Value ([Console]::In.ReadToEnd())" if write else "Get-Clipboard -Raw"
        command = windows_system_executable("powershell", "WindowsPowerShell", "v1.0")
        return [str(command), "-NoProfile", "-NonInteractive", "-Command", script] if command else None
    if command := trusted_path_executable("wl-copy" if write else "wl-paste"):
        return [str(command), *( [] if write else ["--no-newline"] )]
    if command := trusted_path_executable("xclip"):
        return [str(command), "-selection", "clipboard", "-in" if write else "-out"]
    return None


def clipboard_read() -> str:
    command = _clipboard_command(False)
    if not command:
        raise BrainError("No clipboard command found; use --file or stdin")
    try:
        result = run_bounded_process(
            command,
            Path.cwd(),
            max_stdout_bytes=MAX_CLIPBOARD_BYTES,
            timeout=MAX_CLIPBOARD_SECONDS,
        )
    except OSError as error:
        raise BrainError(f"Clipboard read failed: {error}") from error
    if getattr(result, "output_truncated", False) or getattr(result, "timed_out", False):
        raise BrainError("Clipboard content exceeds its retrieval limit")
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
        from .agent import final_solution_contract

        handoff_directory = handoff_dir(settings, ticket)
        current_handoff = handoff_directory / "current.md"
        _validated_generated_artifact(settings, current_handoff)
        internal_handoff = directory / "current-handoff.md"
        _atomic_session_text_write(settings, ticket, internal_handoff, text)
        _atomic_generated_text_write(settings, current_handoff, text)
        if text.startswith("# PROJECT BRAIN — START"):
            label = "start"
        elif text.startswith("# PROJECT BRAIN — EXTERNAL EVIDENCE"):
            match = re.search(r"(?m)^Evidence: `(\d+)`", text)
            label = f"evidence-{int(match.group(1)):03d}" if match else "evidence"
        elif text.startswith("# PROJECT BRAIN — IMPLEMENTATION FEEDBACK"):
            match = re.search(r"(?m)^Feedback: `(\d+)`", text)
            label = f"feedback-{int(match.group(1)):03d}" if match else "feedback"
        elif final_solution_contract(text)[0]:
            label = "final"
        else:
            match = re.search(r"(?m)^Request: `(\d+)`", text)
            label = f"context-{int(match.group(1)):03d}" if match else "update"
        handoff = handoff_directory / f"{label}.md"
        _atomic_generated_text_write(settings, handoff, text)
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
    paths: list[Path] = []
    total = len(parts)
    for index, part in enumerate(parts, 1):
        header = f"PROJECT BRAIN CONTEXT — PART {index} OF {total}\n\n" if total > 1 else ""
        path = delivery_dir / f"part-{index:03d}.txt"
        _atomic_session_text_write(settings, ticket, path, header + part)
        paths.append(path)
    state["delivery"] = {"target": target, "parts": [str(path) for path in paths], "current": 1}
    save_session(settings, ticket, state)
    if copy:
        clipboard_write(_read_session_artifact(
            settings, ticket, paths[0], MAX_DELIVERY_ARTIFACT_BYTES,
        ))
    return paths, 1


def delivery_artifact(
    settings: Settings, ticket: str, value: object,
) -> tuple[Path, str]:
    path = Path(str(value or ""))
    if not path.is_absolute():
        raise BrainError("Invalid delivery artifact in session state")
    directory = session_dir(settings, ticket)
    handoffs = settings.generated_dir / "handoffs"
    if handoffs.is_symlink() or settings.generated_dir.is_symlink():
        raise BrainError("Invalid generated handoff directory")
    try:
        session_relative = path.relative_to(directory)
    except ValueError:
        session_relative = None
    try:
        handoff_relative = path.relative_to(handoffs)
    except ValueError:
        handoff_relative = None
    if session_relative is None and handoff_relative is None:
        raise BrainError("Invalid delivery artifact in session state")
    if handoff_relative is not None:
        legacy_label = re.compile(
            rf"^{re.escape(directory.name)}-(?:current|start|final|update|context-\d+|evidence-\d+|"
            rf"feedback-\d+|checkpoint-\d+|checkpoint-delta-\d+)\.md$"
        )
        ticket_label = re.compile(
            r"^(?:current|start|final|update|context-\d+|evidence-\d+|feedback-\d+|"
            r"checkpoint-\d+|checkpoint-delta-\d+)\.md$"
        )
        legacy = len(handoff_relative.parts) == 1 and legacy_label.fullmatch(path.name)
        organized = (
            len(handoff_relative.parts) == 2
            and handoff_relative.parts[0] == directory.name
            and ticket_label.fullmatch(path.name)
        )
        if not legacy and not organized:
            raise BrainError("Delivery handoff does not belong to this session")
    root = handoffs if handoff_relative is not None else directory
    try:
        raw = read_managed_bytes(root, path, max_bytes=MAX_DELIVERY_ARTIFACT_BYTES)
    except (OSError, ValueError) as error:
        raise BrainError(f"Invalid delivery artifact in session state: {error}") from error
    return path, raw.decode("utf-8", errors="replace")


def move_delivery(settings: Settings, ticket: str, delta: int) -> tuple[Path, int, int]:
    state = session_state(settings, ticket)
    delivery = state.get("delivery") or {}
    parts = delivery.get("parts") or []
    if not parts:
        raise BrainError(f"No delivery exists for {ticket}")
    current = max(1, min(len(parts), int(delivery.get("current") or 1) + delta))
    path, content = delivery_artifact(settings, ticket, parts[current - 1])
    delivery["current"] = current
    state["delivery"] = delivery
    save_session(settings, ticket, state)
    clipboard_write(content)
    return path, current, len(parts)


def create_learning_template(settings: Settings, ticket: str) -> Path:
    directory = settings.knowledge_dir / "tickets"
    target = directory / f"{filesystem_component(ticket)}.md"
    legacy = directory / f"{re.sub(r'[^A-Za-z0-9._-]+', '-', ticket)}.md"
    try:
        reusable_legacy = legacy != target and legacy.exists() and not legacy.is_symlink()
    except OSError:
        reusable_legacy = False
    if reusable_legacy:
        try:
            first_line = read_managed_text(
                settings.knowledge_dir, legacy, max_bytes=MAX_KNOWLEDGE_ITEM_BYTES,
            ).splitlines()[0]
        except (IndexError, OSError, UnicodeError, ValueError):
            first_line = ""
        if first_line == f"# {ticket}":
            return legacy
    if target.exists() or target.is_symlink():
        try:
            read_managed_text(settings.knowledge_dir, target, max_bytes=MAX_KNOWLEDGE_ITEM_BYTES)
        except (OSError, UnicodeError, ValueError) as error:
            raise BrainError("Learning template is not a safe managed file") from error
        return target
    content = (
        f"# {ticket}\n\n## Problem\n\n\n## Repositories\n\n\n## Execution Flow\n\n\n"
        "## Root Cause\n\n\n## Solution\n\n\n## Tests\n\n\n## Gotchas\n"
    )
    try:
        atomic_managed_text_write(settings.knowledge_dir, target, content)
    except (OSError, ValueError) as error:
        raise BrainError("Unable to create a safe learning template") from error
    return target

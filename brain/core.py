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
from array import array
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from importlib.resources import files as package_files
from pathlib import Path
from typing import Any, Iterable

from .investigation import source_verification_scope
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
PINNED_SYMBOL_ANCHOR_PREFIX = 'pinned symbol anchor (navigation only): '
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
MAX_TOPIC_SEED_FILES = 4
MAX_TOPIC_NAVIGATION_BYTES = 1_000_000
MAX_TOPIC_NAVIGATION_KEYS = 8
MAX_SYMBOL_TRACE_CACHED_FILES = 8
MAX_SYMBOL_TRACE_CACHE_BYTES = 8 * 1024 * 1024


class BrainError(RuntimeError):
    pass


class InvestigationContinuationRequired(BrainError):
    """Request sequence or legacy continuation-token validation failed."""


class CheckpointRetryRequired(BrainError):
    """A published early checkpoint still owns the unfinished request."""

    def __init__(self, ticket: str, retry_available: bool = False, *, checkpoint_problem: bool = False) -> None:
        self.ticket, self.retry_available = ticket, retry_available
        self.checkpoint_problem = checkpoint_problem
        super().__init__(
            "The early checkpoint has a pending or failed continuation. "
            + ("The checkpoint artifact is corrupt or unavailable. Restore this ticket's retained checkpoint/source "
               "artifacts before retrying; do not reset, refresh or substitute a newer generation."
               if checkpoint_problem else "Retry the saved request on this ticket's original generation."
               if retry_available else
               "The saved request cannot be verified. Paste the same original AI request with its original "
               "include-diff option. Keep this ticket and its pinned generation; do not reset or refresh to retry.")
        )


class ContextDeliveryError(BrainError):
    """The retrieval committed successfully but its chat delivery failed."""

    def __init__(self, ticket: str, artifact: str) -> None:
        self.ticket, self.artifact = ticket, artifact
        super().__init__(
            f"Evidence is saved as {artifact}; AI delivery failed. "
            "Open that context in this ticket's history and copy it to your AI. Do not rerun retrieval or refresh."
        )


class ClipboardWriteError(BrainError):
    """A clipboard transport failure, not a source or retrieval error."""


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
            os.O_RDWR | os.O_APPEND | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(settings.config_path, flags)
        opened = os.fstat(descriptor)
        path_metadata = settings.config_path.lstat()
        if (
            settings.config_path.is_symlink()
            or not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (path_metadata.st_dev, path_metadata.st_ino)
            or (opened.st_dev, opened.st_ino, opened.st_size)
            != (current.st_dev, current.st_ino, current.st_size)
        ):
            raise OSError("brain.toml identity changed while opening")
        # Windows path and handle timestamp observations can differ. Verify the
        # exact bounded input through the same descriptor that will append it.
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = len(raw) + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after_read = settings.config_path.lstat()
        if (
            b"".join(chunks) != raw
            or not stat.S_ISREG(after_read.st_mode)
            or (after_read.st_dev, after_read.st_ino, after_read.st_size)
            != (opened.st_dev, opened.st_ino, opened.st_size)
        ):
            raise OSError("brain.toml content changed while opening")
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
    # Transient query resolutions, never loaded from ticket state or used as
    # evidence authority. Runtime revalidates their pinned entities and edges.
    _resolved_relation_seeds: dict[str, str] = field(default_factory=dict, repr=False)
    _python_package_sources: dict[tuple[str, str], str] = field(default_factory=dict, repr=False)


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


def _yaml_parts(value: str, delimiter: str) -> list[str]:
    """Split flow values outside quotes/collections; strip only YAML comments."""
    parts: list[str] = []
    start = 0
    quote = ""
    depth = 0
    escaped = False
    for index, char in enumerate(value):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = ""
        elif char in "\"'" and (index == start or value[index - 1] in " \t:[{,"):
            quote = char
        elif char == "#" and (index == 0 or value[index - 1].isspace()):
            value = value[:index]
            break
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        elif char == delimiter and depth == 0 and (
            delimiter != ":" or index + 1 == len(value) or value[index + 1].isspace()
            or value[start:index].strip().startswith(('"', "'"))
        ):
            parts.append(value[start:index].strip())
            start = index + 1
            if delimiter == ":":
                # The rest is one scalar; a colon inside it is not another key.
                return [*parts, _yaml_parts(value[start:], "\0")[0]]
    parts.append(value[start:].strip())
    return parts


def _scalar(value: str) -> Any:
    value = value.strip()
    if value == "":
        return None
    lowered = value.lower()
    if lowered in {"null", "~"}:
        return None
    if lowered in {"true", "false"}:
        return lowered == "true"
    if value.startswith('"') and value.endswith('"'):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_scalar(part) for part in _yaml_parts(inner, ",") if part]
    if value.startswith("{") and value.endswith("}"):
        result = {}
        for part in _yaml_parts(value[1:-1], ","):
            if not part:
                continue
            pair = _yaml_parts(part, ":")
            if len(pair) != 2:
                raise BrainError(f"Invalid YAML mapping: {part}")
            result[str(_scalar(pair[0]))] = _scalar(pair[1])
        return result
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
        stripped = _yaml_parts(line.strip(), "\0")[0]
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
        pair = _yaml_parts(value, ":")
        if len(pair) != 2:
            raise BrainError(f"Invalid YAML line: {value}")
        return str(_scalar(pair[0])), pair[1]

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
                if len(_yaml_parts(rest, ":")) == 2:
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


def _lexical_cache_scope(settings: Settings, selected: list[Repository]) -> tuple[Any, ...]:
    generation = settings.atlas_generation
    component = generation.component("lexical") if generation is not None else {}
    return (
        settings.state_dir, settings.atlas_generation_mode,
        generation.identity if generation is not None else None,
        tuple(component.get(field) for field in ("status", "schema_version", "content_hash")),
        tuple((repo.name, repo.path, repo.source_path, repo.source_sha) for repo in selected),
        settings.max_results, settings.candidate_limit,
        MAX_PINNED_QUERY_CANDIDATE_FILES, MAX_PINNED_QUERY_BYTES, MAX_PINNED_QUERY_SECONDS,
    )


def _record_source_lookup_failure(kind: str) -> None:
    trace = _ACTIVE_RETRIEVAL_TRACE.get()
    if trace is not None:
        trace.fallback_reasons.append(f"{kind}_lookup_unavailable")
        if trace.stop_reason == "coverage_satisfied":
            trace.stop_reason = "source_lookup_incomplete"


def _source_lookup_failed_since(trace: Any, offset: int) -> bool:
    return any(reason.endswith("_lookup_unavailable") for reason in trace.fallback_reasons[offset:])


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
    key = ("component-validation", "lexical", _lexical_cache_scope(settings, [repo] if repo else settings.repositories))
    cache = _ACTIVE_RETRIEVAL_CACHE.get()
    if cache is not None and key in cache:
        return bool(cache[key])
    component = generation.component("lexical")
    from .index import LEXICAL_COMPONENT_SCHEMA_VERSION

    details = component.get("details") if isinstance(component.get("details"), dict) else {}
    snapshots = details.get("snapshots")
    if not isinstance(snapshots, dict):
        return False
    expected_snapshots = {str(name): str(value) for name, value in snapshots.items()}
    valid = False
    if (
        component.get("status") == "ready"
        and component.get("schema_version") == str(LEXICAL_COMPONENT_SCHEMA_VERSION)
        and expected_snapshots == generation.snapshots
    ):
        repository_hashes = details.get("repository_hashes") if isinstance(details.get("repository_hashes"), dict) else {}
        repository_files = details.get("repository_files") if isinstance(details.get("repository_files"), dict) else {}
        if repo is not None and repo.name in repository_hashes:
            from .index import lexical_repository_identity

            try:
                expected_files = int(repository_files.get(repo.name, -1))
            except (TypeError, ValueError, OverflowError):
                return False
            identity = lexical_repository_identity(settings, repo.name, generation.snapshots.get(repo.name, ""))
            valid = bool(
                identity and identity[0] == repository_hashes.get(repo.name)
                and identity[1] == expected_files
            )
        else:
            from .index import lexical_membership_identity

            identity = lexical_membership_identity(settings, generation.snapshots)
            valid = bool(identity and identity[0] == component.get("content_hash"))
    if valid and cache is not None and len(cache) < 256:
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

    # A regex without metacharacters is the same literal query. Share its cache
    # and batched index path, including a verified empty result.
    fixed = fixed or re.escape(pattern) == pattern
    if settings.atlas_generation is None and settings.atlas_generation_mode == "current":
        from .catalog import current_generation_ref

        settings = replace(settings, atlas_generation=current_generation_ref(settings))
    selected = settings.repos(repos)
    cache_scope = _lexical_cache_scope(settings, selected)
    key = ("search", pattern, fixed, cache_scope)
    cached = _cached_hits(key)
    if cached is not None:
        return cached
    trace = _ACTIVE_RETRIEVAL_TRACE.get()
    generation = settings.atlas_generation
    if (
        fixed
        and generation is not None
        and (trace is None or trace.try_reserve_backend())
    ):
        # One registered lexical query covers all requested snapshots. Installed
        # Zoekt shards remain a fallback, not one subprocess per repo per literal.
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
            collect_java_declarations=(_ACTIVE_RETRIEVAL_CACHE.get() is not None
                                       and bool(re.fullmatch(r"[A-Za-z_$][\w$]{0,499}", pattern))),
            collect_java_call_references=(bool(re.fullmatch(r"[A-Za-z_$][\w$]{0,499}", pattern))
                                          and pattern in (_ACTIVE_RETRIEVAL_CACHE.get() or {}).get(("java-call-queries",), ())),
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
                if indexed_stats.get("java_declarations_complete"):
                    _store_hits(("java-declarations", pattern, cache_scope), [
                        SearchHit(repo, path, line, text, "definition", 100, ["sqlite trigram index", "symbol declaration"])
                        for repo, path, line, text in indexed_stats["java_declarations"]
                    ])
                if indexed_stats.get("java_call_references_complete"):
                    _store_hits(("java-caller-references", pattern, cache_scope), [
                        SearchHit(repo, path, line, text, "Java call reference candidate", 98,
                                  ["pinned lexical call reference; receiver type and dispatch are not established"])
                        for repo, path, line, text in indexed_stats["java_call_references"]
                    ])
            return _clone_hits(hits)
    unavailable_repos: list[str] = []
    if trace is not None:
        unavailable_repos.extend(repo.name for repo in selected[trace.physical_budget_remaining:])
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
            unavailable_repos.append(repo.name)
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
                unavailable_repos.append(repo.name)
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
                unavailable_repos.append(repo.name)
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
    if unavailable_repos:
        _record_source_lookup_failure("lexical")
    else:
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
    key = ("path", needle, _lexical_cache_scope(settings, selected), settings.path_result_limit,
           MAX_PINNED_PATH_CANDIDATES, MAX_PINNED_PATH_SECONDS)
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
    unavailable_repos: list[str] = []
    if trace is not None:
        unavailable_repos.extend(repo.name for repo in selected[trace.physical_budget_remaining:])
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
        if indexed is None and settings.atlas_generation_mode == "pinned":
            unavailable_repos.append(repo.name)
        paths = indexed if indexed is not None else (
            [] if settings.atlas_generation_mode == "pinned"
            else (logical_path(path.relative_to(root)) for path in _walk_files(root)) if root.is_dir() else []
        )
        return ranked(repo, paths)

    hits = [hit for rows in _parallel_repositories(settings, selected, one) for hit in rows]
    if unavailable_repos:
        _record_source_lookup_failure("path")
    else:
        _store_hits(key, hits)
    return _clone_hits(hits)


def _is_documentation_path(path: str) -> bool:
    value = Path(path)
    return value.suffix.lower() in {".md", ".rst", ".txt", ".adoc"} or value.stem.casefold() in {"readme", "license", "changelog"}


def _symbol_declaration(name: str, *, path: str = "") -> re.Pattern[str]:
    escaped = re.escape(name)
    if path.casefold().endswith(".java"):
        # A return type is a qualified identifier with optional type arguments
        # and array suffixes, not arbitrary words/operators before a call.
        identifier = r"[A-Za-z_$][\w$]*"
        non_type = r"(?!(?:return|new|throw|throws|assert|case|else|yield|break|continue|instanceof|if|for|while|switch|catch|try|do|finally)\b)"
        java_type = rf"{non_type}{identifier}(?:\s*\.\s*{identifier})*(?:\s*<[\w$?, .<>\[\]\s]+>)?(?:\s*\[\s*\])*"
        modifiers = r"(?:(?:public|protected|private|static|final|abstract|synchronized|native|default|strictfp)\s+)*"
        annotations = rf"(?:@{identifier}(?:\.{identifier})*(?:\([^()]*\))?\s*)*"
        return re.compile(
            rf"(?:^|(?<=[;{{}}]))\s*{annotations}{modifiers}(?:"
            rf"(?:class|interface|enum|record)\s+(?P<class_name>{escaped})(?![\w$])"
            rf"|(?:<[\w$?, .<>\[\]&\s]+>\s*)?{java_type}\s+(?P<method_name>{escaped})\s*\()",
            re.MULTILINE,
        )
    if path.casefold().endswith((".py", ".pyi")):
        # Python control-flow words are not Java-style return types. In
        # particular, `for x in name(...)` is a call, never a definition.
        return re.compile(rf"^\s*(?:async\s+)?(?:class|def)\s+{escaped}\b")
    declaration = (
        rf"\b(?:class|interface|enum|record|trait|struct|type|object|def|fn|func|function|fun)\s+{escaped}\b"
        rf"|\b{escaped}\s*[:=]\s*(?:async\s+)?(?:function|\([^)]*\)\s*=>)"
        rf"|\b(?!(?:return|new|throw|await|yield)\b)(?:public|protected|private|static|final|abstract|synchronized|native\s+)*[A-Za-z_$][\w$<>, ?\[\].]*\s+{escaped}\s*\("
    )
    return re.compile(declaration)


def symbol_hits(settings: Settings, query: str, repos: Iterable[str] | None = None) -> list[SearchHit]:
    from .graph import graph_symbol_hits

    scope = list(repos or [])
    name = query.rsplit(".", 1)[-1]
    escaped = re.escape(name)
    hits = _symbol_definition_hits(settings, name, scope)
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
    # Pinned lexical indexes serve literals without an optional regex backend.
    # Apply the symbol boundary to verified source lines before returning refs.
    reference = re.compile(rf"\b{escaped}\b")
    fallback = [hit for hit in search(settings, name, scope, fixed=True) if reference.search(hit.text)]
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


def _java_caller_reference_hits(settings: Settings, query: str, repos: Iterable[str] | None = None) -> tuple[list[SearchHit], str | None]:
    """Retrieve possible receiver/static-import callers without asserting dispatch."""
    return _java_symbol_index_hits(settings, query, repos, declarations=False)


def _java_symbol_index_hits(
    settings: Settings, query: str, repos: Iterable[str] | None, *, declarations: bool,
) -> tuple[list[SearchHit], str | None]:
    from .index import query_generation_indexes

    generation = settings.atlas_generation
    if generation is None:
        return [], "pinned lexical generation unavailable"
    name = query.replace("#", ".").rsplit(".", 1)[-1]
    scope = list(repos or [])
    if any(name not in generation.snapshots for name in scope):
        return [], "requested repository is outside the pinned generation"
    selected = [repo for repo in settings.repos(scope) if repo.name in generation.snapshots]
    backend = "java-declarations" if declarations else "java-caller-references"
    key = (backend, name, _lexical_cache_scope(settings, selected))
    if (cached := _cached_hits(key)) is not None:
        return cached, None
    trace = _ACTIVE_RETRIEVAL_TRACE.get()
    if trace is not None and not trace.try_reserve_backend():
        return [], "physical operation budget"
    started = time.perf_counter()
    stats: dict[str, object] = {}
    try:
        indexed = query_generation_indexes(
            settings, generation, selected, name, max_results=settings.max_results,
            max_candidate_files=min(MAX_PINNED_QUERY_CANDIDATE_FILES, max(len(selected), settings.candidate_limit)),
            max_hits=min(settings.candidate_limit, settings.max_results * max(1, len(selected))),
            max_bytes=MAX_PINNED_QUERY_BYTES, max_seconds=MAX_PINNED_QUERY_SECONDS,
            stats=stats, java_calls_only=not declarations, java_declarations_only=declarations,
        )
    finally:
        if trace is not None:
            trace.complete_reserved_backend(backend, (time.perf_counter() - started) * 1000,
                                            bytes_scanned=int(stats.get("candidate_bytes") or 0),
                                            files=int(stats.get("candidate_files") or 0),
                                            raw_hits=int(stats.get("hits") or 0))
    if indexed is None:
        return [], "pinned lexical component unavailable or incompatible"
    hits = [SearchHit(repo.name, path, line, text, "definition" if declarations else "Java call reference candidate",
                      100 if declarations else 98,
                      ["sqlite trigram index", "symbol declaration"] if declarations else
                      ["pinned lexical call reference; receiver type and dispatch are not established"])
            for repo in selected for path, line, text in indexed[repo.name]]
    if stats.get("budget_exhausted"):
        return hits, f"reference search budget: {stats.get('reason') or 'unknown'}"
    _store_hits(key, hits)
    return hits, None


def _symbol_definition_hits(
    settings: Settings, name: str, repos: Iterable[str] | None, lexical: list[SearchHit] | None = None,
    *, _verified_sources: dict[tuple[str, str], str] | None = None,
) -> list[SearchHit]:
    """Share source-aware Java definition classification across discovery/trace."""
    from .index import _java_declaration_lines

    if settings.atlas_generation is None and settings.atlas_generation_mode == "current":
        from .catalog import current_generation_ref

        settings = replace(settings, atlas_generation=current_generation_ref(settings))
    lexical = search(settings, name, repos, fixed=True) if lexical is None else lexical
    hits = [replace(hit, found_by=list(hit.found_by)) for hit in lexical
            if not hit.path.lower().endswith(".java") and not _is_documentation_path(hit.path)
            and _symbol_declaration(name, path=hit.path).search(hit.text)]
    for hit in hits:
        hit.kind, hit.score = "definition", 100
        hit.found_by = sorted(set([*hit.found_by, "symbol declaration"]))
    trace = _ACTIVE_RETRIEVAL_TRACE.get()
    reason = None
    if settings.atlas_generation is not None or settings.atlas_generation_mode == "pinned":
        java_hits, reason = _java_symbol_index_hits(settings, name, repos, declarations=True)
        hits.extend(java_hits)
    else:
        # Legacy source-pin operation has no registered lexical component.
        # Validate only bounded candidate files using the same full-source mask.
        files = {(hit.repo, hit.path): hit for hit in lexical if hit.path.lower().endswith(".java")
                 and _symbol_declaration(name, path=hit.path).search(hit.text)}
        deadline = time.monotonic() + MAX_PINNED_QUERY_SECONDS
        total_bytes = 0
        retained_bytes = 0
        for number, hit in enumerate(files.values()):
            if number >= min(MAX_PINNED_QUERY_CANDIDATE_FILES, settings.candidate_limit) or time.monotonic() >= deadline:
                reason = "source file/time budget"
                break
            if trace is not None and not trace.try_reserve_backend():
                reason = "physical operation budget"
                break
            started = time.perf_counter()
            size = 0
            try:
                evidence = read_source(settings, hit, full=True)
                source = evidence.verification_content or evidence.content
                size = len(source.encode("utf-8"))
                total_bytes += size
                if total_bytes > MAX_PINNED_QUERY_BYTES:
                    reason = "source byte budget"
                    break
                if (_verified_sources is not None and len(_verified_sources) < MAX_SYMBOL_TRACE_CACHED_FILES
                        and retained_bytes + size <= MAX_SYMBOL_TRACE_CACHE_BYTES):
                    _verified_sources[(hit.repo, hit.path)] = source
                    retained_bytes += size
                for line, text in _java_declaration_lines(source, name, deadline=deadline):
                    hits.append(SearchHit(hit.repo, hit.path, line, text, "definition", 100,
                                          sorted(set([*hit.found_by, "symbol declaration"]))))
                    if len(hits) >= settings.candidate_limit:
                        reason = "source hit budget"
                        break
                if time.monotonic() >= deadline:
                    reason = "source time budget"
                if reason:
                    break
            except (BrainError, OSError):
                reason = "candidate source unavailable"
            finally:
                if trace is not None:
                    trace.complete_reserved_backend("java-declaration-source", (time.perf_counter() - started) * 1000,
                                                    bytes_scanned=size, files=1)
    if reason and trace is not None:
        message = f"Java declaration verification is incomplete: {reason}; source availability is unknown."
        if message not in trace.fallback_reasons:
            trace.fallback_reasons.append(message)
    return hits


def _configuration_prefix_hits(settings: Settings, anchors: Iterable[object]) -> tuple[list[SearchHit], str | None]:
    """Find declared Java configuration prefixes, not inferred member bindings."""
    from .investigation import MAX_ANCHOR_CANDIDATES, MAX_EXACT_ANCHOR_QUERIES, _bounded_anchor_queries, resolve_runtime_anchors

    prefixes: list[str] = []
    limited = False
    for kind, key in _bounded_anchor_queries(anchors):
        if kind != "config_key":
            continue
        parts = key.split(".")
        for count in range(len(parts) - 1, 0, -1):
            prefix = ".".join(parts[:count])
            if prefix in prefixes:
                continue
            if len(prefixes) >= MAX_EXACT_ANCHOR_QUERIES:
                limited = True
                break
            prefixes.append(prefix)
    if not prefixes:
        return [], None
    generation = settings.atlas_generation
    if generation is None:
        return [], "pinned runtime-anchor component unavailable"
    trace = _ACTIVE_RETRIEVAL_TRACE.get()
    if trace is not None and not trace.try_reserve_backend():
        return [], "physical operation budget"
    started = time.perf_counter()
    resolved: dict[str, Any] = {}
    try:
        resolved = resolve_runtime_anchors(
            settings, generation, [{"kind": "config_key", "value": prefix} for prefix in prefixes],
            use_cache="generation_cache" not in settings.evaluation_ablations,
        )
    finally:
        if trace is not None:
            trace.complete_reserved_backend("configuration-prefix", (time.perf_counter() - started) * 1000,
                                            raw_hits=len(resolved.get("candidates") or []))
    if resolved.get("status") != "ready":
        return [], str(resolved.get("reason") or "pinned configuration anchors unavailable")
    normalized = {prefix.strip().casefold() for prefix in prefixes}
    hits = [SearchHit(item["repo"], item["path"], item["line"], item["value"],
                      "requested configuration prefix candidate", 100,
                      ["generation-validated configuration prefix; member binding not established"])
            for item in resolved.get("candidates") or []
            if item.get("method") == "exact" and item.get("kind") == "config_key"
            and str(item.get("value") or "").strip().casefold() in normalized
            and str(item.get("path") or "").lower().endswith(".java")
            and (item.get("provenance") or {}).get("direction") == "prefix"]
    if limited or len(resolved.get("candidates") or []) >= MAX_ANCHOR_CANDIDATES:
        return hits, "configuration prefix candidate/input budget"
    return hits, None


def _topic_peer_hits(
    settings: Settings, anchors: list[dict[str, Any]], candidates: list[SearchHit],
    source_cache: dict[tuple[str, str], str],
) -> tuple[list[SearchHit], str | None]:
    """Follow concrete topic declarations near event hits as navigation, not payload proof."""
    from .index import read_generation_files
    from .investigation import MAX_ANCHOR_CANDIDATES, _literal_java_topics, _mask_java_comments, resolve_runtime_anchors

    topics = list(dict.fromkeys(str(item.get("value") or "") for item in anchors if item.get("kind") == "topic"))
    events = {str(item.get("value") or "").rsplit(".", 1)[-1] for item in anchors if item.get("kind") == "event"}
    events = {name for name in events if re.fullmatch(r"[A-Za-z_$][\w$]{0,199}", name)}
    if not topics and not events:
        return [], None
    generation = settings.atlas_generation
    if generation is None:
        return [], "pinned runtime-anchor component unavailable"
    trace = _ACTIVE_RETRIEVAL_TRACE.get()
    limited: list[str] = []
    # Source-backed lexical hits only; a generated card mentioning an event is
    # not a reason to scan its entire repository or infer a payload relationship.
    locations = list(dict.fromkeys(
        (hit.repo, hit.path) for hit in candidates
        if hit.path.endswith('.java') and hit.repo in generation.snapshots
        and any(channel.startswith('lexical anchor ') for channel in hit.found_by)
        and any(re.search(rf'\b{re.escape(name)}\b', hit.text) for name in events)
    ))
    if len(locations) > MAX_TOPIC_SEED_FILES:
        limited.append("event source file budget")
    locations = locations[:MAX_TOPIC_SEED_FILES]
    if locations:
        if trace is not None and not trace.try_reserve_backend():
            return [], "physical operation budget"
        started = time.perf_counter()
        loaded: dict[tuple[str, str], str] = {}
        try:
            loaded = read_generation_files(settings, generation, locations,
                                           max_bytes=MAX_TOPIC_NAVIGATION_BYTES, max_seconds=.25) or {}
            source_cache.update(loaded)
        finally:
            if trace is not None:
                trace.complete_reserved_backend('topic-source', (time.perf_counter() - started) * 1000,
                                                files=len(loaded), bytes_scanned=sum(len(text.encode('utf-8')) for text in loaded.values()))
        if len(loaded) != len(locations):
            limited.append("pinned event source unavailable or source budget")
        for key in locations:
            content = loaded.get(key)
            if content is None:
                continue
            code = _mask_java_comments(content, strings=True)
            if any(re.search(rf'\b{re.escape(name)}\b', code) for name in events):
                topics.extend(topic for topic, _, _, _ in sorted(_literal_java_topics(content), key=lambda row: row[1:]))
    topics = list(dict.fromkeys(topics))
    if len(topics) > MAX_TOPIC_NAVIGATION_KEYS:
        limited.append("topic key budget")
    topics = topics[:MAX_TOPIC_NAVIGATION_KEYS]
    if not topics:
        return [], '; '.join(limited) or "no literal topic established in the bounded event source candidates"
    if trace is not None and not trace.try_reserve_backend():
        return [], "physical operation budget"
    started = time.perf_counter()
    resolved: dict[str, Any] = {}
    try:
        resolved = resolve_runtime_anchors(settings, generation, [{"kind": "topic", "value": topic} for topic in topics],
                                           use_cache="generation_cache" not in settings.evaluation_ablations)
    finally:
        if trace is not None:
            trace.complete_reserved_backend('topic-peers', (time.perf_counter() - started) * 1000,
                                            raw_hits=len(resolved.get('candidates') or []))
    if resolved.get("status") != "ready":
        return [], str(resolved.get("reason") or "pinned topic anchors unavailable")
    hits = [SearchHit(item['repo'], item['path'], item['line'], item['value'], 'requested topic peer candidate', 100,
                      ['generation-validated topic location; event payload association is not established'])
            for item in resolved.get('candidates') or []
            if item.get('method') == 'exact' and item.get('kind') == 'topic' and item.get('value') in topics
            and (item.get('provenance') or {}).get('direction') in {'inbound', 'outbound'}]
    if len(resolved.get('candidates') or []) >= MAX_ANCHOR_CANDIDATES:
        limited.append('topic candidate budget')
    if not hits:
        limited.append('no generation-validated topic producer or consumer registered')
    return hits, '; '.join(limited) or None


def test_hits(settings: Settings, name: str, repos: Iterable[str] | None = None) -> list[SearchHit]:
    from .index import query_generation_indexes

    if settings.atlas_generation is None and settings.atlas_generation_mode == "current":
        from .catalog import current_generation_ref

        settings = replace(settings, atlas_generation=current_generation_ref(settings))
    selected = settings.repos(repos)
    short = name.replace('#', '.').rsplit('.', 1)[-1]
    key = ("test-search", short, _lexical_cache_scope(settings, selected))
    if (cached := _cached_hits(key)) is not None:
        return cached
    trace = _ACTIVE_RETRIEVAL_TRACE.get()
    if settings.atlas_generation is not None and (trace is None or trace.try_reserve_backend()):
        started = time.perf_counter()
        stats: dict[str, object] = {}
        indexed = query_generation_indexes(
            settings, settings.atlas_generation, selected, short,
            max_results=settings.max_results,
            max_candidate_files=min(MAX_PINNED_QUERY_CANDIDATE_FILES, max(len(selected), settings.candidate_limit)),
            max_hits=min(settings.candidate_limit, settings.max_results * max(1, len(selected))),
            max_bytes=MAX_PINNED_QUERY_BYTES, max_seconds=MAX_PINNED_QUERY_SECONDS,
            stats=stats, test_only=True,
        )
        if trace is not None:
            trace.complete_reserved_backend("test-index", (time.perf_counter() - started) * 1000,
                                            raw_hits=int(stats.get("hits") or 0))
        if indexed is not None:
            tests = [SearchHit(repo.name, path, line, text, "test", 97, ["test discovery", "pinned test-only index"])
                     for repo in selected for path, line, text in indexed[repo.name]]
            if stats.get("budget_exhausted"):
                if trace is not None:
                    trace.fallback_reasons.append(f"test_search_budget:{stats.get('reason') or 'unknown'}")
                    trace.stop_reason = "lexical_batch_budget"
            else:
                _store_hits(key, tests)
            return tests
        if trace is not None:
            trace.fallback_reasons.append("test_index_filter_unavailable")
        _record_source_lookup_failure("test")
    candidates = search(settings, short, [repo.name for repo in selected], fixed=True)
    tests = [hit for hit in candidates if is_test_path(hit.path)]
    for hit in tests:
        hit.kind = "test"
        hit.score = 97
        hit.found_by.append("test discovery")
    return tests


_INDEXED_SOURCE_UNSET = object()


def _retrieval_source_path(repo: Repository, relative: str) -> Path:
    root = repo.scan_path.resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise BrainError(f"Unsafe or missing file: {repo.name}:{relative}")
    if path.name.lower() in SENSITIVE_FILE_NAMES or path.suffix.lower() in SENSITIVE_SUFFIXES:
        raise BrainError(f"Sensitive source path is excluded from automatic retrieval: {repo.name}:{relative}")
    return path


def read_source(
    settings: Settings,
    hit: SearchHit,
    *,
    full: bool = False,
    lines: tuple[int, int] | None = None,
    _indexed_source: object = _INDEXED_SOURCE_UNSET,
) -> Evidence:
    repo = settings.repo(hit.repo)
    path = _retrieval_source_path(repo, hit.path)
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


def _symbol_trace_file_intelligence(
    settings: Settings, repo: str, path: str, blob: str, source: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    from .atlas import ATLAS_SCHEMA_VERSION, EXTRACTOR_VERSION, _file_intelligence

    cache = _ACTIVE_RETRIEVAL_CACHE.get()
    generation = settings.atlas_generation
    key = ("symbol-trace-file-intelligence", ATLAS_SCHEMA_VERSION, EXTRACTOR_VERSION,
           generation.identity if generation is not None else "legacy", repo, path, blob)
    if cache is not None and key in cache:
        _, entities, edges = cache[key]
        return entities, edges, True
    _, entities, _, edges = _file_intelligence(repo, path, blob, source)
    if cache is not None and len(cache) < 256:
        retained = [value for item, value in cache.items() if item[:1] == key[:1]]
        remaining = MAX_SYMBOL_TRACE_CACHE_BYTES - sum(value[0] for value in retained)
        if len(retained) < MAX_SYMBOL_TRACE_CACHED_FILES and remaining > 0:
            # Count the complete retained projection (including nested metadata),
            # not just its source. Stream ASCII JSON to avoid a large temporary.
            size = 0
            for fragment in json.JSONEncoder(ensure_ascii=True, separators=(",", ":")).iterencode((key, entities, edges)):
                size += len(fragment)
                if size > remaining:
                    break
            else:
                cache[key] = (size, entities, edges)
    return entities, edges, False


@source_verification_scope()
def trace_symbol(settings: Settings, query: str, repos: Iterable[str] | None = None) -> tuple[list[SearchHit], list[str]]:
    # Standalone traces use the same request-local projection cache as retrieval.
    # Declare intent before the lexical pass; ordinary searches do no caller scan.
    cache = _ACTIVE_RETRIEVAL_CACHE.get()
    token = _ACTIVE_RETRIEVAL_CACHE.set({}) if cache is None else None
    try:
        _ACTIVE_RETRIEVAL_CACHE.get().setdefault(("java-call-queries",), set()).add(query.rsplit(".", 1)[-1])
        return _trace_symbol(settings, query, repos)
    finally:
        if token is not None:
            _ACTIVE_RETRIEVAL_CACHE.reset(token)


def _load_python_binding_sources(
    settings: Settings, generation: Any, keys: list[tuple[str, str]],
    sources: dict[tuple[str, str], str], attempted: set[tuple[str, str]], deadline: float,
) -> dict[tuple[str, str], str]:
    """Accounted pinned batch shared by graph navigation and symbol tracing."""
    from .index import read_generation_files
    from .investigation import MAX_FLOW_SEEDS

    trace = _ACTIVE_RETRIEVAL_TRACE.get()
    missing = [key for key in dict.fromkeys(keys) if key not in sources and key not in attempted]
    missing = missing[:max(0, MAX_FLOW_SEEDS - len(attempted))]
    remaining = MAX_PINNED_HYDRATION_BYTES - sum(len(value.encode('utf-8')) for value in sources.values())
    if generation is not None and missing and remaining > 0 and time.perf_counter() < deadline and (
        trace is None or trace.try_reserve_backend()
    ):
        attempted.update(missing)
        started = time.perf_counter()
        loaded = {}
        try:
            loaded = read_generation_files(settings, generation, missing, max_bytes=remaining,
                max_seconds=min(MAX_PINNED_HYDRATION_SECONDS, max(.001, deadline - time.perf_counter()))) or {}
            sources.update(loaded)
        finally:
            if trace is not None:
                trace.complete_reserved_backend('python-binding-source', (time.perf_counter() - started) * 1000,
                    bytes_scanned=sum(len(value.encode('utf-8')) for value in loaded.values()), files=len(loaded))
    return {key: sources[key] for key in keys if key in sources}


def _find_python_caller_sources(
    settings: Settings, generation: Any, entities: Iterable[dict[str, Any]],
    sources: dict[tuple[str, str], str], deadline: float,
) -> str | None:
    """Find alias candidates in the existing pinned lexical index, not by call name."""
    from pathlib import PurePosixPath
    from .index import query_generation_indexes
    from .investigation import MAX_FLOW_SEEDS

    trace = _ACTIVE_RETRIEVAL_TRACE.get()
    searched = set()
    reason = None
    for entity in entities:
        path = PurePosixPath(entity['path'])
        if path.suffix.lower() != '.py':
            continue
        module = path.parent.name if path.name == '__init__.py' else path.stem
        key = entity['repo'], module
        if not module or key in searched:
            continue
        searched.add(key)
        remaining_files = MAX_FLOW_SEEDS - len(sources)
        remaining_bytes = MAX_PINNED_HYDRATION_BYTES - sum(len(value.encode('utf-8')) for value in sources.values())
        if remaining_files <= 0 or remaining_bytes <= 0 or time.perf_counter() >= deadline or (
            trace is not None and not trace.try_reserve_backend()
        ):
            return 'Python caller candidate scope budget reached'
        started = time.perf_counter()
        loaded: dict[tuple[str, str], str] = {}
        stats: dict[str, object] = {}
        try:
            result = query_generation_indexes(settings, generation,
                [repo for repo in settings.repositories if repo.name == entity['repo']], module,
                max_results=remaining_files, max_candidate_files=remaining_files, max_hits=remaining_files,
                max_bytes=remaining_bytes, max_seconds=min(MAX_PINNED_QUERY_SECONDS, max(.001, deadline - started)),
                stats=stats, python_sources=loaded)
            if result is None:
                reason = 'pinned Python caller index is unavailable'
            else:
                sources.update(loaded)
                if stats.get('budget_exhausted'):
                    reason = 'Python caller candidate ' + str(stats.get('reason') or 'scope') + ' budget reached'
        finally:
            if trace is not None:
                trace.complete_reserved_backend('python-caller-index', (time.perf_counter() - started) * 1000,
                    bytes_scanned=int(stats.get('candidate_bytes') or 0), files=int(stats.get('candidate_files') or 0))
    return reason


def _trace_symbol(settings: Settings, query: str, repos: Iterable[str] | None = None) -> tuple[list[SearchHit], list[str]]:
    from .atlas import AtlasCapacityError, symbol_call_edges
    from .index import _java_method_reference_prefix
    from .investigation import MAX_REFRESH_FILE_BYTES
    from .graph import graph_trace

    if settings.atlas_generation is None and settings.atlas_generation_mode == "current":
        from .catalog import current_generation_ref

        settings = replace(settings, atlas_generation=current_generation_ref(settings))
    scope = list(repos or [])
    name = query.rsplit(".", 1)[-1]
    invocation = re.compile(rf"\b{re.escape(name)}\s*\(")
    identifier = re.compile(rf"(?<![\w$@]){re.escape(name)}(?![\w$])")
    lexical = search(settings, name, scope, fixed=True)
    uses = [hit for hit in lexical if invocation.search(hit.text) or (
        hit.path.lower().endswith(".java") and any(
            _java_method_reference_prefix(hit.text, match.start()) for match in identifier.finditer(hit.text)
        )
    )]
    inbound: list[SearchHit] = []
    # Reuse an already-validated legacy source only within this single trace.
    # A later trace must re-read it before consulting the parser-result cache.
    verified_sources: dict[tuple[str, str], str] = {}
    definitions = [hit for hit in _symbol_definition_hits(settings, name, scope, uses, _verified_sources=verified_sources)
                   if invocation.search(hit.text)]
    definition_keys = {(hit.repo, hit.path, hit.line) for hit in definitions}
    for hit in uses:
        if (hit.repo, hit.path, hit.line) not in definition_keys:
            hit.kind = "lexical reference candidate (call and dispatch not established)"
            hit.score = 96
            inbound.append(hit)
    if settings.atlas_generation is not None:
        references, reference_limit = _java_caller_reference_hits(settings, name, scope)
        seen = definition_keys | {(hit.repo, hit.path, hit.line) for hit in inbound}
        inbound.extend(hit for hit in references if (hit.repo, hit.path, hit.line) not in seen)
        trace = _ACTIVE_RETRIEVAL_TRACE.get()
        if reference_limit and trace is not None:
            trace.fallback_reasons.append(
                f"Java caller verification is incomplete: {reference_limit}; source availability is unknown."
            )
    graph_scope = scope or sorted({hit.repo for hit in definitions + inbound})
    graph_hits, graph_relationships = graph_trace(settings, query, graph_scope)
    # Raw lexical text may be a comment or literal, not a static call edge.
    relationships = list(graph_relationships)
    call_names: set[str] = set()
    unknown_calls: set[str] = set()
    callee_hits: list[SearchHit] = []
    selected = definitions[:5]
    trace = _ACTIVE_RETRIEVAL_TRACE.get()
    if selected and (trace is None or trace.try_reserve_backend()):
        started = time.perf_counter()
        try:
            attempted: set[tuple[str, str]] = set()
            def load_sources(keys):
                return _load_python_binding_sources(settings, settings.atlas_generation, keys,
                                                    verified_sources, attempted, started + 2.0)
            if settings.atlas_generation is not None:
                caller_reason = _find_python_caller_sources(settings, settings.atlas_generation,
                    ({'repo': hit.repo, 'path': hit.path} for hit in selected), verified_sources, started + 2.0)
                if caller_reason and trace is not None:
                    trace.fallback_reasons.append(caller_reason + '; caller coverage is incomplete')
            result = symbol_call_edges(
                settings, settings.atlas_generation,
                ((hit.repo, hit.path, hit.line) for hit in selected), name,
                python_sources=verified_sources, load_python_sources=load_sources,
            )
            if result is None:
                if trace is not None:
                    trace.fallback_reasons.append("symbol_trace_exact_source_fallback")
                # Legacy/unavailable Atlas: reuse the same bounded extractor
                # against exact source, never infer callees from a whole file.
                edges, targets, python_targets = [], {}, {}
                source_settings = settings
                if settings.atlas_generation is not None:
                    source_settings = replace(
                        settings, atlas_generation_mode="pinned",
                        repositories=[replace(repo, source_sha=settings.atlas_generation.snapshots.get(repo.name))
                                      for repo in settings.repositories],
                    )
                by_file: dict[tuple[str, str], list[SearchHit]] = {}
                for hit in selected:
                    by_file.setdefault((hit.repo, hit.path), []).append(hit)
                for (repo, path), hits in by_file.items():
                    if time.perf_counter() - started >= 2.0:
                        if trace is not None:
                            trace.fallback_reasons.append("symbol_trace_time_budget")
                        break
                    source = verified_sources.get((repo, path))
                    reused_source = source is not None
                    if not reused_source and trace is not None and not trace.try_reserve_backend():
                        trace.fallback_reasons.append("symbol_trace_physical_budget")
                        break
                    read_started = time.perf_counter()
                    parsed_cache_hit = False
                    try:
                        if source is None:
                            evidence = read_source(source_settings, hits[0], full=True)
                            source = evidence.verification_content or evidence.content
                        raw = source.encode("utf-8")
                        if trace is not None and not reused_source:
                            trace.bytes_read += len(raw)
                        if len(raw) > MAX_REFRESH_FILE_BYTES:
                            if trace is not None:
                                trace.fallback_reasons.append("symbol_trace_source_byte_budget")
                            continue
                        entities, extracted, parsed_cache_hit = _symbol_trace_file_intelligence(
                            source_settings, repo, path, hashlib.sha256(raw).hexdigest(), source,
                        )
                    except (BrainError, AtlasCapacityError, OSError):
                        if trace is not None:
                            trace.fallback_reasons.append("symbol_trace_source_unavailable")
                        continue
                    finally:
                        if trace is not None and not reused_source:
                            trace.complete_reserved_backend("symbol_trace_source", (time.perf_counter() - read_started) * 1000,
                                                            cache_hit=parsed_cache_hit)
                    owners = {item["entity_id"] for item in entities if item["simple_name"] == name
                              and item["line_start"] in {hit.line for hit in hits}}
                    file_edges = [item for item in extracted if item["edge_type"] == "CALLS" and item["source_id"] in owners]
                    edges.extend(file_edges)
                    targets.update({item["entity_id"]: item for item in entities})
                    if path.lower().endswith('.py'):
                        from .python_bindings import source_bindings
                        bindings = source_bindings(source)[0]
                        for edge in file_edges:
                            owner = targets.get(edge['source_id'])
                            if owner is None or (edge['repo'], edge['path']) != (repo, path):
                                continue
                            metadata = edge['metadata']
                            binding = bindings.get((owner['line_start'], edge['line_start'],
                                                    metadata.get('target_name'), metadata.get('receiver')))
                            matches = [item for item in entities if binding and binding[0] == 'definition'
                                       and item['simple_name'] == binding[3] and item['line_start'] == binding[4]]
                            if len(matches) == 1:
                                python_targets[edge['edge_id']] = matches[0]
                result = {"edges": edges[:80], "targets": targets, "python_targets": python_targets,
                          "truncated": len(edges) > 80}
            if result.get("truncated") and trace is not None:
                trace.fallback_reasons.append("symbol_trace_edge_budget")
            inbound.extend(SearchHit(item['repo'], item['path'], item['line_start'], item['signature'],
                'caller candidate', 96, ['generation-validated Python caller binding'])
                for item in result.get('callers') or [])
            for edge in result["edges"]:
                metadata = edge["metadata"]
                target_name = str(metadata.get("target_name") or "")
                receiver = str(metadata.get("receiver") or "")
                python_call = edge['path'].lower().endswith('.py')
                target = (result.get('python_targets', {}).get(edge['edge_id']) if python_call
                          else result["targets"].get(edge["target_id"]))
                bound = target is not None and (python_call or metadata.get('resolved') and receiver in {'', 'this'})
                if target_name:
                    (call_names if bound else unknown_calls).add(
                        (receiver + "." if receiver else "") + target_name)
                if bound:
                    callee_hits.append(SearchHit(
                        str(target["repo"]), str(target["path"]), int(target["line_start"]),
                        str(target["signature"]), "callee candidate", 96, ["scoped static call candidate"],
                    ))
        finally:
            if trace is not None:
                trace.complete_reserved_backend("symbol_trace", (time.perf_counter() - started) * 1000)
    elif selected and trace is not None:
        trace.fallback_reasons.append("symbol_trace_physical_budget")
    relationships.extend(f"{query}  CALLS  {called}" for called in sorted(call_names)[:80])
    relationships.extend(f"{query}  CALL CANDIDATE (binding unavailable)  {called}" for called in sorted(unknown_calls)[:80])
    combined = graph_hits + definitions + inbound + callee_hits
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


def _request_document(text: str) -> str | None:
    """Select the latest structural directive, shared by routing and parsing."""
    if len(text) > MAX_REQUEST_TEXT_CHARS or len(text.encode("utf-8")) > MAX_REQUEST_TEXT_BYTES:
        raise BrainError(f"Project Brain request exceeds the {MAX_REQUEST_TEXT_CHARS:,}-character input limit")
    text = text.lstrip("\ufeff \t\r\n")
    candidates: list[tuple[int, str]] = []
    fence_pattern = re.compile(r"(?ms)^[ \t]*(`{3,}|~{3,})[^\r\n]*\r?\n(.*?)^[ \t]*\1[ \t]*\r?$")

    def is_request(value: str) -> bool:
        value = re.sub(r"\A(?:(?:[ \t]*#[^\r\n]*|[ \t]*---[ \t]*|[ \t]*)\r?\n)+", "", value).lstrip()
        return bool(re.match(r"(?:CONTEXT_REQUEST|INVESTIGATION_REQUEST)\s*:", value) or (
            value.startswith("{") and re.search(r'"(?:CONTEXT_REQUEST|INVESTIGATION_REQUEST|objective)"\s*:', value)
        ))

    # Mask whole fences so markers inside JSON strings or source examples do
    # not supersede their containing document.
    outside = list(text)
    for match in fence_pattern.finditer(text):
        body = match.group(2).strip()
        if is_request(body):
            candidates.append((match.start(), body))
        outside[match.start():match.end()] = " " * (match.end() - match.start())
    markers = list(re.finditer(r"(?m)^[ \t]*(?:(?:CONTEXT_REQUEST|INVESTIGATION_REQUEST)[ \t]*:|\{)", "".join(outside)))
    consumed = 0
    for index, marker in enumerate(markers):
        if marker.start() < consumed:
            continue
        payload = text[marker.start():].lstrip()
        if payload.startswith("{"):
            try:
                _, end = json.JSONDecoder().raw_decode(payload)
            except (ValueError, RecursionError):
                end = len(payload)
            consumed = marker.end() - 1 + end
            payload = payload[:end]
        else:
            indent = len(marker.group()) - len(marker.group().lstrip())
            end = next((following.start() for following in markers[index + 1:]
                        if len(following.group()) - len(following.group().lstrip()) <= indent), len(text))
            # Nested JSON or protocol-looking text in a YAML block scalar is
            # data belonging to this document, never a newer command.
            consumed = end
            payload = text[marker.start():end].strip()
            payload = re.split(r"(?m)^[ \t]*(?:`{3,}|~{3,})", payload, maxsplit=1)[0].strip()
        if is_request(payload):
            candidates.append((marker.start(), payload))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _request_body(text: str) -> dict[str, Any]:
    """Extract a versioned request from a whole chat response or request file."""
    # Windows PowerShell 5.1 writes ``Set-Content -Encoding UTF8`` files with
    # a UTF-8 BOM.  Treat that transport marker as encoding metadata, not as
    # part of the protocol document, for JSON, YAML, stdin, and clipboard use.
    if not text.lstrip("\ufeff \t\r\n"):
        raise BrainError("The AI response is empty")
    stripped = _request_document(text)
    if stripped is None:
        raise BrainError(
            "Input does not contain CONTEXT_REQUEST: or INVESTIGATION_REQUEST:. Copy the AI's complete response, "
            "or ask it to return one Project Brain request block."
        )
    loaded: Any = None
    if stripped.startswith("{"):
        try:
            loaded = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise BrainError(f"Invalid CONTEXT_REQUEST JSON: line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
        except RecursionError as exc:
            raise BrainError("Project Brain JSON nesting is too deep") from exc

    if loaded is None:
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError:
            try:
                loaded = simple_yaml_load(stripped)
            except RecursionError as exc:
                raise BrainError("Project Brain YAML nesting is too deep") from exc
        else:
            try:
                loaded = yaml.safe_load(stripped)
            except Exception as exc:
                raise BrainError(f"Invalid CONTEXT_REQUEST YAML: {exc}") from exc

    if isinstance(loaded, dict) and not ({"CONTEXT_REQUEST", "INVESTIGATION_REQUEST"} & set(loaded)) and "objective" in loaded:
        wrapper = "INVESTIGATION_REQUEST" if loaded.get("version") in (4, 5) else "CONTEXT_REQUEST"
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
    if not isinstance(version, int) or isinstance(version, bool) or version not in {1, 2, 3, 4, 5}:
        raise BrainError(f"Unsupported Project Brain request version {version!r}; this build supports versions 1, 2, 3, 4, and 5")
    if version in {4, 5} and wrapper != "INVESTIGATION_REQUEST":
        raise BrainError(f"version {version} must use the INVESTIGATION_REQUEST wrapper")
    request["version"] = version
    allowed = {
        1: {"version", "objective", "searches", "paths", "symbols", "files", "history", "expand"},
        2: {"version", "objective", "searches", "paths", "symbols", "files", "history", "expand"},
        3: {"version", "objective", "hints", "coverage", "expand"},
        4: {"version", "objective", "runtime_facts", "hypotheses", "required", "resolve", "base_context_id", "checkpoint"},
        5: {"version", "mode", "objective", "runtime_facts", "hypotheses", "required", "resolve", "anchors", "files", "base_context_id", "checkpoint", "wave"},
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
            if wave is not None and (not isinstance(wave, int) or isinstance(wave, bool) or wave < 1):
                raise BrainError("wave must be a positive integer; omit it to use the ticket's next wave")
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
        from .retrieval.planner import objective_terms, requested_symbol_relations
        from .investigation import _qualified_symbol_queries

        exact_anchor_terms = [
            str(item.get("value") or "")
            for item in request.get("anchors") or [] if isinstance(item, dict) and item.get("value")
        ] if version == 5 else []
        qualified_symbols = set(_qualified_symbol_queries(request.get("anchors") or []))
        qualified_parts = {term.casefold() for value in qualified_symbols for term in objective_terms(value, limit=8)}
        derived_anchor_terms = [
            term
            for value in exact_anchor_terms if value not in qualified_symbols
            for term in objective_terms(value, limit=4)
            if term != value
        ]
        explicit_terms = list(dict.fromkeys([*exact_anchor_terms, *request["resolve"]]))
        derived_terms = [term for term in dict.fromkeys([
            *derived_anchor_terms,
            *(term for term in objective_terms(objective, limit=8, allow_prose=not qualified_symbols)
              if term.casefold() not in qualified_parts),
        ]) if term not in explicit_terms]
        # Parsing preserves explicit intent; only the execution planner defers it.
        # Generated discovery terms retain their small independent work bound.
        derived_limit = 12 if version == 5 else max(0, 12 - len(explicit_terms))
        resolve_terms = [*explicit_terms, *derived_terms[:derived_limit]]
        requested_files = request.get("files", []) if version == 5 else []
        if not isinstance(requested_files, list) or len(requested_files) > MAX_REQUEST_ITEMS:
            raise BrainError(f"files must be a list of at most {MAX_REQUEST_ITEMS} items")
        if requested_files and not exact_anchor_terms and not request["resolve"]:
            resolve_terms = []  # Known files need exact reads, not another discovery pass.
        request["searches"] = [{"query": value, "repos": []} for value in resolve_terms]
        request["paths"] = []
        request["symbols"] = []
        request["files"] = requested_files
        request["history"] = [
            {"query": value, "repos": []} for value in request["resolve"]
            if any(token in value.lower() for token in ("history", "commit", "change", "ticket"))
        ][:4]
        if version == 5 and request.get("mode") == "history" and not requested_files:
            request["history"] = [{"query": value, "repos": []} for value in objective_terms(
                " ".join([*exact_anchor_terms, *request["resolve"], objective]), limit=2,
            )]
        request["expand"] = []
        required_text = " ".join(request["required"]).lower()
        request["coverage"] = {
            "production": "required",
            "tests": "required" if request.get("mode") == "test_surface" or "test" in required_text else "auto",
            "relationships": "required" if requested_symbol_relations(request) or any(
                value in required_text for value in ("flow", "integration", "relationship", "graph")
            ) else "auto",
            "configuration": "required" if "config" in required_text else "auto",
            "history": "required" if request.get("mode") == "history" or any(value in required_text for value in ("history", "change", "commit")) else "auto",
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
            item_limit = MAX_REQUEST_ITEMS * 2 + 12 if version == 5 and key == "searches" else MAX_REQUEST_ITEMS
            if len(value) > item_limit:
                raise BrainError(f"{key} exceeds the {item_limit}-item limit")
            request[key] = value
    return request


def parse_context_request(text: str) -> dict[str, Any]:
    request = _request_body(text)
    for index, item in enumerate(request["searches"]):
        if not isinstance(item, dict) or not str(item.get("query") or "").strip():
            raise BrainError(f"searches[{index}].query is required")
        if set(item) - {"query", "repos"}:
            raise BrainError(f"searches[{index}] has unknown keys")
        query_limit = 1_000 if request["version"] == 5 else 500
        if len(str(item["query"])) > query_limit:
            raise BrainError(f"searches[{index}].query exceeds {query_limit} characters")
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
        if request["version"] == 5:
            if (
                not isinstance(item["repo"], str) or len(item["repo"].encode("utf-8")) > 200
                or not isinstance(item["path"], str) or len(item["path"].encode("utf-8")) > 1_000
                or item["path"].startswith("/") or "\\" in item["path"] or re.match(r"^[A-Za-z]:", item["path"])
                or any(ord(character) < 32 or ord(character) == 127 for character in item["path"])
            ):
                raise BrainError(f"files[{index}] requires a bounded repository name and repository-relative POSIX path")
            if "lines" in item and (not isinstance(item["lines"], str) or len(item["lines"]) > 32):
                raise BrainError(f"files[{index}].lines must be a bounded start-end string")
        if item.get("lines") and not re.fullmatch(r"\s*\d+\s*[-:]\s*\d+\s*", str(item["lines"])):
            raise BrainError(f"files[{index}].lines must look like 10-40")
        if item.get("lines"):
            start, end = (int(value) for value in re.split(r"[-:]", str(item["lines"])))
            if start < 1 or end < start or (
                end > MAX_SOURCE_FILE_BYTES if request["version"] == 5 else end - start > 2_000
            ):
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
        else {key: request[key] for key in ("version", "mode", "objective", "runtime_facts", "hypotheses", "required", "resolve", "anchors", "files", "base_context_id", "checkpoint", "wave") if key in request and (key != "files" or request[key])}
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
        "Return only one minimal fenced json block with a valid JSON object. Use ordinary double quotes, escaped strings, no comments and no trailing commas. State the repository fact to establish; do not invent repository names or enumerate command matrices.\n\n"
        "Legacy CONTEXT_REQUEST forms with version: 1, version: 2, or version: 3 and INVESTIGATION_REQUEST version: 4 remain supported; new repairs use investigation protocol v5.\n\n"
        "Preserve this ticket's protocol and actual latest base_context_id when supplied; omit wave and never invent a context ID.\n\n"
        "```json\n"
        + json.dumps({"INVESTIGATION_REQUEST": {"version": PROTOCOL_VERSION, "mode": "root_cause",
                      "objective": "State the next repository fact that must be established."}}, indent=2) + "\n"
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


def _requested_symbol_ranges(
    settings: Settings, anchors: dict[str, dict[str, Any]],
    *, definitions: dict[tuple[str, str, int], set[str]] | None = None,
    enclosing: Iterable[tuple[str, str, int]] = (),
) -> tuple[list[dict[str, Any]], str | None]:
    """Revalidate a bounded batch of resolved symbols; ranges remain navigation, not source."""
    from .atlas import ATLAS_SCHEMA_VERSION, _valid_generation_entities
    from .catalog import connect
    from .investigation import GENERATION_INTELLIGENCE_INPUT_SCHEMA_VERSION

    generation = settings.atlas_generation
    if generation is None or generation.component("hierarchy").get("status") != "ready" or (
        generation.component("hierarchy").get("schema_version") != ATLAS_SCHEMA_VERSION
    ):
        return [], "pinned hierarchy unavailable"
    connection = None
    deadline = time.monotonic() + .25
    try:
        connection = connect(settings)
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1_000)
        identifiers = list(anchors)[:MAX_REQUEST_ITEMS]
        definitions = definitions or {}
        locations = [(repo, path, line, name) for (repo, path, line), names in definitions.items()
                     for name in sorted(names)]
        incomplete = len(locations) > MAX_REQUEST_ITEMS
        locations = locations[:MAX_REQUEST_ITEMS]
        definition_ids: dict[tuple[str, str, int, str], str] = {}
        if locations:
            # Recover bodies only at already-selected declaration locations.
            # A same-name owner elsewhere is not a resolved dispatch target.
            slots = ",".join("(?,?,?,?)" for _ in locations)
            rows = connection.execute(
                f"WITH requested(repo,path,line,name) AS (VALUES {slots}) "
                "SELECT r.repo,r.path,r.line,r.name,e.entity_id FROM requested r "
                "CROSS JOIN atlas_entities e ON e.entity_id IN ("
                "SELECT a.entity_id FROM atlas_entities a INDEXED BY atlas_entities_repo_path "
                "CROSS JOIN generation_entities g ON g.entity_id=a.entity_id AND g.generation=? "
                "CROSS JOIN generation_intelligence_files f ON f.generation=g.generation "
                "AND f.repo=a.repo AND f.path=a.path AND f.blob_sha=a.blob_sha AND f.schema_version=? "
                "WHERE a.repo=r.repo AND a.path=r.path AND a.line_start=r.line "
                "AND a.simple_name=r.name AND a.kind!='file' LIMIT 2)",
                (*[value for location in locations for value in location], generation.generation,
                 GENERATION_INTELLIGENCE_INPUT_SCHEMA_VERSION),
            ).fetchall()
            # A crowded/ambiguous location must not consume another one's
            # lookup budget, or be mistaken for a uniquely identified body.
            matches: dict[tuple[str, str, int, str], list[str]] = {}
            for row in rows:
                matches.setdefault(tuple(row[:4]), []).append(str(row[4]))
            definition_ids = {location: values[0] for location, values in matches.items() if len(values) == 1}
            incomplete = incomplete or set(locations) != set(definition_ids)
            identifiers = list(dict.fromkeys([*identifiers, *definition_ids.values()]))
            incomplete = incomplete or len(identifiers) > MAX_REQUEST_ITEMS
            identifiers = identifiers[:MAX_REQUEST_ITEMS]
        enclosing_points = list(dict.fromkeys(enclosing))[:MAX_REQUEST_ITEMS]
        enclosing_ids: dict[tuple[str, str, int], list[str]] = {}
        if enclosing_points:
            # Literal loci are not declarations. Seek only their pinned file's
            # two narrowest callable scopes; tied/overlapping scopes stay windows.
            rows = connection.execute(
                f"WITH requested(repo,path,line) AS (VALUES {','.join('(?,?,?)' for _ in enclosing_points)}) "
                "SELECT r.repo,r.path,r.line,e.entity_id FROM requested r "
                "CROSS JOIN atlas_entities e ON e.entity_id IN ("
                "SELECT a.entity_id FROM atlas_entities a INDEXED BY atlas_entities_repo_path "
                "CROSS JOIN generation_entities g ON g.entity_id=a.entity_id AND g.generation=? "
                "CROSS JOIN generation_intelligence_files f ON f.generation=g.generation "
                "AND f.repo=a.repo AND f.path=a.path AND f.blob_sha=a.blob_sha AND f.schema_version=? "
                "WHERE a.repo=r.repo AND a.path=r.path AND a.line_start<=r.line AND a.line_end>=r.line "
                "AND a.language IN ('python','java') AND a.kind IN ('function','method','constructor','test') "
                "AND a.signature NOT LIKE 'class %' "
                "ORDER BY a.line_end-a.line_start,a.line_start DESC,a.entity_id LIMIT 2)",
                (*[value for point in enclosing_points for value in point], generation.generation,
                 GENERATION_INTELLIGENCE_INPUT_SCHEMA_VERSION),
            ).fetchall()
            for row in rows:
                enclosing_ids.setdefault(tuple(row[:3]), []).append(str(row[3]))
            identifiers = list(dict.fromkeys([*identifiers, *(key for values in enclosing_ids.values() for key in values)]))
        entities = _valid_generation_entities(connection, generation.generation, identifiers)
        invalid = set(identifiers) - set(entities) | (set(anchors) - set(entities))
        invalid.update(key for key, entity in entities.items() if key in anchors and
            (entity["repo"], entity["path"], entity["line_start"], entity["qualified_name"], entity["module_id"]) != (
                anchors[key]["repo"], anchors[key]["path"], anchors[key]["line"], anchors[key]["value"], anchors[key]["module_id"]
            ))
        if anchors:
            # Flow targets did not pass the runtime-anchor resolver. Validate
            # their file membership in one bounded batch, never per callee.
            members = {str(row[0]) for row in connection.execute(
                "SELECT e.entity_id FROM atlas_entities e CROSS JOIN generation_intelligence_files f "
                "ON f.generation=? AND f.repo=e.repo AND f.path=e.path AND f.blob_sha=e.blob_sha "
                f"AND f.schema_version=? WHERE e.entity_id IN ({','.join('?' for _ in anchors)})",
                (generation.generation, GENERATION_INTELLIGENCE_INPUT_SCHEMA_VERSION, *anchors),
            )}
            invalid.update(set(anchors) - members)
        parent_ids = {item['parent_entity_id'] for item in entities.values() if item['parent_entity_id']} - set(entities)
        parents = {**entities, **(_valid_generation_entities(connection, generation.generation, parent_ids) if parent_ids else {})}
        invalid.update(key for key, item in entities.items() if item['parent_entity_id'] and (
            item['parent_entity_id'] not in parents or any(
                item[key] != parents[item['parent_entity_id']][key] for key in ('repo', 'path', 'module_id', 'blob_sha'))
            or not parents[item['parent_entity_id']]['line_start'] <= item['line_start'] <= item['line_end'] <= parents[item['parent_entity_id']]['line_end']
        ))
        invalid.update(key for location, key in definition_ids.items() if key in entities and
                       (entities[key]["repo"], entities[key]["path"], entities[key]["line_start"], entities[key]["simple_name"]) != location)
        # One unavailable derived callee must not erase a valid root body.
        # Keep only independently validated ranges and report partial identity.
        entities = {key: item for key, item in entities.items() if key not in invalid}
        enclosing_matches: dict[str, list[int]] = {}
        enclosing_incomplete = False
        if enclosing_points:
            for point in enclosing_points:
                if invalid.intersection(enclosing_ids.get(point, [])):
                    enclosing_incomplete = True
                    continue  # Do not promote an outer scope when its narrower candidate is damaged.
                scopes = sorted((entities[key] for key in enclosing_ids.get(point, []) if key in entities),
                                key=lambda item: (item['line_end'] - item['line_start'], -item['line_start'], item['entity_id']))
                if not scopes:
                    continue  # A class field/configuration literal need not be inside a callable.
                child = scopes[0]
                parent = parents.get(child['parent_entity_id']) if child['parent_entity_id'] else None
                # Java boundary rows can also contain adjacent fields/callables.
                # Without columns, even a one-line method cannot own that locus.
                if ((child['repo'], child['path']) != point[:2]
                    or not child['line_start'] <= point[2] <= child['line_end']
                    or (child['language'] == 'java' and point[2] in (child['line_start'], child['line_end']))
                    or (child['parent_entity_id'] and (parent is None or any(
                        child[key] != parent[key] for key in ('repo', 'path', 'module_id', 'blob_sha'))
                        or not parent['line_start'] <= child['line_start'] <= child['line_end'] <= parent['line_end']))
                    or (len(scopes) > 1 and (child['parent_entity_id'] != scopes[1]['entity_id']
                        or (child['line_start'], child['line_end']) == (scopes[1]['line_start'], scopes[1]['line_end'])))):
                    enclosing_incomplete = True
                    continue
                enclosing_matches.setdefault(child['entity_id'], []).append(point[2])
            requested_ids = list(dict.fromkeys([*anchors, *definition_ids.values(), *enclosing_matches]))
            enclosing_incomplete = enclosing_incomplete or len(requested_ids) > MAX_REQUEST_ITEMS
            entities = {key: entities[key] for key in requested_ids[:MAX_REQUEST_ITEMS] if key in entities}
            for key, lines in enclosing_matches.items():
                if key in entities:
                    entities[key]['enclosing_match_lines'] = lines
        # Generic-language entity ranges are fixed-window estimates. Never call
        # those complete methods. Python AST and Java brace ranges are reusable.
        ranges = [entity for entity in entities.values() if entity["language"] in {"python", "java"}
                  and entity["kind"] in {"function", "method", "constructor", "class", "interface", "type", "test"}]
        matched = {(item["repo"], item["path"], item["line_start"], item["simple_name"]) for item in ranges}
        if invalid:
            return ranges, "some pinned symbol file/owner identity unavailable; only independently validated ranges returned"
        if incomplete or set(locations) - matched:
            return ranges, "some requested definitions have no validated exact range within the lookup budget; their source windows may be incomplete"
        if enclosing_incomplete:
            return ranges, "some enclosing callable ranges are ambiguous or unavailable; exact-match windows remain authoritative"
        return ranges, ("some symbols have only estimated ranges; their source windows may be incomplete"
                        if len(ranges) != len(entities) else None)
    except (sqlite3.Error, OSError):
        return [], "pinned symbol lookup unavailable or over budget"
    finally:
        if connection is not None:
            connection.close()


def _source_line_offsets(content: str) -> array:
    """Compact boundaries for the authoritative reader's normalized lines."""
    offsets = array("Q", [0])
    offsets.extend(match.end() for match in re.finditer("\n", content))
    offsets.append(len(content) + 1)
    return offsets


def _requested_file_page(
    settings: Settings, item: dict[str, Any], *, max_bytes: int,
    source_cache: dict[tuple[str, str], tuple[Evidence, int, array]] | None = None,
    _indexed_source: object = _INDEXED_SOURCE_UNSET,
    _source_anchor_line: int | None = None,
) -> tuple[Evidence | None, dict[str, Any]]:
    """Read a bounded, whole-line page from the existing authoritative reader."""
    repo, path = str(item["repo"]), str(item["path"])
    result: dict[str, Any] = {"repo": repo, "path": path, "requested_lines": item.get("lines") or "all"}
    cached = source_cache.get((repo, path)) if source_cache is not None else None
    if cached is not None:
        source, _, offsets = cached
        result["source_bytes_read"] = 0
    else:
        try:
            source = read_source(settings, SearchHit(repo, path, 1, "", "requested file", 100, ["direct file request"]),
                                 full=True, _indexed_source=_indexed_source)
        except BrainError as error:
            return None, {**result, "status": "unavailable", "reason": str(error)}
        result["source_bytes_read"] = len(source.content.encode("utf-8"))
        offsets = _source_line_offsets(source.content) if source.line_end else array("Q", [0])
        retained_bytes = result["source_bytes_read"] + len((source.verification_content or "").encode("utf-8")) + sys.getsizeof(offsets)
        if source_cache is not None and retained_bytes + sum(value[1] for value in source_cache.values()) <= MAX_PINNED_HYDRATION_BYTES:
            source_cache[(repo, path)] = source, retained_bytes, offsets
    # Page slicing and provenance must not mutate the full verified cached file.
    source = replace(source, found_by=list(source.found_by))
    total = len(offsets) - 1
    start, end = (1, total) if not item.get("lines") else tuple(int(value) for value in re.split(r"[-:]", item["lines"]))
    result["total_lines"] = total
    if not total and not item.get("lines"):
        return None, {**result, "status": "empty", "reason": "The pinned file exists and is empty."}
    if start > total or end > total:
        return None, {**result, "status": "out_of_range", "reason": f"The pinned file contains {total} lines; requested {start}-{end}."}
    if _source_anchor_line is not None:
        # Extend the old source window; do not drop annotations or fused call
        # sites that were already visible when filling in the method's tail.
        requested_start, requested_end = start, end
        radius = max(10, settings.source_window_lines // 2)
        start, end = min(start, max(1, _source_anchor_line - radius)), max(end, min(total, _source_anchor_line + radius))
        if total <= settings.full_file_lines and len(source.content.encode("utf-8")) <= max_bytes:
            start, end = 1, total
        window_start = offsets[start - 1]
        window = source.content[window_start:min(offsets[end] - 1, window_start + max_bytes + 1)]
        if end - start >= 2_000 or len(window.encode("utf-8")) > max_bytes:
            # Optional surrounding code must not crowd a complete requested
            # method off the page. Inspect at most one page worth of text.
            first, last = offsets[requested_start - 1], offsets[requested_end] - 1
            if (requested_end - requested_start < 2_000 and last - first <= max_bytes
                    and len(source.content[first:last].encode("utf-8")) <= max_bytes):
                start, end = requested_start, requested_end
    page: list[str] = []
    used = 0
    for number in range(start - 1, min(end, start + 1_999)):
        row = source.content[offsets[number]:offsets[number + 1] - 1]
        size = len(row.encode("utf-8")) + (1 if page else 0)
        if used + size > max_bytes:
            break
        page.append(row)
        used += size
    if not page:
        return None, {**result, "status": "blocked", "reason": f"Line {start} exceeds this request's {max_bytes}-byte source-page budget; no partial line was emitted."}
    last = start + len(page) - 1
    next_lines = f"{last + 1}-{end}" if last < end else None
    status = "partial" if next_lines else "complete_file" if start == 1 and last == total else "complete_range"
    result.update(status=status, returned_lines=f"{start}-{last}", next_lines=next_lines)
    source.line_start, source.line_end, source.content = start, last, "\n".join(page)
    source.found_by.append(
        f"pinned file read: {status}; total lines {total}; requested {start}-{end}; returned {start}-{last}"
        + (f"; continue the same files entry with lines={next_lines}" if next_lines else "")
    )
    return source, result


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


@source_verification_scope()
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
    from .retrieval.planner import SOURCE_SYMBOL_RE, route_repositories

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
        if compiled_plan.protocol_version != 5:
            _ACTIVE_RETRIEVAL_CACHE.get()[("java-call-queries",)] = {
                item.value.rsplit(".", 1)[-1] for item in compiled_plan.operations
                if item.kind == "symbol" and {"callers", "callees"}.intersection(item.includes)
            }
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

        # The plan protects explicit source requests. Execute them before optional
        # discovery/model work can consume the deadline, including expanded v5
        # candidate requests, without reading duplicate operations twice.
        first_verified_evidence_ms: float | None = None
        direct_started = time.perf_counter()
        file_values = {item.value for item in compiled_plan.operations if item.kind == "file"}
        file_reads: list[dict[str, Any]] = []
        # Scoped to this request only. Cache capacity never limits which files
        # may be read; uncached files still use the normal physical budget.
        direct_sources: dict[tuple[str, str], tuple[Evidence, int, array]] | None = (
            {} if settings.atlas_generation_mode == "pinned" else None
        )
        seen_files: set[str] = set()
        page_bytes = min(64_000, max(1, settings.hard_context_chars // 3 // max(1, len(file_values))))
        for item in request["files"]:
            value = f"{item['repo']}:{item['path']}" + (f":{item['lines']}" if item.get("lines") else "")
            if value in seen_files:
                continue
            seen_files.add(value)
            if time_budget_exhausted() or value not in file_values:
                if request.get("version") == 5:
                    bundle.unresolved.append(f"File read deferred by the request budget: {item['repo']}:{item['path']}; request it again in a focused files request.")
                continue
            file_values.remove(value)
            if request.get("version") == 5:
                cached_source = direct_sources is not None and (str(item["repo"]), str(item["path"])) in direct_sources
                if cached_source:
                    trace.add_cache_hit()
                elif not trace.try_reserve_backend():
                    bundle.unresolved.append(f"File read deferred by the physical-operation budget: {item['repo']}:{item['path']}.")
                    continue
                evidence, report = _requested_file_page(settings, item, max_bytes=page_bytes, source_cache=direct_sources)
                file_reads.append(report)
                trace.bytes_read += int(report.get("source_bytes_read") or 0)
                if not evidence:
                    bundle.unresolved.append(f"File read {report['status']}: {item['repo']}:{item['path']}: {report['reason']}")
                    continue
            else:
                evidence = _direct_file(settings, item)
            if evidence:
                bundle.evidence.append(evidence)
                if request.get("version") != 5:
                    trace.bytes_read += len(evidence.content.encode("utf-8", errors="replace"))
                if first_verified_evidence_ms is None:
                    first_verified_evidence_ms = (time.perf_counter() - started) * 1000
            else:
                bundle.unresolved.append(f"Requested file `{item['repo']}:{item['path']}` was not found")
        trace.add_stage("source_hydration_ms", (time.perf_counter() - direct_started) * 1000)
        files_incomplete = bool(bundle.unresolved) or any(report.get("next_lines") for report in file_reads)
        if request.get("version") == 5 and files_incomplete and trace.stop_reason == "coverage_satisfied":
            trace.stop_reason = "requested_files_incomplete"

        if request.get("version") == 5 and request["files"] and not any(
            request.get(key) for key in ("searches", "paths", "symbols", "history", "expand")
        ) and not include_diff:
            # Keep ticket runtime/lineage in create_context; only bypass optional
            # discovery and models for an exact, already-addressed source read.
            trace.hydrated_regions = len(bundle.evidence)
            if trace.stop_reason == "coverage_satisfied":
                trace.stop_reason = "requested_files_read"
            scope = sorted({str(item["repo"]) for item in request["files"]})
            trace.initial_repo_scope = trace.final_repo_scope = scope
            bundle.metrics = {
                "total_ms": round((time.perf_counter() - started) * 1000, 3),
                "source_hydration_ms": trace.stage_ms["source_hydration_ms"],
                "physical_backend_operations": trace.physical_backend_operations,
                "bytes_read": trace.bytes_read, "hydrated_regions": len(bundle.evidence),
                "candidates": 0, "raw_candidates": 0, "rerank_input_count": 0,
                "repo_scope_count": len(scope), "repo_scope_limit": len(scope), "semantic_repo_count": 0,
            }
            bundle.trace = {**trace.as_dict(), "file_reads": file_reads, "direct_files_only": True}
            emit("complete", evidence_count=len(bundle.evidence), physical_operations_completed=trace.physical_backend_operations)
            return bundle

        candidates: list[SearchHit] = []
        from .atlas import route as route_atlas

        if settings.atlas_generation_mode == "pinned" and bundle.atlas_generation is not None:
            # Candidate metadata is not deliverable evidence. Leave capacity
            # for one pinned-source batch unless direct reads already buffered
            # source, and, for symbols, exact range lookup.
            # Direct file requests above retain their full original budget.
            symbol_source = bool(request["symbols"]) or any(
                item.get("kind") == "symbol" for item in request.get("anchors") or []
            )
            trace._set_backend_headroom(min(int(not direct_sources) + int(symbol_source), max(0, trace.physical_budget_remaining - 1)))

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
        exact_symbol_scope = bool(atlas_route.get("qualified_symbols_only")) and not any(
            (request.get("coverage") or {}).get(key) == "required" for key in ("configuration", "history")
        )
        trace.add_stage("atlas_route_ms", atlas_route_ms)
        first_repo_ms = (time.perf_counter() - started) * 1000 if atlas_route.get("repos") else None
        first_entity_ms = (time.perf_counter() - started) * 1000 if atlas_route.get("candidates") else None
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
        if request.get("version") == 5 and not time_budget_exhausted():
            config_hits, config_limit = _configuration_prefix_hits(settings, request.get("anchors") or [])
            candidates.extend(config_hits)
            if config_hits:
                bundle.warnings.append(
                    "Configuration prefix declarations are candidate owners: member binding and active configuration "
                    "precedence are not established by a prefix match. Inspect the pinned declarations and members."
                )
            if config_limit:
                bundle.unresolved.append(f"Configuration prefix lookup is incomplete: {config_limit}")
        emit("global_discovery", requested_operations=trace.requested_operations, effective_operations=trace.effective_operations)
        discovery_started = time.perf_counter()
        from .investigation import _qualified_symbol_queries, resolve_runtime_anchors
        from .retrieval.ranker import _SOURCE_CHANNELS

        qualified_queries = set(_qualified_symbol_queries(request.get("anchors") or []))
        explicit_symbol_queries = {item["value"] for item in request.get("anchors") or [] if item.get("kind") == "symbol"}
        explicit_anchor_queries = {item["value"] for item in request.get("anchors") or []} | set(request.get("resolve") or [])
        literal_relation_queries = {item['value'] for item in request.get('anchors') or []
                                    if item.get('kind') in {'log_literal', 'error_code', 'exception'}}
        relation_loci: dict[str, set[tuple[str, str, int]]] = {}
        incomplete_relation_loci: set[str] = set()
        search_operations = [item for item in compiled_plan.operations if item.kind == "search" or (
            item.kind == "symbol" and "definition" in item.includes and item.value in qualified_queries
        )]
        if exact_symbol_scope:
            # The router has validated every generated term as an identity
            # alias in a source-only objective. Its canonical definition read
            # already covers those terms; preserve all explicit requests.
            search_operations = [item for item in search_operations
                                 if item.value in qualified_queries or item.value in explicit_anchor_queries]
        lexical_anchors = {
            value: index for index, value in enumerate(dict.fromkeys(item.value.casefold() for item in search_operations), 1)
        }
        lexical_started = time.perf_counter()
        requested_symbols: dict[str, dict[str, Any]] = {}
        symbol_query_owners: dict[str, str] = {}
        requested_definition_names: dict[tuple[str, str, int], set[str]] = {}
        requested_anchor_locations: set[tuple[str, str, int]] = set()

        def protect_definitions(hits: list[SearchHit], name: str) -> bool:
            found = False
            for hit in hits:
                if "definition" in hit.kind.casefold():
                    requested_definition_names.setdefault((hit.repo, hit.path, hit.line), set()).add(
                        name.replace("#", ".").rsplit(".", 1)[-1])
                    hit.kind = "requested symbol definition"
                    found = True
            return found

        symbol_overflow = False
        scheduled_searches = {operation.value for operation in search_operations}
        bundle.unresolved.extend(
            f"Explicit resolve `{value}` lookup was deferred by this request's operation budget; "
            "its result is unknown, not evidence that source is absent."
            for value in dict.fromkeys(request.get("resolve") or []) if value not in scheduled_searches
        )
        for anchor in request.get("anchors") or []:
            if anchor.get("kind") == "symbol" and anchor["value"] not in scheduled_searches:
                bundle.unresolved.append(f"No dedicated symbol lookup was scheduled for `{anchor['value']}` within this request's operation budget; request it in a focused follow-up if needed.")
                if trace.stop_reason == "coverage_satisfied":
                    trace.stop_reason = "requested_symbols_incomplete"
        for operation_index, operation in enumerate(search_operations):
            if time_budget_exhausted():
                bundle.unresolved.extend(
                    f"Search `{pending.value}` lookup was not executed within this request's time budget; source availability is unknown."
                    for pending in search_operations[operation_index:]
                )
                break
            lookup_offset = len(trace.fallback_reasons)
            repos = list(operation.repos) or atlas_repo_scope[: settings.initial_repo_limit]
            qualified_query = operation.value in qualified_queries and bundle.atlas_generation is not None
            if qualified_query:
                resolved = resolve_runtime_anchors(settings, bundle.atlas_generation, [
                    {"kind": "symbol", "value": operation.value},
                ], use_cache="generation_cache" not in settings.evaluation_ablations)
                if resolved.get("status") != "ready":
                    bundle.warnings.append("Qualified symbol resolution is unavailable for the pinned Atlas generation.")
                    bundle.unresolved.append(
                        f"Search `{operation.value}` lookup is unavailable for the pinned Atlas generation "
                        f"({resolved.get('reason') or 'component unavailable'}); source availability is unknown."
                    )
                    if trace.stop_reason == "coverage_satisfied":
                        trace.stop_reason = "requested_symbols_incomplete"
                    continue
                hits = [
                    SearchHit(item["repo"], item["path"], item["line"], item["value"],
                              "definition", 100, ["generation-validated qualified symbol"])
                    for item in resolved.get("candidates") or []
                    if not operation.repos or item["repo"] in operation.repos
                ]
                if len(resolved.get('candidates') or []) == 1:
                    symbol_query_owners[operation.value] = str(resolved['candidates'][0]['entity_id'])
                for item in resolved.get("candidates") or []:
                    if not operation.repos or item["repo"] in operation.repos:
                        if len(requested_symbols) < MAX_REQUEST_ITEMS or str(item["entity_id"]) in requested_symbols:
                            requested_symbols[str(item["entity_id"])] = item
                        else:
                            symbol_overflow = True
            else:
                hits = search(settings, operation.value, repos, fixed=True)
            if not hits and not qualified_query and not _source_lookup_failed_since(trace, lookup_offset):
                try:
                    hits = search(settings, operation.value, repos)
                except BrainError:
                    hits = []
            symbol_query = operation.value in explicit_symbol_queries or bool(SOURCE_SYMBOL_RE.fullmatch(operation.value))
            symbol_name = operation.value.rsplit(".", 1)[-1] if symbol_query else None
            if symbol_name is not None and not qualified_query:
                hits.extend(_symbol_definition_hits(settings, symbol_name, repos, hits))

            def source_match() -> bool:
                return any(
                    not _is_documentation_path(hit.path)
                    and (qualified_query or symbol_name is None or hit.kind == "definition")
                    for hit in hits
                )

            # A README mentioning an adaptor is not enough to stop before its
            # implementation repo. Keep those hits, but widen disjoint scopes.
            searched_repos = set(repos or [repo.name for repo in settings.repositories])
            if hits and not source_match():
                reason = "lexical_references_only" if symbol_name is not None and any(
                    not _is_documentation_path(hit.path) for hit in hits
                ) else "lexical_documentation_only"
                if reason not in trace.fallback_reasons:
                    trace.fallback_reasons.append(reason)
            if not operation.repos and not qualified_query:
                for scope in (atlas_repo_scope[:settings.widen_repo_limit], [repo.name for repo in settings.repositories]):
                    pending = [name for name in scope if name not in searched_repos]
                    if source_match() or time_budget_exhausted() or trace.physical_budget_remaining <= 0:
                        break
                    if pending:
                        wider = search(settings, operation.value, pending, fixed=True)
                        if symbol_name is not None:
                            wider.extend(_symbol_definition_hits(settings, symbol_name, pending, wider))
                        hits.extend(wider)
                        searched_repos.update(pending)
                        trace.widening_rounds += 1
            if _source_lookup_failed_since(trace, lookup_offset):
                bundle.unresolved.append(
                    f"Search `{operation.value}` could not verify the full requested source scope; "
                    "source availability is unknown for unavailable repositories."
                )
            elif not hits:
                if trace.stop_reason in {"physical_budget", "time_budget", "lexical_batch_budget"}:
                    bundle.unresolved.append(
                        f"Search `{operation.value}` did not complete within this request's retrieval budget; source availability is unknown."
                    )
                else:
                    bundle.unresolved.append(f"Search `{operation.value}` returned no code matches in {repos or ['all repositories']}")
            definitions_protected = (not qualified_query and operation.value in explicit_symbol_queries
                                     and protect_definitions(hits, operation.value))
            if operation.value in literal_relation_queries and (
                _source_lookup_failed_since(trace, lookup_offset)
                or len(hits) >= min(settings.candidate_limit, settings.max_results)
            ):
                incomplete_relation_loci.add(operation.value)
            for hit in hits:
                # An explicitly supplied literal is a stronger delivery request
                # than incidental prose overlap. Promote only an observed source
                # line, not a fuzzy regex result or a navigation-card label.
                if (not definitions_protected and operation.value in explicit_anchor_queries and operation.value in hit.text
                    and "requested symbol definition" not in hit.kind
                    and _SOURCE_CHANNELS.intersection(hit.found_by)):
                    hit.kind = ", ".join(dict.fromkeys([*hit.kind.split(", "), "requested anchor match"]))
                    if hit.path.lower().endswith(('.py', '.java')) and len(requested_anchor_locations) < settings.pre_rerank_candidate_limit:
                        requested_anchor_locations.add((hit.repo, hit.path, hit.line))
                        if operation.value in literal_relation_queries:
                            relation_loci.setdefault(operation.value, set()).add((hit.repo, hit.path, hit.line))
                    elif operation.value in literal_relation_queries and hit.path.lower().endswith(('.py', '.java')):
                        incomplete_relation_loci.add(operation.value)
                # Request-local opaque ordinals retain joint-query coverage
                # without copying private search text into provenance/metrics.
                hit.found_by = sorted(set([*hit.found_by, f"lexical anchor {lexical_anchors[operation.value.casefold()]}"]))
            candidates.extend(hits)
            bundle.evidence.extend(knowledge_hits(settings, operation.value, deadline=deadline))
        trace.add_stage("exact_lexical_ms", (time.perf_counter() - lexical_started) * 1000)

        semantic_started = time.perf_counter()
        semantic_repo_scores: dict[str, float] = {}
        try:
            from .editions import current_edition

            edition = current_edition(settings)
            if edition in {"semantic", "precision"} and not exact_symbol_scope:
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
                    ]))
                    # An explicit scope is a hard filter. Otherwise rank the
                    # global Repo vectors before bounding source-shard search.
                    scoped_semantic_repos = set(explicit_semantic_repos) if (
                        compiled_plan.operations and all(operation.repos for operation in compiled_plan.operations)
                    ) else None
                    trace.semantic_repo_scope = semantic_repo_scope[:max(1, settings.widen_repo_limit)]
                    semantic = search_semantic(
                        settings,
                        bundle.objective,
                        repos=scoped_semantic_repos,
                        repo_hints=semantic_repo_scope,
                        repo_limit=max(1, settings.widen_repo_limit),
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
                            "Global Semantic repository routing was incomplete because its budget or pinned routing data was unavailable; "
                            "results may omit repositories. Continue with a focused repository, path or symbol request."
                            if "semantic_repo_routing_incomplete" in trace.fallback_reasons else
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
        from .retrieval.planner import requested_symbol_relations
        from .retrieval.models import QueryOperation

        literal_relations = tuple(name for name in requested_symbol_relations(request) if name in {'callers', 'callees'})
        enclosing_queries: dict[str, set[str]] = {}
        added_relation_operations = 0
        relation_incomplete = False
        if literal_relations and literal_relation_queries:
            # Reuse exact pinned ownership, never guess a callable from prose
            # or promote an arbitrary same-literal owner to the root cause.
            points = sorted({point for loci in relation_loci.values() for point in loci})[:MAX_REQUEST_ITEMS]
            owners: list[dict[str, Any]] = []
            reason = 'request time or physical-operation budget'
            if points and not time_budget_exhausted() and trace.try_reserve_backend():
                lookup_started = time.perf_counter()
                try:
                    owners, reason = _requested_symbol_ranges(settings, {}, enclosing=points)
                finally:
                    trace.complete_reserved_backend('anchor_owner', (time.perf_counter() - lookup_started) * 1000)
            for query in sorted(literal_relation_queries):
                loci = relation_loci.get(query, set())
                matched = [item for item in owners if any(
                    (item['repo'], item['path'], line) in loci for line in item.get('enclosing_match_lines', []))]
                covered = {(item['repo'], item['path'], line) for item in matched for line in item.get('enclosing_match_lines', [])}
                if query in incomplete_relation_loci or not loci or len(matched) != 1 or not loci.issubset(covered):
                    relation_incomplete = True
                    bundle.unresolved.append(f'Requested literal relationships for `{query}` have no unique validated callable owner: '
                                             f'{reason or "missing or ambiguous source scope"}; inspect the exact-match source or its pinned symbol anchor.')
                    continue
                owner = matched[0]
                identifier = owner['entity_id']
                existing = next((item for item in targeted if item.kind == 'symbol'
                                 and (not item.repos or owner['repo'] in item.repos)
                                 and (item.value == identifier or symbol_query_owners.get(item.value) == identifier)
                                 and set(literal_relations).issubset(item.includes)), None)
                if existing is None:
                    if len(compiled_plan.operations) + added_relation_operations >= settings.max_effective_operations:
                        relation_incomplete = True
                        bundle.unresolved.append(f'Literal relationships for `{query}` were deferred by this request\'s operation budget.')
                        continue
                    targeted.append(QueryOperation('symbol', identifier, (owner['repo'],), includes=literal_relations))
                    added_relation_operations += 1
                enclosing_queries.setdefault(existing.value if existing else identifier, set()).add(query)
            trace.effective_operations += added_relation_operations
            if enclosing_queries:
                bundle.warnings.append('Literal relationships describe the enclosing callable, not proof that the literal is an executed log or the root cause.')
        unscoped = any(not item.repos for item in targeted)
        missing_definitions = {item.value for item in targeted if item.kind == "symbol"
                               and "definition" in item.includes and item.value not in qualified_queries}
        seen_wave_repos: set[str] = set()
        relation_sources = {
            key: source.verification_content if source.verification_content is not None else source.content
            for key, (source, _, _) in (direct_sources or {}).items()
        }
        relation_source_attempts: set[tuple[str, str]] = set()
        pending_enclosing_queries = set(enclosing_queries)

        def load_relation_sources(keys: list[tuple[str, str]]) -> dict[tuple[str, str], str]:
            loaded = _load_python_binding_sources(settings, bundle.atlas_generation, keys,
                                                 relation_sources, relation_source_attempts, deadline)
            # Package exports are binding proof, not an additional source page.
            # Retain only request-local buffers for runtime revalidation; no IO there.
            bundle._python_package_sources.update((key, value) for key, value in loaded.items()
                                                  if key[1].endswith('/__init__.py'))
            return loaded

        def scope_for(operation: Any, wave: list[str]) -> list[str]:
            return list(operation.repos) if operation.repos else [name for name in wave if name not in seen_wave_repos]

        def needs_widening() -> bool:
            coverage = request.get("coverage") or {}
            production = any("test" not in item.kind.lower() for item in candidates)
            tests = any("test" in item.kind.lower() or re.search(r"(^|/)(test|tests|src/test)/", item.path, re.I) for item in candidates)
            relationships = bool(bundle.relationships)
            return (
                not production
                or any(not item.repos and item.value in missing_definitions for item in targeted if item.kind == "symbol")
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
                if wave_number and operation.repos:
                    continue  # Explicit scopes were already evaluated in the first wave.
                if time_budget_exhausted():
                    break
                repos = scope_for(operation, wave)
                if not repos:
                    continue
                if operation.kind == "path":
                    operation_started = time.perf_counter()
                    lookup_offset = len(trace.fallback_reasons)
                    hits = path_hits(settings, operation.value, repos)
                    trace.add_stage("path_ms", (time.perf_counter() - operation_started) * 1000)
                    candidates.extend(hits)
                    if _source_lookup_failed_since(trace, lookup_offset) or trace.stop_reason in {"physical_budget", "path_batch_budget"}:
                        bundle.unresolved.append(
                            f"Path search `{operation.value}` did not complete in the requested scope; source availability is unknown."
                        )
                    elif not hits:
                        bundle.unresolved.append(f"Path search `{operation.value}` returned no matches in {repos}")
                elif operation.kind == "symbol":
                    operation_started = time.perf_counter()
                    pending_enclosing_queries.discard(operation.value)
                    include = set(operation.includes)
                    if operation.value in qualified_queries:
                        include.discard("definition")  # Already resolved with exact ranges above.
                    typed_relations = include & {"callers", "callees", "implementations"} if request.get("version") == 5 else set()
                    if typed_relations:
                        # Query the one pinned graph, not a short-name scan that
                        # can mistake a different owner's method for a caller.
                        if not wave_number and trace.try_reserve_backend():
                            from .investigation import _execution_flow, resolve_runtime_anchors

                            generation = bundle.atlas_generation
                            flow: dict[str, Any] = {"steps": [], "reason": "no pinned Atlas generation"}
                            try:
                                resolved = resolve_runtime_anchors(settings, generation, [
                                    {"kind": "symbol", "value": operation.value},
                                ], use_cache="generation_cache" not in settings.evaluation_ablations) if generation is not None else {}
                                seeds = list(dict.fromkeys(
                                    str(item["entity_id"]) for item in resolved.get("candidates") or []
                                    if item.get("entity_id") and item.get("method") == "entity_name"
                                ))
                                if generation is not None and resolved.get("status") == "ready" and len(seeds) == 1:
                                    bundle._resolved_relation_seeds[operation.value] = seeds[0]
                                    for query in enclosing_queries.get(operation.value, ()):
                                        bundle._resolved_relation_seeds[query] = seeds[0]
                                    if 'callers' in typed_relations:
                                        caller_reason = _find_python_caller_sources(settings, generation,
                                            resolved.get('candidates') or [], relation_sources, deadline)
                                        if caller_reason:
                                            relation_incomplete = True
                                            bundle.unresolved.append(caller_reason + '; caller coverage is incomplete.')
                                    flow = _execution_flow(
                                        settings, generation, seeds, bundle,
                                        incoming_types=(
                                            *(("CALLS",) if "callers" in typed_relations else ()),
                                            *(("IMPLEMENTS", "EXTENDS") if "implementations" in typed_relations else ()),
                                        ),
                                        outgoing_types=("CALLS",) if "callees" in typed_relations else (),
                                        python_sources=relation_sources, load_python_sources=load_relation_sources,
                                    )
                                elif generation is not None:
                                    flow["reason"] = "symbol is unresolved, ambiguous, or its pinned component is unavailable; qualify the owner/package"
                            finally:
                                trace.complete_reserved_backend(
                                    "symbol_relation", (time.perf_counter() - operation_started) * 1000,
                                    raw_hits=len(flow.get("steps") or []),
                                )
                            for step in flow.get("steps") or []:
                                candidates.append(SearchHit(
                                    str(step["repo"]), str(step["path"]), int(step["line"]), str(step["target"]),
                                    "requested symbol relationship candidate", 100,
                                    [f"pinned Atlas {step['edge_type']} edge"],
                                ))
                                target = (step.get('target_symbol') if 'callees' in typed_relations and step['source_id'] in seeds
                                          else step.get('source_symbol') if 'callers' in typed_relations and step['target_id'] in seeds else None)
                                if step['depth'] == 0 and step['edge_type'] == 'CALLS' and target:
                                    # Reuse the same canonical range/source batch;
                                    # callsite provenance is not the callee body.
                                    if len(requested_symbols) < MAX_REQUEST_ITEMS or target['entity_id'] in requested_symbols:
                                        requested_symbols.setdefault(target['entity_id'], {**target, 'requested_relation': True})
                                        candidates.append(SearchHit(target['repo'], target['path'], target['line'], target['value'],
                                            'definition', 100, ['generation-validated qualified symbol']))
                                    else:
                                        symbol_overflow = True
                                bundle.relationships.append(
                                    f"{step['repo']}:{step['path']}:{step['line']}  {step['edge_type']}  {step['target']} "
                                    "| pinned Atlas candidate; exact source verification required"
                                )
                            if flow.get('unresolved_python_bindings'):
                                relation_incomplete = True
                                bundle.unresolved.append(
                                    f"{flow['unresolved_python_bindings']} Python call bindings remain unknown/unavailable: "
                                    "a same-name candidate is not an established callee; inspect its pinned source binding."
                                )
                            if not flow.get("steps"):
                                relation_incomplete = True
                                bundle.unresolved.append(
                                    f"Requested {', '.join(sorted(typed_relations))} for `{operation.value}` are not established "
                                    f"in the pinned graph: {flow.get('reason') or 'no resolved typed edge'}. "
                                    "A same-name reference is not proof of dispatch or implementation."
                                )
                            elif flow.get("truncated"):
                                relation_incomplete = True
                                bundle.unresolved.append(f"Symbol relationships for `{operation.value}` are a bounded partial graph; additional callers/callees may exist")
                            if "callers" in typed_relations and resolved.get("status") == "ready" and len(seeds) == 1 and any(
                                item.get("entity_id") in seeds and str(item.get("path") or "").lower().endswith(".java")
                                for item in resolved.get("candidates") or []
                            ):
                                references, reference_limit = _java_caller_reference_hits(settings, operation.value, operation.repos)
                                candidates.extend(references)
                                if references:
                                    bundle.warnings.append(
                                        f"Java call references for `{operation.value}` are candidate call sites, not proof of receiver dispatch. "
                                        "Inspect their exact source and types before drawing a call relationship."
                                    )
                                if reference_limit:
                                    relation_incomplete = True
                                    bundle.unresolved.append(f"Caller reference search for `{operation.value}` is incomplete: {reference_limit}")
                        elif not wave_number:
                            bundle.unresolved.append(f"Symbol relationships for `{operation.value}` were deferred by the physical operation budget")
                            trace.stop_reason = "physical_budget"
                        include -= typed_relations
                    if "definition" in include:
                        definitions = symbol_hits(settings, operation.value, repos)
                        if protect_definitions(definitions, operation.value):
                            missing_definitions.discard(operation.value)
                        candidates.extend(definitions)
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
                        # A requested test may live outside the implementation's
                        # routed repositories. One pinned query searches all test
                        # files before applying the source candidate budget.
                        test_scope = repos if operation.repos else None
                        lookup_offset = len(trace.fallback_reasons)
                        tests = test_hits(settings, operation.value, test_scope)
                        candidates.extend(tests)
                        if _source_lookup_failed_since(trace, lookup_offset) or trace.stop_reason in {"physical_budget", "lexical_batch_budget"}:
                            bundle.unresolved.append(
                                f"Test search for `{operation.value}` did not complete in the requested scope; source availability is unknown."
                            )
                        elif not tests:
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

        for identifier in sorted(pending_enclosing_queries):
            relation_incomplete = True
            bundle.unresolved.extend(f'Literal relationships for `{query}` were deferred by this request\'s retrieval budget; '
                                     'caller/callee coverage is unknown.' for query in sorted(enclosing_queries[identifier]))
        bundle.unresolved.extend(
            f"Definition for `{name}` is not verified in the searched scope; lexical references are navigation only."
            for name in sorted(missing_definitions)
        )
        bundle.unresolved.extend(reason for reason in trace.fallback_reasons
                                 if reason.startswith(("Java declaration verification is incomplete:",
                                                       "Java caller verification is incomplete:")))

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

        # Direct pages retain the full verified file, not just the emitted
        # range. Reuse that request-local source for navigation and hydration.
        navigation_sources: dict[tuple[str, str], str] = dict(relation_sources)
        if request.get('version') == 5 and not time_budget_exhausted():
            topic_hits, topic_limit = _topic_peer_hits(settings, request.get('anchors') or [], candidates, navigation_sources)
            candidates.extend(topic_hits)
            if topic_hits:
                bundle.warnings.append(
                    "Topic peers are navigation candidates from the pinned generation. Event payload association "
                    "and complete consumer coverage are not established by matching topic declarations."
                )
                if trace.stop_reason == 'coverage_satisfied':
                    trace.stop_reason = 'candidate_evidence_collected'
            if topic_limit:
                bundle.unresolved.append(f"Topic peer navigation is incomplete: {topic_limit}")
                if trace.stop_reason == 'coverage_satisfied':
                    trace.stop_reason = 'candidate_evidence_incomplete'
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
        rerank_sources: dict[tuple[str, str], str] = navigation_sources
        try:
            from .editions import current_edition

            if current_edition(settings) == "precision" and not exact_symbol_scope and not time_budget_exhausted():
                from .models import rerank_candidates

                requested = [name for name in ("searches", "paths", "symbols", "files", "history") if request.get(name)]
                rerank_query = bundle.objective + ("\nRequested evidence: " + ", ".join(requested) if requested else "")
                candidates = rerank_candidates(settings, rerank_query, candidates, trace=trace, _source_cache=rerank_sources)
                if "rerank_source_preview_incomplete" in trace.fallback_reasons:
                    bundle.warnings.append(
                        "Some pinned candidate code could not be previewed within the reranking budget; "
                        "those candidates retain their original retrieval scores. Request a focused file or line range if needed."
                    )
        except (OSError, ValueError, RuntimeError):
            bundle.warnings.append("Local reranker failed; used semantic/lexical candidate ranking.")
            trace.fallback_reasons.append("reranker_runtime")
        trace.add_stage("rerank_ms", (time.perf_counter() - rerank_started) * 1000)

        selection_started = time.perf_counter()
        selected, omitted = select_candidates(settings, candidates, already_fused=True)
        omitted.extend(early_omitted)
        trace.add_stage("selection_ms", (time.perf_counter() - selection_started) * 1000)

        symbol_ranges: list[dict[str, Any]] = []
        symbol_reads: list[dict[str, Any]] = []
        selected_files = {(hit.repo, hit.path) for hit in selected}
        for item in requested_symbols.values():
            if (item["repo"], item["path"]) not in selected_files:
                bundle.unresolved.append(f"Requested symbol source deferred by candidate selection: {item['repo']}:{item['path']}:{item['line']}; request this file in a focused follow-up.")
                if trace.stop_reason == "coverage_satisfied":
                    trace.stop_reason = "requested_symbols_incomplete"
        if symbol_overflow:
            bundle.unresolved.append("Some requested symbol ranges exceeded the per-request range budget; remaining source windows may be incomplete.")
            if trace.stop_reason == "coverage_satisfied":
                trace.stop_reason = "requested_symbols_incomplete"
        requested_symbols = {key: item for key, item in requested_symbols.items()
                             if (item["repo"], item["path"]) in selected_files}
        definition_locations = {
            (hit.repo, hit.path, hit.line): requested_definition_names[(hit.repo, hit.path, hit.line)]
            for hit in selected if "requested symbol definition" in hit.kind
        }
        requested_definition_names.clear()
        radius = max(10, settings.source_window_lines // 2)
        enclosing_locations = sorted(point for point in requested_anchor_locations if any(
            'requested anchor match' in hit.kind and point[:2] == (hit.repo, hit.path)
            and abs(point[2] - hit.line) <= radius for hit in selected))[:MAX_REQUEST_ITEMS]
        trace._set_backend_headroom(0)
        emit("hydrating", evidence_count=len(bundle.evidence), candidate_count=len(selected))
        hydrate_started = time.perf_counter()
        indexed_sources: dict[tuple[str, str], str] | None = None
        if settings.atlas_generation_mode == "pinned" and bundle.atlas_generation is not None:
            from .index import read_generation_files

            indexed_sources = dict(rerank_sources)
            missing = list(dict.fromkeys((hit.repo, hit.path) for hit in selected if (hit.repo, hit.path) not in indexed_sources))
            buffered = any((hit.repo, hit.path) in indexed_sources for hit in selected)
            if missing and (
                not time_budget_exhausted() or (first_verified_evidence_ms is None and not buffered)
            ):
                if trace.try_reserve_backend():
                    source_started = time.perf_counter()
                    loaded: dict[tuple[str, str], str] = {}
                    try:
                        loaded = read_generation_files(
                            settings, bundle.atlas_generation, missing,
                            max_bytes=MAX_PINNED_HYDRATION_BYTES,
                            max_seconds=MAX_PINNED_HYDRATION_SECONDS,
                        ) or {}
                        indexed_sources.update(loaded)
                    finally:
                        trace.complete_reserved_backend(
                            "source-hydration", (time.perf_counter() - source_started) * 1000,
                            bytes_scanned=sum(len(content.encode("utf-8")) for content in loaded.values()),
                            files=len(loaded),
                        )
                else:
                    bundle.unresolved.append("Pinned source hydration deferred by the physical-operation budget; unread candidate source remains unknown.")
                    if trace.stop_reason == "coverage_satisfied":
                        trace.stop_reason = "operation_budget"
        # Small buffered files already fit the ordinary full-file reader. No
        # entity lookup is needed to deliver those unqualified definitions.
        full_file_budget = min(64_000, max(1, settings.hard_context_chars // 3 // max(1, len(requested_symbols) + len(definition_locations))))
        complete_files = {
            key for key in {(repo, path) for repo, path, _ in [*definition_locations, *enclosing_locations]}
            if key in (indexed_sources or {}) and len(indexed_sources[key].encode("utf-8")) <= full_file_budget
            and len(indexed_sources[key].splitlines()) <= settings.full_file_lines
        }
        definition_locations = {key: names for key, names in definition_locations.items() if key[:2] not in complete_files}
        enclosing_locations = [point for point in enclosing_locations if point[:2] not in complete_files]
        # Secure the source before spending the last physical operation on
        # optional range metadata. A source window is useful even when the full
        # method range cannot be validated; it must not claim complete coverage.
        if (requested_symbols or definition_locations or enclosing_locations) and settings.atlas_generation_mode == "pinned":
            reason = "request time or physical-operation budget"
            if not time_budget_exhausted() and trace.try_reserve_backend():
                range_started = time.perf_counter()
                try:
                    symbol_ranges, reason = _requested_symbol_ranges(
                        settings, requested_symbols, definitions=definition_locations, enclosing=enclosing_locations,
                    )
                finally:
                    trace.complete_reserved_backend("symbol_ranges", (time.perf_counter() - range_started) * 1000)
            if reason and not (requested_symbols or definition_locations):
                bundle.warnings.append(f"Enclosing source scope unavailable: {reason}; retained exact-match windows, not complete callable bodies.")
            elif reason:
                bundle.unresolved.append(f"Full requested symbol ranges unavailable: {reason}; returned source windows may be incomplete.")
                if trace.stop_reason == "coverage_satisfied":
                    trace.stop_reason = "requested_symbols_incomplete"
        source_budget = max(10_000, settings.hard_context_chars - 40_000)
        source_bytes = sum(len(item.content.encode("utf-8")) for item in bundle.evidence)
        symbol_page_bytes = min(64_000, max(1, settings.hard_context_chars // 3 // max(1, len(symbol_ranges))))
        delivered_symbols: set[str] = set()
        for hit in selected:
            # Stop new reads after the soft deadline, not delivery of pinned
            # source already read and verified. Later candidates may be buffered
            # even when an earlier one is not. Selection/context bounds still apply.
            buffered = indexed_sources is not None and (hit.repo, hit.path) in indexed_sources
            if time_budget_exhausted() and not buffered and (
                indexed_sources is not None or first_verified_evidence_ms is not None
            ):
                omitted.append(hit)
                continue
            ranges = [item for item in symbol_ranges if (item["repo"], item["path"]) == (hit.repo, hit.path) and (
                ("generation-validated qualified symbol" in hit.found_by and item["entity_id"] in requested_symbols)
                or ("requested symbol definition" in hit.kind and item["line_start"] == hit.line
                    and item["simple_name"] in definition_locations.get((hit.repo, hit.path, hit.line), set()))
                or ('requested anchor match' in hit.kind and any(
                    abs(line - hit.line) <= radius for line in item.get('enclosing_match_lines', [])))
            )]
            window_reports: list[dict[str, Any]] = []
            if ranges:
                for item in ranges:
                    if item["entity_id"] in delivered_symbols:
                        continue
                    delivered_symbols.add(item["entity_id"])
                    enclosing_only = ('requested symbol definition' not in hit.kind
                                      and 'generation-validated qualified symbol' not in hit.found_by)
                    evidence, report = _requested_file_page(
                        settings, {"repo": hit.repo, "path": hit.path, "lines": f"{item['line_start']}-{item['line_end']}"},
                        max_bytes=max(0, min(symbol_page_bytes, source_budget - source_bytes)),
                        source_cache=direct_sources,
                        _indexed_source=indexed_sources.get((hit.repo, hit.path)) if indexed_sources is not None else None,
                        # Only a matching candidate window belongs to this
                        # range; a distant method in the same file has its own.
                        _source_anchor_line=(hit.line if enclosing_only or abs(hit.line - item["line_start"]) <= max(10, settings.source_window_lines // 2)
                                             else item["line_start"]),
                    )
                    symbol_reads.append(report)
                    if enclosing_only and (not evidence or report.get('next_lines')):
                        # Auto-expansion must not replace the known literal with
                        # the first page of a huge method that lacks that literal.
                        report.update(status='window_only', returned_lines=None,
                                      next_lines=f"{item['line_start']}-{item['line_end']}")
                        delivered_symbols.discard(item['entity_id'])
                        window_reports.append(report)
                        bundle.warnings.append(f"Enclosing callable exceeds this page budget: {hit.repo}:{hit.path}; "
                                               f"kept its exact-match window. Read lines={report['next_lines']} for the callable body.")
                        continue
                    if not evidence or report.get("next_lines"):
                        bundle.unresolved.append(
                            f"Requested symbol source {report['status']}: {hit.repo}:{hit.path}; "
                            + (f"continue with a files request, lines={report['next_lines']}." if report.get("next_lines")
                               else str(report.get("reason") or "Source not delivered."))
                        )
                        if trace.stop_reason == "coverage_satisfied":
                            trace.stop_reason = "requested_symbols_incomplete"
                    if evidence:
                        delivered_symbols.update(candidate["entity_id"] for candidate in symbol_ranges
                                                 if (candidate["repo"], candidate["path"]) == (hit.repo, hit.path)
                                                 and evidence.line_start <= candidate["line_start"] <= candidate["line_end"] <= evidence.line_end)
                        evidence.kind, evidence.score = hit.kind, hit.score
                        if requested_symbols.get(item['entity_id'], {}).get('requested_relation'):
                            evidence.kind += ', requested symbol relationship source'
                        evidence.found_by = [*hit.found_by, *evidence.found_by[1:],
                            ('enclosing callable source range (not dispatch or behavioral proof)' if enclosing_only
                             else 'requested symbol source range (not a behavioral proof)')]
                        bundle.evidence.append(evidence)
                        size = len(evidence.content.encode("utf-8"))
                        source_bytes += size
                        trace.bytes_read += size
                        if first_verified_evidence_ms is None:
                            first_verified_evidence_ms = (time.perf_counter() - started) * 1000
                if not window_reports:
                    continue
            try:
                evidence = read_source(
                    settings,
                    hit,
                    _indexed_source=(
                        indexed_sources.get((hit.repo, hit.path)) if indexed_sources is not None else None
                    ) if settings.atlas_generation_mode == "pinned" else _INDEXED_SOURCE_UNSET,
                )
            except BrainError:
                for report in window_reports:
                    report['status'] = 'unavailable'
                bundle.warnings.append(f"Candidate source unavailable for hydration: {hit.repo}:{hit.path}")
                bundle.unresolved.append(
                    f"Source availability is unknown for `{hit.repo}:{hit.path}`; candidate source could not be read. "
                    "Relationship metadata is not a substitute for the missing source evidence."
                )
                if trace.stop_reason == "coverage_satisfied":
                    trace.stop_reason = "candidate_evidence_incomplete"
                continue
            evidence_bytes = len(evidence.content.encode("utf-8"))
            if source_bytes and source_bytes + evidence_bytes > source_budget:
                omitted.append(hit)
                trace.stop_reason = "context_budget"
                continue
            bundle.evidence.append(evidence)
            for report in window_reports:
                report['returned_lines'] = f'{evidence.line_start}-{evidence.line_end}'
            source_bytes += evidence_bytes
            trace.bytes_read += evidence_bytes
            if first_verified_evidence_ms is None:
                first_verified_evidence_ms = (time.perf_counter() - started) * 1000
        trace.add_stage("source_hydration_ms", (time.perf_counter() - hydrate_started) * 1000)
        bundle.additional_candidates = sorted(omitted, key=lambda item: (-item.score, item.repo, item.path, item.line))
        bundle.evidence = merge_evidence(bundle.evidence)
        # Keep navigable identity with its actual source block: final byte
        # bounding must omit both together. This never changes the evidence ID
        # or establishes a call edge; the next request revalidates the same pin.
        for evidence in bundle.evidence:
            evidence.found_by.extend(
                PINNED_SYMBOL_ANCHOR_PREFIX + json.dumps(
                    {'kind': 'symbol', 'value': item['entity_id']}, separators=(',', ':'))
                for item in symbol_ranges if (item['repo'], item['path']) == (evidence.repo, evidence.path)
                and any(evidence.line_start <= line <= evidence.line_end
                        for line in [item['line_start'], *item.get('enclosing_match_lines', [])])
            )

        deferred_definitions = {(hit.repo, hit.path, hit.line): hit for hit in omitted
                                if "requested symbol definition" in hit.kind}
        deferred_files = {(hit.repo, hit.path) for hit in deferred_definitions.values()}
        # Check actual delivered windows, not just declaration lines. Reuse
        # already-verified source to clamp at EOF; never read a deferred file.
        line_counts = {key: len(source.splitlines()) for key, source in (indexed_sources or {}).items()
                       if key in deferred_files}
        for evidence in bundle.evidence:
            key = evidence.repo, evidence.path
            if key in deferred_files and key not in line_counts and evidence.verification_content is not None:
                line_counts[key] = len(evidence.verification_content.splitlines())
        for hit in deferred_definitions.values():
            total = line_counts.get((hit.repo, hit.path))
            radius = max(10, settings.source_window_lines // 2)
            start, end = max(1, hit.line - radius), hit.line + radius
            if total is not None:
                start, end = (1, total) if total <= settings.full_file_lines else (start, min(total, end))
            if any(item.repo == hit.repo and item.path == hit.path and item.line_start <= start and item.line_end >= end
                   for item in bundle.evidence):
                continue
            bundle.unresolved.append(f"Requested symbol source window deferred: {hit.repo}:{hit.path}:{start}-{end}; candidate metadata is not the unread implementation.")
            if trace.stop_reason == "coverage_satisfied":
                trace.stop_reason = "requested_symbols_incomplete"

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
        if relation_incomplete and trace.stop_reason == 'coverage_satisfied':
            trace.stop_reason = 'requested_symbols_incomplete'
        bundle.trace = trace.as_dict()
        if exact_symbol_scope:
            bundle.trace["qualified_symbols_only"] = True
        if file_reads:
            bundle.trace["file_reads"] = file_reads
        if symbol_reads:
            bundle.trace["symbol_reads"] = symbol_reads
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
        trace._set_backend_headroom(0)
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

    repository_evidence = [item for item in bundle.evidence if authoritative(item) and not _is_documentation_path(item.path)]
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
            source_bytes = len(evidence.content.encode("utf-8"))
            if max_chars is not None and restored_chars + source_bytes > max_chars:
                missed += 1
                continue
            restored.append(evidence)
            restored_chars += source_bytes
        except (BrainError, KeyError, OSError, TypeError, ValueError):
            missed += 1
    return restored, missed


def _delivery_evidence(bundle: ContextBundle) -> list[Evidence]:
    """Keep this request's exact pages ahead of optional ranked/retained source."""
    pages: dict[tuple[str, str], list[tuple[int, int, bool]]] = {}
    for channel in ("file_reads", "symbol_reads"):
        for item in bundle.trace.get(channel) or []:
            returned = str(item.get("returned_lines") or "")
            if re.fullmatch(r"[0-9]+-[0-9]+", returned):
                start, end = (int(value) for value in returned.split("-"))
                pages.setdefault((str(item["repo"]), str(item["path"])), []).append((start, end, channel == "file_reads"))

    def priority(item: Evidence) -> int:
        matched = [direct for start, end, direct in pages.get((item.repo, item.path), [])
                   if item.line_start <= start and item.line_end >= end]
        if "direct file request" in item.found_by and True in matched:
            return 0
        if 'requested anchor match' in item.kind:
            return 1  # The observed failure precedes bodies derived from it.
        if "requested symbol definition" in item.kind or (
            matched and {"direct file request", "generation-validated qualified symbol"}.intersection(item.found_by)
        ):
            return 2
        return 3

    return sorted(bundle.evidence, key=priority)


def _source_request_memory(memory: dict[str, Any]) -> dict[str, Any]:
    """Summarize duplicated source references on the wire, never in ticket state."""
    duplicated = {"verified_facts", "verified_references", "implementation_surface", "test_surface"}
    return {
        key: {
            "retained_count": len(value),
            "authority": "Reference count only, not delivered evidence; use source blocks and evidence lineage.",
        } if key in duplicated and isinstance(value, list) else value
        for key, value in memory.items()
    }


def pack_delta_context(
    settings: Settings,
    ticket: str,
    request_number: int,
    bundle: ContextBundle,
    progress: dict[str, Any],
    new_evidence_ids: set[str],
    *,
    emitted_ids: set[str] | None = None,
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
    if bundle.trace.get("direct_files_only"):
        memory_changes = _source_request_memory(memory_changes)
    for key, value in sorted(memory_changes.items()):
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
        output.append(f"- `{key}`: {rendered[:2_000]}")
    if not memory_changes:
        output.append("- None")
    output.extend(["", "## Evidence lineage", ""])
    output.append(f"- New or not-yet-emitted evidence: `{len(new_evidence_ids)}`")
    superseded = progress.get("superseded_evidence_ids") or []
    output.append(f"- Invalidated/superseded evidence: `{', '.join(superseded) if superseded else 'none'}`")
    new_items = [item for item in _delivery_evidence(bundle) if _evidence_id(item) in new_evidence_ids
                 or "requested symbol definition" in item.kind
                 or 'requested anchor match' in item.kind
                 or any(channel.startswith(PINNED_SYMBOL_ANCHOR_PREFIX) for channel in item.found_by)
                 or {"direct file request", "generation-validated qualified symbol"}.intersection(item.found_by)]
    new_public_ids = [_public_evidence_id(progress, item) for item in new_items]
    output.append(f"- Embedded evidence IDs: `{', '.join(new_public_ids) or 'none'}`")
    output.append("- Omitted evidence IDs due to byte limit: `none`")
    if bundle.trace.get("file_reads"):
        output.append("- Explicitly requested file pages are re-emitted even when their stable evidence IDs are already known.")
    if bundle.trace.get("symbol_reads"):
        output.append("- Explicitly requested symbol pages are re-emitted even when their stable evidence IDs are already known.")
    output.extend(["", "## New source evidence", ""])
    if progress.get("protocol_version") == 5 and progress.get("investigation_runtime"):
        from .investigation import render_protocol_v5

        output.extend([render_protocol_v5(progress["investigation_runtime"], delta=True,
                                          compact=bool(bundle.trace.get("direct_files_only"))), ""])
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
        emitted_ids=emitted_ids,
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


def _validate_checkpoint_artifacts(
    settings: Settings, ticket: str, number: int, context_id: str,
    requested_base: str | None, bundle: ContextBundle, state: dict[str, Any],
) -> None:
    """Validate at most three exact pinned proofs before retry or republication."""
    from .investigation import stable_evidence_id

    existing_checkpoint = state["progressive_checkpoint"]
    generation = bundle.atlas_generation
    directory = session_dir(settings, ticket)
    expected_hash = str(existing_checkpoint.get("content_hash") or "")
    artifact_name = str(existing_checkpoint.get("artifact") or "")
    handoff_name = str(existing_checkpoint.get("handoff_artifact") or "")
    artifact_path = directory / artifact_name
    handoff_candidate = Path(handoff_name)
    try:
        handoff_path = handoff_candidate
        if (
            not artifact_name or Path(artifact_name).name != artifact_name
            or artifact_name != f"checkpoint-{number:03d}.md"
            or handoff_path != settings.generated_dir / "handoffs" / directory.name / artifact_name
            or not artifact_path.is_file() or artifact_path.is_symlink()
            or not handoff_path.is_file() or handoff_path.is_symlink()
        ):
            raise BrainError("Published first-useful checkpoint artifact is unavailable")
        try:
            artifact_content = read_managed_bytes(
                directory, artifact_path, max_bytes=MAX_CHECKPOINT_ARTIFACT_BYTES,
            )
            handoff_content = read_managed_bytes(
                settings.generated_dir, handoff_path, max_bytes=MAX_CHECKPOINT_ARTIFACT_BYTES,
            )
        except (OSError, ValueError) as error:
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
                        snapshot_sha=generation.snapshots[repo_name],
                    )
                except (KeyError, TypeError, ValueError):
                    pinned = None
                    start = end = 0
                pinned_lines = pinned.splitlines() if pinned is not None else []
                content = "\n".join(pinned_lines[start - 1:end])
                # Full-file evidence may preserve the final newline; snippets
                # normally do not. Accept only the hash-proven pinned spelling.
                preserved = "".join(pinned.splitlines(keepends=True)[start - 1:end]) if pinned is not None else ""
                if hashlib.sha256(preserved.encode("utf-8")).hexdigest() == proof.get("content_sha256"):
                    content = preserved
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
            if match is None or stable_evidence_id(copy.deepcopy(state), match) != public_id:
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


def _checkpoint_execution_signature(signature: str, include_diff: bool) -> str:
    return hashlib.sha256((signature + "\0include_diff=" + str(int(include_diff))).encode("utf-8")).hexdigest()


def checkpoint_retry_request(
    settings: Settings, ticket: str, *, manual_request: tuple[str, bool] | None = None,
) -> tuple[str, bool]:
    """Validate saved retry; manual repair/execution require retrieval_session."""
    try:
        state = session_state(settings, ticket)
        checkpoint = state.get("progressive_checkpoint") or {}
        binding = checkpoint.get("retry_request") or {}
        legacy = "retry_request" not in checkpoint
        number = int(state.get("requests") or 0) + 1
        artifact = f"request-{number:03d}.yml"
        include_diff = manual_request[1] if legacy and manual_request is not None else binding.get("include_diff")
        generation, _ = _resolve_session_generation(settings, state)
        context_id = checkpoint.get("context_id")
        signature = checkpoint.get("request_signature")
        registry = (state.get("stable_identities") or {}).get("contexts") or {}
        reserved = sum(value == context_id for value in registry.values())
        if legacy and manual_request is not None and reserved == 0:
            from .investigation import _allocate

            reserved = int(_allocate({"contexts": dict(registry)}, "contexts", "retry", "CTX-", 3) == context_id)
        if (
            checkpoint.get("status") != "published"
            or checkpoint.get("continuation_status") not in {"pending", "failed"}
            or checkpoint.get("artifact") != f"checkpoint-{number:03d}.md"
            or type(include_diff) is not bool or not isinstance(signature, str)
            or generation is None
            or checkpoint.get("generation") != generation.generation
            or not context_id or checkpoint.get("checkpoint_id") != f"{context_id}-P1"
            or reserved != 1 or state.get("last_context_id") == context_id
            or any(row.get("context_id") == context_id for row in state.get("context_lineage") or [])
            or (legacy and manual_request is None)
            or (not legacy and (
                binding.get("artifact") != artifact
                or binding.get("atlas_generation_id") != generation.identity
                or binding.get("source_signature") != generation.source_signature
                or binding.get("execution_signature") != _checkpoint_execution_signature(signature, include_diff)
                or ("active_artifacts" in state and artifact not in state["active_artifacts"])
            ))
        ):
            raise ValueError("Unverified checkpoint request binding or uncommitted context reservation")
        if manual_request is not None:
            manual_plan = request_preview(manual_request[0], settings)
            if (manual_request[1] != include_diff
                or manual_plan["request"].get("version") != 5
                or protocol_request_signature(manual_plan, ticket, state) != signature):
                raise ValueError("Manual retry must preserve the original plan and options")
        try:
            _validate_checkpoint_artifacts(
                settings, ticket, number, context_id, checkpoint.get("base_context_id"),
                ContextBundle("Checkpoint retry", atlas_generation=generation), state,
            )
        except (BrainError, OSError, ValueError, TypeError) as exc:
            raise CheckpointRetryRequired(ticket, checkpoint_problem=True) from exc
        if legacy:
            # Legacy writers did not save enough information for automatic replay.
            # The explicit, validated manual request remains supported.
            return manual_request
        directory = session_dir(settings, ticket)
        valid_saved = False
        try:
            payload = read_managed_bytes(directory, directory / artifact, max_bytes=MAX_REQUEST_TEXT_BYTES)
            text = payload.decode("utf-8")
            plan = request_preview(text, settings)
            valid_saved = (
                hashlib.sha256(payload).hexdigest() == binding.get("content_sha256")
                and plan["request"].get("version") == 5
                and protocol_request_signature(plan, ticket, state) == signature
            )
        except (BrainError, OSError, ValueError):
            pass
        if not valid_saved:
            if manual_request is None:
                raise ValueError("Saved request content changed")
            text = manual_request[0]
            payload = text.encode("utf-8")
            _atomic_session_bytes_write(settings, ticket, directory / artifact, payload)
            binding["content_sha256"] = hashlib.sha256(payload).hexdigest()
            save_session(settings, ticket, state)
        return text, include_diff
    except CheckpointRetryRequired:
        raise
    except (BrainError, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise CheckpointRetryRequired(ticket) from exc


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
    *,
    request_text: str | None = None,
    include_diff: bool = False,
) -> dict[str, Any] | None:
    """Durably expose bounded pinned evidence before later v5 flow construction."""
    from .investigation import (
        _exact_evidence_anchors,
        _is_runtime_entry_anchor,
        _java_file_intelligence,
        _runtime_anchor_inputs,
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
            _validate_checkpoint_artifacts(settings, ticket, number, context_id, requested_base, bundle, state)
            return existing_checkpoint
        return None
    ablations = set(str(value) for value in request.get("_evaluation_ablation") or [])
    if generation is None or "anchors" in ablations:
        return None

    entry_anchors: list[dict[str, Any]] = [
        item for item in _exact_evidence_anchors(request, bundle, generation)
        if item.get("evidence_authority") == "exact_source"
        and float(item.get("confidence") or 0) >= .9
        and _is_runtime_entry_anchor(item)
        and not is_test_path(str(item.get("path") or ""))
    ]
    resolved = resolve_runtime_anchors(
        settings, generation, _runtime_anchor_inputs(request), use_cache="generation_cache" not in ablations,
    )
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
    parsed_entry_sources: set[tuple[str, str, str]] = set()
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
        source_key = (evidence.repo, evidence.path, structural_source)
        if source_key in parsed_entry_sources:
            continue
        extracted, _ = _java_file_intelligence(
            evidence.repo, evidence.path, _evidence_id(evidence), None, structural_source,
        )
        # These anchors only select source regions below; published evidence IDs
        # still come from each region, not the first region's extraction IDs.
        parsed_entry_sources.add(source_key)
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
    request_artifact = directory / f"request-{number:03d}.yml"
    try:
        if request_text is not None:
            payload = request_text.encode("utf-8")
            if len(payload) > MAX_REQUEST_TEXT_BYTES:
                raise BrainError("Checkpoint request exceeds the supported input size")
            checkpoint["retry_request"] = {
                "artifact": request_artifact.name,
                "content_sha256": hashlib.sha256(payload).hexdigest(),
                "include_diff": include_diff,
                "execution_signature": _checkpoint_execution_signature(request_signature, include_diff),
                "atlas_generation_id": generation.identity,
                "source_signature": generation.source_signature,
            }
            _atomic_session_bytes_write(settings, ticket, request_artifact, payload)
            mark_active_artifacts(state, request_artifact)
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
        for unfinished in (artifact, handoff):
            try:
                unfinished.unlink(missing_ok=True)
            except OSError:
                pass
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


def _protocol_markdown_lines(text: str) -> Iterable[tuple[str, bool]]:
    """Identify outer protocol lines without changing any fenced source bytes."""
    fence: str | None = None
    pre = False
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        outer = False
        if pre:
            pre = line.rstrip("\r\n") != "</code></pre>"
        elif fence is not None:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
        elif marker:
            fence = marker[1]
        elif line.startswith('<pre data-language="'):
            pre = True
        else:
            outer = True
        yield line, outer


def _bounded_markdown_details(text: str, max_bytes: int) -> tuple[str, set[str]]:
    """Apply the final byte ceiling and report whole evidence regions that were omitted."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, set()
    omitted_ids: list[str] = []
    heading = re.compile(r"(?m)^### (?:\d+\. )?(E(?:-|[0-9])[A-Za-z0-9-]*)\s+—")
    # Find outer regions once; source-looking headings inside fences are data.
    sections: list[tuple[int, re.Match[str] | None]] = []
    offset = 0
    for line, outer in _protocol_markdown_lines(text):
        if outer and re.match(r"^#{2,3}\s", line):
            sections.append((offset, heading.match(line)))
        offset += len(line)
    remaining_bytes = len(encoded)
    manifest_header = (
        "\n\n## Omitted evidence IDs\n\n"
        "Each listed source region was omitted whole; no partial evidence was emitted.\n\n"
    )
    manifest_bytes = 0
    manifest_lines: list[str] = []
    removed: list[tuple[int, int]] = []
    for index in range(len(sections) - 1, -1, -1):
        start, match = sections[index]
        if match is None:
            continue
        if remaining_bytes <= max_bytes:
            break
        end = sections[index + 1][0] if index + 1 < len(sections) else len(text)
        removed.append((start, end))
        remaining_bytes -= len(text[start:end].encode("utf-8"))
        omitted_ids.append(match.group(1))
        line = f"- `{match.group(1)}` — omitted\n"
        if not manifest_lines:
            manifest_bytes = len(manifest_header.encode("utf-8"))
        manifest_lines.append(line)
        manifest_bytes += len(line.encode("utf-8"))
    pieces = []
    offset = 0
    for start, end in reversed(removed):
        pieces.append(text[offset:start])
        offset = end
    pieces.append(text[offset:])
    text = "".join(pieces)
    omission_manifest = manifest_header + "".join(reversed(manifest_lines)) if manifest_lines else ""
    if remaining_bytes + manifest_bytes <= max_bytes:
        return text + omission_manifest, set(omitted_ids)
    tail_notice = (
        "\n\n## Bounded omission manifest\n\n"
        "- The remaining lower-priority tail was omitted to satisfy the protocol UTF-8 byte limit.\n"
        "- Exact pinned source remains authoritative; request a narrower checkpoint for omitted evidence.\n"
    )
    # Retained source sections precede all removed source sections, so their
    # original offsets still apply. Do not discard a requested source page just
    # to keep lower-priority candidate metadata at the end of the message.
    retained = [
        (start, start + len(text[start:sections[index + 1][0] if index + 1 < len(sections) else len(text)].rstrip()), match[1])
        for index, (start, match) in enumerate(sections)
        if match is not None and match[1] not in omitted_ids
    ]

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

    extra_reserve = 128
    while True:
        omission_manifest = manifest_header + "".join(reversed(manifest_lines)) if manifest_lines else ""
        suffix = tail_notice
        if len((omission_manifest + suffix).encode("utf-8")) + extra_reserve <= max_bytes:
            suffix = omission_manifest + suffix
        budget = max(0, max_bytes - len(suffix.encode("utf-8")) - extra_reserve)
        prefix = text.encode("utf-8")[:budget].decode("utf-8", errors="ignore").rsplit("\n", 1)[0]
        cutoff = len(prefix)
        newly_omitted = [(start, identifier) for start, end, identifier in retained if end > cutoff]
        if newly_omitted:
            # The byte prefix may reach into a source block. Move it back to
            # that block's heading and account for every later whole block.
            cutoff = min(cutoff, min(start for start, _ in newly_omitted))
            for _, identifier in reversed(newly_omitted):
                omitted_ids.append(identifier)
                manifest_lines.append(f"- `{identifier}` — omitted\n")
            retained = [(start, end, identifier) for start, end, identifier in retained if end <= cutoff]
            text = text[:cutoff]
            continue
        result = prefix.rstrip() + close_fence(prefix) + suffix
        if len(result.encode("utf-8")) <= max_bytes:
            return (result if result.endswith("\n") else result + "\n"), set(omitted_ids)
        if not prefix:
            return suffix.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore"), set(omitted_ids)
        extra_reserve += len(result.encode("utf-8")) - max_bytes + 1


def _bounded_markdown(text: str, max_bytes: int) -> str:
    """Apply the final protocol byte ceiling without emitting invalid UTF-8 or an open fence."""
    return _bounded_markdown_details(text, max_bytes)[0]


def _bounded_protocol_context(
    text: str, max_bytes: int, evidence_ids: Iterable[str], *, emitted_ids: set[str] | None = None,
) -> str:
    """Keep protocol delivery metadata consistent with the evidence that survived bounding."""
    known = {str(identifier) for identifier in evidence_ids if identifier}
    omitted: set[str] = set()
    bounded = text
    protocol_lines = list(_protocol_markdown_lines(text))
    for _ in range(len(known) + 2):
        embedded = sorted(known - omitted)
        lines = []
        for line, outer in protocol_lines:
            if outer:
                body = line.rstrip("\r\n")
                ending = line[len(body):]
                if re.fullmatch(r"- Embedded evidence IDs: `[^`]*`", body):
                    body = f"- Embedded evidence IDs: `{', '.join(embedded) or 'none'}`"
                elif re.fullmatch(r"- Omitted evidence IDs due to byte limit: `[^`]*`", body):
                    body = f"- Omitted evidence IDs due to byte limit: `{', '.join(sorted(omitted)) or 'none'}`"
                elif omitted and body == "- Replacement status: `complete_replacement`":
                    body = "- Replacement status: `incomplete_non_replacing`"
                elif omitted and (entry := re.match(r"- `([^`]+)` ", body)) and entry[1] in omitted:
                    body = re.sub(r"`included`$", "`omitted_by_byte_limit`", body)
                line = body + ending
            lines.append(line)
        adjusted = "".join(lines)
        bounded, observed = _bounded_markdown_details(adjusted, max_bytes)
        actual = {
            match[1] for line, outer in _protocol_markdown_lines(bounded) if outer
            and (match := re.match(r"^### (?:\d+\. )?(E(?:-|[0-9])[A-Za-z0-9-]*)\s+—", line))
        }
        expanded = omitted | (observed & known) | (known - actual)
        if expanded == omitted:
            if emitted_ids is not None:
                emitted_ids.update(known - omitted)
            return bounded
        omitted = expanded
    return bounded


def pack_context(
    settings: Settings,
    ticket: str,
    request_number: int,
    bundle: ContextBundle,
    progress: dict[str, Any] | None = None,
    *,
    emitted_ids: set[str] | None = None,
) -> str:
    output = [
        "# PROJECT BRAIN CONTEXT", "", f"Ticket: `{ticket}`", f"Request: `{request_number:03d}`", "",
        "## Objective", "", *_source_markdown_block(bundle.objective, "text"), "", "## Repository state", "",
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
                f"- {int(item.get('number') or 0):03d}: {' '.join(str(item.get('objective') or '').splitlines())} "
                f"({item.get('new_evidence', 0)} new evidence regions)"
                for item in history[-8:]
            )
        if progress["no_progress_rounds"]:
            output.append(
                "- This request added no new repository evidence; that is not proof of completeness. "
                "For a material repository blocker, change the discriminating anchor or request its exact source range. "
                "Ask the user only for facts outside repository scope. Do not repeat open-ended retrieval or invent a final answer."
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
            output.append("- Suggested next action: name the missing decision-critical fact and seek focused exact evidence; produce FINAL_SOLUTION only when the evidence supports it.")
        else:
            output.append("- Suggested next action: the AI must decide whether remaining unknowns can change the implementation; if not, produce FINAL_SOLUTION.")
        coverage_map = progress.get("coverage_map") or {}
        if coverage_map:
            output.extend(["", "## Coverage Map", ""])
            output.extend(f"- `{key}`: `{value}`" for key, value in sorted(coverage_map.items()))
        investigation_memory = progress.get("investigation_memory") or {}
        if investigation_memory:
            if bundle.trace.get("direct_files_only"):
                investigation_memory = _source_request_memory(investigation_memory)
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

            output.extend(["", render_protocol_v5(progress["investigation_runtime"],
                                                   compact=bool(bundle.trace.get("direct_files_only"))), ""])
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
            summarized = 0
            if bundle.trace.get("direct_files_only"):
                requested_ids = {_public_evidence_id(progress, item) for item in bundle.evidence
                                 if "direct file request" in item.found_by}
                retained = [item for item in manifest if item.get("status") != "included"
                            or item.get("evidence_id") not in requested_ids]
                summarized = len(manifest) - len(retained)
                if summarized:
                    output.append(
                        f"- {summarized} current-request source regions catalogued; actual delivery is listed under "
                        "Embedded/Omitted evidence IDs. Repeat a needed file entry from this request if it was omitted."
                    )
                manifest = retained
            output.extend(
                f"- `{item.get('evidence_id')}` `{item.get('repo')}:{item.get('path')}:{item.get('line_start')}-{item.get('line_end')}` — `{item.get('status')}`"
                for item in manifest
            )
            if not manifest and not summarized:
                output.append("- None")
        memory_changes = progress.get("memory_changes") or {}
        if bundle.trace.get("direct_files_only"):
            memory_changes = _source_request_memory(memory_changes)
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
        output.extend(["", "## Similar ticket history (navigation only)", "",
                       *_source_markdown_block(bundle.experience.rstrip(), "text"), ""])
    output.extend(["", "## Source evidence", ""])
    if not bundle.evidence:
        output.append("No source evidence was retrieved.")
    for index, item in enumerate(_delivery_evidence(bundle), 1):
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
        emitted_ids=emitted_ids,
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


def _session_prompt(generation: Any | None) -> str:
    prompt = package_files("brain").joinpath("prompt.md").read_text(encoding="utf-8")
    if generation is not None:
        return prompt
    # Share investigation discipline, but never advertise Atlas-only requests
    # to a legacy source-pin ticket. This does not migrate or replace its pin.
    common, marker, _ = prompt.partition("## Request contract\n")
    if not marker:
        raise BrainError("Packaged investigation prompt is missing its request contract")
    return common + """## Request contract

This ticket uses legacy source pinning, not an Atlas generation. This ticket-specific contract takes precedence over generic v5 examples. Continue it with CONTEXT_REQUEST version 2; do not send INVESTIGATION_REQUEST, mode, wave, checkpoint, or base_context_id. Do not refresh or restart the ticket to obtain evidence. Additional requests keep the original source pin and use normal per-request resource limits, with no extra round approval.

Use one focused source request, replacing the example query with a discriminating symbol or literal from this ticket:

```yaml
CONTEXT_REQUEST:
  version: 2
  objective: State the exact repository fact this request must establish.
  searches:
    - query: KnownSymbolOrLiteral
```

For an exact known file, replace searches with files entries containing repo, repository-relative path, and lines: "start-end". Request at most 2,000 lines per entry, preferably the relevant method. Follow returned line ranges and request missing ranges separately; do not assume a truncated excerpt is a whole file. Never guess paths or ask the user to copy source Brain can read. Only exact retained source blocks are evidence authority; search results are navigation, not proof. Never substitute a newer Atlas generation.
"""


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
    prompt = _session_prompt(pinned_atlas)
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
            # Global maps are navigation hints, not the ticket's evidence. A
            # whole workspace graph need not occupy every new chat window.
            text, omitted = _bounded_text_file(path, min(MAX_START_KNOWLEDGE_ITEM_BYTES, 8_000))
            sections.extend([f"## {title}", "", text.strip()])
            if omitted:
                sections.append("[Project Brain omitted unsafe or excess bytes. Navigation excerpt only; ask Brain for focused evidence instead of copying the full workspace map.]")
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


def investigation_continuation(settings: Settings, state: dict[str, Any]) -> dict[str, Any]:
    """Describe the next bounded request; prior usage is telemetry, not a quota."""
    runtime = state.get("investigation_runtime") or {}
    wave = int(runtime.get("wave") or 0)
    used = max(
        int(state.get("physical_operations_total") or 0),
        int((runtime.get("bounds") or {}).get("physical_operations_used") or 0),
        sum(int((item.get("retrieval") or {}).get("physical_backend_operations") or 0)
            for item in state.get("request_history") or [] if isinstance(item, dict)),
    )
    return {
        "required": False, "reason": "", "completed_wave": wave, "next_wave": wave + 1,
        "token": hashlib.sha256(json.dumps({
            "ticket": state.get("ticket"), "generation": state.get("atlas_generation_id"),
            "started_at": state.get("started_at"),
            "sources": state.get("source_signature"), "context": state.get("last_context_id"),
            "wave": wave, "physical_operations": used,
            "operation_limit": settings.max_backend_operations, "context_limit": settings.hard_context_chars,
        }, sort_keys=True).encode("utf-8")).hexdigest(),
        "generation": state.get("generation"), "physical_operations_used": used,
        "physical_operations_per_wave": settings.max_backend_operations,
        "context_bytes_per_wave": settings.hard_context_chars,
    }


@ticket_retrieval_exclusive
@source_verification_scope()
def create_context(
    settings: Settings,
    ticket: str,
    request_text: str,
    include_diff: bool = False,
    progress: Any | None = None,
    *,
    continue_investigation: bool = False,
    continuation_token: str | None = None,
) -> tuple[str, Path, int]:
    if not isinstance(continue_investigation, bool):
        raise BrainError("continue_investigation must be a boolean user action")
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
    continuation = investigation_continuation(settings, state)
    if continue_investigation and continuation_token is not None and continuation_token != continuation["token"]:
        raise InvestigationContinuationRequired("Investigation changed after approval; classify the latest request and confirm continuation again")
    prior_physical_operations = continuation["physical_operations_used"]
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
        expected_wave = continuation["next_wave"]
        if request.get("wave") is not None and int(request["wave"]) != expected_wave:
            raise InvestigationContinuationRequired(
                f"Protocol v5 wave must be {expected_wave} for this ticket; use that number or omit wave, then classify the request again"
            )
        if progress_callback is not None:
            progress_callback({"phase": "wave_started", "wave": expected_wave, "generation": atlas_generation.generation})
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
        elif request.get("version") == 4 and number % settings.context_checkpoint_interval == 0:
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
        checkpoint_retry_request(settings, ticket)
        raise CheckpointRetryRequired(ticket, retry_available=True)
    progressive_checkpoint: dict[str, Any] | None = None
    durable_checkpoint_state: dict[str, Any] | None = None
    if (
        isinstance(failed_checkpoint, dict)
        and failed_checkpoint.get("continuation_status") in {"pending", "failed"}
        and failed_checkpoint.get("request_signature") == plan["signature"]
    ):
        checkpoint_retry_request(settings, ticket, manual_request=(request_text, include_diff))
        # A repaired request binding is authoritative before any retrieval work.
        state = session_state(settings, ticket)
        pre_wave_state = copy.deepcopy(state)
        failed_checkpoint = state.get("progressive_checkpoint")
    context_committed = False
    continuation_path: Path | None = None
    continuation_handoff: Path | None = None
    continuation_event: dict[str, Any] | None = None
    completion_event: dict[str, Any] | None = None
    try:
        bundle = retrieve_context(retrieval_settings, request, include_diff=include_diff, progress=progress_callback)
        if request.get("version") == 5 and int(bundle.trace.get("physical_backend_operations") or 0) > settings.max_backend_operations:
            raise BrainError("Protocol v5 request exceeded its per-request physical-operation budget")
        from .query import merge_evidence

        bundle.evidence = merge_evidence(bundle.evidence + _external_evidence(settings, ticket))
        checkpoint_restore_missed = 0
        if full_checkpoint and state.get("evidence_records"):
            checkpoint_source_budget = max(
                0,
                settings.hard_context_chars
                - 40_000
                - sum(len(item.content.encode("utf-8")) for item in bundle.evidence),
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
        if request.get("version") != 5:
            for identifier, record in evidence_records.items():
                if previous_records.get(identifier, {}).get("public_id"):
                    record["public_id"] = previous_records[identifier]["public_id"]
        new_evidence_ids = set(evidence_records) - set(previous_records)
        # Retrieval identity is not delivery: a whole region removed by the
        # message byte ceiling must remain eligible for a later focused delta.
        delivery_evidence_ids = new_evidence_ids | {
            identifier for identifier in evidence_records
            if not previous_records.get(identifier, {}).get("emitted_in_context")
        }
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

            if (
                isinstance(failed_checkpoint, dict)
                and failed_checkpoint.get("continuation_status") in {"pending", "failed"}
                and failed_checkpoint.get("request_signature") == plan["signature"]
            ):
                # The early artifact has already promised this operation's ID.
                # Retry coverage may change with budgets/cache warmth; finalize
                # only this uncommitted reservation, never a completed context.
                context_id = str(failed_checkpoint.get("context_id") or "")
                reserved = [key for key, value in registry.items() if value == context_id]
                # Older early-checkpoint writers may not have reserved a hash.
                # Accept only the next normal allocation, never an arbitrary ID.
                if not reserved and _allocate({"contexts": registry}, "contexts", context_hash, "CTX-", 3) == context_id:
                    reserved = [context_hash]
                if (
                    len(reserved) != 1
                    or context_id == state.get("last_context_id")
                    or any(row.get("context_id") == context_id for row in state.get("context_lineage") or [])
                    or registry.get(context_hash, context_id) != context_id
                ):
                    raise BrainError("Pending checkpoint has no uncommitted context reservation")
                del registry[reserved[0]]
                registry[context_hash] = context_id
            else:
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
            # Only these adjacent phases share resolver envelopes. No earlier
            # retrieval hit/source entries are carried forward; each invocation
            # (including nested contexts and retries) gets a fresh scope.
            anchor_cache_token = _ACTIVE_RETRIEVAL_CACHE.set({})
            anchor_trace_token = _ACTIVE_RETRIEVAL_TRACE.set(None)
            try:
                progressive_checkpoint = _publish_first_useful_checkpoint(
                    settings, ticket, number, context_id, requested_base, bundle, request, plan["signature"],
                    state, directory,
                    progress_callback,
                    request_text=request_text, include_diff=include_diff,
                )
                if progressive_checkpoint is not None:
                    durable_checkpoint_state = json.loads(json.dumps(state))
                runtime_started = time.perf_counter()
                runtime = build_ticket_runtime(
                    settings, atlas_generation, request, bundle, state, context_id=context_id,
                    next_best_evidence=next_evidence,
                    validated_prior_evidence_ids=validated_prior_evidence_ids,
                    continue_investigation=continue_investigation,
                )
            finally:
                _ACTIVE_RETRIEVAL_TRACE.reset(anchor_trace_token)
                _ACTIVE_RETRIEVAL_CACHE.reset(anchor_cache_token)
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
        if request.get("version") == 5 and not full_checkpoint:
            for key, value in list(memory_changes.items()):
                prior = memory_before.get(key)
                if isinstance(value, list) and isinstance(prior, list):
                    before_items = {json.dumps(item, sort_keys=True, ensure_ascii=False): item for item in prior}
                    after_items = {json.dumps(item, sort_keys=True, ensure_ascii=False): item for item in value}
                    memory_changes[key] = {
                        "added": [item for identity, item in after_items.items() if identity not in before_items],
                        "removed": [item for identity, item in before_items.items() if identity not in after_items],
                    }
                    if key in {"verified_facts", "verified_references", "implementation_surface", "test_surface"} and memory_changes[key]["removed"]:
                        # Old session summaries may be forged or fail current
                        # proof validation. Do not echo them, even as removals.
                        memory_changes[key] = {
                            "reset": True, "verified_count": len(value),
                            "authority": "Use pinned source blocks and evidence lineage, not retained memory summaries.",
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
        emitted_ids: set[str] = set()
        content = (
            pack_context(retrieval_settings, ticket, number, bundle, investigation_progress, emitted_ids=emitted_ids)
            if full_checkpoint
            else pack_delta_context(retrieval_settings, ticket, number, bundle, investigation_progress, delivery_evidence_ids, emitted_ids=emitted_ids)
        )
        for identifier, record in lineage_records.items():
            record["emitted_in_context"] = bool(
                previous_records.get(identifier, {}).get("emitted_in_context")
                or str(record.get("public_id") or identifier) in emitted_ids
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
        if not (progressive_checkpoint or {}).get("retry_request"):
            _atomic_session_text_write(settings, ticket, request_path, request_text.rstrip() + "\n")
        _atomic_session_text_write(settings, ticket, path, content)
        state["requests"] = number
        state["physical_operations_total"] = prior_physical_operations + int(bundle.trace.get("physical_backend_operations") or 0)
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
        if bundle.trace.get("qualified_symbols_only") and bundle.trace.get("semantic_status") == "not_requested":
            effective_edition = "Core"  # Deliberate exact-source retrieval, not a failed Precision attempt.
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
            "user_approved_continuation": bool(request.get("version") == 5 and continue_investigation),
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
            phase = "investigation_paused" if runtime.get("stop_reason") != "continue" else "wave_complete"
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
        context_committed = True
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
        if context_committed:
            # A notification failure cannot undo authoritative publication.
            raise ContextDeliveryError(ticket, path.name) from None
        if progressive_checkpoint is None:
            # The early progress callback can fail after the checkpoint save,
            # before _publish_first_useful_checkpoint returns to this caller.
            try:
                persisted = session_state(settings, ticket)
                saved = persisted.get("progressive_checkpoint") or {}
                if (persisted != pre_wave_state
                    and saved.get("continuation_status") in {"pending", "failed"}
                    and saved.get("request_signature") == plan["signature"]):
                    progressive_checkpoint = saved
                    durable_checkpoint_state = persisted
            except (BrainError, OSError):
                pass
        cleanup = [path, trace_path, continuation_path, continuation_handoff]
        retained_checkpoint = (durable_checkpoint_state or {}).get("progressive_checkpoint") or failed_checkpoint or {}
        retained_binding = retained_checkpoint.get("retry_request") or {}
        if not (isinstance(retained_binding, dict)
                and retained_binding.get("artifact") == request_path.name
                and retained_checkpoint.get("artifact") == f"checkpoint-{number:03d}.md"
                and retained_checkpoint.get("continuation_status") in {"pending", "failed"}
                and retained_checkpoint.get("request_signature") == plan["signature"]
                and retained_binding.get("include_diff") is include_diff
                and retained_binding.get("execution_signature") == _checkpoint_execution_signature(plan["signature"], include_diff)
                and atlas_generation is not None
                and retained_binding.get("atlas_generation_id") == atlas_generation.identity
                and retained_binding.get("source_signature") == atlas_generation.source_signature):
            cleanup.append(request_path)
        for unfinished in cleanup:
            if unfinished is not None:
                try:
                    unfinished.unlink(missing_ok=True)
                except OSError:
                    pass  # Preserve the original failure if cleanup is denied.
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
        raise ClipboardWriteError("No clipboard command found; use the generated file")
    result = run(command, input_text=text)
    if result.returncode != 0:
        raise ClipboardWriteError(f"Clipboard write failed: {result.stderr.strip()}")


@ticket_retrieval_exclusive
def resume_session(
    settings: Settings, ticket: str, notes: str = "", *, target: str | None = None, copy: bool = False,
) -> tuple[str, Path]:
    """Export bounded chat recovery without searching, changing pins or spending a wave."""
    from .atlas import _bounded_json_projection

    directory = session_dir(settings, ticket)
    if not directory.is_dir():
        raise BrainError(f"Session {ticket} does not exist")
    target = delivery_target(settings, ticket, target)
    if len(notes.encode("utf-8")) > 8_000:
        raise BrainError("Conversation handover notes exceed the 8,000-byte limit")
    state = session_state(settings, ticket)
    generation, _ = _resolve_session_generation(settings, state)
    checkpoint = state.get("progressive_checkpoint") or {}
    if checkpoint.get("continuation_status") in {"pending", "failed"}:
        raise BrainError("Finish or retry the pending checkpoint continuation before exporting a new-chat handover")
    if state.get("stable_identities"):
        from .investigation import validate_stable_identity_registry

        validate_stable_identity_registry(state)
    pinned_paths = _validated_session_snapshot_paths(settings, state, generation)
    pinned = replace(
        settings, atlas_generation=generation,
        atlas_generation_mode="pinned" if generation is not None else "legacy_source_pin",
        repositories=[replace(repo, source_path=pinned_paths.get(repo.name),
                              source_sha=str(((state.get("sources") or {}).get(repo.name) or {}).get("sha") or "") or None)
                      for repo in settings.repositories],
    )
    records = [item for item in state.get("evidence_records") or [] if isinstance(item, dict)]
    public_ids = {str(item.get("evidence_id")): str(item.get("public_id") or item.get("evidence_id")) for item in records}
    # Rehydrate a small recent-ID working set, not every source ever seen. Hash
    # verification is the same as checkpoint recovery; no newer-source fallback.
    recent = sorted(records, key=lambda item: (str(item.get("public_id") or ""), str(item.get("evidence_id") or "")), reverse=True)[:8]
    restored, missed = _restore_checkpoint_evidence(pinned, recent, max_chars=16_000)
    embedded = {public_ids[_evidence_id(item)] for item in restored}
    runtime = state.get("investigation_runtime") or {}
    memory = state.get("investigation_memory") or {}
    ticket_text = _read_session_artifact(settings, ticket, directory / "ticket.md", MAX_START_TICKET_BYTES)
    ticket_excerpt, ticket_omitted = _bounded_utf8_text(ticket_text, 8_000, "\n[Ticket text omitted; ask for the remaining acceptance criteria.]\n")
    output = [
        "# PROJECT BRAIN — RESUME", "", f"Ticket: `{ticket}`", f"Request: `{int(state.get('requests') or 0):03d}`",
        f"Context ID: `{state.get('last_context_id') or 'none'}`",
        f"Pinned Atlas identity: `{state.get('atlas_generation_id') or 'legacy_source_pin'}`",
        f"Pinned generation: `{state.get('generation')}`", f"Source signature: `{state.get('source_signature')}`", "",
        "## New conversation contract", "",
        "This is a bounded, non-replacing handover, not the complete previous conversation or a new investigation.",
        "Continue this same ticket. Do not reset its wave or refresh to recover evidence.",
        (f"Next wave: `{investigation_continuation(settings, state)['next_wave']}`; continue from the Context ID above."
         if generation is not None else "Legacy source-pin requests do not send wave or base_context_id."),
        "No extra round approval is required. Preserve this ticket's generation and context lineage.",
        "Only source blocks embedded below have been re-read and hash-verified for this handover. IDs in the manifest are references, not visible proof.",
        "Coverage, hypotheses and earlier decisions are navigation state, not a verified root cause. Re-read missing decision-critical source using files entries (repo, path, lines) in the request contract below. Known IDs requested this way are re-emitted.",
        "Brain cannot recover reasoning or user answers that stayed only in the old chat. Ask for a concise handover of those decisions if absent; do not invent them.",
        "Do not paste every old context into the new chat. Maintain a short decision ledger: claim, supporting/refuting E IDs, one material missing fact, and the next focused request.",
        "", "## Operating protocol", "", _session_prompt(generation),
        "", "## Ticket", "", *_source_markdown_block(ticket_excerpt, "text"),
        f"Ticket text complete: `{not ticket_omitted}`", "", "## Conversation handover notes (user supplied; not source evidence)", "",
        *_source_markdown_block(notes or "Not supplied. Prior chat-only reasoning is not available to Brain.", "text"),
    ]
    sections = (
        ("Latest objective", [memory.get("objective") or ""], 1, 4_500),
        ("Runtime observations (not repository proof)", memory.get("runtime_facts") or [], 12, 2_000),
        ("Hypothesis Ledger", (runtime.get("hypothesis_ledger") or {}).get("items") or memory.get("hypotheses") or [], 12, 3_000),
        ("Evidence Frontier", (runtime.get("evidence_frontier") or {}).get("items") or memory.get("blocking_unknowns") or [], 12, 4_000),
    )
    for title, values, count, size in sections:
        projected = _bounded_json_projection(values, max_items=count, max_bytes=size)
        output.extend(["", f"## {title} (bounded navigation)", "",
                       *_source_markdown_block(json.dumps(projected, ensure_ascii=False), "json"),
                       f"Items included: `{len(projected)}/{len(values)}`"])
    manifest = [{"evidence_id": public_ids[str(item.get("evidence_id"))], "repo": item.get("repo"),
                 "path": item.get("path"), "lines": f"{item.get('line_start')}-{item.get('line_end')}"}
                for item in sorted(records, key=lambda item: str(item.get("public_id") or item.get("evidence_id")))]
    projected_manifest = _bounded_json_projection(manifest, max_items=100, max_bytes=12_000)
    output.extend(["", "## Retained evidence manifest (bounded)", "",
                   f"Manifest entries included: `{len(projected_manifest)}/{len(manifest)}`; omitted entries remain in the local ticket session.",
                   *_source_markdown_block(json.dumps(projected_manifest, ensure_ascii=False), "json"),
                   "", "## Evidence lineage", "",
                   f"- Embedded evidence IDs: `{', '.join(sorted(embedded)) or 'none'}`",
                   "- Omitted evidence IDs due to byte limit: `none`",
                   f"- Recent regions unavailable, changed, or outside the source budget: `{missed}`",
                   "", "## Re-verified source evidence", ""])
    for item in restored:
        output.extend([f"### {public_ids[_evidence_id(item)]} — {item.repo} — `{item.path}:{item.line_start}-{item.line_end}`", "",
                       *_source_markdown_block(item.content, _language(item.path)), ""])
    content = _bounded_protocol_context("\n".join(output) + "\n", min(settings.hard_context_chars, 64_000), embedded)
    path = directory / "resume.md"
    _atomic_session_text_write(settings, ticket, path, content)
    mark_active_artifacts(state, path)
    save_session(settings, ticket, state)
    # Keep export and delivery under the same ticket/workspace lease: another
    # wave must not publish between reading this base and selecting its handoff.
    deliver(settings, ticket, content, target, copy=copy)
    return content, path


def delivery_target(settings: Settings, ticket: str, target: str | None = None) -> str:
    value = target or (session_state(settings, ticket).get("delivery") or {}).get("target") or "claude"
    if not isinstance(value, str) or value not in {"claude", "m365"}:
        raise BrainError("target must be claude or m365")
    return str(value)


@ticket_exclusive
def deliver(settings: Settings, ticket: str, text: str, target: str | None, *, copy: bool) -> tuple[list[Path], int]:
    try:
        return _prepare_delivery(settings, ticket, text, target, copy=copy)
    except (OSError, ClipboardWriteError) as error:
        match = re.search(r"(?m)^Request: `(\d+)`", text) if text.startswith("# PROJECT BRAIN CONTEXT") else None
        if match:
            number = int(match.group(1))
            state = session_state(settings, ticket)
            artifact = f"context-{number:03d}.md"
            if any(isinstance(item, dict) and item.get("number") == number for item in state.get("request_history") or []):
                try:
                    directory = session_dir(settings, ticket)
                    saved = read_managed_bytes(directory, directory / artifact, max_bytes=MAX_DELIVERY_ARTIFACT_BYTES)
                except (BrainError, OSError, ValueError):
                    pass
                else:
                    if saved == text.encode("utf-8"):
                        raise ContextDeliveryError(ticket, artifact) from error
        raise


def _prepare_delivery(settings: Settings, ticket: str, text: str, target: str | None, *, copy: bool) -> tuple[list[Path], int]:
    from .agent import final_solution_contract

    directory = session_dir(settings, ticket)
    state = session_state(settings, ticket)
    target = delivery_target(settings, ticket, target)
    # Both chat transports expose the same per-ticket handoff location. Retain
    # clipboard parts and the internal copy for existing clients and sessions.
    handoff_directory = handoff_dir(settings, ticket)
    current_handoff = handoff_directory / "current.md"
    _validated_generated_artifact(settings, current_handoff)
    _atomic_session_text_write(settings, ticket, directory / "current-handoff.md", text)
    _atomic_generated_text_write(settings, current_handoff, text)
    if text.startswith("# PROJECT BRAIN — START"):
        label = "start"
    elif text.startswith("# PROJECT BRAIN — RESUME"):
        label = "resume"
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
    if target == "claude":
        parts = chunk_text(text, settings.clipboard_chunk_chars)
        paths = []
        for index, part in enumerate(parts, 1):
            header = f"PROJECT BRAIN CONTEXT — PART {index} OF {len(parts)}\n\n" if len(parts) > 1 else ""
            path = directory / "delivery" / f"part-{index:03d}.txt"
            _atomic_session_text_write(settings, ticket, path, header + part)
            paths.append(path)
    size = len(text.encode("utf-8"))
    state["delivery"] = {"target": target, "parts": [str(path) for path in paths], "current": 1,
                         "handoff": str(current_handoff), "latest": str(handoff), "bytes": size}
    usage = dict(state.get("delivery_usage") or {})
    usage["prepared_bytes"] = int(usage.get("prepared_bytes") or 0) + size
    usage["since_resume_bytes"] = (0 if label == "resume" else int(usage.get("since_resume_bytes") or 0)) + size
    state["delivery_usage"] = usage
    save_session(settings, ticket, state)
    if copy:
        clipboard_write(delivery_artifact(settings, ticket, paths[0])[1])
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
            r"^(?:current|start|final|update|resume|context-\d+|evidence-\d+|feedback-\d+|"
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

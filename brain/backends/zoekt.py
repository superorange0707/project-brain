from __future__ import annotations

import base64
import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..core import Repository, Settings


@dataclass(frozen=True)
class ZoektStatus:
    available: bool
    executable: str | None
    indexer: str | None
    reason: str | None = None


def status() -> ZoektStatus:
    executable = shutil.which("zoekt")
    indexer = shutil.which("zoekt-index")
    if executable and indexer:
        return ZoektStatus(True, executable, indexer)
    missing = ", ".join(name for name, value in (("zoekt", executable), ("zoekt-index", indexer)) if not value)
    return ZoektStatus(False, executable, indexer, f"Zoekt {missing} is not installed; SQLite FTS5/ripgrep fallback is active")


def shard_path(state_dir: Path, repo: str, sha: str) -> Path:
    return state_dir / "zoekt" / repo / sha


def _manifest(target: Path) -> dict[str, Any] | None:
    try:
        value = json.loads((target / "brain-shard.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def build(settings: Settings, repositories: list[Repository]) -> dict[str, dict[str, object]]:
    """Build immutable per-repository/snapshot local shards when Zoekt is installed."""
    available = status()
    if not available.available or not available.indexer:
        return {}
    result: dict[str, dict[str, object]] = {}
    for repo in repositories:
        sha = repo.source_sha or "working-tree"
        target = shard_path(settings.state_dir, repo.name, sha)
        current = _manifest(target)
        if current and current.get("source_sha") == sha:
            result[repo.name] = {"path": str(target), "source_sha": sha, "status": "current"}
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f"{sha[:12]}-", dir=target.parent))
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                [available.indexer, "-index", str(temporary), str(repo.scan_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=120,
                check=False,
            )
            if completed.returncode != 0 or not any(temporary.iterdir()):
                result[repo.name] = {"status": "failed", "reason": "zoekt indexing failed"}
                continue
            (temporary / "brain-shard.json").write_text(json.dumps({"source_sha": sha, "repo": repo.name}), encoding="utf-8")
            if target.exists():
                shutil.rmtree(target)
            temporary.replace(target)
            result[repo.name] = {"path": str(target), "source_sha": sha, "status": "built", "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)}
        except (OSError, subprocess.TimeoutExpired):
            result[repo.name] = {"status": "failed", "reason": "zoekt indexing unavailable or timed out"}
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
    return result


def _field(value: dict[str, object], *names: str) -> object | None:
    for name in names:
        if name in value:
            return value[name]
    return None


def _line_text(match: dict[str, object]) -> str | None:
    value = _field(match, "Line", "line_text", "text")
    if not isinstance(value, str):
        return None
    # The current Zoekt JSONL CLI serializes Go []byte line content as base64;
    # the compatibility form used by older tools/tests carries plain text.
    if "LineStart" not in match:
        return value
    try:
        return base64.b64decode(value, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def search(settings: Settings, repo: Repository, pattern: str, *, fixed: bool, max_results: int) -> tuple[list[tuple[str, int, str, float]], dict[str, object]] | None:
    """Use an immutable local shard, or return None so Core can use its fallback."""
    available = status()
    sha = repo.source_sha or "working-tree"
    target = shard_path(settings.state_dir, repo.name, sha)
    manifest = _manifest(target)
    if not available.available or not available.executable or not manifest or manifest.get("source_sha") != sha:
        return None
    query = "content:" + json.dumps(pattern) if fixed else "regex:" + pattern
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [available.executable, "-index_dir", str(target), "-jsonl", query],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode:
        return None
    rows: list[tuple[str, int, str, float]] = []
    for raw in completed.stdout.splitlines():
        try:
            file = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(file, dict):
            continue
        path = _field(file, "FileName", "file_name", "path")
        matches = _field(file, "LineMatches", "line_matches")
        if not isinstance(path, str) or not isinstance(matches, list):
            continue
        score = float(_field(file, "Score", "score") or 0)
        for match in matches:
            if not isinstance(match, dict):
                continue
            line = _field(match, "LineNumber", "line_number", "line")
            text = _line_text(match)
            if not isinstance(line, int) or text is None:
                continue
            if fixed and pattern not in text:
                continue
            rows.append((path, line, text.rstrip("\n"), score))
            if len(rows) >= max_results:
                break
        if len(rows) >= max_results:
            break
    return rows, {"elapsed_ms": round((time.perf_counter() - started) * 1000, 3), "raw_hits": len(rows), "shard": str(target)}

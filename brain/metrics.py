from __future__ import annotations

import json
import math
import os
import platform
import stat
import threading
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import __version__
from .platforms import (
    atomic_managed_bytes_write,
    atomic_managed_text_write,
    open_managed_lock,
    run_bounded_process,
)

if TYPE_CHECKING:
    from .core import Settings


MAX_METRIC_ROW_BYTES = 256 * 1024
MAX_METRIC_FIELD_BYTES = 64 * 1024
MAX_METRIC_HISTORY_BYTES = 8 * 1024 * 1024
MAX_METRIC_HISTORY_ROWS = 10_000
_METRIC_THREAD_LOCK = threading.Lock()


def _bounded_metric_row(event: str, values: dict[str, Any]) -> bytes:
    row: dict[str, Any] = {"timestamp": datetime.now(UTC).isoformat(), "event": event}
    omitted: list[str] = []
    for key in sorted(values):
        try:
            candidate = json.dumps(values[key], separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError):
            omitted.append(key)
            continue
        if len(candidate) > MAX_METRIC_FIELD_BYTES:
            omitted.append(key)
            continue
        row[key] = values[key]
        encoded = json.dumps(row, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_METRIC_ROW_BYTES:
            row.pop(key, None)
            omitted.append(key)
    if omitted:
        row["omitted_fields"] = omitted[:256]
    encoded = json.dumps(row, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_METRIC_ROW_BYTES:
        row = {
            "timestamp": row["timestamp"], "event": event,
            "omitted_fields": ["metric payload exceeded the row limit"],
        }
        encoded = json.dumps(row, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return encoded + b"\n"


def record_metric(settings: Settings, event: str, **values: Any) -> None:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    path = settings.state_dir / "metrics.jsonl"
    payload = _bounded_metric_row(event, values)
    if len(payload) > MAX_METRIC_HISTORY_BYTES:
        raise OSError("metric row exceeds the retained history byte limit")
    lock_path = settings.state_dir / "metrics.lock"
    with _METRIC_THREAD_LOCK:
        handle = open_managed_lock(settings.state_dir, lock_path)
        try:
            from .locks import _acquire, _release

            while True:
                try:
                    _acquire(handle)
                    break
                except BlockingIOError:
                    time.sleep(0.01)
            try:
                existing_size = 0
                if path.exists() or path.is_symlink():
                    metadata = path.lstat()
                    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                        raise OSError("metrics history must be a direct regular file")
                    existing_size = metadata.st_size
                if existing_size + len(payload) > MAX_METRIC_HISTORY_BYTES:
                    keep = max(0, MAX_METRIC_HISTORY_BYTES - len(payload))
                    retained = b""
                    if existing_size and keep:
                        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                        try:
                            opened = os.fstat(descriptor)
                            if not stat.S_ISREG(opened.st_mode):
                                raise OSError("metrics history must be a direct regular file")
                            start = max(0, existing_size - keep)
                            os.lseek(descriptor, start, os.SEEK_SET)
                            retained = os.read(descriptor, keep)
                        finally:
                            os.close(descriptor)
                        if start and b"\n" in retained:
                            retained = retained.split(b"\n", 1)[1]
                        elif start:
                            retained = b""
                    atomic_managed_bytes_write(
                        settings.state_dir, path, retained[-keep:] + payload,
                    )
                else:
                    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                    descriptor = os.open(path, flags, 0o600)
                    try:
                        opened = os.fstat(descriptor)
                        metadata = path.lstat()
                        if (
                            path.is_symlink()
                            or not stat.S_ISREG(opened.st_mode)
                            or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
                        ):
                            raise OSError("metrics history identity changed while opening")
                        os.write(descriptor, payload)
                    finally:
                        os.close(descriptor)
            finally:
                _release(handle)
        finally:
            handle.close()


def trace_metadata(settings: Settings) -> dict[str, Any]:
    brain_sha = None
    for root in (settings.root, Path(__file__).resolve().parents[1]):
        try:
            result = run_bounded_process(
                ["git", "rev-parse", "HEAD"], root,
                max_stdout_bytes=256, max_stderr_bytes=4096, timeout=5,
                environment={**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"},
            )
            candidate = result.stdout.strip() if (
                result.returncode == 0
                and not getattr(result, "timed_out", False)
                and not getattr(result, "output_truncated", False)
            ) else ""
        except OSError:
            candidate = ""
        if candidate:
            brain_sha = candidate
            break
    snapshots = sorted((repo.name, repo.source_sha or "working-tree") for repo in settings.repositories)
    import hashlib

    corpus_signature = hashlib.sha256(json.dumps(snapshots).encode("utf-8")).hexdigest()
    return {
        "machine": platform.machine(),
        "os": platform.platform(),
        "python": platform.python_version(),
        "brain_version": __version__,
        "brain_sha": brain_sha,
        "corpus_signature": corpus_signature,
    }


def machine_profile(settings: Settings) -> dict[str, Any]:
    """Return a local, non-identifying machine profile for later comparison.

    The profile deliberately excludes host names, serial numbers, repository
    paths, and environment variables.  It captures only the inputs that affect
    a local model/index benchmark and is therefore safe to retain beneath the
    owner-only Brain state directory.
    """
    memory_bytes: int | None = None
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        pages = int(os.sysconf("SC_PHYS_PAGES"))
        if page_size > 0 and pages > 0:
            memory_bytes = page_size * pages
    except (AttributeError, OSError, ValueError):
        pass
    return {
        "schema_version": 1,
        "recorded_at": datetime.now(UTC).isoformat(),
        "brain_version": __version__,
        "os": platform.platform(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "logical_cpu_count": os.cpu_count(),
        "physical_memory_bytes": memory_bytes,
        "physical_memory_gb": round(memory_bytes / 1_000_000_000, 2) if memory_bytes else None,
    }


def write_machine_profile(settings: Settings) -> dict[str, Any]:
    """Persist the current local benchmark target without device identifiers."""
    profile = machine_profile(settings)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    target = settings.state_dir / "machine-profile.json"
    atomic_managed_text_write(settings.state_dir, target, json.dumps(profile, indent=2) + "\n")
    return profile


def record_trace(settings: Settings, ticket: str, number: int, trace: dict[str, Any]) -> None:
    from .core import _atomic_session_text_write, session_dir

    payload = {"ticket": ticket, "request": number, **trace_metadata(settings), **trace}
    path = session_dir(settings, ticket) / f"trace-{number:03d}.json"
    _atomic_session_text_write(settings, ticket, path, json.dumps(payload, indent=2) + "\n")
    record_metric(settings, "retrieve_trace", **payload)


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1)]


def benchmark_report(settings: Settings) -> dict[str, Any]:
    path = settings.state_dir / "metrics.jsonl"
    if not path.is_file():
        return {"samples": 0, "events": {}}
    try:
        if path.is_symlink():
            return {"samples": 0, "events": {}}
        with path.open("rb") as source:
            size = source.seek(0, os.SEEK_END)
            start = max(0, size - MAX_METRIC_HISTORY_BYTES)
            source.seek(start)
            raw = source.read(MAX_METRIC_HISTORY_BYTES + 1)
    except OSError:
        return {"samples": 0, "events": {}}
    if start and b"\n" in raw:
        raw = raw.split(b"\n", 1)[1]
    lines = raw.splitlines()[-MAX_METRIC_HISTORY_ROWS:]
    rows: list[dict[str, Any]] = []
    for raw_line in lines:
        try:
            row = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(row, dict) and row.get("event"):
            rows.append(row)

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["event"])].append(row)
    events: dict[str, Any] = {}
    for event, samples in sorted(grouped.items()):
        timings: dict[str, Any] = {}
        keys = sorted({key for sample in samples for key in sample if key.endswith("_ms")})
        for key in keys:
            values = [float(sample[key]) for sample in samples if isinstance(sample.get(key), (int, float))]
            if values:
                timings[key] = {
                    "p50": round(_percentile(values, 0.50), 3),
                    "p95": round(_percentile(values, 0.95), 3),
                    "max": round(max(values), 3),
                }
        events[event] = {"samples": len(samples), "timings": timings, "latest": samples[-1]}
    return {"samples": len(rows), "events": events, "environment": trace_metadata(settings)}

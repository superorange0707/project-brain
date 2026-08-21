from __future__ import annotations

import json
import math
import os
import platform
import subprocess
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import __version__

if TYPE_CHECKING:
    from .core import Settings


def record_metric(settings: Settings, event: str, **values: Any) -> None:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    row = {"timestamp": datetime.now(UTC).isoformat(), "event": event, **values}
    with (settings.state_dir / "metrics.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, separators=(",", ":")) + "\n")


def trace_metadata(settings: Settings) -> dict[str, Any]:
    brain_sha = None
    for root in (settings.root, Path(__file__).resolve().parents[1]):
        try:
            candidate = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, check=False,
            ).stdout.strip()
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
    temporary = target.with_suffix(".writing")
    temporary.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return profile


def record_trace(settings: Settings, ticket: str, number: int, trace: dict[str, Any]) -> None:
    from .core import session_dir

    payload = {"ticket": ticket, "request": number, **trace_metadata(settings), **trace}
    path = session_dir(settings, ticket) / f"trace-{number:03d}.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    record_metric(settings, "retrieve_trace", **payload)


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1)]


def benchmark_report(settings: Settings) -> dict[str, Any]:
    path = settings.state_dir / "metrics.jsonl"
    if not path.is_file():
        return {"samples": 0, "events": {}}
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-10_000:]:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
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

from __future__ import annotations

import time
from typing import Any

from .core import Evidence, SearchHit, Settings, _bounded_utf8_text

MAX_RERANK_SOURCE_BYTES = 8 * 1024 * 1024
MAX_RERANK_SOURCE_SECONDS = 0.25


def rerank_snippets(
    settings: Settings, hits: list[SearchHit], positions: list[int],
    source_cache: dict[tuple[str, str], str], *, trace: Any | None = None,
) -> dict[int, str]:
    """Replace navigation labels with bounded pinned-code previews, not evidence."""
    from .core import BrainError, _retrieval_source_path
    from .index import read_generation_files
    from .retrieval.ranker import _SOURCE_CHANNELS

    snippets = {index: str(hits[index].text) for index in positions}
    if settings.atlas_generation_mode != "pinned" or settings.atlas_generation is None:
        return snippets
    pending = [index for index in positions if not _SOURCE_CHANNELS.intersection(hits[index].found_by)]
    if not pending:
        return snippets
    for index in pending:
        snippets.pop(index)
    started = time.perf_counter()
    deadline = started + MAX_RERANK_SOURCE_SECONDS
    by_file: dict[tuple[str, str], list[int]] = {}
    for index in pending:
        hit = hits[index]
        if hit.repo not in settings.atlas_generation.snapshots:
            continue
        key = (hit.repo, hit.path)
        if key not in by_file:
            try:
                _retrieval_source_path(settings.repo(hit.repo), hit.path)
            except (BrainError, OSError):
                continue
            by_file[key] = []
        by_file[key].append(index)
    requested = [key for key in by_file if key not in source_cache]
    if requested and (trace is None or trace.try_reserve_backend()):
        loaded: dict[tuple[str, str], str] = {}
        try:
            loaded = read_generation_files(
                settings, settings.atlas_generation, requested,
                max_bytes=MAX_RERANK_SOURCE_BYTES,
                max_seconds=max(0.0, deadline - time.perf_counter()),
            ) or {}
            source_cache.update(loaded)
        finally:
            if trace is not None:
                trace.complete_reserved_backend(
                    "rerank-source", (time.perf_counter() - started) * 1000,
                    bytes_scanned=sum(len(source.encode("utf-8")) for source in loaded.values()), files=len(loaded),
                )
    for key, indexes in by_file.items():
        if time.perf_counter() >= deadline:
            break
        source = source_cache.get(key)
        if source is None:
            continue
        # Split once per file, not once per candidate in a large source file.
        lines = source.splitlines()
        for index in indexes:
            if time.perf_counter() >= deadline:
                break
            hit = hits[index]
            # ponytail: a short preview, not a claim to have read the whole
            # method. Explicit files requests remain the complete-source path.
            preview = "\n".join(lines[max(0, hit.line - 3):hit.line + 24])
            snippet = _bounded_utf8_text(preview[:1_200], 1_200, "…")[0]
            if snippet.strip():
                snippets[index] = snippet
    if trace is not None:
        trace.add_stage("rerank_source_preview_ms", (time.perf_counter() - started) * 1000)
        if len(snippets) < len(positions):
            trace.fallback_reasons.append("rerank_source_preview_incomplete")
    return snippets


def merge_evidence(evidence: list[Evidence]) -> list[Evidence]:
    """Merge duplicate and overlapping source windows before they reach context."""
    merged: list[Evidence] = []
    ordered = sorted(evidence, key=lambda item: (item.repo, item.path, item.line_start, item.line_end))
    for item in ordered:
        existing = merged[-1] if merged else None
        if not existing or (existing.repo, existing.path) != (item.repo, item.path) or item.line_start > existing.line_end + 1:
            merged.append(item)
            continue
        lines = {
            **{number: line for number, line in enumerate(existing.content.splitlines(), existing.line_start)},
            **{number: line for number, line in enumerate(item.content.splitlines(), item.line_start)},
        }
        existing.line_start = min(existing.line_start, item.line_start)
        existing.line_end = max(existing.line_end, item.line_end)
        existing.content = "\n".join(lines.get(number, "") for number in range(existing.line_start, existing.line_end + 1))
        existing.score = max(existing.score, item.score)
        existing.found_by = sorted(set(existing.found_by + item.found_by))
        kinds = list(dict.fromkeys(part.strip() for part in (existing.kind + ", " + item.kind).split(",")))
        existing.kind = ", ".join(kinds)
    return sorted(merged, key=lambda item: (-item.score, item.repo, item.path, item.line_start))


def _candidate_regions(settings: Settings, hits: list[SearchHit]) -> list[SearchHit]:
    from .retrieval.ranker import fuse_and_rank

    hits = fuse_and_rank(hits)
    groups: dict[tuple[str, str], list[SearchHit]] = {}
    for hit in hits:
        groups.setdefault((hit.repo, hit.path), []).append(hit)

    regions: list[SearchHit] = []
    region_matches: list[list[SearchHit]] = []
    radius = max(10, settings.source_window_lines // 2)
    for file_hits in groups.values():
        current: SearchHit | None = None
        first_line = 0
        for hit in sorted(file_hits, key=lambda item: item.line):
            anchor = hit if current is None or hit.score > current.score else current
            # Match read_source's real window, including when a stronger later
            # hit moves the anchor. Never merge away a still-unread source match.
            if current is None or hit.line - anchor.line > radius or anchor.line - first_line > radius:
                current = SearchHit(
                    hit.repo, hit.path, hit.line, hit.text, hit.kind, hit.score, list(hit.found_by)
                )
                first_line = hit.line
                regions.append(current)
                region_matches.append([hit])
                continue
            region_matches[-1].append(hit)
            if hit.score > current.score:
                current.line, current.text, current.score = hit.line, hit.text, hit.score
            current.found_by = sorted(set(current.found_by + hit.found_by))
            kinds = list(dict.fromkeys(part.strip() for part in (current.kind + ", " + hit.kind).split(",")))
            current.kind = ", ".join(kinds)

    for region, matches in zip(regions, region_matches, strict=True):
        matched = {channel for channel in region.found_by if channel.startswith("lexical anchor ")}
        # ponytail: bounded local co-occurrence, not a semantic relevance score;
        # calibrate against labelled tickets before making this boost stronger.
        region.score = round(region.score + min(20, 5 * max(0, len(matched) - 1)), 3)
        # ponytail: reuse at most three already-found lines, favouring distinct
        # query clues. Richer method context needs labelled reranker evaluation.
        remaining = [(hit, {value for value in hit.found_by if value.startswith("lexical anchor ")})
                     for hit in matches if hit.text]
        chosen: list[SearchHit] = []
        represented: set[str] = set()
        for _ in range(min(3, len(remaining))):
            best = max(remaining, key=lambda pair: (len(pair[1] - represented), pair[0].score, -pair[0].line))
            remaining.remove(best)
            chosen.append(best[0])
            represented.update(best[1])
        if len(chosen) > 1:
            region.text = "\n".join(
                _bounded_utf8_text(f"L{hit.line}: {hit.text[:1_200]}", 1_200 // len(chosen) - 1, "…")[0]
                for hit in chosen
            )
        else:
            region.text = _bounded_utf8_text(region.text[:1_200], 1_200, "…")[0]
    return sorted(regions, key=lambda item: (-item.score, item.repo, item.path, item.line))


def prune_candidates(settings: Settings, hits: list[SearchHit], limit: int) -> tuple[list[SearchHit], list[SearchHit]]:
    """Bound the reranker pool while retaining direct/path/definition evidence."""
    ranked = _candidate_regions(settings, hits)
    protected = [
        item for item in ranked
        if any(value in item.kind.lower() for value in ("requested", "verified path", "definition"))
    ]
    protected_keys = {(item.repo, item.path, item.line) for item in protected}
    ordinary = [item for item in ranked if (item.repo, item.path, item.line) not in protected_keys]
    kept = (protected + ordinary)[: max(1, limit)]
    kept_keys = {(item.repo, item.path, item.line) for item in kept}
    return kept, [item for item in ranked if (item.repo, item.path, item.line) not in kept_keys]


def select_candidates(
    settings: Settings,
    hits: list[SearchHit],
    *,
    already_fused: bool = False,
) -> tuple[list[SearchHit], list[SearchHit]]:
    """Merge nearby hits, then enforce global/file/repository hydration diversity."""
    # Keep the protection established before reranking through hydration too;
    # an optional learned/co-occurrence bonus must not evict requested source.
    ranked = sorted(
        hits if already_fused else _candidate_regions(settings, hits),
        key=lambda item: (
            not any(value in item.kind.lower() for value in ("requested", "verified path", "definition")),
            -item.score, item.repo, item.path, item.line,
        ),
    )
    considered, omitted = ranked[: settings.candidate_limit], ranked[settings.candidate_limit :]
    selected: list[SearchHit] = []
    repo_counts: dict[str, int] = {}
    file_counts: dict[tuple[str, str], int] = {}
    for hit in considered:
        file_key = hit.repo, hit.path
        if (
            len(selected) >= settings.hydrate_limit
            or repo_counts.get(hit.repo, 0) >= settings.max_regions_per_repo
            or file_counts.get(file_key, 0) >= settings.max_regions_per_file
        ):
            omitted.append(hit)
            continue
        selected.append(hit)
        repo_counts[hit.repo] = repo_counts.get(hit.repo, 0) + 1
        file_counts[file_key] = file_counts.get(file_key, 0) + 1
    return selected, sorted(omitted, key=lambda item: (-item.score, item.repo, item.path, item.line))

from __future__ import annotations

from .core import Evidence, SearchHit, Settings


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


def select_candidates(settings: Settings, hits: list[SearchHit]) -> tuple[list[SearchHit], list[SearchHit]]:
    """Merge nearby hits, then enforce global/file/repository hydration diversity."""
    from .retrieval.ranker import fuse_and_rank

    hits = fuse_and_rank(hits)
    groups: dict[tuple[str, str], list[SearchHit]] = {}
    for hit in hits:
        groups.setdefault((hit.repo, hit.path), []).append(hit)

    regions: list[SearchHit] = []
    merge_distance = max(10, settings.source_window_lines)
    for file_hits in groups.values():
        current: SearchHit | None = None
        for hit in sorted(file_hits, key=lambda item: item.line):
            if current is None or hit.line - current.line > merge_distance:
                current = SearchHit(
                    hit.repo, hit.path, hit.line, hit.text, hit.kind, hit.score, list(hit.found_by)
                )
                regions.append(current)
                continue
            if hit.score > current.score:
                current.line, current.text, current.score = hit.line, hit.text, hit.score
            current.found_by = sorted(set(current.found_by + hit.found_by))
            kinds = list(dict.fromkeys(part.strip() for part in (current.kind + ", " + hit.kind).split(",")))
            current.kind = ", ".join(kinds)

    ranked = sorted(regions, key=lambda item: (-item.score, item.repo, item.path, item.line))
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

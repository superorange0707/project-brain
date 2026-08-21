from __future__ import annotations

import json
import math
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .core import Settings


INDEX_VERSION = 1
DEFAULT_TICKET_PATTERN = r"(?<![A-Z0-9])([A-Z][A-Z0-9]+-[0-9]+)(?![A-Z0-9])"
TEST_PATH = re.compile(r"(^|/)(test|tests|src/test)/|(?:Test|Tests|IT|Spec)\.", re.I)
CONFIG_SUFFIXES = {".conf", ".gradle", ".json", ".properties", ".toml", ".xml", ".yaml", ".yml"}
PATCH_SUFFIXES = CONFIG_SUFFIXES | {
    ".avsc", ".bash", ".c", ".cc", ".cfg", ".cpp", ".cs", ".gql", ".go", ".graphql",
    ".graphqls", ".groovy", ".h", ".hcl", ".hpp", ".ini", ".java", ".js", ".jsx", ".kt",
    ".kts", ".md", ".php", ".proto", ".py", ".rb", ".rs", ".scala", ".sh", ".sql", ".swift",
    ".tf", ".tfvars", ".tpl", ".ts", ".tsx", ".vue", ".zsh",
}
SENSITIVE_PATH = re.compile(
    r"(^|/)(?:\.env(?:\.|$)|\.git-credentials$|\.netrc$|\.npmrc$|\.pypirc$|authorized_keys$|"
    r"id_(?:dsa|ecdsa|ed25519|rsa)$|settings\.xml$|.*(?:credential|password|private[-_.]?key|secret|token).*)|"
    r"\.(?:jks|key|keystore|p12|pfx|pem)$",
    re.I,
)
STOP_WORDS = {
    "add", "and", "api", "app", "application", "build", "change", "code", "config", "create",
    "develop", "feature", "file", "fix", "for", "from", "java", "main", "merge", "service", "src",
    "test", "tests", "the", "this", "update", "with",
}


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _tokens(text: str) -> set[str]:
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", expanded)
        if token.lower() not in STOP_WORDS and not re.fullmatch(r"[A-Z][A-Z0-9]+-[0-9]+", token)
    }


def _ticket_id(match: re.Match[str]) -> str:
    return next((value for value in match.groups() if value), match.group(0))


def _load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _redact_patch(text: str) -> str:
    text = re.sub(
        r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
        "[REDACTED PRIVATE KEY]",
        text,
        flags=re.S,
    )
    text = re.sub(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b", "[REDACTED AWS ACCESS KEY]", text)
    text = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|glpat-[A-Za-z0-9_-]{20,})\b", "[REDACTED TOKEN]", text)
    text = re.sub(r"\b(?:xox[baprs]-[A-Za-z0-9-]{20,})\b", "[REDACTED TOKEN]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b", "[REDACTED JWT]", text)
    text = re.sub(r"(?i)(authorization\s*[:=]\s*(?:bearer|basic)\s+)\S+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(https?://[^:/\s]+:)[^@\s]+@", r"\1[REDACTED]@", text)
    text = re.sub(r"(?i)([?&](?:api[-_]?key|password|secret|token)=)[^&\s]+", r"\1[REDACTED]", text)
    rows: list[str] = []
    assignment = re.compile(
        r"(?i)^(?P<prefix>[+\- ]?[^\n]*(?:api[-_.]?key|client[-_.]?secret|credential|password|secret|token)[^:=\n]*\s*[:=]\s*)(?P<value>.+)$"
    )
    for line in text.splitlines():
        match = assignment.match(line)
        rows.append(match.group("prefix") + "[REDACTED]" if match else line)
    return "\n".join(rows)


def load_experience_index(settings: Settings) -> dict[str, Any]:
    return _load(settings.state_dir / "ticket-history.json")


def _parse_log(text: str, ticket_pattern: re.Pattern[str]) -> list[dict[str, Any]]:
    commits: list[dict[str, Any]] = []
    for record in text.split("\x1e"):
        lines = record.strip("\n").splitlines()
        if not lines:
            continue
        header = lines[0].split("\x1f", 2)
        if len(header) != 3:
            continue
        sha, date, subject = header
        subject = _redact_patch(subject)
        tickets = sorted({_ticket_id(match) for match in ticket_pattern.finditer(subject)})
        if not tickets:
            continue
        changes: list[dict[str, str]] = []
        for line in lines[1:]:
            parts = line.split("\t")
            if len(parts) >= 2 and re.fullmatch(r"[A-Z][0-9]*", parts[0]):
                changes.append({"status": parts[0], "path": parts[-1]})
            if len(changes) >= 250:
                break
        commits.append({"sha": sha, "date": date, "subject": subject, "tickets": tickets, "changes": changes})
    return commits


def build_experience_index(settings: Settings, *, changed_only: bool = True) -> dict[str, Any]:
    """Index ticket-labelled Git changes locally without reading credentials or calling a model."""
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    path = settings.state_dir / "ticket-history.json"
    previous = load_experience_index(settings)
    previous_repositories = previous.get("repositories") if isinstance(previous.get("repositories"), dict) else {}
    try:
        pattern = re.compile(settings.ticket_pattern)
    except re.error as exc:
        raise ValueError(f"Invalid experience.ticket_pattern: {exc}") from exc

    repositories: dict[str, Any] = {}
    for repo in settings.repositories:
        sha = repo.source_sha
        if not sha and (repo.path / ".git").exists():
            head = _run(["git", "rev-parse", "HEAD"], repo.path)
            sha = head.stdout.strip() if head.returncode == 0 else None
        cached = previous_repositories.get(repo.name) if isinstance(previous_repositories, dict) else None
        if changed_only and isinstance(cached, dict) and cached.get("sha") == sha:
            repositories[repo.name] = cached
            continue
        commits: list[dict[str, Any]] = []
        if settings.experience_enabled and sha and (repo.path / ".git").exists():
            history_range = sha
            cached_commits: list[dict[str, Any]] = []
            previous_sha = str(cached.get("sha") or "") if isinstance(cached, dict) else ""
            if changed_only and previous_sha:
                ancestor = _run(["git", "merge-base", "--is-ancestor", previous_sha, sha], repo.path)
                if ancestor.returncode == 0:
                    history_range = f"{previous_sha}..{sha}"
                    cached_commits = list(cached.get("commits") or [])
            command = [
                "git", "log", history_range, f"-n{settings.experience_commit_limit}", "--date=short",
                "--pretty=format:%x1e%H%x1f%ad%x1f%s", "--name-status", "--find-renames",
                "--diff-merges=first-parent",
            ]
            result = _run(command, repo.path)
            if result.returncode != 0:
                # Older Git versions can still contribute commit subjects and
                # ordinary commit paths even if they cannot render merge diffs.
                result = _run(command[:-1], repo.path)
            if result.returncode == 0:
                commits = _parse_log(result.stdout, pattern) + cached_commits
                unique: dict[str, dict[str, Any]] = {str(item["sha"]): item for item in commits}
                commits = list(unique.values())[: settings.experience_commit_limit]
        repositories[repo.name] = {"sha": sha, "commits": commits}

    grouped: dict[str, dict[str, Any]] = {}
    for repo_name, repository in repositories.items():
        for commit in repository.get("commits") or []:
            for ticket in commit.get("tickets") or []:
                case = grouped.setdefault(ticket, {"ticket": ticket, "commits": []})
                case["commits"].append(
                    {
                        "repo": repo_name,
                        "sha": commit["sha"],
                        "date": commit["date"],
                        "subject": commit["subject"],
                        "changes": commit["changes"],
                    }
                )

    ticket_texts: dict[str, str] = {}
    if settings.runs_dir.is_dir():
        for directory in settings.runs_dir.iterdir():
            state = _load(directory / "session.json") if directory.is_dir() else {}
            ticket = str(state.get("ticket") or "")
            ticket_path = directory / "ticket.md"
            if ticket and ticket_path.is_file():
                ticket_texts[ticket] = ticket_path.read_text(encoding="utf-8", errors="replace")[:20_000]

    cases: list[dict[str, Any]] = []
    for case in grouped.values():
        commits = sorted(case["commits"], key=lambda item: (item["date"], item["repo"], item["sha"]), reverse=True)
        paths = sorted({f"{item['repo']}:{change['path']}" for item in commits for change in item["changes"]})
        subjects = sorted({item["subject"] for item in commits})
        ticket_text = ticket_texts.get(case["ticket"], "")
        knowledge_path = settings.knowledge_dir / "tickets" / f"{case['ticket']}.md"
        knowledge_text = (
            knowledge_path.read_text(encoding="utf-8", errors="replace")[:20_000]
            if knowledge_path.is_file()
            else ""
        )
        terms = sorted(_tokens(" ".join(subjects + paths + [ticket_text, knowledge_text])))
        cases.append(
            {
                "ticket": case["ticket"],
                "latest_date": max((item["date"] for item in commits), default=""),
                "repos": sorted({item["repo"] for item in commits}),
                "paths": paths,
                "test_paths": [value for value in paths if TEST_PATH.search(value.split(":", 1)[-1])],
                "config_paths": [value for value in paths if Path(value.split(":", 1)[-1]).suffix.lower() in CONFIG_SUFFIXES],
                "subjects": subjects,
                "ticket_excerpt": _redact_patch(ticket_text[:6_000]),
                "knowledge_excerpt": _redact_patch(knowledge_text[:12_000]),
                "terms": terms,
                "commits": commits,
            }
        )
    cases.sort(key=lambda item: (item["latest_date"], item["ticket"]), reverse=True)
    index = {
        "version": INDEX_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "ticket_pattern": settings.ticket_pattern,
        "repositories": repositories,
        "cases": cases,
    }
    path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    return index


def similar_cases(settings: Settings, text: str, *, limit: int | None = None) -> list[dict[str, Any]]:
    index = load_experience_index(settings)
    query_terms = _tokens(text)
    pattern = re.compile(settings.ticket_pattern)
    ticket_ids = {_ticket_id(match) for match in pattern.finditer(text)}
    scored: list[dict[str, Any]] = []
    for case in index.get("cases") or []:
        exact = case.get("ticket") in ticket_ids
        overlap = query_terms & set(case.get("terms") or [])
        if not exact and not overlap:
            continue
        score = (1000 if exact else 0) + sum(min(12, len(term)) for term in overlap)
        scored.append({**case, "score": score, "matched_terms": sorted(overlap)})
    scored.sort(key=lambda item: (item["score"], item.get("latest_date") or "", item["ticket"]), reverse=True)
    return scored[: (limit if limit is not None else settings.experience_similar_cases)]


def _patches(settings: Settings, case: dict[str, Any], max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    output: list[str] = []
    remaining = max_chars
    for commit in case.get("commits") or []:
        if remaining <= 0:
            break
        try:
            repo = settings.repo(str(commit["repo"]))
        except (KeyError, RuntimeError):
            continue
        sha = str(commit.get("sha") or "")
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}", sha) or not (repo.path / ".git").exists():
            continue
        paths = [
            str(change.get("path") or "")
            for change in commit.get("changes") or []
            if Path(str(change.get("path") or "")).suffix.lower() in PATCH_SUFFIXES
            and not SENSITIVE_PATH.search(str(change.get("path") or ""))
        ][:80]
        if not paths:
            continue
        command = [
            "git", "show", sha, "--format=", "--no-ext-diff", "--find-renames", "--unified=2",
            "--diff-merges=first-parent", "--", *paths,
        ]
        result = _run(command, repo.path)
        if result.returncode != 0:
            result = _run([*command[:7], *command[8:]], repo.path)
        if result.returncode != 0 or not result.stdout.strip():
            continue
        patch = _redact_patch(result.stdout.strip())
        clipped = patch[:remaining]
        output.extend([f"### {repo.name} `{sha[:12]}` — {commit.get('subject', '')}", "", "```diff", clipped, "```", ""])
        remaining -= len(clipped)
    if remaining <= 0:
        output.append("Patch excerpts were truncated to the configured historical evidence budget.")
    return "\n".join(output).strip()


def render_similar_cases(
    settings: Settings,
    text: str,
    *,
    include_patches: bool = False,
    patch_chars: int | None = None,
) -> str:
    cases = similar_cases(settings, text)
    if not cases:
        return ""
    output = [
        "## Similar ticket history",
        "",
        "These are deterministic local Git analogues, not model training. A commit proves changed files, not that its implementation was correct. Patch excerpts exclude sensitive file types and redact common credential patterns.",
        "",
    ]
    for case in cases:
        output.extend(
            [
                f"### {case['ticket']} — relevance {case['score']}",
                "",
                f"- Repositories: {', '.join(f'`{value}`' for value in case['repos']) or 'none'}",
                f"- Matched terms: {', '.join(f'`{value}`' for value in case['matched_terms']) or 'exact ticket identifier'}",
                f"- Commit subjects: {'; '.join(case['subjects'])}",
                f"- Changed paths: {', '.join(f'`{value}`' for value in case['paths'][:30]) or 'none recorded'}",
                f"- Tests changed: {', '.join(f'`{value}`' for value in case['test_paths'][:12]) or 'none recorded'}",
                f"- Configuration changed: {', '.join(f'`{value}`' for value in case['config_paths'][:12]) or 'none recorded'}",
                "",
            ]
        )
        if case.get("ticket_excerpt"):
            output.extend(["Prior Brain ticket description:", "", str(case["ticket_excerpt"]).strip(), ""])
        if case.get("knowledge_excerpt"):
            output.extend(["Human-maintained ticket knowledge:", "", str(case["knowledge_excerpt"]).strip(), ""])
    if include_patches:
        budget = settings.experience_patch_chars if patch_chars is None else max(0, patch_chars)
        for case in cases[:2]:
            patch = _patches(settings, case, budget)
            if patch:
                output.extend([f"## Historical patch evidence — {case['ticket']}", "", patch, ""])
    return "\n".join(output).rstrip() + "\n"


def evaluate_sessions(settings: Settings, index: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compare retrieved paths with later ticket-labelled commits when both exist locally."""
    index = index or load_experience_index(settings)
    cases = {item["ticket"]: item for item in index.get("cases") or []}
    evaluations: list[dict[str, Any]] = []
    if settings.runs_dir.is_dir():
        evidence_pattern = re.compile(r"(?m)^### \d+\. ([^—\n]+?) — `(.+?):\d+-\d+`$")
        for directory in settings.runs_dir.iterdir():
            state_path = directory / "session.json"
            if not directory.is_dir() or not state_path.is_file():
                continue
            state = _load(state_path)
            ticket = str(state.get("ticket") or directory.name)
            case = cases.get(ticket)
            if not case:
                continue
            retrieved: set[str] = set()
            for context in directory.glob("context-*.md"):
                for repo, path in evidence_pattern.findall(context.read_text(encoding="utf-8", errors="replace")):
                    retrieved.add(f"{repo.strip()}:{path}")
            actual = set(case.get("paths") or [])
            actual_repos = set(case.get("repos") or [])
            retrieved_repos = {value.split(":", 1)[0] for value in retrieved}
            actual_tests = set(case.get("test_paths") or [])
            matched_files = actual & retrieved
            matched_repos = actual_repos & retrieved_repos
            matched_tests = actual_tests & retrieved
            ordered_paths: list[str] = []
            for context in sorted(directory.glob("context-*.md")):
                ordered_paths.extend(f"{repo.strip()}:{path}" for repo, path in evidence_pattern.findall(context.read_text(encoding="utf-8", errors="replace")))
            first_relevant = next((rank for rank, value in enumerate(ordered_paths[:10], 1) if value in actual), None)
            dcg = sum(1 / math.log2(rank + 1) for rank, value in enumerate(ordered_paths[:10], 1) if value in actual)
            ideal = sum(1 / math.log2(rank + 1) for rank in range(1, min(10, len(actual)) + 1))
            evaluations.append(
                {
                    "ticket": ticket,
                    "actual_files": len(actual),
                    "retrieved_files": len(retrieved),
                    "matched_files": len(matched_files),
                    "actual_repos": len(actual_repos),
                    "matched_repos": len(matched_repos),
                    "actual_tests": len(actual_tests),
                    "matched_tests": len(matched_tests),
                    "file_recall": len(matched_files) / len(actual) if actual else None,
                    "repo_recall": len(matched_repos) / len(actual_repos) if actual_repos else None,
                    "test_recall": len(matched_tests) / len(actual_tests) if actual_tests else None,
                    "mrr_at_10": 1 / first_relevant if first_relevant else 0.0,
                    "ndcg_at_10": dcg / ideal if ideal else None,
                    "changed_file_precision": len(matched_files) / len(retrieved) if retrieved else None,
                    "duplicate_window_ratio": 1 - len(set(ordered_paths)) / len(ordered_paths) if ordered_paths else 0.0,
                    "missed_paths": sorted(actual - retrieved),
                }
            )
    def aggregate(matched: str, actual: str) -> float | None:
        denominator = sum(int(item[actual]) for item in evaluations)
        return sum(int(item[matched]) for item in evaluations) / denominator if denominator else None

    summary = {
        "repo_recall": aggregate("matched_repos", "actual_repos"),
        "file_recall": aggregate("matched_files", "actual_files"),
        "test_recall": aggregate("matched_tests", "actual_tests"),
        "mrr_at_10": sum(float(item["mrr_at_10"]) for item in evaluations) / len(evaluations) if evaluations else None,
        "ndcg_at_10": sum(float(item["ndcg_at_10"] or 0) for item in evaluations) / len(evaluations) if evaluations else None,
        "changed_file_precision": sum(float(item["changed_file_precision"] or 0) for item in evaluations) / len(evaluations) if evaluations else None,
        "duplicate_window_ratio": sum(float(item["duplicate_window_ratio"]) for item in evaluations) / len(evaluations) if evaluations else None,
    }

    def percent(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.0%}"

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "indexed_cases": len(cases),
        "evaluated_sessions": len(evaluations),
        "summary": summary,
        "evaluations": evaluations,
    }
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    (settings.state_dir / "experience-eval.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    settings.generated_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Project Brain Experience Evaluation",
        "",
        f"Indexed ticket cases: {len(cases)}",
        f"Sessions with a later matching commit: {len(evaluations)}",
        f"Aggregate repository recall: {percent(summary['repo_recall'])}",
        f"Aggregate changed-file recall: {percent(summary['file_recall'])}",
        f"Aggregate changed-test recall: {percent(summary['test_recall'])}",
        f"MRR@10: {percent(summary['mrr_at_10'])}",
        f"nDCG@10: {percent(summary['ndcg_at_10'])}",
        f"Changed-file precision: {percent(summary['changed_file_precision'])}",
        f"Duplicate-window ratio: {percent(summary['duplicate_window_ratio'])}",
        "",
        "File recall measures whether investigation evidence included files later changed by a ticket-labelled commit. Related evidence may be valuable even when it was not changed, so precision is intentionally not claimed.",
        "",
    ]
    for item in evaluations:
        file_recall = "n/a" if item["file_recall"] is None else f"{item['file_recall']:.0%}"
        repo_recall = "n/a" if item["repo_recall"] is None else f"{item['repo_recall']:.0%}"
        test_recall = "n/a" if item["test_recall"] is None else f"{item['test_recall']:.0%}"
        lines.extend(
            [
                f"## {item['ticket']}", "",
                f"- Repository recall: {repo_recall}",
                f"- Changed-file recall: {file_recall}",
                f"- Changed-test recall: {test_recall}",
                f"- Missed changed paths: {', '.join(f'`{value}`' for value in item['missed_paths']) or 'none'}",
                "",
            ]
        )
    (settings.generated_dir / "EXPERIENCE_REPORT.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return report

"""Check the AI's SEARCH/REPLACE edit blocks against the developer's working trees.

Chat output is an unreliable patch format: models miscount unified-diff hunks and
chat rendering eats indentation. Brain therefore asks for search/replace blocks,
matches every block against the working tree, and stages one standard patch per
repository only when the whole set applies. Brain still never writes into a
repository; the developer runs the printed ``git apply``. A block that does not
match yields a precise repair prompt for the same chat instead of a wrong patch.
New definitions whose names already exist elsewhere are reported so that shared
library code is reused rather than written again.
"""
from __future__ import annotations

import difflib
import os
import re
import shlex
import sqlite3
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .core import (
    BrainError,
    Repository,
    Settings,
    _validated_generated_artifact,
    handoff_dir,
    save_session,
    session_dir,
    session_state,
)
from .locks import ticket_exclusive
from .mps import MODEL_SUFFIXES
from .platforms import atomic_managed_bytes_write, is_test_path, logical_path, read_direct_file_bytes, run_bounded_process

MAX_EDIT_BLOCKS = 60
MAX_EDIT_FILE_BYTES = 4 * 1024 * 1024
MAX_REPAIR_CONTEXT_LINES = 30
MAX_REUSE_NAMES = 8
MAX_REUSE_HITS = 3
MAX_EDIT_HISTORY = 50

_SEARCH = re.compile(r"^\s*<{5,9}\s*SEARCH\b")
_DIVIDER = re.compile(r"^\s*={5,9}\s*$")
_REPLACE = re.compile(r"^\s*>{5,9}\s*REPLACE\b")
_FILE_LINE = re.compile(r"^[\s>*_#`-]*(?:FILE|PATH)\s*:\s*(.+?)\s*$", re.IGNORECASE)
_NEW_FILE_NOTE = re.compile(r"\s+\((?:new|new file|create|created)\)$", re.IGNORECASE)

_CLASS = re.compile(r"\b(?:class|interface|enum|record|object|trait|struct)\s+([A-Z]\w*)")
_JVM_METHOD = re.compile(
    r"^\s*(?:@\w+(?:\([^)]*\))?\s+)*"
    r"(?:(?:public|protected|private|static|final|abstract|synchronized|default|native|override|virtual|async|"
    r"internal|sealed|open)\s+)*(?:<[^>]+>\s+)?[A-Za-z_$][\w$.]*(?:<[^()]*>)?(?:\[\])*\s+([a-z_$][\w$]*)\s*\("
)
_KOTLIN_FUN = re.compile(r"\bfun\s+(?:<[^>]+>\s*)?(?:[\w.]+\.)?([A-Za-z_]\w*)\s*\(")
_PYTHON_DEF = re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(")
_JS_FUNCTION = re.compile(
    r"\bfunction\s+([A-Za-z_$][\w$]*)|\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?"
    r"(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"
)
_GO_FUNC = re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\(|^\s*type\s+([A-Za-z_]\w*)\s+(?:struct|interface)")
_NOT_A_DECLARATION = {"return", "new", "throw", "else", "case", "yield", "await", "if", "for", "while", "switch",
                      "catch", "do", "try", "assert", "goto", "package", "import"}
_COMMON_NAMES = {
    "main", "toString", "equals", "hashCode", "compareTo", "close", "call", "apply", "accept", "test", "handle",
    "execute", "process", "init", "build", "builder", "create", "update", "delete", "save", "load", "values",
    "valueOf", "from", "setUp", "tearDown", "__init__", "__str__", "__repr__", "constructor", "render", "Builder",
}
_SHARED_REPOSITORY = re.compile(
    r"(?:^|[-_.])(?:shared|common|commons|lib|libs|library|core|platform|framework|sdk|util|utils)(?:$|[-_.])",
    re.IGNORECASE,
)


class _BlockProblem(Exception):
    pass


@dataclass
class EditBlock:
    number: int
    target: str
    search: list[str]
    replace: list[str]
    problem: str = ""


@dataclass
class _File:
    repo: Repository
    relative: str
    bom: bool
    original: list[str] | None
    lines: list[str]
    eol: str
    blocks: list[int] = field(default_factory=list)


def has_edit_blocks(text: str) -> bool:
    return bool(re.search(r"(?m)^\s*<{5,9}\s*SEARCH\b", text) and re.search(r"(?m)^\s*>{5,9}\s*REPLACE\b", text))


def _clean_target(value: str) -> str:
    value = value.strip().strip("*_`'\" ")
    return _NEW_FILE_NOTE.sub("", value).strip("*_`'\" ")


def _trim_blank(lines: list[str]) -> list[str]:
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def parse_edit_blocks(text: str) -> list[EditBlock]:
    """Parse SEARCH/REPLACE blocks; the most recent ``FILE:`` line names each block's target."""
    blocks: list[EditBlock] = []
    target = ""
    mode = ""
    search: list[str] = []
    replace: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not mode:
            if _SEARCH.match(line):
                mode, search, replace = "search", [], []
            elif match := _FILE_LINE.match(line):
                target = _clean_target(match.group(1))
            continue
        if mode == "search":
            if _DIVIDER.match(line):
                mode = "replace"
            elif _REPLACE.match(line) or _SEARCH.match(line):
                blocks.append(EditBlock(len(blocks) + 1, target, [], [], "the block has no ======= line between "
                                        "SEARCH and REPLACE"))
                mode = "search" if _SEARCH.match(line) else ""
                search, replace = [], []
            else:
                search.append(line)
        elif _REPLACE.match(line):
            blocks.append(EditBlock(len(blocks) + 1, target, _trim_blank(search), _trim_blank(replace)))
            mode = ""
        else:
            replace.append(line)
    if mode:
        blocks.append(EditBlock(len(blocks) + 1, target, [], [], "the block is not closed with >>>>>>> REPLACE"))
    return blocks


def _resolve(settings: Settings, target: str) -> tuple[Repository, str]:
    if not target:
        raise _BlockProblem("no FILE: line precedes the block; start each block with FILE: <repository>/<path>")
    if re.match(r"^[A-Za-z]:[\\/]", target) or target.startswith(("/", "\\")):
        raise _BlockProblem("FILE must be <repository>/<repository-relative path>, not an absolute path")
    value = logical_path(target)
    names = {repo.name: repo for repo in settings.repositories}
    folded = {repo.name.casefold(): repo for repo in settings.repositories}
    repo: Repository | None = None
    relative = value
    prefix, separator, rest = value.partition(":")
    if separator and (prefix in names or prefix.casefold() in folded):
        repo, relative = names.get(prefix) or folded[prefix.casefold()], rest
    else:
        first, separator, rest = value.partition("/")
        if separator and (first in names or first.casefold() in folded):
            repo, relative = names.get(first) or folded[first.casefold()], rest
        elif len(settings.repositories) == 1:
            repo = settings.repositories[0]
    if repo is None:
        known = ", ".join(sorted(names)[:20])
        raise _BlockProblem(f"FILE {target!r} does not start with a configured repository name ({known})")
    relative = logical_path(relative).lstrip("/")
    parts = PurePosixPath(relative).parts
    if (not relative or relative == "." or ".." in parts or ".git" in parts or "\t" in relative or '"' in relative
            or any(ord(character) < 32 or ord(character) == 127 for character in relative)):
        raise _BlockProblem(f"FILE {target!r} is not a safe repository-relative path")
    if PurePosixPath(relative).suffix.lower() in MODEL_SUFFIXES:
        raise _BlockProblem("this is an MPS model file; MPS models are edited in the MPS editor. Give a fenced "
                            "`mps` editor-operation block (model/node/concept/role, before/after) instead")
    return repo, relative


def _split_lines(text: str) -> list[str]:
    """Split on LF only, keeping each line's ending (CRLF stays intact)."""
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _content(line: str) -> str:
    if line.endswith("\n"):
        line = line[:-1]
        if line.endswith("\r"):
            line = line[:-1]
    return line


def _load(repo: Repository, relative: str) -> _File:
    root = repo.path.resolve()
    path = repo.path / relative
    try:
        path.resolve().relative_to(root)
    except ValueError as error:
        raise _BlockProblem("the path leaves the repository") from error
    if not os.path.lexists(path):
        return _File(repo, relative, False, None, [], "\n")
    try:
        raw, exceeded = read_direct_file_bytes(path, max_bytes=MAX_EDIT_FILE_BYTES)
    except (OSError, ValueError) as error:
        raise _BlockProblem("the file is not a regular file (a symbolic link or special file)") from error
    if exceeded:
        raise _BlockProblem("the file is too large for a chat edit; change it in the IDE")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _BlockProblem("the file is not UTF-8 text; change it in the IDE") from error
    bom = text.startswith("\ufeff")
    lines = _split_lines(text[1:] if bom else text)
    crlf = sum(1 for line in lines if line.endswith("\r\n"))
    lf = sum(1 for line in lines if line.endswith("\n")) - crlf
    return _File(repo, relative, bom, list(lines), list(lines), "\r\n" if crlf > lf else "\n")


def _indent(line: str) -> str:
    return line[:len(line) - len(line.lstrip())]


def _indent_delta(body: list[str], start: int, search: list[str]) -> tuple[str, str] | None:
    """The one consistent indentation shift between file and SEARCH lines, if any."""
    delta: tuple[str, str] | None = None
    for offset, wanted in enumerate(search):
        actual = body[start + offset]
        if actual.strip() != wanted.strip():
            return None
        if not wanted.strip():
            continue
        have, want = _indent(actual), _indent(wanted)
        if have.endswith(want):
            current = ("+", have[:len(have) - len(want)])
        elif want.endswith(have):
            current = ("-", want[:len(want) - len(have)])
        else:
            return None
        if not current[1]:
            current = ("+", "")
        if delta is None:
            delta = current
        elif delta != current:
            return None
    return delta or ("+", "")


def _locate(body: list[str], search: list[str]) -> tuple[list[int], str, tuple[str, str]]:
    """Find SEARCH exactly, then ignoring trailing spaces, then under one indentation shift."""
    size = len(search)
    stripped = [line.strip() for line in body]
    anchor = next(index for index, line in enumerate(search) if line.strip())
    for level in ("exact", "trailing", "indent"):
        starts: list[int] = []
        deltas: list[tuple[str, str]] = []
        for start in range(len(body) - size + 1):
            if stripped[start + anchor] != search[anchor].strip():
                continue
            window = body[start:start + size]
            if level == "exact":
                matched, delta = window == search, ("+", "")
            elif level == "trailing":
                matched, delta = [line.rstrip() for line in window] == [line.rstrip() for line in search], ("+", "")
            else:
                found = _indent_delta(body, start, search)
                matched, delta = found is not None, found or ("+", "")
            if matched:
                starts.append(start)
                deltas.append(delta)
        if starts:
            return starts, level, deltas[0]
    return [], "", ("+", "")


def _reindent(lines: list[str], delta: tuple[str, str]) -> list[str]:
    sign, prefix = delta
    if not prefix:
        return list(lines)
    if sign == "+":
        return [prefix + line if line.strip() else line for line in lines]
    return [line[len(prefix):] if line.startswith(prefix) else line.lstrip() if line.strip() else line
            for line in lines]


def _fence(lines: list[str]) -> str:
    longest = max((len(run) for line in lines for run in re.findall(r"`+", line)), default=0)
    return "`" * max(3, longest + 1)


def _nearest(body: list[str], search: list[str]) -> tuple[int, int, float] | None:
    """The current lines most like SEARCH, for a precise repair prompt."""
    wanted: dict[str, list[int]] = {}
    for offset, line in enumerate(search):
        if line.strip():
            wanted.setdefault(line.strip(), []).append(offset)
    votes: Counter[int] = Counter()
    for index, line in enumerate(body):
        for offset in wanted.get(line.strip(), ()):
            votes[index - offset] += 1
    if votes:
        start = votes.most_common(1)[0][0]
    else:
        first = next((line.strip() for line in search if line.strip()), "")
        close = difflib.get_close_matches(first, [line.strip() for line in body], n=1, cutoff=0.6)
        if not close:
            return None
        start = [line.strip() for line in body].index(close[0])
    start = max(0, min(start, len(body) - 1))
    end = min(len(body), start + max(len(search), 1))
    ratio = difflib.SequenceMatcher(None, "\n".join(body[start:end]).strip(), "\n".join(search).strip()).ratio()
    return start, end, ratio


def _apply(file: _File, block: EditBlock) -> str:
    """Apply one block to the in-memory file; return a note about tolerated differences."""
    if not block.search:
        if file.original is not None or file.lines:
            raise _BlockProblem("SEARCH is empty but the file already exists; quote the exact lines to replace "
                                "(to append, quote the file's last lines and repeat them in REPLACE)")
        file.lines = [line + "\n" for line in block.replace]
        return "new file"
    if file.original is None and not file.lines:
        raise _BlockProblem("the file does not exist in the working tree; use the exact path from the evidence, or "
                            "leave SEARCH empty to create a new file")
    body = [_content(line) for line in file.lines]
    starts, level, delta = _locate(body, block.search)
    if not starts:
        message = "SEARCH does not match the current file."
        nearest = _nearest(body, block.search)
        if nearest is None:
            raise _BlockProblem(message + " No similar lines exist; request this file again before editing it.")
        start, end, ratio = nearest
        context = body[max(0, start - 2):min(len(body), end + 2)][:MAX_REPAIR_CONTEXT_LINES]
        first = max(0, start - 2) + 1
        fence = _fence(context)
        raise _BlockProblem(
            f"{message} Closest current lines ({ratio:.0%} similar), lines {first}-{first + len(context) - 1}:\n"
            f"{fence}\n" + "\n".join(context) + f"\n{fence}"
        )
    if len(starts) > 1:
        lines = ", ".join(str(start + 1) for start in starts[:5])
        raise _BlockProblem(f"SEARCH matches {len(starts)} places (lines {lines}); add surrounding lines so that "
                            "it matches exactly one")
    start = starts[0]
    old = file.lines[start:start + len(block.search)]
    new_body = _reindent(block.replace, delta)
    if level != "exact":
        # Lines the AI kept unchanged keep their exact original text, so the patch stays minimal.
        matcher = difflib.SequenceMatcher(None, [line.strip() for line in block.search],
                                          [line.strip() for line in block.replace], autojunk=False)
        for tag, first, last, target, _ in matcher.get_opcodes():
            if tag == "equal":
                new_body[target:target + last - first] = [_content(line) for line in old[first:last]]
    replacement = [line + file.eol for line in new_body]
    if replacement and old and not old[-1].endswith("\n"):
        replacement[-1] = replacement[-1][:-len(file.eol)]
    file.lines = file.lines[:start] + replacement + file.lines[start + len(block.search):]
    if level == "trailing":
        return "matched after ignoring trailing spaces"
    if level == "indent":
        return "matched after correcting a uniform indentation shift"
    return ""


def _declared_names(path: str, lines: list[str]) -> set[str]:
    suffix = PurePosixPath(path).suffix.lower()
    names: set[str] = set()
    for line in lines:
        code = line.split("//", 1)[0] if suffix != ".py" else line.split("#", 1)[0]
        if suffix in {".java", ".groovy", ".cs", ".scala", ".kt", ".kts", ".py", ".ts", ".tsx", ".js", ".jsx",
                      ".mjs", ".cjs", ".go", ".swift"}:
            names.update(_CLASS.findall(code))
        if suffix in {".java", ".groovy", ".cs", ".scala"}:
            words = code.split()
            match = _JVM_METHOD.match(code)
            if (match and words and words[0] not in _NOT_A_DECLARATION and not code.rstrip().endswith(";")
                    and "=" not in code.split("(", 1)[0]):
                names.add(match.group(1))
        elif suffix in {".kt", ".kts"}:
            names.update(_KOTLIN_FUN.findall(code))
        elif suffix == ".py":
            names.update(_PYTHON_DEF.findall(code))
        elif suffix in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
            names.update(name for pair in _JS_FUNCTION.findall(code) for name in pair if name)
        elif suffix == ".go":
            names.update(name for pair in _GO_FUNC.findall(code) for name in pair if name)
    return {name for name in names
            if len(name) >= 4 and name not in _COMMON_NAMES and not re.match(r"^(?:get|set|is|has)[A-Z_]", name)}


def _is_shared(repo: Repository) -> bool:
    return bool(_SHARED_REPOSITORY.search(repo.name)) or any(_SHARED_REPOSITORY.search(tag) for tag in repo.tags)


def _reuse_warnings(settings: Settings, files: list[_File], blocks: list[EditBlock]) -> list[dict[str, Any]]:
    """Name new declarations that already exist elsewhere, shared libraries first."""
    from .core import _symbol_definition_hits

    by_number = {block.number: block for block in blocks}
    candidates: list[tuple[str, _File]] = []
    for file in files:
        if is_test_path(file.relative):
            continue
        existing = _declared_names(file.relative, [_content(line) for line in file.original or []])
        added: set[str] = set()
        for number in file.blocks:
            block = by_number[number]
            added |= _declared_names(file.relative, block.replace) - _declared_names(file.relative, block.search)
        candidates.extend((name, file) for name in sorted(added - existing))
    repositories = {repo.name: repo for repo in settings.repositories}
    warnings: list[dict[str, Any]] = []
    for name, file in candidates[:MAX_REUSE_NAMES]:
        try:
            hits = _symbol_definition_hits(settings, name, None)
        except (BrainError, OSError, RuntimeError, ValueError, sqlite3.Error):
            continue
        def shared(repo: str) -> bool:
            return repo in repositories and _is_shared(repositories[repo])

        matches = []
        for hit in sorted(hits, key=lambda hit: (not shared(hit.repo), hit.repo, hit.path, hit.line)):
            if (hit.repo, hit.path) == (file.repo.name, file.relative) or hit.kind != "definition":
                continue
            matches.append({"repo": hit.repo, "path": hit.path, "line": hit.line, "text": hit.text.strip()[:200],
                            "shared": shared(hit.repo)})
            if len(matches) >= MAX_REUSE_HITS:
                break
        if matches:
            warnings.append({"name": name, "repo": file.repo.name, "path": file.relative, "existing": matches})
    return warnings


def _patch(files: list[_File]) -> bytes:
    output: list[str] = []
    for file in files:
        old = list(file.original or [])
        new = list(file.lines)
        if old == new:
            continue
        if file.bom:
            old[:1] = ["\ufeff" + line for line in old[:1]]
            new[:1] = ["\ufeff" + line for line in new[:1]]
        a, b = f"a/{file.relative}", f"b/{file.relative}"
        output.append(f"diff --git {a} {b}\n")
        if file.original is None:
            output.extend(["new file mode 100644\n", "--- /dev/null\n", f"+++ {b}\n"])
        else:
            output.extend([f"--- {a}\n", f"+++ {b}\n"])
        for line in list(difflib.unified_diff(old, new, n=3))[2:]:
            output.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(output).encode("utf-8")


def apply_command(repo: Repository, patch: Path) -> str:
    arguments = ["git", "-C", str(repo.path), "apply", str(patch)]
    return subprocess.list2cmdline(arguments) if os.name == "nt" else shlex.join(arguments)


def _git_check(repo: Repository, payload: bytes) -> str:
    """Ask the developer's Git whether the patch applies; an empty string means yes or Git is unavailable."""
    try:
        result = run_bounded_process(
            ["git", "apply", "--check", "-"], repo.path, input_bytes=payload,
            max_stdout_bytes=64 * 1024, max_stderr_bytes=64 * 1024, timeout=60,
        )
    except OSError:
        return ""
    if getattr(result, "timed_out", False) or result.returncode == 0:
        return ""
    error = result.stderr if isinstance(result.stderr, str) else result.stderr.decode("utf-8", "replace")
    return error.strip()[:2000] or f"git apply --check exited with {result.returncode}"


def _repair_prompt(failures: list[dict[str, Any]], total: int, warnings: list[dict[str, Any]]) -> str:
    lines = [
        "PROJECT BRAIN — EDIT CHECK FAILED",
        "",
        f"{len(failures)} of {total} SEARCH/REPLACE blocks do not apply to the developer's current working tree, "
        "so Brain staged no patch. Resend the complete corrected set of SEARCH/REPLACE blocks for this change "
        "(every file, not only the failed blocks). Copy SEARCH text exactly from the current lines below, keep "
        "indentation, and include enough unchanged lines for exactly one match. Do not change the plan unless "
        "these lines show that it is wrong; if a file is missing here, request it before editing it.",
    ]
    for failure in failures:
        lines.extend(["", f"Block {failure['block']} — {failure['file'] or '(no FILE line)'}",
                      f"Problem: {failure['problem']}"])
    lines.extend(_reuse_lines(warnings))
    return "\n".join(lines).rstrip() + "\n"


def _reuse_lines(warnings: list[dict[str, Any]]) -> list[str]:
    if not warnings:
        return []
    lines = ["", "Possible duplicates — these new definitions already exist elsewhere. Reuse the existing code, or "
             "say why a new one is needed:"]
    for warning in warnings:
        for match in warning["existing"]:
            label = " (shared library)" if match["shared"] else ""
            lines.append(f"- new `{warning['name']}` in {warning['repo']}/{warning['path']} — existing in "
                         f"{match['repo']}/{match['path']}:{match['line']}{label}: {match['text']}")
    return lines


def _review_prompt(warnings: list[dict[str, Any]]) -> str:
    return "\n".join(["PROJECT BRAIN — REUSE CHECK", "", "The edits apply cleanly, but Brain found existing "
                      "definitions with the same names as code the edits add.", *_reuse_lines(warnings)[1:]]) + "\n"


def verify_edits(settings: Settings, text: str) -> tuple[dict[str, Any], dict[str, tuple[Repository, bytes]]]:
    """Check every block without writing anything; return the report and the patch for each repository."""
    blocks = parse_edit_blocks(text)
    report: dict[str, Any] = {"blocks": len(blocks), "verified": 0, "failures": [], "notes": [], "warnings": [],
                              "patches": [], "status": "none", "repair_prompt": None, "review_prompt": None}
    if not blocks:
        return report, {}
    if len(blocks) > MAX_EDIT_BLOCKS:
        raise BrainError(f"The reply contains {len(blocks)} edit blocks; split the change into at most "
                         f"{MAX_EDIT_BLOCKS} blocks per reply")
    files: dict[tuple[str, str], _File] = {}
    for block in blocks:
        target = block.target
        try:
            if block.problem:
                raise _BlockProblem(block.problem)
            repo, relative = _resolve(settings, block.target)
            target = f"{repo.name}/{relative}"
            key = repo.name, relative
            if key not in files:
                files[key] = _load(repo, relative)
            note = _apply(files[key], block)
            files[key].blocks.append(block.number)
            report["verified"] += 1
            if note and note != "new file":
                report["notes"].append(f"Block {block.number} ({target}): {note}.")
        except _BlockProblem as problem:
            report["failures"].append({"block": block.number, "file": target, "problem": str(problem)})
    changed = [file for file in files.values() if file.lines != (file.original or [])]
    patches: dict[str, tuple[Repository, bytes]] = {}
    if not report["failures"]:
        for repo in settings.repositories:
            payload = _patch([file for file in changed if file.repo.name == repo.name])
            if not payload:
                continue
            if problem := _git_check(repo, payload):
                report["failures"].append({"block": "all", "file": repo.name,
                                           "problem": f"git apply --check rejected the staged patch: {problem}"})
            patches[repo.name] = repo, payload
    report["warnings"] = _reuse_warnings(settings, changed, blocks)
    if report["failures"]:
        report["status"] = "needs_repair"
        report["repair_prompt"] = _repair_prompt(report["failures"], len(blocks), report["warnings"])
        return report, {}
    report["status"] = "verified_with_warnings" if report["warnings"] else "verified"
    if report["warnings"]:
        report["review_prompt"] = _review_prompt(report["warnings"])
    return report, patches


def check_edits(settings: Settings, ticket: str, text: str) -> dict[str, Any]:
    """Verify a reply's edit blocks and, when all apply, stage one patch per repository."""
    if not session_dir(settings, ticket).is_dir():
        raise BrainError(f"Session {ticket} does not exist. Run `brain start {ticket}` first.")
    # Matching and the reuse lookup read the index; only staging needs the ticket lock.
    report, patches = verify_edits(settings, text)
    if report["status"] == "none":
        return report
    return _stage(settings, ticket, report, patches)


@ticket_exclusive
def _stage(
    settings: Settings, ticket: str, report: dict[str, Any], patches: dict[str, tuple[Repository, bytes]],
) -> dict[str, Any]:
    state = session_state(settings, ticket)
    number = int(state.get("edit_check_count") or 0) + 1
    directory = handoff_dir(settings, ticket)
    for name, (repo, payload) in patches.items():
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-") or "repository"
        path = _validated_generated_artifact(settings, directory / f"edits-{number:03d}-{safe}.patch")
        atomic_managed_bytes_write(settings.generated_dir, path, payload)
        files = sorted({line[len("+++ b/"):] for line in payload.decode("utf-8").splitlines()
                        if line.startswith("+++ b/")})
        report["patches"].append({"repo": name, "path": str(path), "files": files,
                                  "apply": apply_command(repo, path)})
    report["check"] = number
    history = list(state.get("edit_checks") or [])[-(MAX_EDIT_HISTORY - 1):]
    history.append({"check": number, "at": datetime.now(UTC).isoformat(), "status": report["status"],
                    "blocks": report["blocks"], "verified": report["verified"], "failed": len(report["failures"]),
                    "reuse_warnings": len(report["warnings"]), "patches": [item["path"] for item in report["patches"]]})
    state["edit_check_count"] = number
    state["edit_checks"] = history
    save_session(settings, ticket, state)
    return report


def summary(report: dict[str, Any]) -> str:
    """Human-readable result for the CLI and MCP."""
    if report["status"] == "none":
        return ""
    lines = [f"Edit check {report.get('check', '')}".rstrip() + f": {report['verified']} of {report['blocks']} "
             "blocks apply to your working tree."]
    if report["status"] == "needs_repair":
        lines.append("No patch was staged. Send the repair prompt to the AI:")
        lines.extend(f"- Block {item['block']} ({item['file'] or 'no FILE line'}): "
                     f"{item['problem'].splitlines()[0]}" for item in report["failures"])
        return "\n".join(lines)
    lines.extend(report["notes"])
    for patch in report["patches"]:
        lines.append(f"Patch for {patch['repo']} ({', '.join(patch['files'])}):")
        lines.append(f"  {patch['apply']}")
    if report["warnings"]:
        lines.append("Possible duplicates of existing code (review prompt for the AI is available):")
        lines.extend(_reuse_lines(report["warnings"])[2:])
    return "\n".join(lines)

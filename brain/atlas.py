"""Generation-scoped Workspace Intelligence Atlas derived from authoritative state.

The catalog and ticket session remain the only sources of truth.  This module
builds immutable, reproducible routing facts and never serves source content;
all candidate locations are exact-verified by the existing hydration path.
"""

from __future__ import annotations

import ast
from bisect import bisect_left
import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Iterable

from .catalog import (
    RUNTIME_ANCHOR_INDEX_INVALIDATION_TRIGGERS,
    TERM_INDEX_INVALIDATION_TRIGGERS,
    _content_hash,
    connect,
)
from .platforms import (
    atomic_managed_bytes_write, is_test_path, logical_path, read_managed_bytes,
    run_bounded_process,
)

if TYPE_CHECKING:
    from .catalog import AtlasGenerationRef
    from .core import Settings

ATLAS_SCHEMA_VERSION = "3"
EXTRACTOR_VERSION = "atlas-structural-v3"
ATLAS_CARD_TERM_SCHEMA_VERSION = "atlas-card-terms-v1"
ATLAS_CHANGE_TERM_SCHEMA_VERSION = "atlas-change-terms-v1"
ROUTER_SCHEMA_VERSION = "atlas-router-v5"
MAX_CARD_ROUTING_TERMS = 128
MAX_CHANGE_ROUTING_TERMS = 64
MAX_ROUTING_QUERY_TERMS = 64
MAX_ROUTING_EXPLICIT_REPOS = 128
MAX_ROUTING_CARD_CANDIDATES = 2_000
MAX_CHANGE_COMMITS_PER_REPO = 100
MAX_CHANGE_PATHS_PER_COMMIT = 500
MAX_CHANGE_ROWS_PER_REPO = 5_000
MAX_CHANGE_PATH_CHARS = 1_024
MAX_CHANGE_PATH_BYTES = 4 * 1_024
MAX_CHANGE_GIT_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_CHANGE_GIT_STDERR_BYTES = 64 * 1024
MAX_CHANGE_GIT_SECONDS = 20.0
MAX_CHANGE_GIT_OPERATIONS = 300
MAX_CHANGE_GIT_STAGE_OUTPUT_BYTES = 128 * 1024 * 1024
MAX_CHANGE_GIT_STAGE_SECONDS = 300.0
MAX_ATLAS_REPOSITORIES = 100
MAX_ATLAS_FILES = 200_000
MAX_ATLAS_ENTITIES_PER_FILE = 2_048
MAX_ATLAS_EDGES_PER_FILE = 8_192
MAX_ATLAS_REGIONS_PER_FILE = 8_192
MAX_ATLAS_SOURCE_LINES_PER_FILE = 100_000
MAX_ATLAS_PARSE_SECONDS_PER_FILE = 2.0
MAX_ATLAS_ENTITIES = 500_000
MAX_ATLAS_REGIONS = 750_000
MAX_ATLAS_EDGES = 1_000_000
MAX_INVESTIGATION_RECORDS = 2_000
MAX_SIMILAR_INVESTIGATION_ROWS = 500
MAX_INVESTIGATION_OBJECTIVE_BYTES = 32 * 1024
MAX_INVESTIGATION_ENTITY_IDS = 500
MAX_INVESTIGATION_ENTITY_BYTES = 64 * 1024
MAX_INVESTIGATION_EVIDENCE_ROWS = 500
MAX_INVESTIGATION_EVIDENCE_BYTES = 128 * 1024
MAX_INVESTIGATION_JSON_ITEM_BYTES = 4 * 1024
MAX_SIMILAR_INVESTIGATION_SCAN_BYTES = 8 * 1024 * 1024
_TERM_INDEX_VALIDATION_CACHE: dict[tuple[object, ...], bool] = {}
ENTITY_KINDS = {
    "class", "interface", "trait", "function", "method", "constructor", "constant", "type", "test",
    "endpoint", "event", "topic", "queue", "config_key", "feature_flag", "schema", "table", "file", "unknown",
}
EDGE_TYPES = {
    "DEFINES", "CONTAINS", "IMPORTS", "CALLS", "IMPLEMENTS", "EXTENDS", "REFERENCES", "TESTS",
    "EXPOSES_ENDPOINT", "CALLS_ENDPOINT", "PUBLISHES", "CONSUMES", "READS_CONFIG", "WRITES_TABLE",
    "READS_TABLE", "DEPENDS_ON_REPO", "CO_CHANGED_WITH",
}
_CODE_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cs", ".go", ".java", ".js", ".jsx", ".kt", ".kts", ".php", ".py",
    ".rb", ".rs", ".scala", ".swift", ".ts", ".tsx", ".vue", ".sql", ".graphql", ".graphqls",
    ".xml", ".yaml", ".yml", ".toml", ".properties", ".gradle", ".proto", ".avsc",
}
_NAME = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_GENERIC_DEFINITION = re.compile(
    r"(?m)^[ \t]*(?:(?:public|private|protected|internal|export|abstract|static|async|final|open)\s+)*"
    r"(?:(class|interface|trait|enum|record|struct|type|def|function|fun|func)\s+)([A-Za-z_$][\w$]*)"
)


class AtlasCapacityError(RuntimeError):
    """A bounded Atlas refresh could not safely represent the new snapshot."""


def _append_derived(
    rows: list[dict[str, Any]], item: dict[str, Any], limit: int, deadline: float,
) -> None:
    if len(rows) >= limit:
        raise AtlasCapacityError("Atlas per-file derived-row budget exceeded")
    if time.monotonic() >= deadline:
        raise AtlasCapacityError("Atlas per-file parse time budget exceeded")
    rows.append(item)


def _hash(*values: object) -> str:
    return "sha256:" + hashlib.sha256("\0".join(str(value) for value in values).encode("utf-8")).hexdigest()


def _investigation_evidence_id(item: Any) -> str:
    identity = "\0".join(str(value) for value in (
        item.repo, item.path, item.line_start, item.line_end, item.content,
    ))
    return "E-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _language(path: str) -> str:
    return {
        ".py": "python", ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".ts": "typescript",
        ".tsx": "typescript", ".js": "javascript", ".jsx": "javascript", ".go": "go", ".rs": "rust",
        ".rb": "ruby", ".scala": "scala", ".cs": "csharp", ".sql": "sql", ".graphql": "graphql",
        ".graphqls": "graphql", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".xml": "xml",
        ".properties": "properties", ".gradle": "gradle", ".proto": "protobuf", ".avsc": "avro",
    }.get(PurePosixPath(path).suffix.lower(), "text")


def _module_path(path: str) -> str:
    parent = PurePosixPath(logical_path(path)).parent.as_posix()
    return parent if parent not in {"", "."} else "."


def _module(repo: str, path: str) -> dict[str, Any]:
    module_path = _module_path(path)
    language = _language(path)
    fingerprint = _hash(ATLAS_SCHEMA_VERSION, repo, module_path, language)
    module_id = _hash("module", fingerprint)
    return {
        "module_id": module_id, "repo": repo, "path": module_path,
        "name": PurePosixPath(module_path).name if module_path != "." else repo,
        "language": language, "fingerprint": fingerprint, "metadata": {},
    }


def _entity(
    repo: str,
    path: str,
    blob: str,
    module_id: str,
    *,
    line_start: int,
    line_end: int,
    name: str,
    kind: str,
    signature: str = "",
    parent_entity_id: str | None = None,
) -> dict[str, Any]:
    kind = kind if kind in ENTITY_KINDS else "unknown"
    qualified = f"{repo}:{path}:{name}"
    language = _language(path)
    metadata: dict[str, Any] = {}
    fingerprint = _hash(
        "entity", ATLAS_SCHEMA_VERSION, repo, module_id, path, line_start, line_end,
        qualified, name, signature, language, kind, parent_entity_id or "", blob,
        "project-brain", EXTRACTOR_VERSION, json.dumps(metadata, sort_keys=True),
    )
    return {
        "entity_id": _hash("entity", fingerprint), "repo": repo, "module_id": module_id, "path": path,
        "line_start": max(1, line_start), "line_end": max(line_start, line_end), "qualified_name": qualified,
        "simple_name": name, "signature": signature, "language": language, "kind": kind,
        "parent_entity_id": parent_entity_id, "blob_sha": blob, "extractor": "project-brain",
        "extractor_version": EXTRACTOR_VERSION, "fingerprint": fingerprint, "metadata": metadata,
    }


def _python_entities(
    repo: str, path: str, blob: str, module_id: str, content: str, deadline: float,
) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError):
        return []
    rows: list[dict[str, Any]] = []
    source_lines = content.splitlines()

    def visit(body: list[ast.stmt], parent: dict[str, Any] | None = None) -> None:
        for node in body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if isinstance(node, ast.ClassDef):
                    kind = "test" if node.name.startswith("Test") or is_test_path(path) else "class"
                elif node.name == "__init__" and parent:
                    kind = "constructor"
                elif parent:
                    kind = "test" if node.name.startswith("test_") or is_test_path(path) else "method"
                else:
                    kind = "test" if node.name.startswith("test_") or is_test_path(path) else "function"
                try:
                    signature = source_lines[node.lineno - 1].encode("utf-8")[node.col_offset:].decode("utf-8")
                except (IndexError, UnicodeDecodeError):
                    signature = node.name
                signature = signature[:500]
                row = _entity(
                    repo, path, blob, module_id, line_start=node.lineno,
                    line_end=int(getattr(node, "end_lineno", node.lineno)), name=node.name, kind=kind,
                    signature=signature, parent_entity_id=parent["entity_id"] if parent else None,
                )
                _append_derived(rows, row, MAX_ATLAS_ENTITIES_PER_FILE, deadline)
                visit(node.body, row)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and parent is None:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        _append_derived(
                            rows,
                            _entity(repo, path, blob, module_id, line_start=node.lineno,
                                    line_end=int(getattr(node, "end_lineno", node.lineno)),
                                    name=target.id, kind="constant"),
                            MAX_ATLAS_ENTITIES_PER_FILE, deadline,
                        )

    visit(tree.body)
    return rows


def _generic_entities(
    repo: str, path: str, blob: str, module_id: str, content: str, deadline: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    line_count = max(1, content.count("\n") + 1)
    previous_offset, start = 0, 1
    for match in _GENERIC_DEFINITION.finditer(content):
        token, name = match.group(1).lower(), match.group(2)
        kind = {
            "class": "class", "interface": "interface", "trait": "trait", "type": "type",
            "def": "function", "function": "function", "fun": "function", "func": "function",
            "enum": "type", "record": "type", "struct": "type",
        }.get(token, "unknown")
        if is_test_path(path) or name.lower().startswith("test"):
            kind = "test"
        start += content.count("\n", previous_offset, match.start())
        previous_offset = match.start()
        _append_derived(
            rows,
            _entity(repo, path, blob, module_id, line_start=start,
                    line_end=min(line_count, start + 120), name=name, kind=kind,
                    signature=match.group(0).strip()[:500]),
            MAX_ATLAS_ENTITIES_PER_FILE, deadline,
        )
    return rows


def _java_entities(
    repo: str, path: str, blob: str, module_id: str, structural: str, code_only: str, deadline: float,
) -> list[dict[str, Any]]:
    """Extract bounded Java class/method scopes for handler and call ownership."""
    line_count = max(1, structural.count("\n") + 1)
    structural_search = structural.rstrip()
    code_search = code_only.rstrip()
    newlines = [match.start() for match in re.finditer("\n", structural)]

    def line(position: int) -> int:
        return bisect_left(newlines, position) + 1

    # Parse braces once. Nested classes/methods used to rescan the same body
    # for every declaration, multiplying work on large Java files.
    closings: dict[int, int] = {}
    openings: list[int] = []
    for count, match in enumerate(re.finditer(r"[{}]", code_search)):
        if count % 4_096 == 0 and time.monotonic() >= deadline:
            raise AtlasCapacityError("Atlas per-file parse time budget exceeded")
        position, char = match.start(), match.group()
        if char == "{":
            openings.append(position)
        elif char == "}" and openings:
            closings[openings.pop()] = position

    def closing_position(opening: int) -> int:
        return closings.get(opening, len(code_only) - 1)

    classes: list[tuple[int, int, dict[str, Any]]] = []
    for match in re.finditer(r"\b(class|interface|record|enum)\s+([A-Za-z_$][\w$]*)", structural_search):
        if not code_only[match.start():match.start() + 1].strip():
            continue
        opening = code_only.find("{", match.end(), min(len(code_only), match.end() + 1_000))
        if opening < 0:
            continue
        closing = closing_position(opening)
        start, end = line(match.start()), line(closing)
        kind = "test" if is_test_path(path) or match.group(2).lower().startswith("test") else (
            "interface" if match.group(1) == "interface" else "type" if match.group(1) in {"record", "enum"} else "class"
        )
        entity = _entity(
            repo, path, blob, module_id, line_start=start, line_end=end,
            name=match.group(2), kind=kind, signature=match.group(0),
        )
        if len(classes) >= MAX_ATLAS_ENTITIES_PER_FILE or time.monotonic() >= deadline:
            raise AtlasCapacityError("Atlas per-file derived-row budget exceeded")
        classes.append((opening, closing, entity))

    rows = [item[2] for item in classes]
    method_pattern = re.compile(
        r"(?<![@\w$])(?:(?:public|private|protected|static|final|abstract|synchronized|native|default)[ \t]+)*"
        r"(?:<[^>{}\n]+>[ \t]+)?(?:[A-Za-z_$][\w$<>, ?.\[\]]*[ \t]+)?"
        r"([A-Za-z_$][\w$]*)[ \t]*\([^;{}\n]*\)[ \t]*(?:throws[^{\n]+)?\{"
    )
    controls = {"if", "for", "while", "switch", "catch", "try", "synchronized", "return", "new"}
    for match in method_pattern.finditer(code_search):
        name = match.group(1)
        if name in controls:
            continue
        start = line(match.start())
        parent = next(
            (item for opening, closing, item in reversed(classes) if opening < match.start() < closing),
            None,
        )
        if parent is None:
            continue
        opening = code_only.find("{", match.start(), match.end() + 1)
        if opening < 0:
            continue
        _append_derived(
            rows,
            _entity(
                repo, path, blob, module_id, line_start=start, line_end=line(closing_position(opening)),
                name=name, kind="test" if is_test_path(path) or name.lower().startswith("test") else (
                    "constructor" if name == parent["simple_name"] else "method"
                ),
                signature=structural[match.start():match.end()].strip()[:500],
                parent_entity_id=parent["entity_id"],
            ),
            MAX_ATLAS_ENTITIES_PER_FILE, deadline,
        )
    return rows


def _special_entities(
    repo: str, path: str, blob: str, module_id: str, content: str, deadline: float, limit: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    patterns = [
        ("endpoint", re.compile(r"(?m)(?:@(Get|Post|Put|Delete|Patch)Mapping|(?:app|router)\.(get|post|put|delete|patch))\s*\(?[\"']([^\"']+)", re.I)),
        ("topic", re.compile(r"(?im)(?:topic|kafka[^\n]{0,30})\s*[:=(]\s*[\"']([A-Za-z0-9._-]+)")),
        ("queue", re.compile(r"(?im)(?:queue|rabbit[^\n]{0,30})\s*[:=(]\s*[\"']([A-Za-z0-9._-]+)")),
        ("feature_flag", re.compile(r"(?im)(?:feature[_-]?flag|featureToggle)[^\n]{0,40}[\"']([A-Za-z0-9._-]+)")),
        ("table", re.compile(r"(?im)\b(?:from|join|into|update|table)\s+[`\"]?([A-Za-z_][A-Za-z0-9_.]*)")),
    ]
    for kind, pattern in patterns:
        previous_offset, line = 0, 1
        for match in pattern.finditer(content):
            name = next((group for group in reversed(match.groups()) if group), match.group(0))
            line += content.count("\n", previous_offset, match.start())
            previous_offset = match.start()
            _append_derived(
                rows,
                _entity(repo, path, blob, module_id, line_start=line, line_end=line,
                        name=str(name), kind=kind, signature=match.group(0).strip()[:500]),
                limit, deadline,
            )
    if PurePosixPath(path).suffix.lower() in {".yaml", ".yml", ".toml", ".properties", ".xml", ".json"}:
        for line_number, line in enumerate(content.splitlines(), 1):
            match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_.-]{2,})\s*[:=]", line)
            if match:
                _append_derived(
                    rows,
                    _entity(repo, path, blob, module_id, line_start=line_number, line_end=line_number,
                            name=match.group(1), kind="config_key", signature=line.strip()[:500]),
                    limit, deadline,
                )
    return rows


def _edge(
    edge_type: str,
    source_id: str,
    target_id: str,
    *,
    repo: str,
    path: str,
    line: int,
    blob: str,
    confidence: float,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    edge_type = edge_type if edge_type in EDGE_TYPES else "REFERENCES"
    normalized_confidence = max(0.0, min(1.0, confidence))
    normalized_metadata = metadata or {}
    edge_id = _hash(
        "edge", ATLAS_SCHEMA_VERSION, edge_type, source_id, target_id, repo, path,
        line, line, blob, "project-brain", EXTRACTOR_VERSION, normalized_confidence,
        json.dumps(normalized_metadata, sort_keys=True),
    )
    return {
        "edge_id": edge_id, "edge_type": edge_type, "source_id": source_id, "target_id": target_id,
        "repo": repo, "path": path, "line_start": max(1, line), "line_end": max(1, line), "blob_sha": blob,
        "extractor": "project-brain", "extractor_version": EXTRACTOR_VERSION,
        "confidence": normalized_confidence, "metadata": normalized_metadata,
    }


def _file_intelligence(repo: str, path: str, blob: str, content: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    from .investigation import MAX_REFRESH_FILE_BYTES, _java_file_intelligence, _mask_java_comments

    encoded = content.encode("utf-8")
    if len(encoded) > MAX_REFRESH_FILE_BYTES:
        content = encoded[:MAX_REFRESH_FILE_BYTES].decode("utf-8", errors="replace")
    if content.count("\n") + 1 > MAX_ATLAS_SOURCE_LINES_PER_FILE:
        raise AtlasCapacityError("Atlas per-file source-line budget exceeded")
    deadline = time.monotonic() + MAX_ATLAS_PARSE_SECONDS_PER_FILE

    module = _module(repo, path)
    lines = content.splitlines()
    suffix = PurePosixPath(path).suffix.lower()
    comment_syntax = suffix in {
        ".c", ".cc", ".cpp", ".cs", ".go", ".groovy", ".java", ".js", ".jsx",
        ".kt", ".kts", ".rs", ".scala", ".swift", ".ts", ".tsx",
    }
    structural_content = _mask_java_comments(content) if comment_syntax else content
    code_only_content = _mask_java_comments(content, strings=True) if comment_syntax else content
    file_entity = _entity(repo, path, blob, module["module_id"], line_start=1, line_end=max(1, len(lines)),
                          name=PurePosixPath(path).name, kind="file")
    definitions = (
        _python_entities(repo, path, blob, module["module_id"], content, deadline)
        if PurePosixPath(path).suffix.lower() == ".py"
        else _java_entities(repo, path, blob, module["module_id"], structural_content, code_only_content, deadline)
        if PurePosixPath(path).suffix.lower() == ".java"
        else _generic_entities(repo, path, blob, module["module_id"], structural_content, deadline)
    )
    remaining_entities = MAX_ATLAS_ENTITIES_PER_FILE - len(definitions)
    definitions.extend(_special_entities(
        repo, path, blob, module["module_id"], structural_content.rstrip(), deadline, remaining_entities,
    ))
    endpoint_edges: list[dict[str, Any]] = []
    if suffix == ".java":
        definitions = [item for item in definitions if item["kind"] != "endpoint"]
        _, java_facts = _java_file_intelligence(
            repo, path, blob, module["module_id"], content, definitions,
        )
        by_identity = {str(item["entity_id"]): item for item in definitions}
        for fact in java_facts:
            handler = by_identity.get(str(fact.get("entity_id") or ""))
            if fact.get("kind") != "endpoint" or handler is None:
                continue
            endpoint = _entity(
                repo, path, blob, module["module_id"],
                line_start=int(fact["line"]), line_end=int(fact["line"]),
                name=str(fact["key"]), kind="endpoint", signature=str(fact["framework"]),
                parent_entity_id=str(handler["entity_id"]),
            )
            _append_derived(definitions, endpoint, MAX_ATLAS_ENTITIES_PER_FILE, deadline)
            _append_derived(endpoint_edges, _edge(
                "EXPOSES_ENDPOINT", endpoint["entity_id"], handler["entity_id"],
                repo=repo, path=path, line=int(fact["line"]), blob=blob, confidence=1.0,
                metadata={"target_name": handler["simple_name"], "resolved": True},
            ), MAX_ATLAS_EDGES_PER_FILE, deadline)
    deduped = {item["entity_id"]: item for item in definitions}
    entities = [file_entity, *deduped.values()]
    regions: list[dict[str, Any]] = []
    for entity in entities:
        region_metadata = {"entity_id": entity["entity_id"]}
        region_fingerprint = _hash(blob, entity["line_start"], entity["line_end"])
        region_id = _hash(
            "region", ATLAS_SCHEMA_VERSION, repo, path, entity["line_start"], entity["line_end"],
            blob, entity["kind"], region_fingerprint, json.dumps(region_metadata, sort_keys=True),
        )
        _append_derived(regions, {
            "region_id": region_id, "repo": repo, "path": path, "line_start": entity["line_start"],
            "line_end": entity["line_end"], "blob_sha": blob, "kind": entity["kind"],
            "fingerprint": region_fingerprint, "metadata": region_metadata,
        }, MAX_ATLAS_REGIONS_PER_FILE, deadline)
    for start in range(1, max(1, len(lines)) + 1, 120):
        end = min(max(1, len(lines)), start + 119)
        region_metadata = {"file_entity_id": file_entity["entity_id"]}
        region_fingerprint = _hash(blob, start, end)
        region_id = _hash(
            "region", ATLAS_SCHEMA_VERSION, repo, path, start, end, blob, "source_region",
            region_fingerprint, json.dumps(region_metadata, sort_keys=True),
        )
        _append_derived(regions, {
            "region_id": region_id, "repo": repo, "path": path, "line_start": start, "line_end": end,
            "blob_sha": blob, "kind": "source_region", "fingerprint": region_fingerprint,
            "metadata": region_metadata,
        }, MAX_ATLAS_REGIONS_PER_FILE, deadline)
    edges = [_edge("CONTAINS", module["module_id"], file_entity["entity_id"], repo=repo, path=path, line=1, blob=blob, confidence=1.0)]
    edges.extend(endpoint_edges)
    edges.extend(_edge("DEFINES", file_entity["entity_id"], item["entity_id"], repo=repo, path=path,
                       line=item["line_start"], blob=blob, confidence=1.0) for item in definitions)
    name_to_entities: dict[str, list[str]] = {}
    for item in definitions:
        name_to_entities.setdefault(str(item["simple_name"]), []).append(str(item["entity_id"]))
    # A simple Java method name is not a dispatch identity. Resolve it only
    # when the file contains one eligible definition; ambiguous overloads or
    # sibling-class methods remain candidates for exact query-time evidence.
    name_to_entity = {
        name: values[0] for name, values in name_to_entities.items()
        if len(values) == 1
    }
    java_methods_by_owner: dict[tuple[str, str], list[str]] = {}
    if suffix == ".java":
        for item in definitions:
            parent_id = str(item.get("parent_entity_id") or "")
            if parent_id and item.get("kind") in {"method", "constructor"}:
                java_methods_by_owner.setdefault((parent_id, str(item["simple_name"])), []).append(
                    str(item["entity_id"]),
                )
    line_offsets = [0]
    for match in re.finditer("\n", content):
        line_offsets.append(match.end())

    def line_at(position: int) -> int:
        import bisect
        return bisect.bisect_right(line_offsets, position)

    patterns = [
        ("IMPORTS", re.compile(r"(?m)^[ \t]*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+)|(?:import|require)\s*\(?[\"']([^\"']+))"), .95),
        ("EXTENDS", re.compile(r"\b(?:extends)\s+([A-Za-z_$][\w$]*)"), .9),
        ("IMPLEMENTS", re.compile(r"\bimplements\s+([A-Za-z_$][\w$]*)"), .9),
        ("CALLS", re.compile(r"\b([A-Za-z_$][\w$]*)\s*\("), .65),
    ]
    for edge_type, pattern, confidence in patterns:
        pattern_content = code_only_content if edge_type == "CALLS" else structural_content
        search_content = pattern_content.rstrip()
        for match in pattern.finditer(search_content):
            name = next((group for group in match.groups() if group), "")
            if not name or name in {"if", "for", "while", "switch", "return", "class", "def", "function"}:
                continue
            line = line_at(match.start())
            enclosed = [item for item in definitions if item["line_start"] <= line <= item["line_end"]]
            owner = max(
                enclosed,
                key=lambda item: (item["line_start"], item["kind"] in {"method", "constructor"}),
                default=file_entity,
            )
            receiver: str | None = None
            if edge_type == "CALLS" and match.start() > 0 and search_content[match.start() - 1] == ".":
                receiver_match = re.search(r"([A-Za-z_$][\w$]*)\.$", search_content[:match.start()])
                receiver = receiver_match.group(1) if receiver_match else ""
            dispatch_scope = str(
                owner.get("parent_entity_id")
                or (owner.get("entity_id") if owner.get("kind") in {"class", "interface", "type"} else "")
            )
            # Java unqualified/this dispatch is scoped to the owning class.
            # Other receivers require exact type resolution and remain candidates.
            if edge_type == "CALLS" and suffix == ".java":
                scoped = java_methods_by_owner.get((dispatch_scope, name), [])
                resolved_target = scoped[0] if receiver in {None, "this"} and len(scoped) == 1 else None
            else:
                resolved_target = name_to_entity.get(name) if receiver in {None, "this"} else None
            target = resolved_target or _hash("symbol", (receiver + "." if receiver else "") + name.lower())
            if target == owner["entity_id"]:
                continue
            _append_derived(edges, _edge(edge_type, owner["entity_id"], target, repo=repo, path=path, line=line,
                                         blob=blob, confidence=confidence, metadata={
                                   "target_name": name, "receiver": receiver,
                                   "resolved": resolved_target is not None,
                                   "dispatch_scope": dispatch_scope if suffix == ".java" else None,
                                   "language": "java" if suffix == ".java" else None,
                               }), MAX_ATLAS_EDGES_PER_FILE, deadline)
    for test in (item for item in definitions if item["kind"] == "test"):
        tokens = set(_NAME.findall("\n".join(lines[test["line_start"] - 1:test["line_end"]])))
        for name, target in name_to_entity.items():
            if name in tokens and target != test["entity_id"]:
                _append_derived(
                    edges,
                    _edge("TESTS", test["entity_id"], target, repo=repo, path=path,
                          line=test["line_start"], blob=blob, confidence=.85),
                    MAX_ATLAS_EDGES_PER_FILE, deadline,
                )
    return module, entities, regions, list({item["edge_id"]: item for item in edges}.values())


def _card(level: str, target_id: str, repo: str, content: str, *, module_id: str | None = None,
          entity_id: str | None = None, path: str | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    content = content.strip()[:12_000]
    content_hash = _hash("card-content", content)
    normalized_metadata = metadata or {}
    return {
        "card_id": _hash(
            "card", ATLAS_SCHEMA_VERSION, level, target_id, repo, module_id or "", entity_id or "",
            path or "", content_hash, json.dumps(normalized_metadata, sort_keys=True),
        ), "level": level, "target_id": target_id,
        "repo": repo, "module_id": module_id, "entity_id": entity_id, "path": path, "content": content,
        "content_hash": content_hash, "metadata": normalized_metadata,
    }


def _change_rows(
    settings: Settings,
    snapshots: dict[str, str],
    parent_snapshots: dict[str, str],
    previous: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rows: list[dict[str, Any]] = []
    omitted = {
        "git_failures": 0,
        "oversized_paths": 0,
        "row_limit_reached": 0,
        "budget_exhausted_repos": 0,
        "operations": 0,
        "output_bytes": 0,
    }
    remaining_output_bytes = MAX_CHANGE_GIT_STAGE_OUTPUT_BYTES
    deadline = time.monotonic() + MAX_CHANGE_GIT_STAGE_SECONDS

    def bounded_git(args: list[str], cwd: Path, max_stdout_bytes: int) -> Any | None:
        nonlocal remaining_output_bytes
        if (
            omitted["operations"] >= MAX_CHANGE_GIT_OPERATIONS
            or remaining_output_bytes < 2
            or time.monotonic() >= deadline
        ):
            return None
        stdout_limit = min(max_stdout_bytes, max(1, remaining_output_bytes - 1))
        stderr_limit = min(MAX_CHANGE_GIT_STDERR_BYTES, max(1, remaining_output_bytes - stdout_limit))
        completed = run_bounded_process(
            args,
            cwd,
            max_stdout_bytes=stdout_limit,
            max_stderr_bytes=stderr_limit,
            timeout=min(MAX_CHANGE_GIT_SECONDS, max(0.1, deadline - time.monotonic())),
        )
        used = int(getattr(completed, "stdout_bytes", len(completed.stdout.encode("utf-8")))) + int(
            getattr(completed, "stderr_bytes", len(completed.stderr.encode("utf-8")))
        )
        omitted["operations"] += 1
        omitted["output_bytes"] += used
        remaining_output_bytes = max(0, remaining_output_bytes - used)
        return completed

    ticket_pattern = re.compile(settings.ticket_pattern)
    previous_by_repo: dict[str, list[dict[str, Any]]] = {}
    for item in previous:
        previous_by_repo.setdefault(str(item["repo"]), []).append(item)
    for repo in settings.repositories:
        if not (repo.path / ".git").exists():
            continue
        current = snapshots.get(repo.name)
        parent = parent_snapshots.get(repo.name)
        prior = previous_by_repo.get(repo.name, [])
        if parent and current and parent == current:
            rows.extend(prior)
            continue
        if (
            omitted["operations"] >= MAX_CHANGE_GIT_OPERATIONS
            or remaining_output_bytes < 2
            or time.monotonic() >= deadline
        ):
            omitted["budget_exhausted_repos"] += 1
            rows.extend(prior)
            continue
        incremental = False
        if parent and current:
            try:
                merge_base = bounded_git(
                    ["git", "merge-base", "--is-ancestor", parent, current],
                    repo.path,
                    1_024,
                )
                if merge_base is None:
                    omitted["budget_exhausted_repos"] += 1
                    rows.extend(prior)
                    continue
                incremental = merge_base.returncode == 0
            except OSError:
                omitted["git_failures"] += 1
                rows.extend(prior)
                continue
        revision = f"{parent}..{current}" if incremental else str(current or "HEAD")
        args = ["git", "log", "--format=%x1e%H%x1f%cI%x1f%s", f"--max-count={MAX_CHANGE_COMMITS_PER_REPO}"]
        if incremental:
            args.append(revision)
        elif current:
            args.append(current)
        try:
            shown = bounded_git(
                [*args, "--name-status", "--find-renames"],
                repo.path,
                MAX_CHANGE_GIT_OUTPUT_BYTES,
            )
            counted = bounded_git(
                [*args, "--numstat", "--find-renames"],
                repo.path,
                MAX_CHANGE_GIT_OUTPUT_BYTES,
            )
        except OSError:
            omitted["git_failures"] += 1
            rows.extend(prior)
            continue
        if shown is None or counted is None:
            omitted["budget_exhausted_repos"] += 1
            rows.extend(prior)
            continue
        if shown.returncode != 0 or counted.returncode != 0:
            omitted["git_failures"] += 1
            rows.extend(prior)
            continue

        statistics_by_commit: dict[str, dict[str, tuple[int | None, int | None]]] = {}
        for section in counted.stdout.split("\x1e"):
            lines = section.strip().splitlines()
            if not lines:
                continue
            parts = lines[0].split("\x1f", 2)
            if len(parts) != 3:
                continue
            statistics: dict[str, tuple[int | None, int | None]] = {}
            for stat in lines[1:MAX_CHANGE_PATHS_PER_COMMIT + 1]:
                values = stat.split("\t")
                if len(values) >= 3:
                    statistics[values[-1]] = (
                        int(values[0]) if values[0].isdigit() else None,
                        int(values[1]) if values[1].isdigit() else None,
                    )
            statistics_by_commit[parts[0]] = statistics

        repo_change_count = 0
        for section in shown.stdout.split("\x1e"):
            if repo_change_count >= MAX_CHANGE_ROWS_PER_REPO:
                omitted["row_limit_reached"] += 1
                break
            lines = section.strip().splitlines()
            if not lines:
                continue
            parts = lines[0].split("\x1f", 2)
            if len(parts) != 3:
                continue
            commit, committed_at, subject = parts
            subject = subject[:500]
            statistics = statistics_by_commit.get(commit, {})
            ticket_match = ticket_pattern.search(subject)
            ticket = next((value for value in ticket_match.groups() if value), ticket_match.group(0)) if ticket_match else None
            for line in lines[1:MAX_CHANGE_PATHS_PER_COMMIT + 1]:
                if repo_change_count >= MAX_CHANGE_ROWS_PER_REPO:
                    break
                values = line.split("\t")
                if len(values) < 2:
                    continue
                status = values[0][0]
                old_path = values[1] if status == "R" and len(values) > 2 else None
                path = values[2] if old_path else values[1]
                if any(
                    len(value) > MAX_CHANGE_PATH_CHARS
                    or len(value.encode("utf-8")) > MAX_CHANGE_PATH_BYTES
                    for value in (path, old_path) if value is not None
                ):
                    omitted["oversized_paths"] += 1
                    continue
                additions, deletions = statistics.get(path, (None, None))
                normalized_status = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed"}.get(status, "changed")
                metadata = {
                    "subject": subject[:500], "is_test": is_test_path(path),
                    "is_config": PurePosixPath(path).suffix.lower() in {".yaml", ".yml", ".toml", ".properties", ".xml", ".json"},
                }
                change_id = _hash(
                    "change", ATLAS_SCHEMA_VERSION, repo.name, commit, committed_at, ticket or "", path,
                    old_path or "", normalized_status, additions, deletions, json.dumps(metadata, sort_keys=True),
                )
                rows.append({
                    "change_id": change_id, "repo": repo.name, "commit_sha": commit, "committed_at": committed_at,
                    "ticket": ticket, "path": path, "old_path": old_path,
                    "status": normalized_status, "additions": additions, "deletions": deletions,
                    "metadata": metadata,
                })
                repo_change_count += 1
        if incremental:
            rows.extend(prior)
    unique = {str(item["change_id"]): item for item in rows}
    return _retain_change_rows(unique.values()), omitted


def _retain_change_rows(values: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply per-repository retention after one global partition pass."""
    by_repo: dict[str, list[dict[str, Any]]] = {}
    for item in values:
        by_repo.setdefault(str(item["repo"]), []).append(item)
    retained: list[dict[str, Any]] = []
    for repo_name in sorted(by_repo):
        repo_rows = by_repo[repo_name]
        commits = sorted(
            {(str(item.get("committed_at") or ""), str(item["commit_sha"])) for item in repo_rows},
            reverse=True,
        )[:MAX_CHANGE_COMMITS_PER_REPO]
        allowed = {commit for _, commit in commits}
        retained.extend(item for item in repo_rows if str(item["commit_sha"]) in allowed)
    return retained


def validate_atlas_payload(payload: dict[str, Any], repositories: set[str]) -> None:
    """Independently rederive every persisted Atlas row identity and reference.

    Component hashes alone cannot establish authority because a caller could
    recompute a component hash after mutating its payload. Publication uses
    this validator before registration so even previously unseen poisoned IDs
    cannot become immutable Atlas rows.
    """
    collections = {
        name: payload.get(name)
        for name in ("modules", "entities", "regions", "edges", "cards", "changes")
    }
    if not all(isinstance(value, list) for value in collections.values()):
        raise ValueError("Atlas payload collections are incomplete")

    def checked_rows(name: str, identity: str) -> tuple[list[dict[str, Any]], set[str]]:
        values = collections[name]
        assert isinstance(values, list)
        rows: list[dict[str, Any]] = []
        identifiers: set[str] = set()
        for item in values:
            if not isinstance(item, dict) or identity not in item:
                raise ValueError(f"Atlas {name} row is invalid")
            identifier = str(item[identity])
            if not identifier or identifier in identifiers:
                raise ValueError(f"Atlas {name} identity is missing or duplicated")
            identifiers.add(identifier)
            rows.append(item)
        return rows, identifiers

    modules, module_ids = checked_rows("modules", "module_id")
    entities, entity_ids = checked_rows("entities", "entity_id")
    regions, _ = checked_rows("regions", "region_id")
    edges, _ = checked_rows("edges", "edge_id")
    cards, _ = checked_rows("cards", "card_id")
    changes, _ = checked_rows("changes", "change_id")

    for item in modules:
        repo, path, language = str(item.get("repo") or ""), str(item.get("path") or ""), str(item.get("language") or "")
        metadata = item.get("metadata")
        fingerprint = _hash(ATLAS_SCHEMA_VERSION, repo, path, language)
        expected = {
            "module_id": _hash("module", fingerprint), "repo": repo, "path": path,
            "name": PurePosixPath(path).name if path != "." else repo, "language": language,
            "fingerprint": fingerprint, "metadata": {},
        }
        if repo not in repositories or metadata != {} or item != expected:
            raise ValueError("Atlas module content identity is invalid")

    modules_by_id = {str(item["module_id"]): item for item in modules}
    entities_by_id = {str(item["entity_id"]): item for item in entities}
    file_entities: dict[tuple[str, str], dict[str, Any]] = {}
    for item in entities:
        repo = str(item.get("repo") or "")
        try:
            line_start, line_end = int(item["line_start"]), int(item["line_end"])
            expected = _entity(
                repo, str(item["path"]), str(item["blob_sha"]), str(item["module_id"]),
                line_start=line_start, line_end=line_end, name=str(item["simple_name"]),
                kind=str(item["kind"]), signature=str(item["signature"]),
                parent_entity_id=str(item["parent_entity_id"]) if item.get("parent_entity_id") is not None else None,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Atlas entity row is invalid") from error
        if repo not in repositories or line_start < 1 or line_end < line_start or item != expected:
            raise ValueError("Atlas entity content identity is invalid")
        if str(item["module_id"]) not in module_ids:
            raise ValueError("Atlas entity module reference is invalid")
        parent = item.get("parent_entity_id")
        if parent is not None and str(parent) not in entity_ids:
            raise ValueError("Atlas entity parent reference is invalid")
        if item["kind"] == "file":
            file_entities[(repo, str(item["path"]))] = item
    for item in entities:
        if item["kind"] == "file":
            continue
        owner = file_entities.get((str(item["repo"]), str(item["path"])))
        if owner is None or owner["module_id"] != item["module_id"] or owner["blob_sha"] != item["blob_sha"]:
            raise ValueError("Atlas entity file membership is invalid")

    for item in regions:
        try:
            repo, path, blob = str(item["repo"]), str(item["path"]), str(item["blob_sha"])
            line_start, line_end = int(item["line_start"]), int(item["line_end"])
            kind = str(item["kind"])
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else None
            fingerprint = _hash(blob, line_start, line_end)
            expected = {
                "region_id": _hash(
                    "region", ATLAS_SCHEMA_VERSION, repo, path, line_start, line_end,
                    blob, kind, fingerprint, json.dumps(metadata, sort_keys=True),
                ),
                "repo": repo, "path": path, "line_start": line_start, "line_end": line_end,
                "blob_sha": blob, "kind": kind, "fingerprint": fingerprint, "metadata": metadata,
            }
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Atlas region row is invalid") from error
        owner = file_entities.get((repo, path))
        reference = (metadata or {}).get("entity_id") or (metadata or {}).get("file_entity_id")
        if (
            repo not in repositories or line_start < 1 or line_end < line_start or metadata is None
            or item != expected or owner is None or owner["blob_sha"] != blob
            or str(reference or "") not in entity_ids
        ):
            raise ValueError("Atlas region content identity or membership is invalid")

    known_sources = module_ids | entity_ids
    for item in edges:
        try:
            repo, path, blob = str(item["repo"]), str(item["path"]), str(item["blob_sha"])
            line_start, line_end = int(item["line_start"]), int(item["line_end"])
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else None
            expected = _edge(
                str(item["edge_type"]), str(item["source_id"]), str(item["target_id"]),
                repo=repo, path=path, line=line_start, blob=blob,
                confidence=float(item["confidence"]), metadata=metadata,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Atlas edge row is invalid") from error
        source_id, target_id = str(item["source_id"]), str(item["target_id"])
        source_entity = entities_by_id.get(source_id)
        source_file = source_entity or entities_by_id.get(target_id) if source_id in module_ids else source_entity
        if (
            repo not in repositories or line_start != line_end or metadata is None or item != expected
            or source_id not in known_sources
            or (bool(metadata.get("resolved")) and target_id not in entity_ids)
            or source_file is None or str(source_file["repo"]) != repo
            or str(source_file["path"]) != path or str(source_file["blob_sha"]) != blob
        ):
            raise ValueError("Atlas edge content identity or membership is invalid")

    for item in cards:
        try:
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else None
            expected = _card(
                str(item["level"]), str(item["target_id"]), str(item["repo"]), str(item["content"]),
                module_id=str(item["module_id"]) if item.get("module_id") is not None else None,
                entity_id=str(item["entity_id"]) if item.get("entity_id") is not None else None,
                path=str(item["path"]) if item.get("path") is not None else None,
                metadata=metadata,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Atlas card row is invalid") from error
        level, repo = str(item["level"]), str(item["repo"])
        target_valid = (
            (
                level == "repo" and item["target_id"] == _hash("repo", repo)
                and item.get("module_id") is None and item.get("entity_id") is None and item.get("path") is None
            )
            or (
                level == "module" and item["target_id"] in module_ids and item.get("module_id") == item["target_id"]
                and item.get("entity_id") is None
                and modules_by_id[str(item["target_id"])]["repo"] == repo
                and modules_by_id[str(item["target_id"])]["path"] == item.get("path")
            )
            or (
                level == "entity" and item["target_id"] in entity_ids and item.get("entity_id") == item["target_id"]
                and entities_by_id[str(item["target_id"])]["repo"] == repo
                and entities_by_id[str(item["target_id"])]["module_id"] == item.get("module_id")
                and entities_by_id[str(item["target_id"])]["path"] == item.get("path")
            )
        )
        if repo not in repositories or metadata is None or item != expected or not target_valid:
            raise ValueError("Atlas card content identity or membership is invalid")

    allowed_statuses = {"added", "modified", "deleted", "renamed", "changed"}
    for item in changes:
        try:
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else None
            values = (
                str(item["repo"]), str(item["commit_sha"]), str(item.get("committed_at") or ""),
                str(item.get("ticket") or ""), str(item["path"]), str(item.get("old_path") or ""),
                str(item["status"]), item.get("additions"), item.get("deletions"),
            )
            expected_id = _hash(
                "change", ATLAS_SCHEMA_VERSION, *values, json.dumps(metadata, sort_keys=True),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Atlas change row is invalid") from error
        if (
            values[0] not in repositories or metadata is None or item.get("change_id") != expected_id
            or values[6] not in allowed_statuses
        ):
            raise ValueError("Atlas change content identity is invalid")


def _location(value: str) -> tuple[str, str, int] | None:
    match = re.fullmatch(r"([^:]+):(.+):(\d+)", value)
    return (match.group(1), match.group(2), int(match.group(3))) if match else None


def _integration_edges(
    settings: Settings, entities: Iterable[dict[str, Any]], snapshots: dict[str, str],
) -> list[dict[str, Any]]:
    path = settings.state_dir / "relationships.json"
    try:
        from .platforms import read_managed_text

        state = json.loads(read_managed_text(
            settings.state_dir, path, max_bytes=16 * 1024 * 1024,
        ))
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    from .relations import valid_relationship_payload

    if not valid_relationship_payload(state, snapshots):
        return []
    by_path: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entity in entities:
        by_path.setdefault((entity["repo"], entity["path"]), []).append(entity)

    def owner(location: tuple[str, str, int]) -> dict[str, Any] | None:
        repo, file_path, line = location
        values = by_path.get((repo, file_path), [])
        enclosed = [
            item for item in values
            if item["kind"] != "file" and item["line_start"] <= line <= item["line_end"]
        ]
        return (
            max(enclosed, key=lambda item: (item["line_start"], item["kind"] == "method"))
            if enclosed else next((item for item in values if item["kind"] == "file"), None)
        )

    result: list[dict[str, Any]] = []
    for relationship in state.get("relationships") or []:
        if not isinstance(relationship, dict):
            continue
        source_location = _location(str(relationship.get("source_evidence") or ""))
        target_location = _location(str(relationship.get("target_evidence") or ""))
        if source_location is None:
            continue
        source = owner(source_location)
        target = owner(target_location) if target_location else None
        if source is None:
            continue
        target_id = target["entity_id"] if target else _hash("repo", relationship.get("target"))
        mapping = {
            "MAVEN_DEPENDS_ON": "DEPENDS_ON_REPO", "HTTP": "CALLS_ENDPOINT",
            "FEIGN_TARGET": "CALLS_ENDPOINT", "KAFKA": "PUBLISHES",
        }
        edge_type = mapping.get(str(relationship.get("kind")), "REFERENCES")
        result.append(_edge(
            edge_type, source["entity_id"], target_id, repo=source["repo"], path=source["path"],
            line=source_location[2], blob=source["blob_sha"],
            confidence=1.0 if relationship.get("confidence", "high") == "high" else .7,
            metadata={"key": relationship.get("key"), "target_repo": relationship.get("target")},
        ))
        if relationship.get("kind") == "KAFKA" and target is not None:
            result.append(_edge(
                "CONSUMES", target["entity_id"], source["entity_id"], repo=target["repo"], path=target["path"],
                line=target_location[2] if target_location else target["line_start"], blob=target["blob_sha"],
                confidence=1.0, metadata={"key": relationship.get("key"), "source_repo": relationship.get("source")},
            ))
        elif relationship.get("kind") in {"HTTP", "FEIGN_TARGET"} and target is not None:
            result.append(_edge(
                "EXPOSES_ENDPOINT", target["entity_id"], source["entity_id"], repo=target["repo"], path=target["path"],
                line=target_location[2] if target_location else target["line_start"], blob=target["blob_sha"],
                confidence=.9, metadata={"key": relationship.get("key"), "caller_repo": relationship.get("source")},
            ))
    return result


def _parent_snapshots(connection: sqlite3.Connection, generation: int | None) -> dict[str, str]:
    if generation is None:
        return {}
    return {str(repo): str(sha) for repo, sha in connection.execute(
        "SELECT repo,snapshot_sha FROM generation_snapshots WHERE generation=?", (generation,)
    )}


def _json_object(value: object) -> dict[str, Any]:
    try:
        loaded = json.loads(str(value))
        return loaded if isinstance(loaded, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _valid_entity_content_identity(item: dict[str, Any]) -> bool:
    """Re-derive an Atlas entity identity before serving persisted rows."""
    try:
        expected = _entity(
            str(item["repo"]), str(item["path"]), str(item["blob_sha"]), str(item["module_id"]),
            line_start=int(item["line_start"]), line_end=int(item["line_end"]),
            name=str(item["simple_name"]), kind=str(item["kind"]), signature=str(item["signature"]),
            parent_entity_id=(
                str(item["parent_entity_id"]) if item.get("parent_entity_id") is not None else None
            ),
        )
    except (KeyError, TypeError, ValueError):
        return False
    return item == expected


def _valid_edge_content_identity(item: dict[str, Any]) -> bool:
    """Re-derive an Atlas edge identity before treating it as graph evidence."""
    metadata = item.get("metadata")
    if not isinstance(metadata, dict):
        return False
    try:
        expected = _edge(
            str(item["edge_type"]), str(item["source_id"]), str(item["target_id"]),
            repo=str(item["repo"]), path=str(item["path"]), line=int(item["line_start"]),
            blob=str(item["blob_sha"]), confidence=float(item["confidence"]), metadata=metadata,
        )
    except (KeyError, TypeError, ValueError):
        return False
    return item == expected


def _valid_generation_entities(
    connection: sqlite3.Connection,
    generation: int,
    entity_ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Load only canonical entities whose module and snapshot memberships agree."""
    requested = list(dict.fromkeys(str(value) for value in entity_ids if value))[:5_000]
    if not requested:
        return {}
    slots = ",".join("?" for _ in requested)
    rows = connection.execute(
        "SELECT e.entity_id,e.repo,e.module_id,e.path,e.line_start,e.line_end,e.qualified_name,e.simple_name,"
        "e.signature,e.language,e.kind,e.parent_entity_id,e.blob_sha,e.extractor,e.extractor_version,e.fingerprint,"
        "e.metadata_json,m.repo,m.path,m.name,m.language,m.fingerprint,m.metadata_json "
        "FROM generation_entities ge JOIN atlas_entities e ON e.entity_id=ge.entity_id "
        "JOIN generation_snapshots gs ON gs.generation=ge.generation AND gs.repo=e.repo "
        "AND gs.snapshot_sha=ge.snapshot_sha "
        "JOIN generation_modules gm ON gm.generation=ge.generation AND gm.module_id=e.module_id "
        "AND gm.snapshot_sha=ge.snapshot_sha JOIN atlas_modules m ON m.module_id=gm.module_id "
        f"WHERE ge.generation=? AND e.entity_id IN ({slots})",
        (generation, *requested),
    )
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = {
            "entity_id": row[0], "repo": row[1], "module_id": row[2], "path": row[3],
            "line_start": row[4], "line_end": row[5], "qualified_name": row[6],
            "simple_name": row[7], "signature": row[8], "language": row[9], "kind": row[10],
            "parent_entity_id": row[11], "blob_sha": row[12], "extractor": row[13],
            "extractor_version": row[14], "fingerprint": row[15], "metadata": _json_object(row[16]),
        }
        module_fingerprint = _hash(ATLAS_SCHEMA_VERSION, str(row[17]), str(row[18]), str(row[20]))
        module_valid = (
            str(row[2]) == _hash("module", module_fingerprint)
            and str(row[17]) == str(row[1])
            and str(row[19]) == (
                PurePosixPath(str(row[18])).name if str(row[18]) != "." else str(row[17])
            )
            and str(row[21]) == module_fingerprint
            and _json_object(row[22]) == {}
        )
        if module_valid and _valid_entity_content_identity(item):
            result[str(row[0])] = item
    return result


def _valid_generation_edges(
    connection: sqlite3.Connection,
    generation: int,
    edge_ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Load only canonical graph rows with valid pinned-generation membership."""
    requested = list(dict.fromkeys(str(value) for value in edge_ids if value))[:5_000]
    if not requested:
        return {}
    slots = ",".join("?" for _ in requested)
    raw_rows = connection.execute(
        "SELECT e.edge_id,e.edge_type,e.source_id,e.target_id,e.repo,e.path,e.line_start,e.line_end,e.blob_sha,"
        "e.extractor,e.extractor_version,e.confidence,e.metadata_json "
        "FROM generation_edges ge JOIN atlas_edges e ON e.edge_id=ge.edge_id "
        "JOIN generation_snapshots gs ON gs.generation=ge.generation AND gs.repo=e.repo "
        "AND gs.snapshot_sha=ge.snapshot_sha "
        f"WHERE ge.generation=? AND e.edge_id IN ({slots})",
        (generation, *requested),
    ).fetchall()
    items = [{
        "edge_id": row[0], "edge_type": row[1], "source_id": row[2], "target_id": row[3],
        "repo": row[4], "path": row[5], "line_start": row[6], "line_end": row[7],
        "blob_sha": row[8], "extractor": row[9], "extractor_version": row[10],
        "confidence": row[11], "metadata": _json_object(row[12]),
    } for row in raw_rows]
    referenced_entities = {
        str(value)
        for item in items
        for value in (item["source_id"], item["target_id"])
    }
    entities = _valid_generation_entities(connection, generation, referenced_entities)
    module_ids = {str(item["source_id"]) for item in items if str(item["source_id"]) not in entities}
    valid_modules: set[str] = set()
    if module_ids:
        module_slots = ",".join("?" for _ in module_ids)
        for row in connection.execute(
            "SELECT m.module_id,m.repo,m.path,m.name,m.language,m.fingerprint,m.metadata_json "
            "FROM generation_modules gm JOIN atlas_modules m ON m.module_id=gm.module_id "
            "JOIN generation_snapshots gs ON gs.generation=gm.generation AND gs.repo=m.repo "
            "AND gs.snapshot_sha=gm.snapshot_sha "
            f"WHERE gm.generation=? AND m.module_id IN ({module_slots})",
            (generation, *sorted(module_ids)),
        ):
            fingerprint = _hash(ATLAS_SCHEMA_VERSION, str(row[1]), str(row[2]), str(row[4]))
            if (
                str(row[0]) == _hash("module", fingerprint) and str(row[3]) == (
                    PurePosixPath(str(row[2])).name if str(row[2]) != "." else str(row[1])
                ) and str(row[5]) == fingerprint and _json_object(row[6]) == {}
            ):
                valid_modules.add(str(row[0]))
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        source_id, target_id = str(item["source_id"]), str(item["target_id"])
        source_entity = entities.get(source_id)
        source_file = source_entity or (entities.get(target_id) if source_id in valid_modules else None)
        metadata = item["metadata"]
        if (
            _valid_edge_content_identity(item)
            and (source_id in entities or source_id in valid_modules)
            and (not bool(metadata.get("resolved")) or target_id in entities)
            and source_file is not None
            and str(source_file["repo"]) == str(item["repo"])
            and str(source_file["path"]) == str(item["path"])
            and str(source_file["blob_sha"]) == str(item["blob_sha"])
        ):
            result[str(item["edge_id"])] = item
    return result


def _generation_changes(connection: sqlite3.Connection, generation: int | None) -> list[dict[str, Any]]:
    if generation is None:
        return []
    return [{
        "change_id": row[0], "repo": row[1], "commit_sha": row[2], "committed_at": row[3],
        "ticket": row[4], "path": row[5], "old_path": row[6], "status": row[7],
        "additions": row[8], "deletions": row[9], "metadata": _json_object(row[10]),
    } for row in connection.execute(
        "SELECT c.change_id,c.repo,c.commit_sha,c.committed_at,c.ticket,c.path,c.old_path,c.status,"
        "c.additions,c.deletions,c.metadata_json FROM generation_changes g "
        "JOIN atlas_changes c ON c.change_id=g.change_id WHERE g.generation=?",
        (generation,),
    )]


def _reused_generation_intelligence(
    connection: sqlite3.Connection,
    generation: int,
) -> dict[tuple[str, str, str], tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]]:
    reused: dict[tuple[str, str, str], tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]] = {}
    entity_count = 0
    region_count = 0
    edge_count = 0

    def buckets(key: tuple[str, str, str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        return reused.setdefault(key, ([], [], []))

    for row in connection.execute(
        "SELECT e.entity_id,e.repo,e.module_id,e.path,e.line_start,e.line_end,e.qualified_name,e.simple_name,e.signature,"
        "e.language,e.kind,e.parent_entity_id,e.blob_sha,e.extractor,e.extractor_version,e.fingerprint,e.metadata_json "
        "FROM generation_entities g JOIN atlas_entities e ON e.entity_id=g.entity_id WHERE g.generation=?",
        (generation,),
    ):
        entity_count += 1
        if entity_count > MAX_ATLAS_ENTITIES:
            raise AtlasCapacityError("Atlas reused entity budget exceeded")
        item = {
        "entity_id": row[0], "repo": row[1], "module_id": row[2], "path": row[3], "line_start": row[4],
        "line_end": row[5], "qualified_name": row[6], "simple_name": row[7], "signature": row[8],
        "language": row[9], "kind": row[10], "parent_entity_id": row[11], "blob_sha": row[12],
        "extractor": row[13], "extractor_version": row[14], "fingerprint": row[15], "metadata": _json_object(row[16]),
        }
        buckets((str(row[1]), str(row[3]), str(row[12])))[0].append(item)
    for row in connection.execute(
        "SELECT r.region_id,r.repo,r.path,r.line_start,r.line_end,r.blob_sha,r.kind,r.fingerprint,r.metadata_json "
        "FROM generation_regions g JOIN atlas_regions r ON r.region_id=g.region_id WHERE g.generation=?",
        (generation,),
    ):
        region_count += 1
        if region_count > MAX_ATLAS_REGIONS:
            raise AtlasCapacityError("Atlas reused region budget exceeded")
        item = {
        "region_id": row[0], "repo": row[1], "path": row[2], "line_start": row[3], "line_end": row[4],
        "blob_sha": row[5], "kind": row[6], "fingerprint": row[7], "metadata": _json_object(row[8]),
        }
        buckets((str(row[1]), str(row[2]), str(row[5])))[1].append(item)
    for row in connection.execute(
        "SELECT e.edge_id,e.edge_type,e.source_id,e.target_id,e.repo,e.path,e.line_start,e.line_end,e.blob_sha,"
        "e.extractor,e.extractor_version,e.confidence,e.metadata_json FROM generation_edges g "
        "JOIN atlas_edges e ON e.edge_id=g.edge_id WHERE g.generation=?",
        (generation,),
    ):
        edge_count += 1
        if edge_count > MAX_ATLAS_EDGES:
            raise AtlasCapacityError("Atlas reused edge budget exceeded")
        item = {
        "edge_id": row[0], "edge_type": row[1], "source_id": row[2], "target_id": row[3], "repo": row[4],
        "path": row[5], "line_start": row[6], "line_end": row[7], "blob_sha": row[8], "extractor": row[9],
        "extractor_version": row[10], "confidence": row[11], "metadata": _json_object(row[12]),
        }
        buckets((str(row[4]), str(row[5]), str(row[8])))[2].append(item)
    return reused


def build_atlas(settings: Settings, state: dict[str, object]) -> dict[str, Any]:
    """Build normalized generation payload with blob-level incremental reuse."""
    snapshots = {name: str(raw.get("sha") or "working-tree") for name, raw in state.items() if isinstance(raw, dict)}
    if len(snapshots) > MAX_ATLAS_REPOSITORIES:
        raise AtlasCapacityError("Atlas repository budget exceeded")
    connection = connect(settings)
    try:
        current = connection.execute("SELECT value FROM metadata WHERE key='current_generation'").fetchone()
        parent_generation = int(current[0]) if current else None
        parent_schema = None
        if parent_generation is not None:
            row = connection.execute(
                "SELECT schema_version,status FROM generation_components "
                "WHERE generation=? AND component='hierarchy'",
                (parent_generation,),
            ).fetchone()
            if row and str(row[1]) == "ready":
                parent_schema = str(row[0])
        parent_snapshots = _parent_snapshots(connection, parent_generation)
        previous_changes = (
            _generation_changes(connection, parent_generation)
            if parent_schema == ATLAS_SCHEMA_VERSION else []
        )
        previous_files: dict[tuple[str, str], str] = {}
        if parent_generation is not None:
            for repo, path, blob in connection.execute(
                "SELECT e.repo,e.path,e.blob_sha FROM generation_entities g "
                "JOIN atlas_entities e ON e.entity_id=g.entity_id "
                "WHERE g.generation=? AND e.kind='file'",
                (parent_generation,),
            ):
                if len(previous_files) >= MAX_ATLAS_FILES:
                    raise AtlasCapacityError("Atlas previous-generation file budget exceeded")
                previous_files[(str(repo), str(path))] = str(blob)
        current_files: dict[tuple[str, str], str] = {}
        for repo, sha in snapshots.items():
            count = connection.execute(
                "SELECT COUNT(*) FROM snapshot_files WHERE repo=? AND sha=?", (repo, sha),
            ).fetchone()
            if count is None or len(current_files) + int(count[0]) > MAX_ATLAS_FILES:
                raise AtlasCapacityError("Atlas current-generation file budget exceeded")
            for path, blob in connection.execute(
                "SELECT path,blob_sha FROM snapshot_files WHERE repo=? AND sha=? ORDER BY path", (repo, sha)
            ):
                current_files[(repo, str(path))] = str(blob)
    finally:
        connection.close()

    added = sorted(key for key in current_files if key not in previous_files)
    deleted = sorted(key for key in previous_files if key not in current_files)
    modified = sorted(key for key in current_files if key in previous_files and current_files[key] != previous_files[key])
    unchanged = sorted(key for key in current_files if previous_files.get(key) == current_files[key])
    deleted_by_blob: dict[tuple[str, str], list[str]] = {}
    for repo, path in deleted:
        deleted_by_blob.setdefault((repo, previous_files[(repo, path)]), []).append(path)
    renamed: list[dict[str, str]] = []
    for repo, path in list(added):
        old = deleted_by_blob.get((repo, current_files[(repo, path)]), [])
        if old:
            renamed.append({"repo": repo, "old_path": old.pop(0), "path": path})

    modules: dict[str, dict[str, Any]] = {}
    entities: dict[str, dict[str, Any]] = {}
    regions: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}
    reused: dict[tuple[str, str], tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]] = {}
    if parent_generation is not None and parent_schema == ATLAS_SCHEMA_VERSION and unchanged:
        connection = connect(settings)
        try:
            parent_rows = _reused_generation_intelligence(connection, parent_generation)
            for repo_name, path in unchanged:
                key = (repo_name, path, current_files[(repo_name, path)])
                if key in parent_rows:
                    reused[(repo_name, path)] = parent_rows[key]
        finally:
            connection.close()
    from .investigation import (
        MAX_REFRESH_CONTENT_CACHE_BYTES,
        MAX_REFRESH_FILE_BYTES,
        build_generation_intelligence,
    )
    from .index import indexed_snapshot_contents

    parse_files = {
        (repo_name, path): blob
        for (repo_name, path), blob in current_files.items()
        if PurePosixPath(path).suffix.lower() in _CODE_SUFFIXES and (repo_name, path) not in reused
    }
    parsed_files = 0
    v1_source_contents: dict[tuple[str, str], tuple[str, bool]] = {}
    v1_source_cache_bytes = 0
    for (repo_name, path), blob in sorted(current_files.items()):
        if PurePosixPath(path).suffix.lower() not in _CODE_SUFFIXES:
            continue
        reused_rows = reused.get((repo_name, path))
        if reused_rows and reused_rows[0]:
            module = _module(repo_name, path)
            modules[module["module_id"]] = module
            entities.update({item["entity_id"]: item for item in reused_rows[0]})
            regions.update({item["region_id"]: item for item in reused_rows[1]})
            edges.update({item["edge_id"]: item for item in reused_rows[2]})
            if (
                len(entities) > MAX_ATLAS_ENTITIES
                or len(regions) > MAX_ATLAS_REGIONS
                or len(edges) > MAX_ATLAS_EDGES
            ):
                raise AtlasCapacityError("Atlas reused derived-row budget exceeded")
    for (repo_name, path), content in indexed_snapshot_contents(settings, parse_files, snapshots):
        blob = parse_files[(repo_name, path)]
        raw = content.encode("utf-8")
        if (
            PurePosixPath(path).suffix.lower() in {".java", ".kt", ".kts", ".groovy", ".properties", ".yaml", ".yml", ".toml", ".xml"}
            and v1_source_cache_bytes < MAX_REFRESH_CONTENT_CACHE_BYTES
        ):
            bounded = raw[:MAX_REFRESH_FILE_BYTES]
            if v1_source_cache_bytes + len(bounded) <= MAX_REFRESH_CONTENT_CACHE_BYTES:
                v1_source_contents[(repo_name, path)] = (
                    bounded.decode("utf-8", errors="replace"), len(raw) > MAX_REFRESH_FILE_BYTES,
                )
                v1_source_cache_bytes += len(bounded)
        module, file_entities, file_regions, file_edges = _file_intelligence(repo_name, path, blob, content)
        modules[module["module_id"]] = module
        entities.update({item["entity_id"]: item for item in file_entities})
        regions.update({item["region_id"]: item for item in file_regions})
        edges.update({item["edge_id"]: item for item in file_edges})
        if (
            len(entities) > MAX_ATLAS_ENTITIES
            or len(regions) > MAX_ATLAS_REGIONS
            or len(edges) > MAX_ATLAS_EDGES
        ):
            raise AtlasCapacityError("Atlas generation derived-row budget exceeded")
        parsed_files += 1

    cards: dict[str, dict[str, Any]] = {}
    by_module: dict[str, list[dict[str, Any]]] = {}
    by_repo: dict[str, list[dict[str, Any]]] = {}
    for entity in entities.values():
        by_module.setdefault(entity["module_id"], []).append(entity)
        by_repo.setdefault(entity["repo"], []).append(entity)
        if entity["kind"] != "file":
            content = (
                f"Entity {entity['qualified_name']}\nKind: {entity['kind']}\nLanguage: {entity['language']}\n"
                f"Location: {entity['repo']}:{entity['path']}:{entity['line_start']}-{entity['line_end']}\n"
                f"Signature: {entity['signature']}"
            )
            item = _card("entity", entity["entity_id"], entity["repo"], content,
                         module_id=entity["module_id"], entity_id=entity["entity_id"], path=entity["path"],
                         metadata={"line_start": entity["line_start"], "line_end": entity["line_end"], "kind": entity["kind"]})
            cards[item["card_id"]] = item
    for module_id, module in modules.items():
        values = by_module.get(module_id, [])
        names = sorted({f"{item['kind']}:{item['simple_name']}" for item in values if item["kind"] != "file"})[:100]
        paths = sorted({item["path"] for item in values})[:100]
        content = f"Module {module['repo']}:{module['path']}\nLanguage: {module['language']}\nFiles: {', '.join(paths)}\nEntities: {', '.join(names)}"
        item = _card("module", module_id, module["repo"], content, module_id=module_id, path=module["path"])
        cards[item["card_id"]] = item
    for repo in settings.repositories:
        if repo.name not in snapshots:
            continue
        values = by_repo.get(repo.name, [])
        module_names = sorted({modules[item["module_id"]]["path"] for item in values if item["module_id"] in modules})[:100]
        kinds: dict[str, int] = {}
        for item in values:
            kinds[item["kind"]] = kinds.get(item["kind"], 0) + 1
        content = (
            f"Repository {repo.name}\nDescription: {repo.description}\nTags: {', '.join(repo.tags)}\n"
            f"Modules: {', '.join(module_names)}\nEntity counts: {json.dumps(kinds, sort_keys=True)}"
        )
        item = _card("repo", _hash("repo", repo.name), repo.name, content)
        cards[item["card_id"]] = item

    changes, change_build = _change_rows(settings, snapshots, parent_snapshots, previous_changes)
    named: dict[str, list[dict[str, Any]]] = {}
    for entity in entities.values():
        named.setdefault(entity["simple_name"].lower(), []).append(entity)
    current_entity_ids = set(entities)
    for edge_id, item in list(edges.items()):
        metadata = item.get("metadata") or {}
        was_resolved = bool(metadata.get("resolved"))
        if was_resolved and item.get("target_id") in current_entity_ids:
            continue
        if was_resolved:
            edges.pop(edge_id, None)
        # Per-file Java extraction has the class ownership needed for safe
        # dispatch. A repo-wide simple-name pass must never override it.
        if item.get("edge_type") == "CALLS" and metadata.get("language") == "java":
            continue
        target_name = str(metadata.get("target_name") or "").rsplit(".", 1)[-1].lower()
        targets = named.get(target_name) or []
        local_targets = sorted(
            (
                target for target in targets
                if target["repo"] == item["repo"] and target["entity_id"] != item["source_id"]
            ),
            key=lambda target: (target["path"], target["line_start"], target["entity_id"]),
        )
        other_targets = sorted(
            (target for target in targets if target["entity_id"] != item["source_id"]),
            key=lambda target: (target["repo"], target["path"], target["line_start"], target["entity_id"]),
        )
        preferred = (
            local_targets[0] if len(local_targets) == 1
            else other_targets[0] if not local_targets and len(other_targets) == 1
            else None
        )
        if preferred is not None and preferred["entity_id"] != item["source_id"]:
            resolved = _edge(item["edge_type"], item["source_id"], preferred["entity_id"], repo=item["repo"],
                             path=item["path"], line=item["line_start"], blob=item["blob_sha"],
                             confidence=float(item["confidence"]) if was_resolved else min(.95, float(item["confidence"]) + .15),
                             metadata={**metadata, "resolved": True})
            edges[resolved["edge_id"]] = resolved
    for item in _integration_edges(settings, entities.values(), snapshots):
        edges[item["edge_id"]] = item
    file_entities = {(item["repo"], item["path"]): item for item in entities.values() if item["kind"] == "file"}
    by_commit: dict[tuple[str, str], list[str]] = {}
    for item in changes:
        if item["status"] != "deleted":
            by_commit.setdefault((item["repo"], item["commit_sha"]), []).append(item["path"])
    cochange_count = 0
    for (repo, commit), paths in sorted(by_commit.items()):
        values = [file_entities[(repo, path)] for path in sorted(set(paths))[:20] if (repo, path) in file_entities]
        for position, source in enumerate(values):
            for target in values[position + 1:]:
                if cochange_count >= 2_000:
                    break
                item = _edge("CO_CHANGED_WITH", source["entity_id"], target["entity_id"], repo=repo,
                             path=source["path"], line=1, blob=source["blob_sha"], confidence=.75,
                             metadata={"commit": commit})
                edges[item["edge_id"]] = item
                cochange_count += 1
    v1_intelligence = build_generation_intelligence(
        settings,
        current_files=current_files,
        unchanged=unchanged,
        parent_generation=parent_generation,
        modules=modules.values(),
        entities=entities.values(),
        source_contents=v1_source_contents,
        snapshots=snapshots,
    )
    reused_entities = sum(len(rows[0]) for rows in reused.values())
    reused_edges = sum(len(rows[2]) for rows in reused.values())
    previous_cards: set[str] = set()
    if parent_generation is not None:
        connection = connect(settings)
        try:
            previous_cards = {
                str(row[0]) for row in connection.execute(
                    "SELECT card_id FROM generation_cards WHERE generation=?", (parent_generation,)
                )
            }
        finally:
            connection.close()
    reused_cards = len(previous_cards & set(cards))
    changed_files = set(added) | set(modified) | set(deleted)
    delta = {
        "added": [{"repo": repo, "path": path} for repo, path in added],
        "modified": [{"repo": repo, "path": path} for repo, path in modified],
        "deleted": [{"repo": repo, "path": path} for repo, path in deleted],
        "renamed": renamed,
        "repos_changed": len({repo for repo, _ in changed_files}),
        "files_changed": len(changed_files),
        "unchanged_files": len(unchanged), "parsed_files": parsed_files,
        "reused_files": len(unchanged), "entities": len(entities), "edges": len(edges), "cards": len(cards),
        "entities_reused": reused_entities, "entities_rebuilt": max(0, len(entities) - reused_entities),
        "graph_edges_reused": reused_edges, "graph_edges_rebuilt": max(0, len(edges) - reused_edges),
        "cards_reused": reused_cards, "cards_rebuilt": max(0, len(cards) - reused_cards),
        "runtime_anchors": len(v1_intelligence["runtime_anchors"]),
        "integration_facts": len(v1_intelligence["integration_facts"]),
        "v1_files_parsed": int(v1_intelligence["v1_build"]["parsed_files"]),
        "v1_files_reused": int(v1_intelligence["v1_build"]["reused_files"]),
        "change_intelligence_build": change_build,
    }
    return {
        "modules": list(modules.values()), "entities": list(entities.values()), "regions": list(regions.values()),
        "edges": list(edges.values()), "cards": list(cards.values()), "changes": changes, "delta": delta,
        **v1_intelligence,
    }


def atlas_components(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    definitions = (
        ("hierarchy", "modules", "entities", "regions"),
        ("typed_graph", "edges"),
        ("change_intelligence", "changes"),
        ("semantic_cards", "cards"),
    )
    result: dict[str, dict[str, Any]] = {}
    identifiers = {
        "modules": "module_id", "entities": "entity_id", "regions": "region_id",
        "edges": "edge_id", "changes": "change_id", "cards": "card_id",
    }
    for row in definitions:
        name, *collections = row
        logical = {
            collection: sorted(
                payload.get(collection) or [], key=lambda item: str(item.get(identifiers[collection]) or "")
            )
            for collection in collections
        }
        count = sum(len(value) for value in logical.values())
        status = "ready"
        details: dict[str, Any] = {"count": count}
        if name == "change_intelligence":
            delta = payload.get("delta") if isinstance(payload.get("delta"), dict) else {}
            build = delta.get("change_intelligence_build") if isinstance(delta, dict) else {}
            build = build if isinstance(build, dict) else {}
            incomplete = {
                key: int(build.get(key) or 0)
                for key in ("git_failures", "oversized_paths", "row_limit_reached", "budget_exhausted_repos")
            }
            if any(incomplete.values()):
                status = "degraded"
                details["reason"] = "change intelligence refresh was incomplete"
            details["build"] = {**build, **incomplete}
        result[name] = {
            "schema_version": ATLAS_SCHEMA_VERSION, "status": status, "content_hash": _content_hash(logical),
            "details": details,
        }
    from .investigation import generation_component_manifests

    result.update(generation_component_manifests(payload))
    return result


def _tokens(value: str) -> set[str]:
    tokens: set[str] = set()
    for token in re.findall(r"[A-Za-z_$][A-Za-z0-9_$.-]{2,}", value):
        tokens.add(token.lower())
        parts = re.split(r"[$_.-]+|(?<=[a-z0-9])(?=[A-Z])", token)
        tokens.update(part.lower() for part in parts if len(part) >= 3)
    return tokens


def _prioritized_routing_terms(objective: str, request: dict[str, Any]) -> set[str]:
    """Keep every bounded typed anchor's primary token before derived query terms."""
    anchors = [
        str(item.get("value") or "")
        for item in request.get("anchors") or [] if isinstance(item, dict) and item.get("value")
    ]
    ordered: list[str] = []
    seen: set[str] = set()

    def add(value: str, *, primary_only: bool = False) -> None:
        values = sorted(_tokens(value), key=lambda token: (-len(token), token))
        if primary_only:
            values = values[:1]
        for token in values:
            if token not in seen and len(ordered) < MAX_ROUTING_QUERY_TERMS:
                seen.add(token)
                ordered.append(token)

    for value in anchors:
        add(value, primary_only=True)
    values: list[str] = [
        *anchors,
        *(str(value) for value in request.get("resolve") or []),
        *(str(item.get("query") or item.get("name") or "") for section in ("searches", "paths", "symbols", "history")
          for item in request.get(section) or [] if isinstance(item, dict)),
        *(str(value) for key in ("runtime_facts", "hypotheses", "required") for value in request.get(key) or []),
        objective,
    ]
    for value in values:
        add(value)
        if len(ordered) >= MAX_ROUTING_QUERY_TERMS:
            break
    return set(ordered)


def _cache_key(
    objective: str,
    request: dict[str, Any],
    edition: str,
    *,
    repo_limit: int,
    entity_limit: int,
    prefetch_entity_ids: Iterable[str] = (),
) -> str:
    logical = {
        "schema": ROUTER_SCHEMA_VERSION, "objective": objective,
        "request": {key: request.get(key) or [] for key in (
            "searches", "paths", "symbols", "history", "required", "resolve",
            "runtime_facts", "hypotheses",
        )},
        "hints": request.get("hints") or {},
        "prior_entity_ids": list(dict.fromkeys(str(value) for value in request.get("_prior_entity_ids") or []))[:200],
        "prefetch_entity_ids": sorted(set(str(value) for value in prefetch_entity_ids))[:200],
        "edition": edition,
        "options": {"repo_limit": repo_limit, "entity_limit": entity_limit},
        "evaluation_ablation": sorted(str(value) for value in request.get("_evaluation_ablation") or []),
    }
    return _hash(json.dumps(logical, sort_keys=True, separators=(",", ":"), default=str))


def _route_cache_identity(value: dict[str, Any]) -> str:
    payload = {
        key: item for key, item in value.items()
        if key not in {"cache_identity", "cache_hit", "cache_validation_db_operations"}
    }
    return _hash(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str))


def _route_cache_registration_identity(generation: AtlasGenerationRef) -> str:
    return _hash(
        "route-cache-registration-v1", ROUTER_SCHEMA_VERSION, generation.identity,
        *(
            str(generation.component(name).get("content_hash") or "")
            for name in ("hierarchy", "typed_graph", "change_intelligence")
        ),
    )


_ROUTE_CACHE_SEALS: dict[tuple[str, int, str], str] = {}


def _route_cache_secret(settings: Settings, *, create: bool) -> bytes | None:
    path = settings.state_dir / "route-cache.key"
    try:
        secret = read_managed_bytes(settings.state_dir, path, max_bytes=32)
        return secret if len(secret) == 32 else None
    except (OSError, ValueError):
        if not create:
            return None
    atomic_managed_bytes_write(settings.state_dir, path, secrets.token_bytes(32))
    try:
        secret = read_managed_bytes(settings.state_dir, path, max_bytes=32)
        return secret if len(secret) == 32 else None
    except (OSError, ValueError):
        return None


def _route_cache_seal(
    settings: Settings, generation: int, cache_key: str, payload_hash: str,
    compatibility_identity: str, *, create: bool,
) -> str | None:
    secret = _route_cache_secret(settings, create=create)
    if secret is None:
        return None
    message = "\0".join((
        ROUTER_SCHEMA_VERSION, str(generation), cache_key, payload_hash, compatibility_identity,
    )).encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def _valid_cached_route(connection: sqlite3.Connection, generation: int, value: Any) -> tuple[bool, int]:
    operations = 0
    if not isinstance(value, dict) or value.get("schema") != ROUTER_SCHEMA_VERSION:
        return False, operations
    if value.get("cache_identity") != _route_cache_identity(value):
        return False, operations
    try:
        cached_generation = int(value.get("generation") or -1)
    except (TypeError, ValueError):
        return False, operations
    if cached_generation != generation or value.get("candidates") != value.get("entities"):
        return False, operations

    repos = [str(repo) for repo in value.get("repos") or []]
    if repos:
        slots = ",".join("?" for _ in repos)
        present = {str(row[0]) for row in connection.execute(
            f"SELECT repo FROM generation_snapshots WHERE generation=? AND repo IN ({slots})",
            (generation, *repos),
        )}
        operations += 1
        if present != set(repos):
            return False, operations
    modules = [str(module) for module in value.get("modules") or []]
    if modules:
        slots = ",".join("?" for _ in modules)
        present = {str(row[0]) for row in connection.execute(
            f"SELECT module_id FROM generation_modules WHERE generation=? AND module_id IN ({slots})",
            (generation, *modules),
        )}
        operations += 1
        if present != set(modules):
            return False, operations

    entity_items = value.get("entities") or []
    if not all(isinstance(item, dict) and item.get("entity_id") for item in entity_items):
        return False, operations
    entity_ids = list(dict.fromkeys(str(item["entity_id"]) for item in entity_items))
    entity_rows = _valid_generation_entities(connection, generation, entity_ids)
    if entity_ids:
        operations += 3
    for item in entity_items:
        row = entity_rows.get(str(item["entity_id"]))
        try:
            line_matches = int(item.get("line") or 0) == int(row["line_start"]) if row else False
            score = float(item.get("score"))
            score_matches = math.isfinite(score)
        except (TypeError, ValueError):
            line_matches = False
            score_matches = False
        if not row or (
            str(item.get("repo")) != str(row["repo"])
            or str(item.get("module_id") or "") != str(row["module_id"] or "")
            or str(item.get("path")) != str(row["path"])
            or not line_matches
            or not score_matches
            or str(item.get("kind")) != str(row["kind"])
            or str(item.get("text") or "") != _entity_routing_text(row)
            or not isinstance(item.get("found_by"), list)
            or not all(isinstance(value, str) for value in item.get("found_by") or [])
        ):
            return False, operations
    graph_items = value.get("graph_edges") or []
    if not all(isinstance(item, dict) and item.get("edge_id") and item.get("source_id") for item in graph_items):
        return False, operations
    graph_ids = [str(item["edge_id"]) for item in graph_items]
    graph_rows = _valid_generation_edges(connection, generation, graph_ids)
    graph_entities = _valid_generation_entities(
        connection, generation,
        (str(value) for row in graph_rows.values() for value in (row["source_id"], row["target_id"])),
    )
    if graph_ids:
        operations += 8
    for item in graph_items:
        row = graph_rows.get(str(item.get("edge_id")))
        source = graph_entities.get(str(row["source_id"])) if row else None
        target = graph_entities.get(str(row["target_id"])) if row else None
        try:
            graph_values_match = (
                int(item.get("line") or 0) == int(target["line_start"])
                and float(item.get("confidence") or 0) == float(row["confidence"])
            ) if isinstance(item, dict) and row and target else False
        except (TypeError, ValueError):
            graph_values_match = False
        if not row or not source or not target or (
            str(item.get("source_id")) != str(row["source_id"])
            or str(item.get("target_id")) != str(row["target_id"])
            or str(item.get("edge_type")) != str(row["edge_type"])
            or str(item.get("source_repo")) != str(source["repo"])
            or str(item.get("repo")) != str(target["repo"])
            or str(item.get("path")) != str(target["path"])
            or not graph_values_match
        ):
            return False, operations
    return True, operations


def _entity_routing_text(entity: dict[str, Any]) -> str:
    """Bounded generation-validated identity text for candidate ranking."""
    return " ".join(dict.fromkeys(
        str(entity.get(key) or "").strip()
        for key in ("qualified_name", "simple_name", "signature")
        if str(entity.get(key) or "").strip()
    ))[:1_200]


def _valid_term_index(
    connection: sqlite3.Connection,
    generation: int,
    *,
    marker_table: str,
    membership_table: str,
    term_table: str,
    id_column: str,
    schema_version: str,
) -> bool:
    count_column = {
        "card_id": "card_count", "change_id": "change_count", "anchor_id": "anchor_count",
    }.get(id_column)
    if count_column is None:
        return False
    marker = connection.execute(
        f"SELECT {count_column},term_count,projection_hash "
        f"FROM {marker_table} WHERE generation=? AND schema_version=?",
        (generation, schema_version),
    ).fetchone()
    if not marker:
        return False
    # Schema-v10 DML triggers delete this publication marker on every source,
    # membership, or term mutation. Serving therefore validates a constant-size
    # authoritative seal; the full projection hash is computed at publication
    # and by explicit integrity checks, never on each route or wave.
    triggers = (
        RUNTIME_ANCHOR_INDEX_INVALIDATION_TRIGGERS
        if id_column == "anchor_id" else TERM_INDEX_INVALIDATION_TRIGGERS
    )
    placeholders = ",".join("?" for _ in triggers)
    trigger_count = connection.execute(
        f"SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name IN ({placeholders})",
        triggers,
    ).fetchone()
    return bool(
        trigger_count and int(trigger_count[0]) == len(triggers)
        and int(marker[0]) >= 0 and int(marker[1]) >= 0
        and re.fullmatch(r"sha256:[0-9a-f]{64}", str(marker[2]) or "")
    )


def _overlay_prefetch_candidates(
    connection: sqlite3.Connection,
    generation: int,
    value: dict[str, Any],
    prefetch_ids: set[str],
    entity_limit: int,
) -> dict[str, Any]:
    """Overlay ticket-local low-authority hints without contaminating the shared route cache."""
    result = dict(value)
    entities = [dict(item) for item in value.get("entities") or [] if isinstance(item, dict)]
    present = {str(item.get("entity_id")) for item in entities}
    missing = sorted(prefetch_ids - present)[:200]
    if missing:
        for entity_id, entity in _valid_generation_entities(connection, generation, missing).items():
            entities.append({
                "entity_id": str(entity_id), "repo": str(entity["repo"]),
                "module_id": str(entity["module_id"] or ""), "path": str(entity["path"]),
                "line": int(entity["line_start"]), "kind": str(entity["kind"]), "score": 1.0,
                "text": _entity_routing_text(entity),
                "found_by": ["generation-validated ticket prefetch"],
            })
    entities = sorted(
        entities,
        key=lambda item: (-float(item.get("score") or 0), str(item.get("repo")), str(item.get("path")), int(item.get("line") or 0)),
    )[:entity_limit]
    returned = {str(item.get("entity_id")) for item in entities}
    result["entities"] = entities
    result["candidates"] = entities
    result["prefetch_reused"] = len(returned & prefetch_ids)
    return result


def route(
    settings: Settings,
    objective: str,
    request: dict[str, Any],
    generation: AtlasGenerationRef | None,
    *,
    repo_limit: int = 16,
    entity_limit: int = 80,
    _verify_registered_cache: bool = True,
) -> dict[str, Any]:
    """Route through generation cards/entities/graph; never return source content."""
    if (
        generation is None
        or generation.component("hierarchy").get("status") != "ready"
        or generation.component("hierarchy").get("schema_version") != ATLAS_SCHEMA_VERSION
        or generation.component("typed_graph").get("status") != "ready"
        or generation.component("typed_graph").get("schema_version") != ATLAS_SCHEMA_VERSION
    ):
        return {"schema": ROUTER_SCHEMA_VERSION, "repos": [], "modules": [], "entities": [], "candidates": [], "cache_hit": False}
    ablation = {str(value) for value in request.get("_evaluation_ablation") or []}
    if "flat" in ablation:
        return {
            "schema": ROUTER_SCHEMA_VERSION, "repos": sorted(generation.snapshots), "modules": [], "entities": [],
            "candidates": [], "graph_edges": [], "cache_hit": False, "prefetch_reused": 0,
            "investigation_reused": 0, "evaluation_ablation": sorted(ablation),
        }
    try:
        from .editions import current_edition
        edition = current_edition(settings)
    except OSError:
        edition = "core"
    connection = connect(settings)
    now = datetime.now(UTC).isoformat()
    try:
        prefetch_ids: set[str] = set()
        if "prefetch" not in ablation:
            from .investigation import _valid_prefetch_envelope

            prefetch = request.get("_prefetch") or {}
            if _valid_prefetch_envelope(generation, prefetch, objective):
                requested_prefetch = list(dict.fromkeys(
                    str(value) for value in prefetch.get("candidate_ids") or [] if value
                ))[:200]
                if requested_prefetch:
                    placeholders = ",".join("?" for _ in requested_prefetch)
                    prefetch_ids = {
                        str(row[0]) for row in connection.execute(
                            f"SELECT entity_id FROM generation_entities WHERE generation=? "
                            f"AND entity_id IN ({placeholders})",
                            (generation.generation, *requested_prefetch),
                        )
                    }
        key = _cache_key(
            objective,
            request,
            edition,
            repo_limit=repo_limit,
            entity_limit=entity_limit,
        )
        seal_key = (str(settings.state_dir.resolve()), generation.generation, key)
        cached = None if "generation_cache" in ablation or not _verify_registered_cache else connection.execute(
            "SELECT c.payload_json,c.payload_hash,r.payload_hash,r.schema_version,r.compatibility_identity,r.seal "
            "FROM atlas_retrieval_cache c LEFT JOIN atlas_retrieval_cache_registrations r "
            "ON r.generation=c.generation AND r.cache_key=c.cache_key "
            "WHERE c.generation=? AND c.cache_key=?",
            (generation.generation, key),
        ).fetchone()
        cache_was_valid = False
        cache_validation_operations = 0
        if cached:
            try:
                value = json.loads(cached[0])
            except (TypeError, json.JSONDecodeError):
                value = None
            try:
                cached_generation = int(value.get("generation") or -1) if isinstance(value, dict) else -1
            except (TypeError, ValueError):
                cached_generation = -1
            row_hash_valid = cached[1] == _hash(
                "route-cache-row", generation.generation, key, str(cached[0]),
            )
            compatibility_identity = _route_cache_registration_identity(generation)
            expected_seal = _route_cache_seal(
                settings, generation.generation, key, str(cached[1] or ""), compatibility_identity,
                create=False,
            )
            registered = (
                row_hash_valid
                and cached[2] == cached[1]
                and cached[3] == ROUTER_SCHEMA_VERSION
                and cached[4] == compatibility_identity
                and isinstance(cached[5], str)
                and expected_seal is not None
                and hmac.compare_digest(cached[5], expected_seal)
                and isinstance(value, dict)
                and value.get("cache_identity") == _route_cache_identity(value)
                and value.get("schema") == ROUTER_SCHEMA_VERSION
                and cached_generation == generation.generation
                and value.get("candidates") == value.get("entities")
            )
            if registered:
                cache_rows_valid, cache_validation_operations = _valid_cached_route(
                    connection, generation.generation, value,
                )
                registered = cache_rows_valid
            if registered:
                connection.execute(
                    "UPDATE atlas_retrieval_cache SET last_used_at=? WHERE generation=? AND cache_key=?",
                    (now, generation.generation, key),
                )
                connection.commit()
                value["cache_hit"] = True
                value["cache_validation_db_operations"] = 1 + cache_validation_operations
                return _overlay_prefetch_candidates(
                    connection, generation.generation, value, prefetch_ids, entity_limit
                )
            else:
                _ROUTE_CACHE_SEALS.pop(seal_key, None)
                connection.execute("DELETE FROM atlas_retrieval_cache WHERE generation=? AND cache_key=?", (generation.generation, key))
                connection.execute(
                    "DELETE FROM atlas_retrieval_cache_registrations WHERE generation=? AND cache_key=?",
                    (generation.generation, key),
                )

        terms = _prioritized_routing_terms(objective, request)
        explicit_repos = {
            str(repo) for section in ("searches", "paths", "symbols", "history")
            for item in request.get(section) or [] if isinstance(item, dict) for repo in item.get("repos") or []
        }
        explicit_repos = set(sorted(explicit_repos & set(generation.snapshots))[:MAX_ROUTING_EXPLICIT_REPOS])
        indexed = _valid_term_index(
            connection,
            generation.generation,
            marker_table="generation_card_indexes",
            membership_table="generation_cards",
            term_table="atlas_card_terms",
            id_column="card_id",
            schema_version=ATLAS_CARD_TERM_SCHEMA_VERSION,
        )
        if indexed and terms:
            term_values = sorted(terms)
            term_slots = ",".join("?" for _ in term_values)
            repo_clause = ""
            parameters: list[Any] = [ATLAS_CARD_TERM_SCHEMA_VERSION, *term_values, generation.generation]
            if explicit_repos:
                repo_slots = ",".join("?" for _ in explicit_repos)
                repo_clause = f" OR c.repo IN ({repo_slots})"
                parameters.extend(sorted(explicit_repos))
            parameters.append(MAX_ROUTING_CARD_CANDIDATES)
            rows = connection.execute(
                "SELECT c.level,c.target_id,c.repo,c.module_id,e.entity_id,c.path,'' AS content,"
                "e.simple_name,e.qualified_name,e.kind,e.line_start,COUNT(DISTINCT t.term) AS overlap "
                "FROM generation_cards g JOIN atlas_cards c ON c.card_id=g.card_id "
                "LEFT JOIN generation_entities ge ON ge.generation=g.generation AND ge.entity_id=c.entity_id "
                "LEFT JOIN atlas_entities e ON e.entity_id=ge.entity_id "
                f"LEFT JOIN atlas_card_terms t ON t.card_id=c.card_id AND t.schema_version=? AND t.term IN ({term_slots}) "
                f"WHERE g.generation=? AND (t.term IS NOT NULL{repo_clause}) "
                "GROUP BY c.card_id ORDER BY overlap DESC,c.card_id LIMIT ?",
                parameters,
            ).fetchall()
            routing_index = "precomputed"
        elif indexed and explicit_repos:
            repo_slots = ",".join("?" for _ in explicit_repos)
            rows = connection.execute(
                "SELECT c.level,c.target_id,c.repo,c.module_id,e.entity_id,c.path,'' AS content,"
                "e.simple_name,e.qualified_name,e.kind,e.line_start,0 AS overlap "
                "FROM generation_cards g JOIN atlas_cards c ON c.card_id=g.card_id "
                "LEFT JOIN generation_entities ge ON ge.generation=g.generation AND ge.entity_id=c.entity_id "
                "LEFT JOIN atlas_entities e ON e.entity_id=ge.entity_id "
                f"WHERE g.generation=? AND c.repo IN ({repo_slots}) ORDER BY c.card_id LIMIT ?",
                (generation.generation, *sorted(explicit_repos), MAX_ROUTING_CARD_CANDIDATES),
            ).fetchall()
            routing_index = "precomputed"
        elif indexed:
            rows = []
            routing_index = "precomputed"
        else:
            # Current Atlas generations publish a sealed term projection.  A
            # missing/invalid marker is component corruption, not permission
            # to rescan and re-tokenise an entire generation at query time.
            rows = []
            routing_index = "unavailable"
        scored: list[dict[str, Any]] = []
        repo_scores: dict[str, float] = {}
        module_scores: dict[str, float] = {}
        for level, target_id, repo, module_id, entity_id, path, content, simple, qualified, kind, line, overlap in rows:
            exact = sum(1 for term in terms if term in {str(simple or "").lower(), str(qualified or "").lower()})
            score = overlap * 8 + exact * 80 + (500 if repo in explicit_repos else 0)
            if score <= 0 and terms:
                continue
            repo_scores[str(repo)] = repo_scores.get(str(repo), 0) + score + (1 if level == "repo" else 0)
            if module_id:
                module_scores[str(module_id)] = module_scores.get(str(module_id), 0) + score
            if entity_id and path:
                scored.append({
                    "entity_id": str(entity_id), "repo": str(repo), "module_id": str(module_id or ""),
                    "path": str(path), "line": int(line or 1), "kind": str(kind or "entity"), "score": float(score),
                    "found_by": ["Atlas hierarchical router"],
                })
        change_component = generation.component("change_intelligence")
        change_ready = (
            change_component.get("status") == "ready"
            and change_component.get("schema_version") == ATLAS_SCHEMA_VERSION
        )
        change_indexed = change_ready and _valid_term_index(
            connection, generation.generation,
            marker_table="generation_change_indexes", membership_table="generation_changes",
            term_table="atlas_change_terms", id_column="change_id",
            schema_version=ATLAS_CHANGE_TERM_SCHEMA_VERSION,
        )
        change_parameters: list[Any] = [generation.generation]
        if not change_ready:
            change_sql = "SELECT NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,0 WHERE 0"
            change_parameters = []
            change_routing_index = "unavailable"
        elif change_indexed and terms:
            change_slots = ",".join("?" for _ in terms)
            change_sql = (
                "SELECT c.repo,c.path,c.ticket,c.metadata_json,e.entity_id,e.module_id,e.line_start,e.kind,"
                "COUNT(DISTINCT t.term) FROM generation_changes g JOIN atlas_changes c ON c.change_id=g.change_id "
                "JOIN atlas_change_terms t ON t.change_id=c.change_id "
                "AND t.schema_version=? AND t.term IN (" + change_slots + ") "
                "LEFT JOIN (SELECT e.entity_id,e.repo,e.path,e.module_id,e.line_start,e.kind "
                "FROM generation_entities ge JOIN atlas_entities e ON e.entity_id=ge.entity_id "
                "WHERE ge.generation=? AND e.kind='file') e ON e.repo=c.repo AND e.path=c.path "
                "WHERE g.generation=? GROUP BY c.change_id ORDER BY COUNT(DISTINCT t.term) DESC,c.change_id LIMIT 2000"
            )
            change_parameters = [
                ATLAS_CHANGE_TERM_SCHEMA_VERSION, *sorted(terms), generation.generation, generation.generation,
            ]
            change_routing_index = "precomputed"
        elif change_indexed:
            change_sql = "SELECT NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,0 WHERE 0"
            change_parameters = []
            change_routing_index = "precomputed"
        else:
            change_sql = "SELECT NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,0 WHERE 0"
            change_parameters = []
            change_routing_index = "unavailable"
        change_rows = connection.execute(change_sql, change_parameters)
        changes_considered = 0
        for repo, path, ticket, metadata_json, entity_id, module_id, line, kind, indexed_overlap in change_rows:
            changes_considered += 1
            metadata = _json_object(metadata_json)
            overlap = int(indexed_overlap) if change_indexed else len(
                terms & _tokens(f"{ticket or ''} {path} {metadata.get('subject', '')}")
            )
            if not overlap:
                continue
            repo_scores[str(repo)] = repo_scores.get(str(repo), 0) + overlap * 4
            if entity_id:
                scored.append({
                    "entity_id": str(entity_id), "repo": str(repo), "module_id": str(module_id or ""),
                    "path": str(path), "line": int(line or 1), "kind": str(kind or "file"),
                    "score": float(overlap * 6), "found_by": ["Atlas change intelligence"],
                })
        prior_ids = list(dict.fromkeys(
            str(value) for value in (
                () if ablation & {"investigation_memory", "historical_prior"}
                else request.get("_prior_entity_ids") or []
            )
        ))[:200]
        reused_prior_ids: set[str] = set()
        seed_ids = prior_ids
        if seed_ids:
            prior_entities = _valid_generation_entities(connection, generation.generation, seed_ids)
            for entity_id, entity in prior_entities.items():
                if entity_id in prior_ids:
                    reused_prior_ids.add(entity_id)
                scored.append({
                    "entity_id": entity_id, "repo": str(entity["repo"]), "module_id": str(entity["module_id"]),
                    "path": str(entity["path"]), "line": int(entity["line_start"]),
                    "kind": str(entity["kind"]), "score": 25.0,
                    "found_by": ["generation-validated investigation prior"],
                })
        candidate_entities = _valid_generation_entities(
            connection, generation.generation, (str(item["entity_id"]) for item in scored),
        )
        scored = [
            {**item, "text": _entity_routing_text(candidate_entities[str(item["entity_id"])])}
            for item in scored
            if str(item["entity_id"]) in candidate_entities
            and str(item["repo"]) == str(candidate_entities[str(item["entity_id"])]["repo"])
            and str(item["module_id"] or "") == str(candidate_entities[str(item["entity_id"])]["module_id"] or "")
            and str(item["path"]) == str(candidate_entities[str(item["entity_id"])]["path"])
            and int(item["line"]) == int(candidate_entities[str(item["entity_id"])]["line_start"])
            and str(item["kind"]) == str(candidate_entities[str(item["entity_id"])]["kind"])
        ]
        # One-hop graph expansion is a routing signal only.  Provenance stays on
        # the edge and exact source hydration remains mandatory downstream.
        graph_routes: list[dict[str, Any]] = []
        seeds = [item["entity_id"] for item in sorted(scored, key=lambda item: (-item["score"], item["entity_id"]))[:20]]
        if seeds and "graph" not in ablation:
            placeholders = ",".join("?" for _ in seeds)
            graph_rows = connection.execute(
                f"SELECT e.edge_id,e.source_id,e.target_id,e.edge_type,e.confidence,s.repo,t.repo,t.path,t.line_start,t.kind,t.module_id "
                f"FROM generation_edges g JOIN atlas_edges e ON e.edge_id=g.edge_id "
                f"JOIN generation_entities gs ON gs.generation=g.generation AND gs.entity_id=e.source_id "
                f"JOIN generation_entities gt ON gt.generation=g.generation AND gt.entity_id=e.target_id "
                f"JOIN atlas_entities s ON s.entity_id=e.source_id "
                f"JOIN atlas_entities t ON t.entity_id=e.target_id "
                f"WHERE g.generation=? AND e.source_id IN ({placeholders}) "
                f"ORDER BY e.source_id,e.edge_type,e.target_id,e.edge_id LIMIT 200",
                (generation.generation, *seeds),
            ).fetchall()
            seed_scores = {item["entity_id"]: item["score"] for item in scored}
            valid_edges = _valid_generation_edges(
                connection, generation.generation, (str(row[0]) for row in graph_rows),
            )
            valid_graph_entities = _valid_generation_entities(
                connection, generation.generation,
                (str(value) for row in graph_rows for value in (row[1], row[2])),
            )
            for edge_id, source_id, target_id, edge_type, confidence, source_repo, repo, path, line, kind, module_id in graph_rows:
                edge = valid_edges.get(str(edge_id))
                source = valid_graph_entities.get(str(source_id))
                target = valid_graph_entities.get(str(target_id))
                if edge is None or source is None or target is None:
                    continue
                if (
                    str(edge_type) != str(edge["edge_type"]) or float(confidence) != float(edge["confidence"])
                    or str(source_repo) != str(source["repo"]) or str(repo) != str(target["repo"])
                    or str(path) != str(target["path"]) or int(line) != int(target["line_start"])
                    or str(kind) != str(target["kind"]) or str(module_id) != str(target["module_id"])
                ):
                    continue
                graph_routes.append({
                    "edge_id": str(edge_id),
                    "source_id": str(source_id), "target_id": str(target_id), "edge_type": str(edge_type),
                    "confidence": float(confidence), "source_repo": str(source_repo), "repo": str(repo),
                    "path": str(path), "line": int(line),
                })
                scored.append({
                    "entity_id": str(target_id), "repo": str(repo), "module_id": str(module_id), "path": str(path),
                    "line": int(line), "kind": str(kind), "score": seed_scores.get(str(source_id), 0) * .35 + float(confidence) * 10,
                    "text": _entity_routing_text(target),
                    "found_by": [f"Atlas {edge_type} edge"],
                })
        merged: dict[tuple[str, str, int], dict[str, Any]] = {}
        for item in scored:
            candidate_key = (item["repo"], item["path"], item["line"])
            previous = merged.get(candidate_key)
            if previous is None:
                merged[candidate_key] = item
            elif item["score"] > previous["score"]:
                item["found_by"] = sorted(set(previous["found_by"] + item["found_by"]))
                merged[candidate_key] = item
            elif previous:
                previous["found_by"] = sorted(set(previous["found_by"] + item["found_by"]))
        entities = sorted(merged.values(), key=lambda item: (-item["score"], item["repo"], item["path"], item["line"]))[:entity_limit]
        returned_entity_ids = {str(item["entity_id"]) for item in entities}
        ordered_repos = sorted(repo_scores, key=lambda repo: (-repo_scores[repo], repo))
        ordered_repos.extend(repo for repo in sorted(generation.snapshots) if repo not in ordered_repos)
        value = {
            "schema": ROUTER_SCHEMA_VERSION, "generation": generation.generation, "repos": ordered_repos[:repo_limit],
            "modules": [module for module, _ in sorted(module_scores.items(), key=lambda item: (-item[1], item[0]))[:40]],
            "entities": entities, "candidates": entities, "graph_edges": graph_routes, "cache_hit": False,
            "prefetch_reused": 0,
            "investigation_reused": len(returned_entity_ids & reused_prior_ids),
            "evaluation_ablation": sorted(ablation),
            "routing_index": routing_index, "routing_terms": len(terms), "cards_considered": len(rows),
            "change_routing_index": change_routing_index, "changes_considered": changes_considered,
            "cache_validation_db_operations": cache_validation_operations,
        }
        value["cache_identity"] = _route_cache_identity(value)
        if "generation_cache" not in ablation and _verify_registered_cache:
            payload_json = json.dumps(value, sort_keys=True)
            payload_hash = _hash("route-cache-row", generation.generation, key, payload_json)
            connection.execute(
                "INSERT OR REPLACE INTO atlas_retrieval_cache(generation,cache_key,payload_json,payload_hash,created_at,last_used_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    generation.generation, key, payload_json, payload_hash, now, now,
                ),
            )
            connection.execute(
                "INSERT OR REPLACE INTO atlas_retrieval_cache_registrations"
                "(generation,cache_key,schema_version,compatibility_identity,payload_hash,seal,registered_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    generation.generation, key, ROUTER_SCHEMA_VERSION,
                    _route_cache_registration_identity(generation), payload_hash,
                    _route_cache_seal(
                        settings, generation.generation, key, payload_hash,
                        _route_cache_registration_identity(generation), create=True,
                    ), now,
                ),
            )
            connection.execute(
                "DELETE FROM atlas_retrieval_cache WHERE rowid IN (SELECT rowid FROM atlas_retrieval_cache ORDER BY last_used_at DESC LIMIT -1 OFFSET 10000)"
            )
            connection.execute(
                "DELETE FROM atlas_retrieval_cache_registrations WHERE NOT EXISTS ("
                "SELECT 1 FROM atlas_retrieval_cache c WHERE c.generation=atlas_retrieval_cache_registrations.generation "
                "AND c.cache_key=atlas_retrieval_cache_registrations.cache_key)"
            )
            connection.commit()
            if len(_ROUTE_CACHE_SEALS) >= 10_000:
                _ROUTE_CACHE_SEALS.clear()
            _ROUTE_CACHE_SEALS[seal_key] = payload_hash
        value["cache_hit"] = cache_was_valid
        return _overlay_prefetch_candidates(
            connection, generation.generation, value, prefetch_ids, entity_limit
        )
    finally:
        connection.close()


def initial_investigation_memory(objective: str = "") -> dict[str, Any]:
    return {
        "objective": objective, "verified_facts": [], "hypotheses": [], "blocking_unknowns": [],
        "non_blocking_unknowns": [], "decisions": [], "rejected_areas": [], "verified_references": [],
        "implementation_surface": [], "test_surface": [], "context_lineage": [],
    }


def initial_coverage_map() -> dict[str, str]:
    return {key: "not_requested" for key in (
        "production_entry_point", "main_execution_flow", "cross_repo_integration", "configuration",
        "data_schema", "tests", "history", "impact_surface", "contract_surface",
    )}


def _authoritative_bundle_evidence(bundle: Any, item: Any) -> bool:
    if item.repo in {"external", "knowledge"}:
        return False
    if item.kind in {"knowledge", "local diff", "user-supplied external evidence"}:
        return False
    if item.path == "(working tree diff)":
        return False
    generation = getattr(bundle, "atlas_generation", None)
    return generation is None or item.repo in generation.snapshots


def update_investigation(memory: dict[str, Any], coverage: dict[str, str], bundle: Any, context_id: str) -> None:
    authoritative = [item for item in bundle.evidence if _authoritative_bundle_evidence(bundle, item)]
    refs = [f"{item.repo}:{item.path}:{item.line_start}-{item.line_end}" for item in authoritative]
    memory["verified_references"] = list(dict.fromkeys([*(memory.get("verified_references") or []), *refs]))[-500:]
    facts = [
        {"evidence_id": _investigation_evidence_id(item),
         "reference": f"{item.repo}:{item.path}:{item.line_start}-{item.line_end}", "kind": item.kind,
         "verified_by": list(item.found_by)}
        for item in authoritative
    ]
    known_facts = {str(item.get("evidence_id")): item for item in memory.get("verified_facts") or [] if isinstance(item, dict)}
    known_facts.update({item["evidence_id"]: item for item in facts})
    memory["verified_facts"] = [known_facts[key] for key in sorted(known_facts)][-500:]
    memory["implementation_surface"] = sorted({*memory.get("implementation_surface", []),
                                                 *(f"{item.repo}:{item.path}" for item in authoritative if not is_test_path(item.path))})[-500:]
    memory["test_surface"] = sorted({*memory.get("test_surface", []),
                                      *(f"{item.repo}:{item.path}" for item in authoritative if is_test_path(item.path))})[-500:]
    memory["blocking_unknowns"] = list(dict.fromkeys([
        *(str(item) for item in memory.get("blocking_unknowns") or []),
        *(str(item) for item in bundle.unresolved),
    ]))[-100:]
    if bundle.unresolved:
        memory["rejected_areas"] = list(dict.fromkeys([
            *(memory.get("rejected_areas") or []),
            *(f"{item} — no verified source evidence" for item in bundle.unresolved),
        ]))[-100:]
    memory["context_lineage"] = [*(memory.get("context_lineage") or []), context_id][-100:]
    if memory["implementation_surface"]:
        coverage["production_entry_point"] = "candidate"
    if memory["test_surface"]:
        coverage["tests"] = "verified"
    if bundle.relationships:
        coverage["main_execution_flow"] = "candidate"
    if bool((bundle.trace or {}).get("cross_repo_relationships")):
        coverage["cross_repo_integration"] = "candidate"
    if bundle.history:
        coverage["history"] = "candidate"
    if any(PurePosixPath(item.path).suffix.lower() in {".yaml", ".yml", ".toml", ".properties", ".xml", ".json"} for item in authoritative):
        coverage["configuration"] = "verified"
    if any(PurePosixPath(item.path).suffix.lower() in {".sql", ".avsc", ".proto", ".graphql", ".graphqls"} for item in authoritative):
        coverage["data_schema"] = "verified"


def next_best_evidence(coverage: dict[str, str], request: dict[str, Any], no_progress_rounds: int = 0) -> dict[str, Any]:
    choices = [
        ("production_entry_point", "symbol", 10, 100), ("main_execution_flow", "graph_expand", 18, 90),
        ("cross_repo_integration", "relationship", 20, 80), ("tests", "test_reference", 12, 75),
        ("impact_surface", "relationship", 16, 70), ("contract_surface", "relationship", 16, 65),
        ("configuration", "path", 8, 55), ("data_schema", "path", 8, 50), ("history", "history", 30, 35),
    ]
    missing = [item for item in choices if coverage.get(item[0], "not_requested") != "verified"]
    if no_progress_rounds >= 2 or not missing:
        return {"action": "stop", "reason": "no_progress" if no_progress_rounds >= 2 else "coverage_satisfied", "cost": 0, "value": 0}
    key, operation, cost, value = sorted(missing, key=lambda item: (-(item[3] / item[2]), item[0]))[0]
    return {"action": operation, "coverage": key, "cost": cost, "value": value,
            "reason": f"highest deterministic value/cost missing coverage: {key}"}


def _bounded_utf8_value(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _bounded_json_projection(
    values: Iterable[Any], *, max_items: int, max_bytes: int,
) -> list[Any]:
    output: list[Any] = []
    used_bytes = 2  # JSON array delimiters.
    for index, value in enumerate(values):
        if index >= max_items:
            break
        try:
            encoded = json.dumps(
                value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError):
            continue
        if len(encoded) > MAX_INVESTIGATION_JSON_ITEM_BYTES:
            continue
        additional = len(encoded) + (1 if output else 0)
        if used_bytes + additional > max_bytes:
            break
        output.append(json.loads(encoded.decode("utf-8")))
        used_bytes += additional
    return output


def record_investigation(settings: Settings, ticket: str, state: dict[str, Any]) -> None:
    objective = _bounded_utf8_value(
        str((state.get("investigation_memory") or {}).get("objective") or "").strip(),
        MAX_INVESTIGATION_OBJECTIVE_BYTES,
    )
    if not objective:
        return
    entity_ids = _bounded_json_projection(
        (str(item) for item in state.get("atlas_entity_ids") or []),
        max_items=MAX_INVESTIGATION_ENTITY_IDS,
        max_bytes=MAX_INVESTIGATION_ENTITY_BYTES,
    )
    evidence = _bounded_json_projection(
        (item for item in state.get("evidence_manifest") or [] if isinstance(item, dict)),
        max_items=MAX_INVESTIGATION_EVIDENCE_ROWS,
        max_bytes=MAX_INVESTIGATION_EVIDENCE_BYTES,
    )
    updated = datetime.now(UTC).isoformat()
    record_id = _hash("investigation", ticket)
    entity_ids_json = json.dumps(entity_ids, ensure_ascii=False, separators=(",", ":"))
    evidence_json = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    connection = connect(settings)
    try:
        connection.execute(
            "INSERT OR REPLACE INTO investigation_records(record_id,ticket,generation,objective,entity_ids_json,evidence_json,outcome,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (record_id, ticket, state.get("generation"), objective, entity_ids_json, evidence_json,
             state.get("status"), updated),
        )
        connection.execute(
            "DELETE FROM investigation_records WHERE record_id NOT IN "
            "(SELECT record_id FROM investigation_records "
            "ORDER BY updated_at DESC, record_id DESC LIMIT ?)",
            (MAX_INVESTIGATION_RECORDS,),
        )
        connection.commit()
    finally:
        connection.close()


def similar_investigations(settings: Settings, objective: str, *, limit: int = 5) -> list[dict[str, Any]]:
    terms = _tokens(_bounded_utf8_value(objective, MAX_INVESTIGATION_OBJECTIVE_BYTES))
    result_limit = max(0, min(int(limit), 50))
    if not terms or result_limit == 0:
        return []
    connection = connect(settings)
    try:
        rows = connection.execute(
            "SELECT ticket,generation,objective,entity_ids_json,evidence_json,outcome FROM investigation_records "
            "WHERE outcome IN ('ready_to_implement','completed') ORDER BY updated_at DESC, record_id DESC LIMIT ?",
            (MAX_SIMILAR_INVESTIGATION_ROWS,),
        )
        scored = []
        scanned_bytes = 0
        for ticket, generation, prior, entities, evidence, outcome in rows:
            values = (str(prior), str(entities), str(evidence))
            sizes = tuple(len(value.encode("utf-8")) for value in values)
            row_bytes = sum(sizes)
            if scanned_bytes + row_bytes > MAX_SIMILAR_INVESTIGATION_SCAN_BYTES:
                break
            scanned_bytes += row_bytes
            if (
                sizes[0] > MAX_INVESTIGATION_OBJECTIVE_BYTES
                or sizes[1] > MAX_INVESTIGATION_ENTITY_BYTES
                or sizes[2] > MAX_INVESTIGATION_EVIDENCE_BYTES
            ):
                continue
            prior_terms = _tokens(values[0])
            union = terms | prior_terms
            score = len(terms & prior_terms) / len(union) if union else 0.0
            if score:
                try:
                    entity_ids = json.loads(values[1])
                    evidence_rows = json.loads(values[2])
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(entity_ids, list) or not isinstance(evidence_rows, list):
                    continue
                if (
                    len(entity_ids) > MAX_INVESTIGATION_ENTITY_IDS
                    or len(evidence_rows) > MAX_INVESTIGATION_EVIDENCE_ROWS
                ):
                    continue
                scored.append({"ticket": ticket, "generation": generation, "objective": values[0],
                               "entity_ids": entity_ids, "evidence": evidence_rows,
                               "outcome": outcome, "score": round(score, 6)})
        return sorted(scored, key=lambda item: (-item["score"], item["ticket"]))[:result_limit]
    finally:
        connection.close()

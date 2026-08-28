"""Generation-scoped Workspace Intelligence Atlas derived from authoritative state.

The catalog and ticket session remain the only sources of truth.  This module
builds immutable, reproducible routing facts and never serves source content;
all candidate locations are exact-verified by the existing hydration path.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import re
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from .catalog import _content_hash, connect

if TYPE_CHECKING:
    from .catalog import AtlasGenerationRef
    from .core import Settings

ATLAS_SCHEMA_VERSION = "1"
EXTRACTOR_VERSION = "atlas-structural-v1"
ROUTER_SCHEMA_VERSION = "atlas-router-v2"
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
_TEST_PATH = re.compile(r"(^|/)(test|tests|src/test)/|(?:Test|Tests|IT|Spec)\.", re.I)
_NAME = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_GENERIC_DEFINITION = re.compile(
    r"(?m)^\s*(?:(?:public|private|protected|internal|export|abstract|static|async|final|open)\s+)*"
    r"(?:(class|interface|trait|enum|record|struct|type|def|function|fun|func)\s+)([A-Za-z_$][\w$]*)"
)


def _hash(*values: object) -> str:
    return "sha256:" + hashlib.sha256("\0".join(str(value) for value in values).encode("utf-8")).hexdigest()


def _language(path: str) -> str:
    return {
        ".py": "python", ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".ts": "typescript",
        ".tsx": "typescript", ".js": "javascript", ".jsx": "javascript", ".go": "go", ".rs": "rust",
        ".rb": "ruby", ".scala": "scala", ".cs": "csharp", ".sql": "sql", ".graphql": "graphql",
        ".graphqls": "graphql", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".xml": "xml",
        ".properties": "properties", ".gradle": "gradle", ".proto": "protobuf", ".avsc": "avro",
    }.get(Path(path).suffix.lower(), "text")


def _module_path(path: str) -> str:
    parent = str(Path(path).parent).replace(".", "")
    return parent or "."


def _module(repo: str, path: str) -> dict[str, Any]:
    module_path = _module_path(path)
    language = _language(path)
    module_id = _hash("module", repo, module_path)
    fingerprint = _hash(repo, module_path, language)
    return {
        "module_id": module_id, "repo": repo, "path": module_path,
        "name": Path(module_path).name if module_path != "." else repo,
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
    fingerprint = _hash(kind, qualified, signature, line_start, line_end, blob)
    return {
        "entity_id": _hash("entity", fingerprint), "repo": repo, "module_id": module_id, "path": path,
        "line_start": max(1, line_start), "line_end": max(line_start, line_end), "qualified_name": qualified,
        "simple_name": name, "signature": signature, "language": _language(path), "kind": kind,
        "parent_entity_id": parent_entity_id, "blob_sha": blob, "extractor": "project-brain",
        "extractor_version": EXTRACTOR_VERSION, "fingerprint": fingerprint, "metadata": {},
    }


def _python_entities(repo: str, path: str, blob: str, module_id: str, content: str) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError):
        return []
    rows: list[dict[str, Any]] = []

    def visit(body: list[ast.stmt], parent: dict[str, Any] | None = None) -> None:
        for node in body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if isinstance(node, ast.ClassDef):
                    kind = "test" if node.name.startswith("Test") or _TEST_PATH.search(path) else "class"
                elif node.name == "__init__" and parent:
                    kind = "constructor"
                elif parent:
                    kind = "test" if node.name.startswith("test_") or _TEST_PATH.search(path) else "method"
                else:
                    kind = "test" if node.name.startswith("test_") or _TEST_PATH.search(path) else "function"
                signature = ast.get_source_segment(content, node) or node.name
                signature = signature.splitlines()[0][:500]
                row = _entity(
                    repo, path, blob, module_id, line_start=node.lineno,
                    line_end=int(getattr(node, "end_lineno", node.lineno)), name=node.name, kind=kind,
                    signature=signature, parent_entity_id=parent["entity_id"] if parent else None,
                )
                rows.append(row)
                visit(node.body, row)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and parent is None:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        rows.append(_entity(repo, path, blob, module_id, line_start=node.lineno,
                                            line_end=int(getattr(node, "end_lineno", node.lineno)),
                                            name=target.id, kind="constant"))

    visit(tree.body)
    return rows


def _generic_entities(repo: str, path: str, blob: str, module_id: str, content: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    line_count = max(1, content.count("\n") + 1)
    for match in _GENERIC_DEFINITION.finditer(content):
        token, name = match.group(1).lower(), match.group(2)
        kind = {
            "class": "class", "interface": "interface", "trait": "trait", "type": "type",
            "def": "function", "function": "function", "fun": "function", "func": "function",
            "enum": "type", "record": "type", "struct": "type",
        }.get(token, "unknown")
        if _TEST_PATH.search(path) or name.lower().startswith("test"):
            kind = "test"
        start = content[:match.start()].count("\n") + 1
        rows.append(_entity(repo, path, blob, module_id, line_start=start,
                            line_end=min(line_count, start + 120), name=name, kind=kind,
                            signature=match.group(0).strip()[:500]))
    return rows


def _special_entities(repo: str, path: str, blob: str, module_id: str, content: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    patterns = [
        ("endpoint", re.compile(r"(?m)(?:@(Get|Post|Put|Delete|Patch)Mapping|(?:app|router)\.(get|post|put|delete|patch))\s*\(?[\"']([^\"']+)", re.I)),
        ("topic", re.compile(r"(?im)(?:topic|kafka[^\n]{0,30})\s*[:=(]\s*[\"']([A-Za-z0-9._-]+)")),
        ("queue", re.compile(r"(?im)(?:queue|rabbit[^\n]{0,30})\s*[:=(]\s*[\"']([A-Za-z0-9._-]+)")),
        ("feature_flag", re.compile(r"(?im)(?:feature[_-]?flag|featureToggle)[^\n]{0,40}[\"']([A-Za-z0-9._-]+)")),
        ("table", re.compile(r"(?im)\b(?:from|join|into|update|table)\s+[`\"]?([A-Za-z_][A-Za-z0-9_.]*)")),
    ]
    for kind, pattern in patterns:
        for match in pattern.finditer(content):
            name = next((group for group in reversed(match.groups()) if group), match.group(0))
            line = content[:match.start()].count("\n") + 1
            rows.append(_entity(repo, path, blob, module_id, line_start=line, line_end=line,
                                name=str(name), kind=kind, signature=match.group(0).strip()[:500]))
    if Path(path).suffix.lower() in {".yaml", ".yml", ".toml", ".properties", ".xml", ".json"}:
        for line_number, line in enumerate(content.splitlines(), 1):
            match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_.-]{2,})\s*[:=]", line)
            if match:
                rows.append(_entity(repo, path, blob, module_id, line_start=line_number, line_end=line_number,
                                    name=match.group(1), kind="config_key", signature=line.strip()[:500]))
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
    edge_id = _hash("edge", edge_type, source_id, target_id, repo, path, line, blob)
    return {
        "edge_id": edge_id, "edge_type": edge_type, "source_id": source_id, "target_id": target_id,
        "repo": repo, "path": path, "line_start": max(1, line), "line_end": max(1, line), "blob_sha": blob,
        "extractor": "project-brain", "extractor_version": EXTRACTOR_VERSION,
        "confidence": max(0.0, min(1.0, confidence)), "metadata": metadata or {},
    }


def _file_intelligence(repo: str, path: str, blob: str, content: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    module = _module(repo, path)
    lines = content.splitlines()
    file_entity = _entity(repo, path, blob, module["module_id"], line_start=1, line_end=max(1, len(lines)),
                          name=Path(path).name, kind="file")
    definitions = (
        _python_entities(repo, path, blob, module["module_id"], content)
        if Path(path).suffix.lower() == ".py"
        else _generic_entities(repo, path, blob, module["module_id"], content)
    )
    definitions.extend(_special_entities(repo, path, blob, module["module_id"], content))
    deduped = {item["entity_id"]: item for item in definitions}
    entities = [file_entity, *deduped.values()]
    regions: list[dict[str, Any]] = []
    for entity in entities:
        region_id = _hash("region", repo, path, blob, entity["line_start"], entity["line_end"], entity["kind"])
        regions.append({
            "region_id": region_id, "repo": repo, "path": path, "line_start": entity["line_start"],
            "line_end": entity["line_end"], "blob_sha": blob, "kind": entity["kind"],
            "fingerprint": _hash(blob, entity["line_start"], entity["line_end"]),
            "metadata": {"entity_id": entity["entity_id"]},
        })
    for start in range(1, max(1, len(lines)) + 1, 120):
        end = min(max(1, len(lines)), start + 119)
        region_id = _hash("region", repo, path, blob, start, end, "source_region")
        regions.append({
            "region_id": region_id, "repo": repo, "path": path, "line_start": start, "line_end": end,
            "blob_sha": blob, "kind": "source_region", "fingerprint": _hash(blob, start, end),
            "metadata": {"file_entity_id": file_entity["entity_id"]},
        })
    edges = [_edge("CONTAINS", module["module_id"], file_entity["entity_id"], repo=repo, path=path, line=1, blob=blob, confidence=1.0)]
    edges.extend(_edge("DEFINES", file_entity["entity_id"], item["entity_id"], repo=repo, path=path,
                       line=item["line_start"], blob=blob, confidence=1.0) for item in definitions)
    name_to_entity = {item["simple_name"]: item["entity_id"] for item in definitions}
    line_offsets = [0]
    for match in re.finditer("\n", content):
        line_offsets.append(match.end())

    def line_at(position: int) -> int:
        import bisect
        return bisect.bisect_right(line_offsets, position)

    patterns = [
        ("IMPORTS", re.compile(r"(?m)^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+)|(?:import|require)\s*\(?[\"']([^\"']+))"), .95),
        ("EXTENDS", re.compile(r"\b(?:extends)\s+([A-Za-z_$][\w$]*)"), .9),
        ("IMPLEMENTS", re.compile(r"\bimplements\s+([A-Za-z_$][\w$]*)"), .9),
        ("CALLS", re.compile(r"\b([A-Za-z_$][\w$]*)\s*\("), .65),
    ]
    for edge_type, pattern, confidence in patterns:
        for match in pattern.finditer(content):
            name = next((group for group in match.groups() if group), "")
            if not name or name in {"if", "for", "while", "switch", "return", "class", "def", "function"}:
                continue
            line = line_at(match.start())
            owner = next((item for item in definitions if item["line_start"] <= line <= item["line_end"]), file_entity)
            target = name_to_entity.get(name) or _hash("symbol", name.lower())
            edges.append(_edge(edge_type, owner["entity_id"], target, repo=repo, path=path, line=line,
                               blob=blob, confidence=confidence, metadata={"target_name": name}))
    for test in (item for item in definitions if item["kind"] == "test"):
        tokens = set(_NAME.findall("\n".join(lines[test["line_start"] - 1:test["line_end"]])))
        for name, target in name_to_entity.items():
            if name in tokens and target != test["entity_id"]:
                edges.append(_edge("TESTS", test["entity_id"], target, repo=repo, path=path,
                                   line=test["line_start"], blob=blob, confidence=.85))
    return module, entities, regions, list({item["edge_id"]: item for item in edges}.values())


def _card(level: str, target_id: str, repo: str, content: str, *, module_id: str | None = None,
          entity_id: str | None = None, path: str | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    content = content.strip()[:12_000]
    content_hash = _hash("card-content", content)
    return {
        "card_id": _hash("card", level, target_id, content_hash), "level": level, "target_id": target_id,
        "repo": repo, "module_id": module_id, "entity_id": entity_id, "path": path, "content": content,
        "content_hash": content_hash, "metadata": metadata or {},
    }


def _change_rows(
    settings: Settings,
    snapshots: dict[str, str],
    parent_snapshots: dict[str, str],
    previous: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
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
        incremental = False
        if parent and current:
            try:
                incremental = subprocess.run(
                    ["git", "merge-base", "--is-ancestor", parent, current], cwd=repo.path,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                ).returncode == 0
            except OSError:
                rows.extend(prior)
                continue
        revision = f"{parent}..{current}" if incremental else str(current or "HEAD")
        args = ["git", "log", "--format=%H%x1f%cI%x1f%s", "--max-count=100"]
        if incremental:
            args.append(revision)
        elif current:
            args.append(current)
        try:
            result = subprocess.run(
                args, cwd=repo.path, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
            )
        except OSError:
            rows.extend(prior)
            continue
        for raw in result.stdout.splitlines():
            parts = raw.split("\x1f", 2)
            if len(parts) != 3:
                continue
            commit, committed_at, subject = parts
            try:
                shown = subprocess.run(["git", "show", "--format=", "--name-status", "--find-renames", commit], cwd=repo.path,
                                       text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
                counted = subprocess.run(["git", "show", "--format=", "--numstat", "--find-renames", commit], cwd=repo.path,
                                         text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
            except OSError:
                continue
            statistics: dict[str, tuple[int | None, int | None]] = {}
            for stat in counted.stdout.splitlines()[:500]:
                values = stat.split("\t")
                if len(values) >= 3:
                    statistics[values[-1]] = (
                        int(values[0]) if values[0].isdigit() else None,
                        int(values[1]) if values[1].isdigit() else None,
                    )
            ticket_match = ticket_pattern.search(subject)
            ticket = ticket_match.group(1) if ticket_match else None
            for line in shown.stdout.splitlines()[:500]:
                values = line.split("\t")
                if len(values) < 2:
                    continue
                status = values[0][0]
                old_path = values[1] if status == "R" and len(values) > 2 else None
                path = values[2] if old_path else values[1]
                additions, deletions = statistics.get(path, (None, None))
                change_id = _hash("change", repo.name, commit, status, old_path or "", path)
                rows.append({
                    "change_id": change_id, "repo": repo.name, "commit_sha": commit, "committed_at": committed_at,
                    "ticket": ticket, "path": path, "old_path": old_path,
                    "status": {"A": "added", "M": "modified", "D": "deleted", "R": "renamed"}.get(status, "changed"),
                    "additions": additions, "deletions": deletions, "metadata": {
                        "subject": subject[:500], "is_test": bool(_TEST_PATH.search(path)),
                        "is_config": Path(path).suffix.lower() in {".yaml", ".yml", ".toml", ".properties", ".xml", ".json"},
                    },
                })
        if incremental:
            rows.extend(prior)
    unique = {str(item["change_id"]): item for item in rows}
    retained: list[dict[str, Any]] = []
    for repo_name in sorted({str(item["repo"]) for item in unique.values()}):
        repo_rows = [item for item in unique.values() if str(item["repo"]) == repo_name]
        commits = sorted(
            {(str(item.get("committed_at") or ""), str(item["commit_sha"])) for item in repo_rows},
            reverse=True,
        )[:100]
        allowed = {commit for _, commit in commits}
        retained.extend(item for item in repo_rows if str(item["commit_sha"]) in allowed)
    return retained


def _location(value: str) -> tuple[str, str, int] | None:
    match = re.fullmatch(r"([^:]+):(.+):(\d+)", value)
    return (match.group(1), match.group(2), int(match.group(3))) if match else None


def _integration_edges(settings: Settings, entities: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    path = settings.state_dir / "relationships.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    by_path: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entity in entities:
        by_path.setdefault((entity["repo"], entity["path"]), []).append(entity)

    def owner(location: tuple[str, str, int]) -> dict[str, Any] | None:
        repo, file_path, line = location
        values = by_path.get((repo, file_path), [])
        return next((item for item in values if item["kind"] != "file" and item["line_start"] <= line <= item["line_end"]),
                    next((item for item in values if item["kind"] == "file"), None))

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


def _reused_file_intelligence(
    connection: sqlite3.Connection,
    generation: int,
    repo: str,
    path: str,
    blob: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    entities = [{
        "entity_id": row[0], "repo": row[1], "module_id": row[2], "path": row[3], "line_start": row[4],
        "line_end": row[5], "qualified_name": row[6], "simple_name": row[7], "signature": row[8],
        "language": row[9], "kind": row[10], "parent_entity_id": row[11], "blob_sha": row[12],
        "extractor": row[13], "extractor_version": row[14], "fingerprint": row[15], "metadata": _json_object(row[16]),
    } for row in connection.execute(
        "SELECT e.entity_id,e.repo,e.module_id,e.path,e.line_start,e.line_end,e.qualified_name,e.simple_name,e.signature,"
        "e.language,e.kind,e.parent_entity_id,e.blob_sha,e.extractor,e.extractor_version,e.fingerprint,e.metadata_json "
        "FROM generation_entities g JOIN atlas_entities e ON e.entity_id=g.entity_id "
        "WHERE g.generation=? AND e.repo=? AND e.path=? AND e.blob_sha=?",
        (generation, repo, path, blob),
    )]
    regions = [{
        "region_id": row[0], "repo": row[1], "path": row[2], "line_start": row[3], "line_end": row[4],
        "blob_sha": row[5], "kind": row[6], "fingerprint": row[7], "metadata": _json_object(row[8]),
    } for row in connection.execute(
        "SELECT r.region_id,r.repo,r.path,r.line_start,r.line_end,r.blob_sha,r.kind,r.fingerprint,r.metadata_json "
        "FROM generation_regions g JOIN atlas_regions r ON r.region_id=g.region_id "
        "WHERE g.generation=? AND r.repo=? AND r.path=? AND r.blob_sha=?",
        (generation, repo, path, blob),
    )]
    edges = [{
        "edge_id": row[0], "edge_type": row[1], "source_id": row[2], "target_id": row[3], "repo": row[4],
        "path": row[5], "line_start": row[6], "line_end": row[7], "blob_sha": row[8], "extractor": row[9],
        "extractor_version": row[10], "confidence": row[11], "metadata": _json_object(row[12]),
    } for row in connection.execute(
        "SELECT e.edge_id,e.edge_type,e.source_id,e.target_id,e.repo,e.path,e.line_start,e.line_end,e.blob_sha,"
        "e.extractor,e.extractor_version,e.confidence,e.metadata_json FROM generation_edges g "
        "JOIN atlas_edges e ON e.edge_id=g.edge_id WHERE g.generation=? AND e.repo=? AND e.path=? AND e.blob_sha=?",
        (generation, repo, path, blob),
    )]
    return entities, regions, edges


def build_atlas(settings: Settings, state: dict[str, object]) -> dict[str, Any]:
    """Build normalized generation payload with blob-level incremental reuse."""
    snapshots = {name: str(raw.get("sha") or "working-tree") for name, raw in state.items() if isinstance(raw, dict)}
    connection = connect(settings)
    try:
        current = connection.execute("SELECT value FROM metadata WHERE key='current_generation'").fetchone()
        parent_generation = int(current[0]) if current else None
        parent_snapshots = _parent_snapshots(connection, parent_generation)
        previous_changes = _generation_changes(connection, parent_generation)
        previous_files: dict[tuple[str, str], str] = {}
        if parent_generation is not None:
            previous_files.update({(str(repo), str(path)): str(blob) for repo, path, blob in connection.execute(
                "SELECT e.repo,e.path,e.blob_sha FROM generation_entities g "
                "JOIN atlas_entities e ON e.entity_id=g.entity_id "
                "WHERE g.generation=? AND e.kind='file'",
                (parent_generation,),
            )})
        current_files: dict[tuple[str, str], str] = {}
        for repo, sha in snapshots.items():
            current_files.update({(repo, str(path)): str(blob) for path, blob in connection.execute(
                "SELECT path,blob_sha FROM snapshot_files WHERE repo=? AND sha=?", (repo, sha)
            )})
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
    if parent_generation is not None and unchanged:
        connection = connect(settings)
        try:
            for repo_name, path in unchanged:
                reused[(repo_name, path)] = _reused_file_intelligence(
                    connection, parent_generation, repo_name, path, current_files[(repo_name, path)]
                )
        finally:
            connection.close()
    parsed_files = 0
    for (repo_name, path), blob in sorted(current_files.items()):
        if Path(path).suffix.lower() not in _CODE_SUFFIXES:
            continue
        reused_rows = reused.get((repo_name, path))
        if reused_rows and reused_rows[0]:
            module = _module(repo_name, path)
            modules[module["module_id"]] = module
            entities.update({item["entity_id"]: item for item in reused_rows[0]})
            regions.update({item["region_id"]: item for item in reused_rows[1]})
            edges.update({item["edge_id"]: item for item in reused_rows[2]})
            continue
        repo = settings.repo(repo_name)
        source = (repo.scan_path / path).resolve()
        root = repo.scan_path.resolve()
        if not source.is_relative_to(root) or not source.is_file():
            continue
        try:
            raw = source.read_bytes()
        except OSError:
            continue
        if len(raw) > 3_000_000 or b"\0" in raw[:8192]:
            continue
        content = raw.decode("utf-8", errors="replace")
        module, file_entities, file_regions, file_edges = _file_intelligence(repo_name, path, blob, content)
        modules[module["module_id"]] = module
        entities.update({item["entity_id"]: item for item in file_entities})
        regions.update({item["region_id"]: item for item in file_regions})
        edges.update({item["edge_id"]: item for item in file_edges})
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

    changes = _change_rows(settings, snapshots, parent_snapshots, previous_changes)
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
        target_name = str(metadata.get("target_name") or "").rsplit(".", 1)[-1].lower()
        targets = named.get(target_name) or []
        preferred = next((target for target in targets if target["repo"] == item["repo"]), targets[0] if targets else None)
        if preferred is not None and preferred["entity_id"] != item["source_id"]:
            resolved = _edge(item["edge_type"], item["source_id"], preferred["entity_id"], repo=item["repo"],
                             path=item["path"], line=item["line_start"], blob=item["blob_sha"],
                             confidence=float(item["confidence"]) if was_resolved else min(.95, float(item["confidence"]) + .15),
                             metadata={**metadata, "resolved": True})
            edges[resolved["edge_id"]] = resolved
    for item in _integration_edges(settings, entities.values()):
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
    }
    return {
        "modules": list(modules.values()), "entities": list(entities.values()), "regions": list(regions.values()),
        "edges": list(edges.values()), "cards": list(cards.values()), "changes": changes, "delta": delta,
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
        result[name] = {
            "schema_version": ATLAS_SCHEMA_VERSION, "status": "ready", "content_hash": _content_hash(logical),
            "details": {"count": count},
        }
    return result


def _tokens(value: str) -> set[str]:
    tokens: set[str] = set()
    for token in re.findall(r"[A-Za-z_$][A-Za-z0-9_$.-]{2,}", value):
        tokens.add(token.lower())
        parts = re.split(r"[$_.-]+|(?<=[a-z0-9])(?=[A-Z])", token)
        tokens.update(part.lower() for part in parts if len(part) >= 3)
    return tokens


def _cache_key(objective: str, request: dict[str, Any], edition: str, *, repo_limit: int, entity_limit: int) -> str:
    logical = {
        "schema": ROUTER_SCHEMA_VERSION, "objective": objective,
        "request": {key: request.get(key) or [] for key in (
            "searches", "paths", "symbols", "history", "required", "resolve",
            "runtime_facts", "hypotheses",
        )},
        "hints": request.get("hints") or {},
        "prior_entity_ids": list(dict.fromkeys(str(value) for value in request.get("_prior_entity_ids") or []))[:200],
        "edition": edition,
        "options": {"repo_limit": repo_limit, "entity_limit": entity_limit},
        "evaluation_ablation": sorted(str(value) for value in request.get("_evaluation_ablation") or []),
    }
    return _hash(json.dumps(logical, sort_keys=True, separators=(",", ":"), default=str))


def _valid_cached_route(connection: sqlite3.Connection, generation: int, value: Any) -> bool:
    if not isinstance(value, dict) or value.get("schema") != ROUTER_SCHEMA_VERSION:
        return False
    try:
        cached_generation = int(value.get("generation") or -1)
    except (TypeError, ValueError):
        return False
    if cached_generation != generation or value.get("candidates") != value.get("entities"):
        return False
    for repo in value.get("repos") or []:
        if not connection.execute(
            "SELECT 1 FROM generation_snapshots WHERE generation=? AND repo=?", (generation, repo)
        ).fetchone():
            return False
    for module_id in value.get("modules") or []:
        if not connection.execute(
            "SELECT 1 FROM generation_modules WHERE generation=? AND module_id=?", (generation, module_id)
        ).fetchone():
            return False
    for item in value.get("entities") or []:
        row = connection.execute(
            "SELECT e.repo,e.module_id,e.path,e.line_start,e.kind FROM generation_entities g "
            "JOIN atlas_entities e ON e.entity_id=g.entity_id WHERE g.generation=? AND e.entity_id=?",
            (generation, item.get("entity_id") if isinstance(item, dict) else None),
        ).fetchone()
        try:
            line_matches = int(item.get("line") or 0) == int(row[3]) if isinstance(item, dict) and row else False
            score = float(item.get("score")) if isinstance(item, dict) else math.nan
            score_matches = math.isfinite(score)
        except (TypeError, ValueError):
            line_matches = False
            score_matches = False
        if not isinstance(item, dict) or not row or (
            str(item.get("repo")) != str(row[0])
            or str(item.get("module_id") or "") != str(row[1] or "")
            or str(item.get("path")) != str(row[2])
            or not line_matches
            or not score_matches
            or str(item.get("kind")) != str(row[4])
            or not isinstance(item.get("found_by"), list)
            or not all(isinstance(value, str) for value in item.get("found_by") or [])
        ):
            return False
    for item in value.get("graph_edges") or []:
        row = connection.execute(
            "SELECT se.repo,te.repo,te.path,te.line_start,e.confidence FROM generation_edges g "
            "JOIN atlas_edges e ON e.edge_id=g.edge_id "
            "JOIN generation_entities s ON s.generation=g.generation AND s.entity_id=e.source_id "
            "JOIN generation_entities t ON t.generation=g.generation AND t.entity_id=e.target_id "
            "JOIN atlas_entities se ON se.entity_id=s.entity_id "
            "JOIN atlas_entities te ON te.entity_id=t.entity_id "
            "WHERE g.generation=? AND e.source_id=? AND e.target_id=? AND e.edge_type=?",
            (generation, item.get("source_id") if isinstance(item, dict) else None,
             item.get("target_id") if isinstance(item, dict) else None,
             item.get("edge_type") if isinstance(item, dict) else None),
        ).fetchone()
        try:
            graph_values_match = (
                int(item.get("line") or 0) == int(row[3])
                and float(item.get("confidence") or 0) == float(row[4])
            ) if isinstance(item, dict) and row else False
        except (TypeError, ValueError):
            graph_values_match = False
        if not isinstance(item, dict) or not row or (
            str(item.get("source_repo")) != str(row[0])
            or str(item.get("repo")) != str(row[1])
            or str(item.get("path")) != str(row[2])
            or not graph_values_match
        ):
            return False
    return True


def route(
    settings: Settings,
    objective: str,
    request: dict[str, Any],
    generation: AtlasGenerationRef | None,
    *,
    repo_limit: int = 16,
    entity_limit: int = 80,
) -> dict[str, Any]:
    """Route through generation cards/entities/graph; never return source content."""
    if generation is None or generation.component("hierarchy").get("status") != "ready":
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
    key = _cache_key(objective, request, edition, repo_limit=repo_limit, entity_limit=entity_limit)
    connection = connect(settings)
    now = datetime.now(UTC).isoformat()
    try:
        cached = None if "generation_cache" in ablation else connection.execute(
            "SELECT payload_json FROM atlas_retrieval_cache WHERE generation=? AND cache_key=?",
            (generation.generation, key),
        ).fetchone()
        if cached:
            try:
                value = json.loads(cached[0])
            except (TypeError, json.JSONDecodeError):
                value = None
            if _valid_cached_route(connection, generation.generation, value):
                connection.execute(
                    "UPDATE atlas_retrieval_cache SET last_used_at=? WHERE generation=? AND cache_key=?",
                    (now, generation.generation, key),
                )
                connection.commit()
                cached_ids = {
                    str(item.get("entity_id")) for item in value.get("entities") or []
                    if isinstance(item, dict) and item.get("entity_id")
                }
                prefetch_ids = set() if "prefetch" in ablation else {
                    str(item) for item in ((request.get("_prefetch") or {}).get("candidate_ids") or [])
                }
                prior_ids = set() if "investigation_memory" in ablation else {
                    str(item) for item in request.get("_prior_entity_ids") or []
                }
                value["prefetch_reused"] = len(cached_ids & prefetch_ids)
                value["investigation_reused"] = len(cached_ids & (prior_ids - prefetch_ids))
                value["cache_hit"] = True
                return value
            connection.execute("DELETE FROM atlas_retrieval_cache WHERE generation=? AND cache_key=?", (generation.generation, key))

        query_text = " ".join([
            objective,
            " ".join(str(value) for key in ("runtime_facts", "hypotheses", "resolve", "required") for value in request.get(key) or []),
            " ".join(str(item.get("query") or item.get("name") or "") for section in ("searches", "paths", "symbols", "history")
                     for item in request.get(section) or [] if isinstance(item, dict)),
        ])
        terms = _tokens(query_text)
        explicit_repos = {
            str(repo) for section in ("searches", "paths", "symbols", "history")
            for item in request.get(section) or [] if isinstance(item, dict) for repo in item.get("repos") or []
        }
        rows = connection.execute(
            "SELECT c.level,c.target_id,c.repo,c.module_id,c.entity_id,c.path,c.content,e.simple_name,e.qualified_name,e.kind,e.line_start "
            "FROM generation_cards g JOIN atlas_cards c ON c.card_id=g.card_id "
            "LEFT JOIN atlas_entities e ON e.entity_id=c.entity_id WHERE g.generation=?",
            (generation.generation,),
        )
        scored: list[dict[str, Any]] = []
        repo_scores: dict[str, float] = {}
        module_scores: dict[str, float] = {}
        for level, target_id, repo, module_id, entity_id, path, content, simple, qualified, kind, line in rows:
            card_terms = _tokens(str(content))
            overlap = len(terms & card_terms)
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
        change_rows = connection.execute(
            "SELECT c.repo,c.path,c.ticket,c.metadata_json,e.entity_id,e.module_id,e.line_start,e.kind "
            "FROM generation_changes g JOIN atlas_changes c ON c.change_id=g.change_id "
            "LEFT JOIN (SELECT e.entity_id,e.repo,e.path,e.module_id,e.line_start,e.kind "
            "           FROM generation_entities ge JOIN atlas_entities e ON e.entity_id=ge.entity_id "
            "           WHERE ge.generation=? AND e.kind='file') e "
            "ON e.repo=c.repo AND e.path=c.path "
            "WHERE g.generation=? LIMIT 2000",
            (generation.generation, generation.generation),
        )
        for repo, path, ticket, metadata_json, entity_id, module_id, line, kind in change_rows:
            metadata = _json_object(metadata_json)
            overlap = len(terms & _tokens(f"{ticket or ''} {path} {metadata.get('subject', '')}"))
            if not overlap:
                continue
            repo_scores[str(repo)] = repo_scores.get(str(repo), 0) + overlap * 4
            if entity_id:
                scored.append({
                    "entity_id": str(entity_id), "repo": str(repo), "module_id": str(module_id or ""),
                    "path": str(path), "line": int(line or 1), "kind": str(kind or "file"),
                    "score": float(overlap * 6), "found_by": ["Atlas change intelligence"],
                })
        prefetch_ids = set() if "prefetch" in ablation else {
            str(value) for value in ((request.get("_prefetch") or {}).get("candidate_ids") or [])
        }
        prior_ids = list(dict.fromkeys(
            str(value) for value in (() if "investigation_memory" in ablation else request.get("_prior_entity_ids") or [])
        ))[:200]
        reused_prior_ids: set[str] = set()
        if prior_ids:
            placeholders = ",".join("?" for _ in prior_ids)
            for entity_id, repo, module_id, path, line, kind in connection.execute(
                f"SELECT e.entity_id,e.repo,e.module_id,e.path,e.line_start,e.kind FROM generation_entities g "
                f"JOIN atlas_entities e ON e.entity_id=g.entity_id WHERE g.generation=? AND e.entity_id IN ({placeholders})",
                (generation.generation, *prior_ids),
            ):
                reused_prior_ids.add(str(entity_id))
                scored.append({
                    "entity_id": str(entity_id), "repo": str(repo), "module_id": str(module_id), "path": str(path),
                    "line": int(line), "kind": str(kind), "score": 25.0,
                    "found_by": ["generation-validated investigation prior"],
                })
        # One-hop graph expansion is a routing signal only.  Provenance stays on
        # the edge and exact source hydration remains mandatory downstream.
        graph_routes: list[dict[str, Any]] = []
        seeds = [item["entity_id"] for item in sorted(scored, key=lambda item: (-item["score"], item["entity_id"]))[:20]]
        if seeds and "graph" not in ablation:
            placeholders = ",".join("?" for _ in seeds)
            graph_rows = connection.execute(
                f"SELECT e.source_id,e.target_id,e.edge_type,e.confidence,s.repo,t.repo,t.path,t.line_start,t.kind,t.module_id "
                f"FROM generation_edges g JOIN atlas_edges e ON e.edge_id=g.edge_id "
                f"JOIN generation_entities gs ON gs.generation=g.generation AND gs.entity_id=e.source_id "
                f"JOIN generation_entities gt ON gt.generation=g.generation AND gt.entity_id=e.target_id "
                f"JOIN atlas_entities s ON s.entity_id=e.source_id "
                f"JOIN atlas_entities t ON t.entity_id=e.target_id "
                f"WHERE g.generation=? AND e.source_id IN ({placeholders}) LIMIT 200",
                (generation.generation, *seeds),
            )
            seed_scores = {item["entity_id"]: item["score"] for item in scored}
            for source_id, target_id, edge_type, confidence, source_repo, repo, path, line, kind, module_id in graph_rows:
                graph_routes.append({
                    "source_id": str(source_id), "target_id": str(target_id), "edge_type": str(edge_type),
                    "confidence": float(confidence), "source_repo": str(source_repo), "repo": str(repo),
                    "path": str(path), "line": int(line),
                })
                scored.append({
                    "entity_id": str(target_id), "repo": str(repo), "module_id": str(module_id), "path": str(path),
                    "line": int(line), "kind": str(kind), "score": seed_scores.get(str(source_id), 0) * .35 + float(confidence) * 10,
                    "found_by": [f"Atlas {edge_type} edge"],
                })
        merged: dict[tuple[str, str, int], dict[str, Any]] = {}
        for item in scored:
            candidate_key = (item["repo"], item["path"], item["line"])
            previous = merged.get(candidate_key)
            if previous is None or item["score"] > previous["score"]:
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
            "prefetch_reused": len(returned_entity_ids & prefetch_ids),
            "investigation_reused": len(returned_entity_ids & reused_prior_ids),
            "evaluation_ablation": sorted(ablation),
        }
        if "generation_cache" not in ablation:
            connection.execute(
                "INSERT OR REPLACE INTO atlas_retrieval_cache(generation,cache_key,payload_json,created_at,last_used_at) VALUES (?,?,?,?,?)",
                (generation.generation, key, json.dumps(value, sort_keys=True), now, now),
            )
            connection.execute(
                "DELETE FROM atlas_retrieval_cache WHERE rowid IN (SELECT rowid FROM atlas_retrieval_cache ORDER BY last_used_at DESC LIMIT -1 OFFSET 10000)"
            )
            connection.commit()
        return value
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
        "data_schema", "tests", "history", "explicit_requested",
    )}


def update_investigation(memory: dict[str, Any], coverage: dict[str, str], bundle: Any, context_id: str) -> None:
    refs = [f"{item.repo}:{item.path}:{item.line_start}-{item.line_end}" for item in bundle.evidence
            if item.repo not in {"external", "knowledge"}]
    memory["verified_references"] = list(dict.fromkeys([*(memory.get("verified_references") or []), *refs]))[-500:]
    facts = [
        {"evidence_id": f"E-{hashlib.sha256(f'{item.repo}\0{item.path}\0{item.line_start}\0{item.line_end}\0{item.content}'.encode()).hexdigest()[:24]}",
         "reference": f"{item.repo}:{item.path}:{item.line_start}-{item.line_end}", "kind": item.kind,
         "verified_by": list(item.found_by)}
        for item in bundle.evidence if item.repo not in {"external", "knowledge"}
    ]
    known_facts = {str(item.get("evidence_id")): item for item in memory.get("verified_facts") or [] if isinstance(item, dict)}
    known_facts.update({item["evidence_id"]: item for item in facts})
    memory["verified_facts"] = [known_facts[key] for key in sorted(known_facts)][-500:]
    memory["implementation_surface"] = sorted({*memory.get("implementation_surface", []),
                                                 *(f"{item.repo}:{item.path}" for item in bundle.evidence if item.repo not in {"external", "knowledge"} and not _TEST_PATH.search(item.path))})[-500:]
    memory["test_surface"] = sorted({*memory.get("test_surface", []),
                                      *(f"{item.repo}:{item.path}" for item in bundle.evidence if _TEST_PATH.search(item.path))})[-500:]
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
        coverage["production_entry_point"] = "verified"
        coverage["main_execution_flow"] = "verified" if bundle.relationships else "candidate"
    if memory["test_surface"]:
        coverage["tests"] = "verified"
    if bundle.relationships:
        coverage["main_execution_flow"] = "verified"
    if bool((bundle.trace or {}).get("cross_repo_relationships")):
        coverage["cross_repo_integration"] = "verified"
    if bundle.history:
        coverage["history"] = "verified"
    if any(Path(item.path).suffix.lower() in {".yaml", ".yml", ".toml", ".properties", ".xml", ".json"} for item in bundle.evidence):
        coverage["configuration"] = "verified"
    if any(Path(item.path).suffix.lower() in {".sql", ".avsc", ".proto", ".graphql", ".graphqls"} for item in bundle.evidence):
        coverage["data_schema"] = "verified"


def next_best_evidence(coverage: dict[str, str], request: dict[str, Any], no_progress_rounds: int = 0) -> dict[str, Any]:
    choices = [
        ("production_entry_point", "symbol", 10, 100), ("main_execution_flow", "graph_expand", 18, 90),
        ("cross_repo_integration", "relationship", 20, 80), ("tests", "test_reference", 12, 75),
        ("configuration", "path", 8, 55), ("data_schema", "path", 8, 50), ("history", "history", 30, 35),
    ]
    missing = [item for item in choices if coverage.get(item[0], "not_requested") != "verified"]
    if no_progress_rounds >= 2 or not missing:
        return {"action": "stop", "reason": "no_progress" if no_progress_rounds >= 2 else "coverage_satisfied", "cost": 0, "value": 0}
    key, operation, cost, value = sorted(missing, key=lambda item: (-(item[3] / item[2]), item[0]))[0]
    return {"action": operation, "coverage": key, "cost": cost, "value": value,
            "reason": f"highest deterministic value/cost missing coverage: {key}"}


def record_investigation(settings: Settings, ticket: str, state: dict[str, Any]) -> None:
    objective = str((state.get("investigation_memory") or {}).get("objective") or "")
    if not objective:
        return
    entity_ids = list(state.get("atlas_entity_ids") or [])
    evidence = list(state.get("evidence_manifest") or [])
    updated = datetime.now(UTC).isoformat()
    record_id = _hash("investigation", ticket)
    connection = connect(settings)
    try:
        connection.execute(
            "INSERT OR REPLACE INTO investigation_records(record_id,ticket,generation,objective,entity_ids_json,evidence_json,outcome,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (record_id, ticket, state.get("generation"), objective, json.dumps(entity_ids), json.dumps(evidence),
             state.get("status"), updated),
        )
        connection.commit()
    finally:
        connection.close()


def similar_investigations(settings: Settings, objective: str, *, limit: int = 5) -> list[dict[str, Any]]:
    terms = _tokens(objective)
    connection = connect(settings)
    try:
        rows = connection.execute(
            "SELECT ticket,generation,objective,entity_ids_json,evidence_json,outcome FROM investigation_records "
            "WHERE outcome IN ('ready_to_implement','completed') ORDER BY updated_at DESC LIMIT 500"
        )
        scored = []
        for ticket, generation, prior, entities, evidence, outcome in rows:
            prior_terms = _tokens(str(prior))
            union = terms | prior_terms
            score = len(terms & prior_terms) / len(union) if union else 0.0
            if score:
                try:
                    entity_ids = json.loads(entities)
                    evidence_rows = json.loads(evidence)
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(entity_ids, list) or not isinstance(evidence_rows, list):
                    continue
                scored.append({"ticket": ticket, "generation": generation, "objective": prior,
                               "entity_ids": entity_ids, "evidence": evidence_rows,
                               "outcome": outcome, "score": round(score, 6)})
        return sorted(scored, key=lambda item: (-item["score"], item["ticket"]))[:limit]
    finally:
        connection.close()

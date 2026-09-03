"""Generation-pinned v1 investigation intelligence.

The catalog owns immutable refresh-time facts and the ticket session owns
query-time investigation state.  This module deliberately creates neither a
second graph nor a second session store: it only builds rows for the existing
Atlas publication transaction and derives bounded ticket state from a pinned
generation plus exact hydrated source evidence.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from .atlas import (
    ATLAS_SCHEMA_VERSION,
    _route_cache_secret,
    _valid_generation_edges,
    _valid_generation_entities,
    _valid_term_index,
)
from .catalog import _content_hash, connect
from .platforms import is_test_path

if TYPE_CHECKING:
    from .catalog import AtlasGenerationRef
    from .core import ContextBundle, Settings


RUNTIME_ANCHOR_SCHEMA_VERSION = "runtime-anchor-v3"
RUNTIME_ANCHOR_TERM_SCHEMA_VERSION = "runtime-anchor-terms-v1"
JAVA_INTELLIGENCE_SCHEMA_VERSION = "java-spring-v3"
GENERATION_INTELLIGENCE_INPUT_SCHEMA_VERSION = "generation-intelligence-input-v3"
EXECUTION_FLOW_SCHEMA_VERSION = "execution-flow-v1"
INTEGRATION_FLOW_SCHEMA_VERSION = "integration-flow-v1"
PROGRAM_SLICE_SCHEMA_VERSION = "program-slice-lite-v1"
SURFACE_SCHEMA_VERSION = "investigation-surfaces-v1"
HYPOTHESIS_LEDGER_SCHEMA_VERSION = "hypothesis-ledger-v1"
EVIDENCE_FRONTIER_SCHEMA_VERSION = "evidence-frontier-v1"
PREFETCH_SCHEMA_VERSION = "ticket-prefetch-v1"
PREFETCH_SEQUENCE_BOUNDS = {"repos": 128, "modules": 200, "candidate_ids": 200, "anchor_ids": 50}
CHECKPOINT_SCHEMA_VERSION = "progressive-checkpoint-v1"
INVESTIGATION_RUNTIME_SCHEMA_VERSION = "investigation-runtime-v2"
PROTOCOL_V5_SCHEMA_VERSION = "investigation-protocol-v5"

MAX_REFRESH_FILE_BYTES = 1_000_000
MAX_PRIOR_EVIDENCE_HYDRATION_BYTES = 16 * 1024 * 1024
MAX_PRIOR_EVIDENCE_HYDRATION_SECONDS = 2.0
MAX_REFRESH_CONTENT_CACHE_BYTES = 32_000_000
MAX_FACTS_PER_FILE = 256
MAX_ANCHOR_INPUTS = 50
MAX_ANCHOR_INPUT_BYTES = 24_000
MAX_ANCHOR_CANDIDATES = 50
MAX_HYPOTHESES = 100
MAX_EXACT_ANCHOR_QUERIES = 16
MAX_COMPOUND_ANCHOR_QUERIES = 12
MAX_LINEAGE_PATH_QUERIES = 4
MAX_FLOW_SEEDS = 24
MAX_FLOW_DEPTH = 3
MAX_FLOW_BRANCH = 8
MAX_FLOW_STEPS = 96
MAX_FLOW_DB_QUERIES = 40
MAX_RUNTIME_DB_OPERATIONS = 128
MAX_SLICE_INPUT_BYTES = 64_000
MAX_SLICE_STATEMENTS = 160
DEFAULT_MAX_WAVES = 3
HARD_MAX_WAVES = 4
EXECUTION_EDGE_TYPES = (
    "DEFINES", "CALLS", "IMPLEMENTS", "EXTENDS", "REFERENCES", "EXPOSES_ENDPOINT",
    "CALLS_ENDPOINT", "PUBLISHES", "CONSUMES", "READS_CONFIG", "WRITES_TABLE",
    "READS_TABLE", "DEPENDS_ON_REPO",
)

_TOKEN = re.compile(r"[A-Za-z_$][A-Za-z0-9_$.-]*")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_JAVA_SUFFIXES = {".java", ".kt", ".kts", ".groovy"}
_CONFIG_SUFFIXES = {".properties", ".yaml", ".yml", ".toml", ".xml"}


def _hash(*values: object) -> str:
    joined = "\0".join(str(value) for value in values)
    return "sha256:" + hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _normalize(value: object) -> str:
    text = str(value or "").strip().strip("`'\"")
    text = re.sub(r"\s+", " ", text)
    return text.casefold()


def _config_assignments(path: str, content: str) -> list[tuple[str, str, int, int]]:
    """Return syntactic scalar assignments while skipping literal/continuation bodies."""
    suffix = Path(path).suffix.lower()
    rows: list[tuple[str, str, int, int]] = []
    lines = content.splitlines(keepends=True)
    offset = 0
    if suffix in {".properties", ".ini", ".cfg", ".conf"}:
        continuation = False
        for line_number, raw in enumerate(lines, 1):
            stripped = raw.lstrip()
            continued = raw.rstrip("\r\n").rstrip().endswith("\\")
            if continuation:
                continuation = continued
                offset += len(raw)
                continue
            match = re.match(r"^\s*([A-Za-z0-9_.-]{1,300})\s*[=:]\s*([^\r\n]*)", raw)
            if match and not stripped.startswith(("#", "!")) and not continued:
                rows.append((match.group(1), match.group(2).strip(), line_number, offset + match.start(1)))
            continuation = continued
            offset += len(raw)
        return rows
    if suffix in {".yaml", ".yml"}:
        parents: list[tuple[int, str]] = []
        block_indent: int | None = None
        for line_number, raw in enumerate(lines, 1):
            indent = len(raw) - len(raw.lstrip(" "))
            if block_indent is not None:
                if not raw.strip() or indent > block_indent:
                    offset += len(raw)
                    continue
                block_indent = None
            match = re.match(r"^(\s*)([A-Za-z0-9_.-]{1,300})\s*:\s*([^#\r\n]*)", raw)
            if match and not raw.lstrip().startswith("-"):
                indent = len(match.group(1).replace("\t", "  "))
                while parents and parents[-1][0] >= indent:
                    parents.pop()
                key = ".".join([*(item[1] for item in parents), match.group(2)])
                value = match.group(3).strip()
                if re.fullmatch(r"[|>][0-9+-]*", value):
                    block_indent = indent
                else:
                    rows.append((key, value, line_number, offset + match.start(2)))
                    if not value:
                        parents.append((indent, match.group(2)))
            offset += len(raw)
        return rows
    if suffix == ".toml":
        section = ""
        multiline: str | None = None
        for line_number, raw in enumerate(lines, 1):
            if multiline is not None:
                if multiline in raw and raw.count(multiline) % 2:
                    multiline = None
                offset += len(raw)
                continue
            heading = re.match(r"^\s*\[([A-Za-z0-9_.-]{1,300})\]\s*(?:#.*)?$", raw)
            if heading:
                section = heading.group(1)
            else:
                match = re.match(r"^\s*([A-Za-z0-9_.-]{1,300})\s*=\s*([^\r\n]*)", raw)
                if match:
                    value = match.group(2).strip()
                    delimiter = next((token for token in ('"""', "'''") if token in value), None)
                    if delimiter is not None and value.count(delimiter) % 2:
                        multiline = delimiter
                    elif delimiter is None:
                        key = ".".join(part for part in (section, match.group(1)) if part)
                        rows.append((key, value, line_number, offset + match.start(1)))
            offset += len(raw)
    return rows


def _boolean_assignments(path: str, content: str, key: str) -> set[bool]:
    """Extract only syntax-backed boolean assignments from supported source forms."""
    suffix = Path(path).suffix.lower()
    if suffix == ".groovy":
        return set()
    escaped = re.escape(key)
    if suffix in _JAVA_SUFFIXES:
        source = _mask_java_comments(content, strings=True)
        matches = re.finditer(
            rf"(?im)^\s*(?:[A-Za-z_$][\w$<>?,.\[\] ]+\s+)?{escaped}\s*=\s*(true|false)\b",
            source,
        )
    elif suffix in {".properties", ".ini", ".cfg", ".conf", ".yaml", ".yml", ".toml"}:
        return {
            value.casefold() in {"true", "enabled"}
            for found_key, value, _, _ in _config_assignments(path, content)
            if _normalize(found_key) == _normalize(key)
            and re.fullmatch(r"(?i:true|false|enabled|disabled)", value)
        }
    else:
        return set()
    return {
        match.group(1).casefold() in {"true", "enabled"}
        for match in matches
    }


def _prefetch_compatibility_identity(
    generation: AtlasGenerationRef, prefetch: dict[str, Any],
) -> str | None:
    sequences: dict[str, list[str]] = {}
    for key, limit in PREFETCH_SEQUENCE_BOUNDS.items():
        raw = prefetch.get(key)
        if not isinstance(raw, list) or len(raw) > limit:
            return None
        values = [value for value in raw if isinstance(value, str) and value and len(value) <= 1_000]
        if len(values) != len(raw) or len(set(values)) != len(values):
            return None
        sequences[key] = values
    anchor_status = prefetch.get("anchor_status")
    if anchor_status not in {"ready", "degraded", "unavailable"}:
        return None
    logical = {
        "schema": PREFETCH_SCHEMA_VERSION,
        "generation": generation.identity,
        "objective": str(prefetch.get("objective") or "").strip(),
        "hierarchy": generation.component("hierarchy").get("content_hash"),
        "runtime_anchors": generation.component("runtime_anchors").get("content_hash"),
        "anchor_status": anchor_status,
        **sequences,
    }
    return "sha256:" + hashlib.sha256(
        json.dumps(logical, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _valid_prefetch_envelope(generation: AtlasGenerationRef, prefetch: Any, objective: str) -> bool:
    if not isinstance(prefetch, dict) or prefetch.get("status") != "ready":
        return False
    try:
        if int(prefetch.get("generation") or -1) != generation.generation:
            return False
    except (TypeError, ValueError):
        return False
    if (
        prefetch.get("schema_version") != PREFETCH_SCHEMA_VERSION
        or prefetch.get("atlas_generation_id") != generation.identity
    ):
        return False
    prefetch_objective = str(prefetch.get("objective") or "").strip()
    stopwords = {
        "trace", "find", "locate", "establish", "investigate", "determine", "verify", "issue",
        "failure", "failed", "behavior", "implementation", "service", "flow", "test", "tests",
    }
    prefetched_terms = {term for term in _compound_terms(prefetch_objective) if len(term) >= 4 and term not in stopwords}
    objective_terms = {term for term in _compound_terms(objective) if len(term) >= 4 and term not in stopwords}
    overlap = prefetched_terms & objective_terms
    similarity = len(overlap) / max(1, len(prefetched_terms | objective_terms))
    if (
        not prefetch_objective
        or (_normalize(prefetch_objective) != _normalize(objective) and (not overlap or similarity < .34))
    ):
        return False
    expected = _prefetch_compatibility_identity(generation, prefetch)
    return prefetch.get("compatibility_identity") == expected


def _line(content: str, position: int) -> int:
    return content.count("\n", 0, position) + 1


def _mask_java_comments(content: str, *, strings: bool = False) -> str:
    """Mask comments, and optionally literals, without changing source offsets."""
    output = list(content)
    index = 0
    state = "code"
    quote = ""
    while index < len(content):
        current = content[index]
        following = content[index + 1] if index + 1 < len(content) else ""
        if state == "code":
            if current == "/" and following == "/":
                output[index] = output[index + 1] = " "
                index += 2
                state = "line"
                continue
            if current == "/" and following == "*":
                output[index] = output[index + 1] = " "
                index += 2
                state = "block"
                continue
            if content.startswith('"""', index):
                quote = '"""'
                state = "string"
                if strings:
                    output[index:index + 3] = [" ", " ", " "]
                index += 3
                continue
            if current in {'"', "'"}:
                quote = current
                state = "string"
                if strings:
                    output[index] = " "
        elif state == "line":
            if current == "\n":
                state = "code"
            else:
                output[index] = " "
        elif state == "block":
            if current == "*" and following == "/":
                output[index] = output[index + 1] = " "
                index += 2
                state = "code"
                continue
            if current != "\n":
                output[index] = " "
        elif state == "string":
            if quote == '"""' and content.startswith('"""', index):
                if strings:
                    output[index:index + 3] = [" ", " ", " "]
                index += 3
                state = "code"
                continue
            if strings and current != "\n":
                output[index] = " "
            if current == "\\" and following:
                if strings and following != "\n":
                    output[index + 1] = " "
                index += 2
                continue
            if quote != '"""' and current == quote:
                state = "code"
        index += 1
    return "".join(output)


def _bounded_source(path: Path) -> tuple[str, bool]:
    try:
        if path.is_symlink() or not path.is_file():
            return "", False
        with path.open("rb") as source:
            raw = source.read(MAX_REFRESH_FILE_BYTES + 1)
    except OSError:
        return "", False
    truncated = len(raw) > MAX_REFRESH_FILE_BYTES
    return raw[:MAX_REFRESH_FILE_BYTES].decode("utf-8", errors="replace"), truncated


def _runtime_anchor_identity(item: dict[str, Any]) -> str:
    return _hash(
        "anchor", RUNTIME_ANCHOR_SCHEMA_VERSION, item["kind"], item["value"], item["normalized"],
        item["repo"], item.get("module_id") or "", item.get("entity_id") or "", item["path"],
        max(1, int(item["line"])), item["blob_sha"], item["confidence"], item["method"],
        json.dumps(item.get("provenance") or {}, sort_keys=True),
    )


def _validated_runtime_anchor_row(
    row: tuple[Any, ...], *, method: str, score: float,
) -> dict[str, Any] | None:
    """Canonicalize one full registered anchor row or reject it as poisoned."""
    try:
        provenance = json.loads(row[12])
        if not isinstance(provenance, dict):
            return None
        identity_item = {
            "kind": row[1], "value": row[2], "normalized": row[3], "repo": row[4],
            "module_id": row[5], "entity_id": row[6], "path": row[7], "line": row[8],
            "blob_sha": row[9], "confidence": row[10], "method": row[11],
            "provenance": provenance,
        }
        expected = _runtime_anchor_identity(identity_item)
        if str(row[0]) != expected or str(row[13]) != expected:
            return None
        return {
            "identity": expected, "kind": str(row[1]), "value": str(row[2]), "repo": str(row[4]),
            "module_id": row[5], "entity_id": row[6], "path": str(row[7]), "line": int(row[8]),
            "confidence": round(float(row[10]) * score, 6), "method": method,
            "extraction_method": str(row[11]), "provenance": provenance,
            "evidence_authority": "atlas_candidate",
        }
    except (IndexError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _integration_fact_identity(item: dict[str, Any]) -> str:
    return _hash(
        "integration", JAVA_INTELLIGENCE_SCHEMA_VERSION, item["kind"], item["key"], item["normalized"],
        item["repo"], item.get("module_id") or "", item.get("entity_id") or "", item["path"],
        max(1, int(item["line"])), item["blob_sha"], item["direction"], item["framework"],
        item["confidence"], json.dumps(item.get("provenance") or {}, sort_keys=True),
    )


def _anchor(
    *, kind: str, value: str, repo: str, path: str, line: int, blob_sha: str,
    module_id: str | None = None, entity_id: str | None = None,
    confidence: float = 1.0, method: str = "deterministic_extract",
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    normalized = _normalize(value)
    if not normalized or len(normalized.encode("utf-8")) > 1_000:
        return None
    value = value[:1_000]
    normalized_confidence = max(0.0, min(1.0, confidence))
    normalized_provenance = provenance or {}
    item = {
        "kind": kind, "value": value, "normalized": normalized,
        "repo": repo, "module_id": module_id, "entity_id": entity_id, "path": path,
        "line": max(1, int(line)), "blob_sha": blob_sha, "confidence": normalized_confidence,
        "method": method, "provenance": normalized_provenance,
    }
    identity = _runtime_anchor_identity(item)
    return {"anchor_id": identity, **item, "fingerprint": identity}


def _fact(
    *, kind: str, key: str, repo: str, path: str, line: int, blob_sha: str,
    direction: str = "reference", framework: str = "java", module_id: str | None = None,
    entity_id: str | None = None, confidence: float = 1.0,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    normalized = _normalize(key)
    if not normalized or len(normalized.encode("utf-8")) > 1_000:
        return None
    key = key[:1_000]
    normalized_confidence = max(0.0, min(1.0, confidence))
    normalized_provenance = provenance or {}
    item = {
        "kind": kind, "key": key, "normalized": normalized,
        "repo": repo, "module_id": module_id, "entity_id": entity_id, "path": path,
        "line": max(1, int(line)), "blob_sha": blob_sha, "direction": direction,
        "framework": framework, "confidence": normalized_confidence, "provenance": normalized_provenance,
    }
    identity = _integration_fact_identity(item)
    return {"fact_id": identity, **item, "fingerprint": identity}


def _quoted_values(value: str) -> list[str]:
    return [match.group(1) for match in re.finditer(r"[\"']([^\"']{1,500})[\"']", value)]


def _annotation_values(body: str, names: set[str]) -> list[str]:
    """Return only positional or explicitly named annotation string values."""
    assignments = list(re.finditer(r"(?:^|,)\s*([A-Za-z_$][\w$]*)\s*=", body))
    result: list[str] = []
    if not assignments:
        return _quoted_values(body)
    positional = body[:assignments[0].start()].strip().strip(",")
    result.extend(_quoted_values(positional))
    for index, match in enumerate(assignments):
        if match.group(1) not in names:
            continue
        end = assignments[index + 1].start() if index + 1 < len(assignments) else len(body)
        result.extend(_quoted_values(body[match.end():end]))
    return list(dict.fromkeys(result))


def _java_file_intelligence(
    repo: str, path: str, blob_sha: str, module_id: str | None, content: str,
    entities: Iterable[dict[str, Any]] = (),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract bounded, provenance-backed Java/Spring routing facts."""
    anchors: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    source = _mask_java_comments(content)
    code_only = _mask_java_comments(content, strings=True)
    # Masking preserves offsets by replacing comments with whitespace.  Regexes
    # do not need to rescan a large trailing comment after it has been masked,
    # while the untrimmed views remain authoritative for offset checks.
    search_source = source.rstrip()
    structurally_exact = Path(path).suffix.lower() != ".groovy"
    scoped_entities = sorted(
        (
            item for item in entities
            if str(item.get("repo")) == repo and str(item.get("path")) == path
            and item.get("kind") != "file"
        ),
        key=lambda item: (int(item.get("line_start") or 1), int(item.get("line_end") or 1)),
    )

    def entity_at(position: int, *, method_annotation: bool = False) -> str | None:
        line = _line(content, position)
        enclosed = [
            item for item in scoped_entities
            if int(item.get("line_start") or 1) <= line <= int(item.get("line_end") or 1)
        ]
        owner = max(
            enclosed,
            key=lambda item: (int(item.get("line_start") or 1), item.get("kind") in {"method", "constructor"}),
            default=None,
        )
        if method_annotation and owner is not None and owner.get("kind") not in {"method", "constructor"}:
            following = next((
                item for item in scoped_entities
                if item.get("kind") in {"method", "constructor"}
                and item.get("parent_entity_id") == owner.get("entity_id")
                and line <= int(item.get("line_start") or 1) <= line + 8
            ), None)
            if following is not None:
                owner = following
        if owner is None:
            owner = next((
                item for item in scoped_entities
                if line <= int(item.get("line_start") or 1) <= line + 8
                and item.get("kind") in {"class", "interface", "type", "test"}
            ), None)
        return str(owner.get("entity_id")) if owner and owner.get("entity_id") else None

    def add_anchor(
        kind: str, value: str, position: int, confidence: float = 1.0, *, entity_id: str | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> None:
        if len(anchors) >= MAX_FACTS_PER_FILE or not code_only[position:position + 1].strip():
            return
        item = _anchor(
            kind=kind, value=value, repo=repo, path=path, line=_line(content, position),
            blob_sha=blob_sha, module_id=module_id, entity_id=entity_id or entity_at(position), confidence=confidence,
            provenance={
                "extractor": JAVA_INTELLIGENCE_SCHEMA_VERSION, "exact_source": structurally_exact,
                **(provenance or {}),
            },
        )
        if item:
            anchors.append(item)

    def add_fact(
        kind: str, key: str, position: int, direction: str = "reference", confidence: float = 1.0,
        framework: str = "spring",
        method_annotation: bool = False,
        provenance: dict[str, Any] | None = None,
    ) -> None:
        if len(facts) >= MAX_FACTS_PER_FILE or not code_only[position:position + 1].strip():
            return
        entity_id = entity_at(position, method_annotation=method_annotation)
        if kind == "endpoint":
            endpoint_entity = next((
                item for item in scoped_entities
                if item.get("kind") == "endpoint"
                and _normalize(item.get("simple_name")) == _normalize(key)
                and int(item.get("line_start") or 1) == _line(content, position)
            ), None)
            if endpoint_entity is not None:
                entity_id = str(endpoint_entity["entity_id"])
        item = _fact(
            kind=kind, key=key, repo=repo, path=path, line=_line(content, position),
            blob_sha=blob_sha, direction=direction, framework=framework, module_id=module_id,
            entity_id=entity_id,
            confidence=confidence,
            provenance={
                "extractor": JAVA_INTELLIGENCE_SCHEMA_VERSION, "exact_source": structurally_exact,
                "direction": direction, "framework": framework, **(provenance or {}),
            },
        )
        if item:
            facts.append(item)
            add_anchor(
                kind, key, position, confidence, entity_id=entity_id,
                provenance={"direction": direction, "framework": framework, **(provenance or {})},
            )

    for match in re.finditer(r"(?m)^[ \t]*package\s+([A-Za-z_$][\w$.]*)\s*;", search_source):
        add_anchor("package", match.group(1), match.start())
    for match in re.finditer(r"(?m)\b(?:class|interface|record|enum)\s+([A-Za-z_$][\w$]*)", search_source):
        add_anchor("symbol", match.group(1), match.start())
        if match.group(1).endswith(("Event", "Message", "Command")):
            add_fact("event", match.group(1), match.start(), "definition", .95)
    for match in re.finditer(
        r"@(Service|Component|Repository|Configuration)\b(?:\([^)]*\))?[\s\S]{0,300}?"
        r"\b(?:class|interface)\s+([A-Za-z_$][\w$]*)",
        search_source,
    ):
        add_fact("spring_component", match.group(2), match.start(), "definition", .98)
    for match in re.finditer(r"(?m)\bstatic\s+final\s+\w+(?:<[^;=]+>)?\s+([A-Za-z_$][\w$]*)\s*=\s*([^;]+)", search_source):
        name, value = match.group(1), match.group(2)
        add_anchor("constant", name, match.start(), .95)
        quoted = _quoted_values(value)
        if quoted and any(token in name.upper() for token in ("TOPIC", "QUEUE", "EVENT")):
            add_fact("topic", quoted[0], match.start(), "definition", .9, "spring-kafka")

    class_declarations = [
        match for match in re.finditer(r"\b(?:class|interface|record|enum)\s+[A-Za-z_$][\w$]*", search_source)
        if code_only[match.start():match.start() + 1].strip()
    ]

    class_entity_kinds = {"class", "interface", "type", "test"}

    def annotated_class(position: int) -> int | None:
        annotation_line = _line(content, position)
        inside_existing_class = any(
            item.get("kind") in class_entity_kinds
            and int(item.get("line_start") or 1) <= annotation_line <= int(item.get("line_end") or 1)
            and any(
                declaration.start() < position
                and _line(content, declaration.start()) == int(item.get("line_start") or 1)
                for declaration in class_declarations
            )
            for item in scoped_entities
        )
        if inside_existing_class:
            return None
        return next(
            (match.start() for match in class_declarations if position <= match.start() <= position + 500),
            None,
        )

    class_mappings: dict[int, str] = {}
    class_mapping_starts: set[int] = set()
    for mapping in re.finditer(r"@RequestMapping\s*\(([^)]*)\)", search_source):
        if not code_only[mapping.start():mapping.start() + 1].strip():
            continue
        class_position = annotated_class(mapping.end())
        if class_position is None:
            continue
        values = _quoted_values(mapping.group(1))
        class_mappings[class_position] = values[0] if values else ""
        class_mapping_starts.add(mapping.start())
    feign_classes: dict[int, str | None] = {}
    for match in re.finditer(r"@FeignClient\s*\(([^)]*)\)", search_source):
        if not code_only[match.start():match.start() + 1].strip():
            continue
        class_position = annotated_class(match.end())
        if class_position is None:
            continue
        body = match.group(1)
        targets = re.findall(r"(?:name|value)\s*=\s*[\"']([^\"']+)[\"']", body)
        if not targets and re.match(r"\s*[\"']", body):
            targets = _quoted_values(body)[:1]
        feign_classes[class_position] = targets[0] if targets else None
    endpoint_pattern = re.compile(
        r"@(GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping|RequestMapping)\s*(?:\(([^)]*)\))?"
    )
    for match in endpoint_pattern.finditer(search_source):
        # A class-level RequestMapping is a prefix, not an endpoint on its own.
        if match.start() in class_mapping_starts:
            continue
        class_position = next(
            (item.start() for item in reversed(class_declarations) if item.start() <= match.start()),
            None,
        )
        class_mapping = class_mappings.get(class_position or -1, "")
        is_feign = class_position in feign_classes
        values = _quoted_values(match.group(2) or "") or [""]
        for value in values[:4]:
            endpoint = "/" + "/".join(part.strip("/") for part in (class_mapping, value) if part.strip("/"))
            endpoint = endpoint if endpoint != "/" or value or class_mapping else "/"
            add_fact("endpoint", endpoint, match.start(), "outbound" if is_feign else "inbound", 1.0,
                     "spring-feign" if is_feign else "spring-mvc", method_annotation=True,
                     provenance={"target_service": feign_classes.get(class_position)} if is_feign else None)

    for match in re.finditer(r"@FeignClient\s*\(([^)]*)\)", search_source):
        body = match.group(1)
        named = re.findall(r"(?:name|value|url)\s*=\s*[\"']([^\"']+)[\"']", body)
        for value in (named or _quoted_values(body))[:4]:
            kind = "endpoint" if value.startswith(("http://", "https://", "/", "${")) else "service"
            add_fact(kind, value, match.start(), "outbound", .95, "spring-feign")

    for match in re.finditer(r"@KafkaListener\s*\(([^)]*)\)", search_source):
        for value in _annotation_values(match.group(1), {"topics", "topicPattern"})[:8]:
            add_fact("topic", value, match.start(), "inbound", .95, "spring-kafka", method_annotation=True)
    for match in re.finditer(r"\b(?:kafkaTemplate|KafkaTemplate)\s*\.\s*send\s*\(([^,\n)]+)", search_source):
        values = _quoted_values(match.group(1))
        if values:
            add_fact("topic", values[0], match.start(), "outbound", 1.0, "spring-kafka")
        else:
            token = match.group(1).strip()
            if re.fullmatch(r"[A-Za-z_$][\w$.]*", token):
                add_fact("topic", token, match.start(), "outbound", .7, "spring-kafka")

    for match in re.finditer(r"@Value\s*\(\s*[\"']\$\{([^}:]+)", search_source):
        add_fact("config_key", match.group(1), match.start(), "read", 1.0, method_annotation=True)
    for match in re.finditer(r"@ConfigurationProperties\s*\(([^)]*)\)", search_source):
        values = _quoted_values(match.group(1))
        if values:
            add_fact("config_key", values[0], match.start(), "prefix", .95)
    for match in re.finditer(r"@Cache(?:able|Evict|Put)\s*\(([^)]*)\)", search_source):
        for value in _annotation_values(match.group(1), {"value", "cacheNames"})[:4]:
            add_fact("cache", value, match.start(), "read_or_write", .9, "spring-cache", method_annotation=True)

    for match in re.finditer(r"@Table\s*\(([^)]*)\)", search_source):
        body = match.group(1)
        for field, value in re.findall(r"(name|schema)\s*=\s*[\"']([^\"']+)[\"']", body):
            add_fact("table" if field == "name" else "schema", value, match.start(), "persistence", 1.0, "jpa")
    for match in re.finditer(r"\b([A-Za-z_$][\w$]*)Repository\s+extends\s+(?:Jpa|Crud|PagingAndSorting)Repository\s*<\s*([A-Za-z_$][\w$]*)", search_source):
        add_fact("persistence_entity", match.group(2), match.start(), "repository", 1.0, "spring-data-jpa")

    if is_test_path(path):
        for match in re.finditer(r"\b(?:MockMvc|Mockito|WebTestClient|TestRestTemplate|assertThat|verify)\b", search_source):
            add_fact("test_reference", match.group(0), match.start(), "test", .9, "spring-test")
        for match in re.finditer(r"\b(?:get|post|put|delete|patch)\s*\(\s*[\"']([^\"']+)[\"']", search_source):
            add_fact("endpoint", match.group(1), match.start(), "test", .9, "spring-test")

    return anchors, facts


def _config_file_intelligence(
    repo: str, path: str, blob_sha: str, module_id: str | None, content: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    anchors: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    matches = [(key, position) for key, _, _, position in _config_assignments(path, content)]
    # XML configuration is deliberately omitted: without namespace-aware Spring
    # semantics, quoted attributes are not authoritative configuration keys.
    for key, position in matches:
        if len(facts) >= MAX_FACTS_PER_FILE:
            break
        item = _fact(
            kind="config_key", key=key, repo=repo, path=path, line=_line(content, position),
            blob_sha=blob_sha, direction="definition", framework="configuration", module_id=module_id,
            confidence=.9, provenance={"extractor": JAVA_INTELLIGENCE_SCHEMA_VERSION, "exact_source": True},
        )
        anchor = _anchor(
            kind="config_key", value=key, repo=repo, path=path, line=_line(content, position),
            blob_sha=blob_sha, module_id=module_id, confidence=.9,
            provenance={"extractor": JAVA_INTELLIGENCE_SCHEMA_VERSION, "exact_source": True},
        )
        if item:
            facts.append(item)
        if anchor:
            anchors.append(anchor)
    return anchors, facts


def build_generation_intelligence(
    settings: Settings,
    *,
    current_files: dict[tuple[str, str], str],
    unchanged: Iterable[tuple[str, str]],
    parent_generation: int | None,
    modules: Iterable[dict[str, Any]],
    entities: Iterable[dict[str, Any]],
    source_contents: dict[tuple[str, str], tuple[str, bool]] | None = None,
    snapshots: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build or reuse bounded refresh-time facts for one Atlas generation."""
    unchanged_set = set(unchanged)
    entity_rows = list(entities)
    module_rows = list(modules)
    module_by_path = {(str(item["repo"]), str(item["path"])): str(item["module_id"]) for item in module_rows}
    entities_by_file: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entity in entity_rows:
        entities_by_file.setdefault((str(entity["repo"]), str(entity["path"])), []).append(entity)

    def module_for_file(repo: str, path: str) -> str | None:
        file_entity = next(
            (item for item in entities_by_file.get((repo, path), ()) if item.get("kind") == "file"),
            None,
        )
        if file_entity is not None and file_entity.get("module_id"):
            return str(file_entity["module_id"])
        parent = Path(path).parent
        # File depth bounds this lookup and avoids file x module fan-out.
        for _ in range(64):
            key = "." if str(parent) in {"", "."} else parent.as_posix()
            if (repo, key) in module_by_path:
                return module_by_path[(repo, key)]
            if key == ".":
                break
            parent = parent.parent
        return None
    anchors: dict[str, dict[str, Any]] = {}
    facts: dict[str, dict[str, Any]] = {}
    reused_files: set[tuple[str, str]] = set()

    if parent_generation is not None and unchanged_set:
        connection = connect(settings)
        try:
            component_rows = {
                str(row[0]): (str(row[1]), str(row[2]))
                for row in connection.execute(
                    "SELECT component,schema_version,status FROM generation_components "
                    "WHERE generation=? AND component IN ('runtime_anchors','java_intelligence')",
                    (parent_generation,),
                )
            }
            parent_compatible = component_rows == {
                "runtime_anchors": (RUNTIME_ANCHOR_SCHEMA_VERSION, "ready"),
                "java_intelligence": (JAVA_INTELLIGENCE_SCHEMA_VERSION, "ready"),
            }
            if parent_compatible:
                for repo, path, blob_sha in connection.execute(
                    "SELECT repo,path,blob_sha FROM generation_intelligence_files "
                    "WHERE generation=? AND schema_version=?",
                    (parent_generation, GENERATION_INTELLIGENCE_INPUT_SCHEMA_VERSION),
                ):
                    key = (str(repo), str(path))
                    if key in unchanged_set and current_files.get(key) == str(blob_sha):
                        reused_files.add(key)
            for row in (() if not parent_compatible else connection.execute(
                "SELECT a.anchor_id,a.kind,a.value,a.normalized,a.repo,a.module_id,a.entity_id,a.path,a.line,"
                "a.blob_sha,a.confidence,a.method,a.provenance_json,a.fingerprint "
                "FROM generation_runtime_anchors g JOIN atlas_runtime_anchors a ON a.anchor_id=g.anchor_id "
                "WHERE g.generation=?", (parent_generation,),
            )):
                if (str(row[4]), str(row[7])) not in reused_files:
                    continue
                item = {
                    "anchor_id": row[0], "kind": row[1], "value": row[2], "normalized": row[3],
                    "repo": row[4], "module_id": row[5], "entity_id": row[6], "path": row[7],
                    "line": row[8], "blob_sha": row[9], "confidence": row[10], "method": row[11],
                    "provenance": json.loads(row[12]), "fingerprint": row[13],
                }
                anchors[str(row[0])] = item
            for row in (() if not parent_compatible else connection.execute(
                "SELECT f.fact_id,f.kind,f.key_value,f.normalized,f.repo,f.module_id,f.entity_id,f.path,f.line,"
                "f.blob_sha,f.direction,f.framework,f.confidence,f.provenance_json,f.fingerprint "
                "FROM generation_integration_facts g JOIN atlas_integration_facts f ON f.fact_id=g.fact_id "
                "WHERE g.generation=?", (parent_generation,),
            )):
                if (str(row[4]), str(row[7])) not in reused_files:
                    continue
                item = {
                    "fact_id": row[0], "kind": row[1], "key": row[2], "normalized": row[3],
                    "repo": row[4], "module_id": row[5], "entity_id": row[6], "path": row[7],
                    "line": row[8], "blob_sha": row[9], "direction": row[10], "framework": row[11],
                    "confidence": row[12], "provenance": json.loads(row[13]), "fingerprint": row[14],
                }
                facts[str(row[0])] = item
        finally:
            connection.close()

    processed_files = set(reused_files)
    processed_files.update(
        (str(entity["repo"]), str(entity["path"]))
        for entity in entity_rows
        if (
            str(entity.get("kind") or "") == "file"
            and current_files.get((str(entity["repo"]), str(entity["path"]))) == str(entity.get("blob_sha") or "")
        )
    )

    # Entity anchors reuse the normalized Atlas hierarchy and are content
    # addressed, so generating these rows is O(entity count) with no file I/O.
    for entity in entity_rows:
        item = _anchor(
            kind="symbol", value=str(entity.get("qualified_name") or entity.get("simple_name") or ""),
            repo=str(entity["repo"]), path=str(entity["path"]), line=int(entity.get("line_start") or 1),
            blob_sha=str(entity["blob_sha"]), module_id=str(entity.get("module_id") or "") or None,
            entity_id=str(entity.get("entity_id") or "") or None, confidence=1.0, method="atlas_entity_exact",
            provenance={"extractor": str(entity.get("extractor") or "atlas"), "exact_source": True},
        )
        if item:
            anchors[item["anchor_id"]] = item

    parsed_files = 0
    truncated_files = 0
    source_cache_hits = 0
    pending_authoritative: dict[tuple[str, str], str] = {}

    def parse_source(repo: str, path: str, blob_sha: str, content: str, truncated: bool) -> None:
        nonlocal parsed_files, truncated_files
        suffix = Path(path).suffix.lower()
        processed_files.add((repo, path))
        parsed_files += 1
        if not content:
            return
        truncated_files += int(truncated)
        module_id = module_for_file(repo, path)
        if suffix in _JAVA_SUFFIXES:
            file_anchors, file_facts = _java_file_intelligence(
                repo, path, blob_sha, module_id, content, entities_by_file.get((repo, path), ()),
            )
        else:
            file_anchors, file_facts = _config_file_intelligence(repo, path, blob_sha, module_id, content)
        anchors.update({item["anchor_id"]: item for item in file_anchors})
        facts.update({item["fact_id"]: item for item in file_facts})

    for (repo, path), blob_sha in sorted(current_files.items()):
        suffix = Path(path).suffix.lower()
        if suffix not in _JAVA_SUFFIXES | _CONFIG_SUFFIXES or (repo, path) in reused_files:
            continue
        cached_source = (source_contents or {}).get((repo, path))
        if cached_source is not None:
            content, truncated = cached_source
            source_cache_hits += 1
            parse_source(repo, path, blob_sha, content, truncated)
        elif snapshots is not None:
            pending_authoritative[(repo, path)] = blob_sha
        else:
            # Direct unit-scale callers without a published lexical generation
            # retain the bounded local-input helper. Atlas publication always
            # passes snapshots and can never reach this path.
            source = (settings.repo(repo).scan_path / path).resolve()
            root = settings.repo(repo).scan_path.resolve()
            if source.is_relative_to(root) and source.is_file():
                content, truncated = _bounded_source(source)
                parse_source(repo, path, blob_sha, content, truncated)

    if pending_authoritative:
        from .index import indexed_snapshot_contents

        for (repo, path), full_content in indexed_snapshot_contents(
            settings, pending_authoritative, snapshots or {},
        ):
            raw = full_content.encode("utf-8")
            bounded = raw[:MAX_REFRESH_FILE_BYTES]
            parse_source(
                repo, path, pending_authoritative[(repo, path)],
                bounded.decode("utf-8", errors="replace"), len(raw) > MAX_REFRESH_FILE_BYTES,
            )

    payload = {
        "runtime_anchors": sorted(anchors.values(), key=lambda item: str(item["anchor_id"])),
        "integration_facts": sorted(facts.values(), key=lambda item: str(item["fact_id"])),
        "v1_files": [
            {"repo": repo, "path": path, "blob_sha": current_files[(repo, path)],
             "schema_version": GENERATION_INTELLIGENCE_INPUT_SCHEMA_VERSION}
            for repo, path in sorted(processed_files)
        ],
        "v1_build": {
            "parsed_files": parsed_files, "reused_files": len(reused_files), "truncated_files": truncated_files,
            "source_cache_hits": source_cache_hits,
            "max_file_bytes": MAX_REFRESH_FILE_BYTES, "max_facts_per_file": MAX_FACTS_PER_FILE,
        },
    }
    validate_generation_intelligence(
        payload, set(repo for repo, _ in current_files), modules=module_rows, entities=entity_rows,
    )
    return payload


def validate_generation_intelligence(
    payload: dict[str, Any], repositories: set[str], *,
    modules: Iterable[dict[str, Any]], entities: Iterable[dict[str, Any]],
) -> None:
    anchors = payload.get("runtime_anchors")
    facts = payload.get("integration_facts")
    files = payload.get("v1_files")
    if not isinstance(anchors, list) or not isinstance(facts, list) or not isinstance(files, list):
        raise ValueError("v1 generation intelligence payload is incomplete")
    module_by_id = {str(item["module_id"]): item for item in modules}
    entity_by_id = {str(item["entity_id"]): item for item in entities}
    file_by_key = {
        (str(item["repo"]), str(item["path"])): item
        for item in entity_by_id.values() if str(item.get("kind") or "") == "file"
    }
    file_keys: set[tuple[str, str]] = set()
    file_blobs: dict[tuple[str, str], str] = {}
    for item in files:
        if not isinstance(item, dict) or not {"repo", "path", "blob_sha", "schema_version"}.issubset(item):
            raise ValueError("v1 generation intelligence file membership is invalid")
        key = (str(item["repo"]), str(item["path"]))
        source = file_by_key.get(key)
        if (
            key in file_keys or key[0] not in repositories
            or str(item["schema_version"]) != GENERATION_INTELLIGENCE_INPUT_SCHEMA_VERSION
            or source is None or str(source.get("blob_sha") or "") != str(item["blob_sha"])
        ):
            raise ValueError("v1 generation intelligence file membership is incompatible")
        file_keys.add(key)
        file_blobs[key] = str(item["blob_sha"])

    for collection, identity, required in (
        (anchors, "anchor_id", {"anchor_id", "kind", "normalized", "repo", "path", "line", "blob_sha"}),
        (facts, "fact_id", {"fact_id", "kind", "normalized", "repo", "path", "line", "blob_sha"}),
    ):
        identifiers: set[str] = set()
        for item in collection:
            if not isinstance(item, dict) or not required.issubset(item):
                raise ValueError("v1 generation intelligence row is invalid")
            identifier = str(item[identity])
            if identifier in identifiers or str(item["repo"]) not in repositories:
                raise ValueError("v1 generation intelligence identity or source scope is invalid")
            if collection is anchors:
                expected = _runtime_anchor_identity(item)
            else:
                expected = _integration_fact_identity(item)
            if identifier != expected or str(item.get("fingerprint") or "") != expected:
                raise ValueError("v1 generation intelligence content identity is invalid")
            key = (str(item["repo"]), str(item["path"]))
            source = file_by_key.get(key)
            try:
                line = int(item["line"])
                line_end = int((source or {}).get("line_end") or 0)
            except (TypeError, ValueError):
                line, line_end = 0, 0
            module_id = str(item.get("module_id") or "")
            entity_id = str(item.get("entity_id") or "")
            module = module_by_id.get(module_id) if module_id else None
            entity = entity_by_id.get(entity_id) if entity_id else None
            if (
                key not in file_keys or source is None or file_blobs.get(key) != str(item["blob_sha"])
                or line < 1 or line > line_end
                or (
                    module_id and (
                        module is None or str(module.get("repo") or "") != key[0]
                        or module_id != str(source.get("module_id") or "")
                    )
                )
                or (
                    entity_id and (
                        entity is None or str(entity.get("repo") or "") != key[0]
                        or str(entity.get("path") or "") != key[1]
                        or str(entity.get("blob_sha") or "") != str(item["blob_sha"])
                        or str(entity.get("module_id") or "") != str(source.get("module_id") or "")
                    )
                )
            ):
                raise ValueError("v1 generation intelligence source membership is invalid")
            identifiers.add(identifier)


def generation_component_manifests(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    anchors = payload.get("runtime_anchors") or []
    facts = payload.get("integration_facts") or []
    files = payload.get("v1_files") or []
    bounds = {"max_file_bytes": MAX_REFRESH_FILE_BYTES, "max_facts_per_file": MAX_FACTS_PER_FILE}
    return {
        "runtime_anchors": {
            "schema_version": RUNTIME_ANCHOR_SCHEMA_VERSION, "status": "ready",
            "content_hash": _content_hash({"anchors": anchors, "input_files": files}),
            "details": {
                "count": len(anchors), "input_files": len(files), "bounded_input": bounds,
                "term_schema_version": RUNTIME_ANCHOR_TERM_SCHEMA_VERSION,
            },
        },
        "java_intelligence": {
            "schema_version": JAVA_INTELLIGENCE_SCHEMA_VERSION, "status": "ready",
            "content_hash": _content_hash({"facts": facts, "input_files": files}),
            "details": {"count": len(facts), "input_files": len(files), "bounded_input": bounds},
        },
    }


def _bounded_anchor_inputs(values: Iterable[object]) -> list[str]:
    result: list[str] = []
    byte_count = 0
    for raw in values:
        value = str(raw or "").strip()
        size = len(value.encode("utf-8"))
        if not value or size > 1_000 or len(result) >= MAX_ANCHOR_INPUTS or byte_count + size > MAX_ANCHOR_INPUT_BYTES:
            continue
        if value not in result:
            result.append(value)
            byte_count += size
    return result


def _bounded_hypothesis_inputs(values: Iterable[object]) -> list[str]:
    result: list[str] = []
    byte_count = 0
    for raw in values:
        value = str(raw or "").strip()
        size = len(value.encode("utf-8"))
        if not value or size > 1_000 or len(result) >= MAX_HYPOTHESES or byte_count + size > MAX_ANCHOR_INPUT_BYTES:
            continue
        if value not in result:
            result.append(value)
            byte_count += size
    return result


def _bounded_anchor_queries(values: Iterable[object]) -> list[tuple[str | None, str]]:
    result: list[tuple[str | None, str]] = []
    byte_count = 0
    for raw in values:
        kind: str | None = None
        value: str
        if isinstance(raw, dict):
            kind = str(raw.get("kind") or "").strip() or None
            value = str(raw.get("value") or "").strip()
        else:
            value = str(raw or "").strip()
        size = len(value.encode("utf-8"))
        item = (kind, value)
        if (
            not value or size > 1_000 or len(result) >= MAX_ANCHOR_INPUTS
            or byte_count + size > MAX_ANCHOR_INPUT_BYTES
        ):
            continue
        if item not in result:
            result.append(item)
            byte_count += size
    return result


def _component_identity(generation: AtlasGenerationRef, name: str, schema: str) -> str:
    component = generation.component(name)
    return _hash(schema, generation.identity, component.get("schema_version"), component.get("content_hash"))


_ANCHOR_CACHE_SEALS: dict[tuple[str, int, str], str] = {}


def _anchor_cache_seal(
    settings: Settings,
    generation: int,
    cache_key: str,
    payload_hash: str,
    compatibility_identity: str,
    *,
    create: bool,
) -> str | None:
    secret = _route_cache_secret(settings, create=create)
    if secret is None:
        return None
    message = "\0".join((
        RUNTIME_ANCHOR_SCHEMA_VERSION, RUNTIME_ANCHOR_TERM_SCHEMA_VERSION,
        str(generation), cache_key, payload_hash, compatibility_identity,
    )).encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def _compound_terms(value: str) -> list[str]:
    expanded = _CAMEL.sub(" ", value.replace("/", " ").replace(".", " ").replace("_", " ").replace("-", " "))
    return [token.casefold() for token in _TOKEN.findall(expanded) if len(token) >= 3][:12]


def resolve_runtime_anchors(
    settings: Settings,
    generation: AtlasGenerationRef,
    values: Iterable[object],
    *,
    limit: int = MAX_ANCHOR_CANDIDATES,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Resolve only within the explicitly pinned generation; never substitute current."""
    limit = max(1, min(int(limit), MAX_ANCHOR_CANDIDATES))
    component = generation.component("runtime_anchors")
    component_details = component.get("details") if isinstance(component.get("details"), dict) else {}
    if (
        component.get("status") != "ready"
        or component.get("schema_version") != RUNTIME_ANCHOR_SCHEMA_VERSION
        or component_details.get("term_schema_version") != RUNTIME_ANCHOR_TERM_SCHEMA_VERSION
    ):
        return {
            "status": "degraded", "reason": "runtime-anchor component is unavailable for the pinned generation",
            "generation": generation.generation, "schema_version": RUNTIME_ANCHOR_SCHEMA_VERSION,
            "compatibility_identity": _component_identity(generation, "runtime_anchors", RUNTIME_ANCHOR_SCHEMA_VERSION),
            "candidates": [], "inputs": [], "cache_hit": False,
        }
    query_inputs = _bounded_anchor_queries(values)
    inputs = [value for _, value in query_inputs]
    if not query_inputs:
        return {"status": "ready", "generation": generation.generation,
                "schema_version": RUNTIME_ANCHOR_SCHEMA_VERSION,
                "compatibility_identity": _component_identity(generation, "runtime_anchors", RUNTIME_ANCHOR_SCHEMA_VERSION),
                "candidates": [], "inputs": [], "cache_hit": False}
    exact_specs = list(dict.fromkeys(
        (_normalize(value), kind) for kind, value in query_inputs if _normalize(value)
    ))[:MAX_EXACT_ANCHOR_QUERIES]
    term_specs = list(dict.fromkeys(
        (term, kind) for kind, value in query_inputs for term in _compound_terms(value)
    ))[:MAX_COMPOUND_ANCHOR_QUERIES]
    path_inputs = list(dict.fromkeys(
        normalized for kind, value in query_inputs
        for normalized in [_normalize(value)]
        if normalized and (
            kind == "file_hint"
            or (kind is None and Path(normalized).suffix.lower() in _JAVA_SUFFIXES | _CONFIG_SUFFIXES)
        )
    ))[:MAX_LINEAGE_PATH_QUERIES]
    compatibility = _component_identity(generation, "runtime_anchors", RUNTIME_ANCHOR_SCHEMA_VERSION)
    cache_key = _hash(
        "runtime-anchors", compatibility, limit,
        json.dumps(exact_specs), json.dumps(term_specs), json.dumps(path_inputs),
    )
    seal_key = (str(settings.state_dir.resolve()), generation.generation, cache_key)
    candidates: dict[str, dict[str, Any]] = {}
    poisoned_row = False
    connection = connect(settings)
    now = datetime.now(UTC).isoformat()
    database_operations = 0
    try:
        database_operations += 1
        actual_count = int(connection.execute(
            "SELECT COUNT(*) FROM generation_runtime_anchors WHERE generation=?", (generation.generation,),
        ).fetchone()[0])
        try:
            expected_count = int((component.get("details") or {}).get("count"))
        except (TypeError, ValueError):
            expected_count = -1
        if expected_count < 0 or actual_count != expected_count:
            return {
                "status": "degraded", "reason": "runtime-anchor membership is incompatible with its registered component",
                "generation": generation.generation, "schema_version": RUNTIME_ANCHOR_SCHEMA_VERSION,
                "compatibility_identity": compatibility,
                "candidates": [], "inputs": inputs, "cache_hit": False,
                "database_operations": database_operations,
            }
        database_operations += 2
        if not _valid_term_index(
            connection,
            generation.generation,
            marker_table="generation_runtime_anchor_indexes",
            membership_table="generation_runtime_anchors",
            term_table="atlas_runtime_anchor_terms",
            id_column="anchor_id",
            schema_version=RUNTIME_ANCHOR_TERM_SCHEMA_VERSION,
        ):
            return {
                "status": "degraded",
                "reason": "runtime-anchor content identity or term projection is unavailable or incompatible",
                "generation": generation.generation, "schema_version": RUNTIME_ANCHOR_SCHEMA_VERSION,
                "compatibility_identity": compatibility,
                "candidates": [], "inputs": inputs, "cache_hit": False,
                "database_operations": database_operations,
            }
        database_operations += int(use_cache)
        cached = connection.execute(
            "SELECT c.payload_json,c.payload_hash,r.payload_hash,r.schema_version,r.compatibility_identity,r.seal "
            "FROM atlas_retrieval_cache c LEFT JOIN atlas_retrieval_cache_registrations r "
            "ON r.generation=c.generation AND r.cache_key=c.cache_key "
            "WHERE c.generation=? AND c.cache_key=?",
            (generation.generation, cache_key),
        ).fetchone() if use_cache else None
        if cached:
            try:
                value = json.loads(cached[0])
            except (TypeError, json.JSONDecodeError):
                value = None
            cached_candidates = value.get("candidates") if isinstance(value, dict) else None
            identifiers = [
                str(item.get("identity")) for item in cached_candidates or []
                if isinstance(item, dict) and item.get("identity")
            ]
            try:
                cached_generation = int(value.get("generation") or -1) if isinstance(value, dict) else -1
            except (TypeError, ValueError):
                cached_generation = -1
            valid = (
                isinstance(value, dict)
                and value.get("schema_version") == RUNTIME_ANCHOR_SCHEMA_VERSION
                and value.get("compatibility_identity") == compatibility
                and cached_generation == generation.generation
                and isinstance(cached_candidates, list)
                and len(identifiers) == len(cached_candidates) <= limit
            )
            payload_hash = _hash("runtime-anchor-cache-row", generation.generation, cache_key, str(cached[0]))
            expected_seal = _anchor_cache_seal(
                settings, generation.generation, cache_key, payload_hash, compatibility, create=False,
            )
            valid = bool(
                valid
                and cached[1] == payload_hash
                and cached[2] == payload_hash
                and cached[3] == RUNTIME_ANCHOR_SCHEMA_VERSION
                and cached[4] == compatibility
                and isinstance(cached[5], str)
                and expected_seal is not None
                and hmac.compare_digest(cached[5], expected_seal)
                and len(set(identifiers)) == len(identifiers)
            )
            if valid and identifiers:
                slots = ",".join("?" for _ in identifiers)
                database_operations += 1
                rows = connection.execute(
                    "SELECT a.anchor_id,a.kind,a.value,a.normalized,a.repo,a.module_id,a.entity_id,a.path,a.line,"
                    "a.blob_sha,a.confidence,a.method,a.provenance_json,a.fingerprint "
                    "FROM generation_runtime_anchors g JOIN atlas_runtime_anchors a ON a.anchor_id=g.anchor_id "
                    "JOIN generation_intelligence_files f ON f.generation=g.generation AND f.repo=a.repo "
                    "AND f.path=a.path AND f.blob_sha=a.blob_sha AND f.schema_version=? "
                    f"WHERE g.generation=? AND a.anchor_id IN ({slots})",
                    (
                        GENERATION_INTELLIGENCE_INPUT_SCHEMA_VERSION,
                        generation.generation,
                        *identifiers,
                    ),
                ).fetchall()
                rows_by_id = {str(row[0]): row for row in rows}
                score_by_method = {
                    "exact": 1.0, "compound": .72, "lineage_alias": .86, "path_fallback": .8,
                }
                canonical_candidates: list[dict[str, Any]] = []
                for cached_item in cached_candidates:
                    method = str(cached_item.get("method") or "")
                    row = rows_by_id.get(str(cached_item.get("identity") or ""))
                    canonical_item = (
                        _validated_runtime_anchor_row(
                            row, method=method, score=score_by_method[method],
                        )
                        if row is not None and method in score_by_method else None
                    )
                    if canonical_item is None:
                        valid = False
                        break
                    canonical_candidates.append(canonical_item)
                valid = valid and canonical_candidates == cached_candidates
            if valid:
                if len(_ANCHOR_CACHE_SEALS) >= 10_000:
                    _ANCHOR_CACHE_SEALS.clear()
                _ANCHOR_CACHE_SEALS[seal_key] = payload_hash
                database_operations += 1
                connection.execute(
                    "UPDATE atlas_retrieval_cache SET last_used_at=? WHERE generation=? AND cache_key=?",
                    (now, generation.generation, cache_key),
                )
                connection.commit()
                value["cache_hit"] = True
                value["database_operations"] = database_operations
                return value
            _ANCHOR_CACHE_SEALS.pop(seal_key, None)
            database_operations += 1
            connection.execute(
                "DELETE FROM atlas_retrieval_cache WHERE generation=? AND cache_key=?",
                (generation.generation, cache_key),
            )
            connection.execute(
                "DELETE FROM atlas_retrieval_cache_registrations WHERE generation=? AND cache_key=?",
                (generation.generation, cache_key),
            )
        anchor_columns = (
            "a.anchor_id,a.kind,a.value,a.normalized,a.repo,a.module_id,a.entity_id,a.path,a.line,"
            "a.blob_sha,a.confidence,a.method,a.provenance_json,a.fingerprint "
        )
        anchor_membership = (
            "FROM generation_runtime_anchors g JOIN atlas_runtime_anchors a ON a.anchor_id=g.anchor_id "
            "JOIN generation_intelligence_files f ON f.generation=g.generation AND f.repo=a.repo "
            "AND f.path=a.path AND f.blob_sha=a.blob_sha "
            "AND f.schema_version='" + GENERATION_INTELLIGENCE_INPUT_SCHEMA_VERSION + "' "
        )

        def candidate_from_row(row: tuple[Any, ...], method: str, score: float) -> dict[str, Any] | None:
            nonlocal poisoned_row
            candidate = _validated_runtime_anchor_row(row, method=method, score=score)
            if candidate is None:
                poisoned_row = True
            return candidate

        for method, queries, score in (("exact", exact_specs, 1.0), ("compound", term_specs, .72)):
            for query, required_kind in queries:
                remaining = limit - len(candidates)
                if remaining <= 0:
                    break
                if method == "exact":
                    database_operations += 1
                    sql = (
                        "SELECT " + anchor_columns + anchor_membership +
                        "WHERE g.generation=? AND a.normalized=?"
                    )
                    parameters: tuple[object, ...] = (generation.generation, query)
                else:
                    database_operations += 1
                    sql = (
                        "SELECT " + anchor_columns + anchor_membership +
                        "JOIN atlas_runtime_anchor_terms t ON t.anchor_id=a.anchor_id "
                        "WHERE g.generation=? AND t.schema_version=? AND t.term=?"
                    )
                    parameters = (
                        generation.generation, RUNTIME_ANCHOR_TERM_SCHEMA_VERSION, query,
                    )
                if required_kind is not None:
                    sql += " AND a.kind=?"
                    parameters += (required_kind,)
                query_limit = remaining if method == "exact" else min(remaining, MAX_FLOW_BRANCH)
                sql += " ORDER BY a.confidence DESC,a.repo,a.path,a.line LIMIT ?"
                rows = connection.execute(sql, (*parameters, query_limit))
                for row in rows:
                    item = candidate_from_row(row, method, score)
                    if item is None:
                        continue
                    identifier = str(item["identity"])
                    previous = candidates.get(identifier)
                    if previous is None or item["confidence"] > previous["confidence"]:
                        candidates[identifier] = item
        for method, query, score in (
            (method, query, score)
            for query in path_inputs
            for method, score in (("lineage_alias", .86), ("path_fallback", .8))
        ):
            remaining = limit - len(candidates)
            if remaining <= 0:
                break
            if method == "lineage_alias":
                database_operations += 1
                rows = connection.execute(
                    "SELECT " + anchor_columns +
                    "FROM generation_changes gc JOIN atlas_changes c ON c.change_id=gc.change_id "
                    "JOIN generation_runtime_anchors ga ON ga.generation=gc.generation "
                    "JOIN atlas_runtime_anchors a ON a.anchor_id=ga.anchor_id AND a.repo=c.repo AND a.path=c.path "
                    "JOIN generation_intelligence_files f ON f.generation=ga.generation AND f.repo=a.repo "
                    "AND f.path=a.path AND f.blob_sha=a.blob_sha "
                    "AND f.schema_version='" + GENERATION_INTELLIGENCE_INPUT_SCHEMA_VERSION + "' "
                    "WHERE gc.generation=? AND lower(c.old_path)=? ORDER BY a.confidence DESC,a.path,a.line LIMIT ?",
                    (generation.generation, query, min(remaining, MAX_FLOW_BRANCH)),
                )
            else:
                database_operations += 1
                rows = connection.execute(
                    "SELECT " + anchor_columns + anchor_membership +
                    "WHERE g.generation=? AND lower(a.path)=? ORDER BY a.confidence DESC,a.repo,a.line LIMIT ?",
                    (generation.generation, query, min(remaining, MAX_FLOW_BRANCH)),
                )
            for row in rows:
                item = candidate_from_row(row, method, score)
                if item is None:
                    continue
                identifier = str(item["identity"])
                previous = candidates.get(identifier)
                if previous is None or item["confidence"] > previous["confidence"]:
                    candidates[identifier] = item
        if poisoned_row:
            return {
                "status": "degraded", "reason": "runtime-anchor content identity is incompatible",
                "generation": generation.generation, "schema_version": RUNTIME_ANCHOR_SCHEMA_VERSION,
                "compatibility_identity": compatibility, "candidates": [], "inputs": inputs,
                "cache_hit": False, "database_operations": database_operations,
            }
        ordered = sorted(candidates.values(), key=lambda item: (-item["confidence"], item["repo"], item["path"], item["line"]))[:limit]
        value = {
            "status": "ready", "generation": generation.generation,
            "schema_version": RUNTIME_ANCHOR_SCHEMA_VERSION,
            "compatibility_identity": compatibility,
            "inputs": inputs,
            "candidates": ordered, "ambiguous": len([item for item in ordered if item["confidence"] >= .9]) > 1,
            "bounds": {
                "input_items": len(inputs), "candidate_limit": limit,
                "compound_terms": len(term_specs), "lineage_path_inputs": len(path_inputs),
            }, "cache_hit": False,
            "database_operations": database_operations,
        }
        if use_cache:
            database_operations += 2
            payload_json = json.dumps(value, sort_keys=True)
            payload_hash = _hash("runtime-anchor-cache-row", generation.generation, cache_key, payload_json)
            connection.execute(
                "INSERT OR REPLACE INTO atlas_retrieval_cache"
                "(generation,cache_key,payload_json,payload_hash,created_at,last_used_at) VALUES (?,?,?,?,?,?)",
                (generation.generation, cache_key, payload_json, payload_hash, now, now),
            )
            connection.execute(
                "INSERT OR REPLACE INTO atlas_retrieval_cache_registrations"
                "(generation,cache_key,schema_version,compatibility_identity,payload_hash,seal,registered_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    generation.generation, cache_key, RUNTIME_ANCHOR_SCHEMA_VERSION,
                    compatibility, payload_hash,
                    _anchor_cache_seal(
                        settings, generation.generation, cache_key, payload_hash,
                        compatibility, create=True,
                    ),
                    now,
                ),
            )
            connection.execute(
                "DELETE FROM atlas_retrieval_cache WHERE rowid IN ("
                "SELECT rowid FROM atlas_retrieval_cache ORDER BY last_used_at DESC LIMIT -1 OFFSET 10000)"
            )
            connection.commit()
            if len(_ANCHOR_CACHE_SEALS) >= 10_000:
                _ANCHOR_CACHE_SEALS.clear()
            _ANCHOR_CACHE_SEALS[seal_key] = payload_hash
        return value
    finally:
        connection.close()


def _pinned_repository_evidence(bundle: ContextBundle, evidence: Any) -> bool:
    generation = bundle.atlas_generation
    return bool(
        generation is not None
        and evidence.repo in generation.snapshots
        and evidence.kind not in {"knowledge", "local diff", "user-supplied external evidence"}
        and evidence.path != "(working tree diff)"
    )


def _verified_location(bundle: ContextBundle, repo: str, path: str, line: int) -> bool:
    return any(
        _pinned_repository_evidence(bundle, item)
        and item.repo == repo and item.path == path and item.line_start <= line <= item.line_end
        for item in bundle.evidence
    )


def _verified_value_location(
    bundle: ContextBundle, repo: str, path: str, line: int, value: object, *, kind: str = "",
) -> bool:
    normalized = _normalize(value).casefold()
    if not normalized:
        return False
    for evidence in bundle.evidence:
        if not (
            _pinned_repository_evidence(bundle, evidence)
            and evidence.repo == repo and evidence.path == path
            and evidence.line_start <= line <= evidence.line_end
        ):
            continue
        if kind == "file_hint" and normalized in evidence.path.casefold():
            return True
        suffix = Path(evidence.path).suffix.lower()
        # Groovy slashy, dollar-slashy and triple-single strings require a full
        # Groovy lexer. Until then its extracted framework facts are navigation
        # candidates and may not be promoted to exact structural evidence.
        if suffix == ".groovy":
            continue
        if kind in {"endpoint", "topic", "event", "queue"} and suffix not in _JAVA_SUFFIXES:
            continue
        structural_content = evidence.verification_content or (
            evidence.content if evidence.line_start == 1 else None
        )
        if structural_content is None:
            continue
        comment_aware_content = _mask_java_comments(structural_content) if suffix in _JAVA_SUFFIXES else structural_content
        code_only_content = _mask_java_comments(structural_content, strings=True) if suffix in _JAVA_SUFFIXES else structural_content
        if kind == "endpoint" and suffix == ".java":
            _, extracted = _java_file_intelligence(repo, path, "verification", None, structural_content)
            if any(
                item.get("kind") == "endpoint"
                and _normalize(item.get("key")) == normalized
                and int(item.get("line") or 1) == line
                and bool((item.get("provenance") or {}).get("exact_source"))
                for item in extracted
            ):
                return True
        lines = comment_aware_content.splitlines()
        code_lines = code_only_content.splitlines()
        relative = line - 1
        local = "\n".join(lines[max(0, relative - 1):relative + 2])
        comment_aware = local
        code_only = "\n".join(code_lines[max(0, relative - 1):relative + 2])
        local_folded = comment_aware.casefold()
        if normalized in local_folded:
            if kind == "endpoint":
                return bool(re.search(r"@(Get|Post|Put|Delete|Patch|Request)Mapping\b|@FeignClient\b", code_only))
            if kind in {"topic", "event"}:
                return bool(re.search(r"@KafkaListener\b|\bkafkaTemplate\s*\.\s*send\s*\(|\b(?:class|record|interface)\s+", code_only))
            if kind == "queue":
                return bool(re.search(r"@RabbitListener\b|\bqueue\b", code_only, re.I))
            if kind == "config_key":
                if suffix in _JAVA_SUFFIXES:
                    return bool(re.search(r"@Value\b|@ConfigurationProperties\b", code_only))
                return any(
                    _normalize(found_key) == normalized and found_line == line
                    for found_key, _, found_line, _ in _config_assignments(evidence.path, structural_content)
                )
            if kind in {"symbol", "class", "interface", "method", "function"}:
                simple = re.split(r"[.$#:]", normalized)[-1]
                return bool(simple and re.search(
                    rf"\b(?:class|interface|record|enum|def|function|fun|func)?\s*{re.escape(simple)}\s*(?:\(|\{{|\b)",
                    code_only, re.I,
                ))
            return normalized in local_folded
        if kind in {"symbol", "class", "interface", "method", "function"}:
            simple = re.split(r"[.$#:]", normalized)[-1]
            if simple and re.search(rf"\b{re.escape(simple)}\s*(?:\(|\{{|\b)", code_only, re.I):
                return True
    return False


def _verified_anchor(bundle: ContextBundle, item: dict[str, Any]) -> bool:
    if item.get("evidence_authority") == "exact_source":
        return True
    return _verified_value_location(
        bundle, str(item.get("repo")), str(item.get("path")), int(item.get("line") or 1),
        item.get("value"), kind=str(item.get("kind") or ""),
    )


def _is_runtime_entry_anchor(item: dict[str, Any]) -> bool:
    """Return whether an exact anchor denotes an inbound production boundary."""
    kind = str(item.get("kind") or "")
    if kind == "stack_frame":
        return True
    provenance = item.get("provenance")
    return bool(
        kind in {"endpoint", "event", "topic", "queue"}
        and isinstance(provenance, dict)
        and provenance.get("direction") == "inbound"
    )


def _verified_stack_frame(value: str, evidence: Any) -> tuple[bool, int]:
    match = re.search(
        r"(?P<qualified>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+)\((?P<file>[^():/\\]+):(?P<line>[0-9]+)\)",
        value,
    )
    if not match:
        return False, -1
    if Path(evidence.path).suffix.lower() == ".groovy":
        return False, -1
    line = int(match.group("line"))
    if Path(evidence.path).name.casefold() != match.group("file").casefold():
        return False, -1
    if not evidence.line_start <= line <= evidence.line_end:
        return False, -1
    parts = match.group("qualified").split(".")
    method = parts[-1].casefold()
    declaring_parts = [part for part in parts[-2].split("$") if part and not part.isdigit()]
    declaring_class = (declaring_parts[-1] if declaring_parts else parts[-2]).casefold()
    structural_content = evidence.verification_content or (
        evidence.content if evidence.line_start == 1 else None
    )
    if structural_content is None:
        return False, -1
    source = (
        _mask_java_comments(structural_content, strings=True)
        if Path(evidence.path).suffix.lower() in _JAVA_SUFFIXES else structural_content
    )
    lines = source.splitlines()
    relative = line - 1
    local = "\n".join(lines[max(0, relative - 3):relative + 4]).casefold()
    if not re.search(rf"\b{re.escape(method)}\s*\(", local):
        return False, -1
    line_offsets = [0]
    for source_line in source.splitlines(keepends=True):
        line_offsets.append(line_offsets[-1] + len(source_line))
    if line < 1 or line >= len(line_offsets):
        return False, -1
    target_position = line_offsets[line - 1]
    owning_classes: list[tuple[int, str]] = []
    for declaration in re.finditer(r"\b(?:class|interface|record|enum)\s+([A-Za-z_$][\w$]*)", source):
        opening = source.find("{", declaration.end(), min(len(source), declaration.end() + 1_000))
        if opening < 0 or opening > target_position:
            continue
        depth = 0
        closing = -1
        for position_in_source in range(opening, len(source)):
            if source[position_in_source] == "{":
                depth += 1
            elif source[position_in_source] == "}":
                depth -= 1
                if depth == 0:
                    closing = position_in_source
                    break
        if closing >= target_position:
            owning_classes.append((opening, declaration.group(1).casefold()))
    if not owning_classes or max(owning_classes)[1] != declaring_class:
        return False, -1
    position = structural_content.casefold().find(method, max(0, relative - 3))
    window_position = evidence.content.casefold().find(method)
    return True, window_position if window_position >= 0 else position


def _exact_evidence_anchors(
    request: dict[str, Any], bundle: ContextBundle, generation: AtlasGenerationRef,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    searchable_evidence = [evidence for evidence in bundle.evidence if _pinned_repository_evidence(bundle, evidence)]
    for requested in (request.get("anchors") or [])[:MAX_ANCHOR_INPUTS]:
        if not isinstance(requested, dict):
            continue
        kind = str(requested.get("kind") or "")
        value = str(requested.get("value") or "").strip()
        normalized = _normalize(value)
        terms = [term for term in _compound_terms(value) if len(term) >= 4]
        for evidence in searchable_evidence:
            searchable_content = evidence.content.casefold()
            exact_content = bool(normalized and normalized in searchable_content)
            exact_path = bool(kind == "file_hint" and normalized and normalized in evidence.path.casefold())
            exact_stack, stack_position = _verified_stack_frame(value, evidence) if kind == "stack_frame" else (False, -1)
            content_position = searchable_content.find(normalized) if exact_content else -1
            content_line = evidence.line_start + (
                evidence.content.count("\n", 0, content_position) if content_position >= 0 else 0
            )
            exact_structural = exact_content and _verified_value_location(
                bundle, evidence.repo, evidence.path, content_line, value, kind=kind,
            )
            exact = exact_structural or exact_path or exact_stack
            compound_matches = sum(term in searchable_content for term in terms)
            if not exact and (not terms or compound_matches < min(2, len(terms))):
                continue
            position = content_position if exact_structural else stack_position
            line = (
                int(re.search(r":([0-9]+)\)$", value).group(1))
                if exact_stack else evidence.line_start + (evidence.content.count("\n", 0, position) if position >= 0 else 0)
            )
            method = "exact_lexical_verified" if exact else "compound_lexical_candidate"
            identity = _hash(
                "evidence-anchor", generation.identity, method, kind, normalized,
                evidence.repo, evidence.path, line,
            )
            result.append({
                "identity": identity, "kind": kind, "value": value, "repo": evidence.repo,
                "module_id": None, "entity_id": None, "path": evidence.path, "line": line,
                "confidence": 1.0 if exact else .7, "method": method,
                "extraction_method": "query_time_exact_source" if exact else "query_time_compound_candidate",
                "provenance": {"exact_source": exact, "generation": generation.generation},
                "evidence_authority": "exact_source" if exact else "inferred_candidate",
            })
            if len(result) >= MAX_ANCHOR_CANDIDATES:
                return result
    return result


def _execution_flow(
    settings: Settings, generation: AtlasGenerationRef, seeds: list[str], bundle: ContextBundle,
) -> dict[str, Any]:
    component = generation.component("typed_graph")
    compatibility = _component_identity(generation, "typed_graph", EXECUTION_FLOW_SCHEMA_VERSION)
    if component.get("status") != "ready" or component.get("schema_version") != ATLAS_SCHEMA_VERSION:
        return {"schema_version": EXECUTION_FLOW_SCHEMA_VERSION, "compatibility_identity": compatibility,
                "generation": generation.generation, "status": "degraded",
                "reason": "typed graph is unavailable or incompatible for the pinned generation", "steps": [],
                "database_operations": 0}
    seeds = list(dict.fromkeys(value for value in seeds if value))[:MAX_FLOW_SEEDS]
    if not seeds:
        return {"schema_version": EXECUTION_FLOW_SCHEMA_VERSION, "compatibility_identity": compatibility,
                "generation": generation.generation, "status": "degraded", "reason": "no anchored entities", "steps": [],
                "database_operations": 0}
    connection = connect(settings)
    steps: list[dict[str, Any]] = []
    frontier = list(seeds)
    seen = set(seeds)
    database_operations = 0
    try:
        for depth in range(MAX_FLOW_DEPTH):
            next_frontier: list[str] = []
            for source_id in frontier[:MAX_FLOW_SEEDS]:
                if database_operations >= MAX_FLOW_DB_QUERIES:
                    break
                database_operations += 1
                rows = connection.execute(
                    "SELECT e.edge_id,e.edge_type,e.source_id,e.target_id,e.repo,e.path,e.line_start,e.confidence,"
                    "t.simple_name,t.module_id FROM generation_edges g JOIN atlas_edges e ON e.edge_id=g.edge_id "
                    "LEFT JOIN atlas_entities t ON t.entity_id=e.target_id "
                    f"WHERE g.generation=? AND e.source_id=? AND e.edge_type IN ({','.join('?' for _ in EXECUTION_EDGE_TYPES)}) "
                    "ORDER BY e.confidence DESC,e.edge_type,e.target_id LIMIT ?",
                    (generation.generation, source_id, *EXECUTION_EDGE_TYPES, MAX_FLOW_BRANCH),
                ).fetchall()
                valid_edges = _valid_generation_edges(
                    connection, generation.generation, (str(row[0]) for row in rows),
                )
                valid_targets = _valid_generation_entities(
                    connection, generation.generation, (str(row[3]) for row in rows),
                )
                database_operations += 5
                if any(str(row[0]) not in valid_edges or str(row[3]) not in valid_targets for row in rows):
                    return {
                        "schema_version": EXECUTION_FLOW_SCHEMA_VERSION,
                        "compatibility_identity": compatibility,
                        "generation": generation.generation,
                        "status": "degraded",
                        "reason": "typed graph content identity is incompatible",
                        "steps": [], "paths": [], "database_operations": database_operations,
                    }
                for row in rows:
                    edge = valid_edges[str(row[0])]
                    target_entity = valid_targets[str(row[3])]
                    target = str(edge["target_id"])
                    if target in seen:
                        continue
                    state = "verified" if _verified_value_location(
                        bundle, str(edge["repo"]), str(edge["path"]), int(edge["line_start"]),
                        str(target_entity["simple_name"] or target), kind="symbol",
                    ) else "candidate"
                    steps.append({
                        "identity": str(edge["edge_id"]), "order": len(steps) + 1, "depth": depth,
                        "edge_type": str(edge["edge_type"]), "source_id": str(edge["source_id"]),
                        "target_id": target, "target": str(target_entity["simple_name"] or target),
                        "module_id": target_entity["module_id"], "repo": str(edge["repo"]),
                        "path": str(edge["path"]), "line": int(edge["line_start"]),
                        "confidence": float(edge["confidence"]),
                        "state": state, "evidence_authority": "exact_source" if state == "verified" else "atlas_candidate",
                    })
                    if target not in seen:
                        seen.add(target)
                        next_frontier.append(target)
                    if len(steps) >= MAX_FLOW_STEPS:
                        break
                if len(steps) >= MAX_FLOW_STEPS:
                    break
            frontier = next_frontier
            if not frontier or len(steps) >= MAX_FLOW_STEPS or database_operations >= MAX_FLOW_DB_QUERIES:
                break
    finally:
        connection.close()
    paths = _execution_paths(steps)
    return {
        "schema_version": EXECUTION_FLOW_SCHEMA_VERSION, "compatibility_identity": compatibility,
        "generation": generation.generation, "status": "ready" if steps else "degraded",
        "reason": None if steps else "no bounded Atlas path from resolved anchors", "steps": steps,
        "paths": paths,
        "order_semantics": "static source-to-target graph paths; never runtime chronology",
        "bounds": {"seed_limit": MAX_FLOW_SEEDS, "depth": MAX_FLOW_DEPTH, "branch": MAX_FLOW_BRANCH, "step_limit": MAX_FLOW_STEPS,
                   "database_query_limit": MAX_FLOW_DB_QUERIES},
        "database_operations": database_operations,
    }


def _execution_paths(steps: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    values = [item for item in steps if isinstance(item, dict) and item.get("identity")]
    by_source: dict[str, list[dict[str, Any]]] = {}
    targets = {str(item.get("target_id")) for item in values}
    for item in values:
        by_source.setdefault(str(item.get("source_id")), []).append(item)
    starts = [item for item in values if str(item.get("source_id")) not in targets] or values
    result: dict[str, dict[str, Any]] = {}

    def walk(path: list[dict[str, Any]]) -> None:
        if len(result) >= 20:
            return
        tail = path[-1]
        identities = [str(item["identity"]) for item in path]
        verified = all(item.get("state") == "verified" for item in path)
        if verified and len(path) >= 2:
            identity = _hash("execution-path", *identities)
            result[identity] = {
                "identity": identity, "step_ids": identities, "length": len(path),
                "state": "verified", "evidence_authority": "exact_source",
            }
        used = {str(item["identity"]) for item in path}
        children = [
            item for item in by_source.get(str(tail.get("target_id")), [])
            if str(item["identity"]) not in used and len(path) < MAX_FLOW_DEPTH
        ]
        if children:
            for item in children[:MAX_FLOW_BRANCH]:
                walk([*path, item])
            return
        identity = _hash("execution-path", *identities)
        result[identity] = {
            "identity": identity, "step_ids": identities, "length": len(path),
            "state": "verified" if verified else "candidate",
            "evidence_authority": "exact_source" if verified else "atlas_candidate",
        }

    for start in starts[:MAX_FLOW_BRANCH]:
        walk([start])
    return sorted(result.values(), key=lambda item: (-int(item["length"]), str(item["identity"])))[:20]


def _integration_flow(
    settings: Settings, generation: AtlasGenerationRef, anchors: list[dict[str, Any]], bundle: ContextBundle,
) -> dict[str, Any]:
    component = generation.component("java_intelligence")
    compatibility = _component_identity(generation, "java_intelligence", INTEGRATION_FLOW_SCHEMA_VERSION)
    if component.get("status") != "ready" or component.get("schema_version") != JAVA_INTELLIGENCE_SCHEMA_VERSION:
        return {"schema_version": INTEGRATION_FLOW_SCHEMA_VERSION, "compatibility_identity": compatibility,
                "generation": generation.generation, "status": "degraded",
                "reason": "Java integration intelligence is unavailable for the pinned generation",
                "steps": [], "repositories": [], "database_operations": 0}
    keys = list(dict.fromkeys(_normalize(item.get("value")) for item in anchors if item.get("value")))[:MAX_FLOW_SEEDS]
    rows: list[tuple[Any, ...]] = []
    connection = connect(settings)
    database_operations = 0
    try:
        database_operations += 1
        actual_count = int(connection.execute(
            "SELECT COUNT(*) FROM generation_integration_facts WHERE generation=?", (generation.generation,),
        ).fetchone()[0])
        try:
            expected_count = int((component.get("details") or {}).get("count"))
        except (TypeError, ValueError):
            expected_count = -1
        if expected_count < 0 or actual_count != expected_count:
            return {"schema_version": INTEGRATION_FLOW_SCHEMA_VERSION, "compatibility_identity": compatibility,
                    "generation": generation.generation, "status": "degraded",
                    "reason": "integration-fact membership is incompatible with its registered component",
                    "steps": [], "repositories": [], "database_operations": database_operations}
        for key in keys:
            database_operations += 1
            rows.extend(connection.execute(
                "SELECT f.fact_id,f.kind,f.key_value,f.normalized,f.repo,f.module_id,f.entity_id,f.path,f.line,"
                "f.blob_sha,f.direction,f.framework,f.confidence,f.provenance_json,f.fingerprint "
                "FROM generation_integration_facts g JOIN atlas_integration_facts f ON f.fact_id=g.fact_id "
                "JOIN generation_intelligence_files i ON i.generation=g.generation AND i.repo=f.repo "
                "AND i.path=f.path AND i.blob_sha=f.blob_sha AND i.schema_version=? "
                "WHERE g.generation=? AND f.normalized=? ORDER BY f.repo,f.path,f.line LIMIT ?",
                (GENERATION_INTELLIGENCE_INPUT_SCHEMA_VERSION, generation.generation, key, MAX_FLOW_BRANCH * 4),
            ))
    finally:
        connection.close()
    validated_rows: list[tuple[Any, ...]] = []
    for row in rows:
        try:
            provenance = json.loads(row[13])
            identity_item = {
                "kind": row[1], "key": row[2], "normalized": row[3], "repo": row[4],
                "module_id": row[5], "entity_id": row[6], "path": row[7], "line": row[8],
                "blob_sha": row[9], "direction": row[10], "framework": row[11],
                "confidence": row[12], "provenance": provenance,
            }
            expected = _integration_fact_identity(identity_item)
        except (TypeError, ValueError, json.JSONDecodeError):
            expected = ""
        if str(row[0]) != expected or str(row[14]) != expected:
            return {
                "schema_version": INTEGRATION_FLOW_SCHEMA_VERSION, "compatibility_identity": compatibility,
                "generation": generation.generation, "status": "degraded",
                "reason": "integration-fact content identity is incompatible",
                "steps": [], "repositories": [], "database_operations": database_operations,
            }
        validated_rows.append(row)
    direction_order = {"definition": 0, "outbound": 1, "inbound": 2, "read": 3, "repository": 4, "test": 5, "reference": 6}
    unique = {str(row[0]): row for row in validated_rows}
    ordered = sorted(unique.values(), key=lambda row: (str(row[2]).casefold(), direction_order.get(str(row[10]), 9), str(row[4]), str(row[7]), int(row[8])))[:MAX_FLOW_STEPS]
    steps = []
    for row in ordered:
        state = "verified" if _verified_value_location(
            bundle, str(row[4]), str(row[7]), int(row[8]), row[2], kind=str(row[1]),
        ) else "candidate"
        steps.append({
            "identity": str(row[0]), "order": len(steps) + 1, "kind": str(row[1]), "key": str(row[2]),
            "repo": str(row[4]), "module_id": row[5], "entity_id": row[6], "path": str(row[7]),
            "line": int(row[8]), "direction": str(row[10]), "framework": str(row[11]),
            "confidence": float(row[12]), "state": state,
            "provenance": json.loads(row[13]),
            "evidence_authority": "exact_source" if state == "verified" else "atlas_candidate",
        })
    repos = {item["repo"] for item in steps}
    return {
        "schema_version": INTEGRATION_FLOW_SCHEMA_VERSION,
        "compatibility_identity": compatibility, "generation": generation.generation,
        "status": "ready" if len(repos) > 1 else "degraded",
        "reason": None if len(repos) > 1 else "cross-repository integration is not established",
        "steps": steps, "repositories": sorted(repos), "bounds": {"key_limit": MAX_FLOW_SEEDS, "step_limit": MAX_FLOW_STEPS},
        "database_operations": database_operations,
    }


def _program_slice(bundle: ContextBundle) -> dict[str, Any]:
    statements: list[dict[str, Any]] = []
    consumed = 0
    unsupported: set[str] = set()
    for evidence in bundle.evidence:
        if not _pinned_repository_evidence(bundle, evidence):
            continue
        suffix = Path(evidence.path).suffix.lower()
        if suffix not in {".java", ".kt", ".kts", ".py"}:
            unsupported.add(suffix or "unknown")
            continue
        remaining = MAX_SLICE_INPUT_BYTES - consumed
        if remaining <= 0 or len(statements) >= MAX_SLICE_STATEMENTS:
            break
        raw = evidence.content.encode("utf-8")[:remaining]
        content = raw.decode("utf-8", errors="ignore")
        consumed += len(raw)
        for offset, source_line in enumerate(content.splitlines()[:160]):
            stripped = source_line.strip()
            if not stripped:
                continue
            kind = None
            if re.search(r"\b(if|else if|switch|when|for|while)\b", stripped):
                kind = "guard_or_branch"
            elif re.search(r"\b(return|yield)\b", stripped):
                kind = "return"
            elif re.search(r"\b(throw|raise)\b", stripped):
                kind = "throw"
            elif re.search(r"(?:\w+\.)?\w+\s*\(", stripped):
                kind = "important_call"
            elif re.search(r"(?:=|\+=|-=|\+\+|--)", stripped):
                kind = "definition_or_mutation"
            if kind:
                identity = _hash("slice", evidence.repo, evidence.path, evidence.line_start + offset, stripped)
                statements.append({
                    "identity": identity, "kind": kind, "repo": evidence.repo, "path": evidence.path,
                    "line": evidence.line_start + offset, "summary": stripped[:500], "state": "candidate",
                    "source_verified": True, "evidence_authority": "derived_navigation_only",
                })
                if len(statements) >= MAX_SLICE_STATEMENTS:
                    break
    return {
        "schema_version": PROGRAM_SLICE_SCHEMA_VERSION,
        "status": "ready" if statements else "degraded", "statements": statements,
        "unsupported_languages": sorted(unsupported),
        "bounds": {"input_bytes": consumed, "max_input_bytes": MAX_SLICE_INPUT_BYTES, "statement_limit": MAX_SLICE_STATEMENTS},
        "authority": "Exact source remains final evidence authority; this slice is navigation-only.",
    }


def _surfaces(bundle: ContextBundle, integrations: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, dict[tuple[str, str], dict[str, Any]]] = {
        name: {} for name in ("implementation", "test", "impact", "contract", "config_data")
    }
    for evidence in bundle.evidence:
        key = (evidence.repo, evidence.path)
        if not _pinned_repository_evidence(bundle, evidence):
            continue
        suffix = Path(evidence.path).suffix.lower()
        surface = "test" if is_test_path(evidence.path) else "config_data" if suffix in _CONFIG_SUFFIXES | {".sql", ".avsc", ".proto", ".graphql", ".graphqls"} else "implementation"
        values[surface][key] = {
            "repo": evidence.repo, "path": evidence.path, "state": "verified", "confidence": 1.0,
            "evidence_authority": "exact_source",
        }
    for step in integrations.get("steps") or []:
        key = (str(step["repo"]), str(step["path"]))
        if step["kind"] == "test_reference" or step.get("direction") == "test":
            surface = "test"
        elif step["kind"] in {"config_key", "table", "schema", "persistence_entity", "cache"}:
            surface = "config_data"
        elif step["kind"] in {"spring_component", "dependency"}:
            surface = "implementation"
        else:
            surface = "contract"
        values[surface].setdefault(key, {
            "repo": key[0], "path": key[1], "state": step["state"], "confidence": step["confidence"],
            "evidence_authority": step["evidence_authority"],
        })
        values["impact"].setdefault(key, {
            "repo": key[0], "path": key[1], "state": step["state"], "confidence": step["confidence"],
            "evidence_authority": step["evidence_authority"],
        })
    return {
        "schema_version": SURFACE_SCHEMA_VERSION, "status": "ready",
        **{name: sorted(rows.values(), key=lambda item: (item["repo"], item["path"]))[:200] for name, rows in values.items()},
        "bounds": {"items_per_surface": 200},
    }


def _allocate(registry: dict[str, dict[str, str]], kind: str, identity: str, prefix: str, width: int) -> str:
    values = registry.setdefault(kind, {})
    if identity not in values:
        suffixes = [
            int(match.group(1)) for value in values.values()
            if (match := re.fullmatch(re.escape(prefix) + r"([0-9]+)", str(value)))
        ]
        candidate = max(suffixes, default=0) + 1
        public_id = f"{prefix}{candidate:0{width}d}"
        while public_id in values.values():
            candidate += 1
            public_id = f"{prefix}{candidate:0{width}d}"
        values[identity] = public_id
    return values[identity]


def validate_stable_identity_registry(state: dict[str, Any]) -> None:
    """Fail closed when persisted Protocol-v5 public identity lineage is corrupt."""
    formats = {
        "evidence": r"E[0-9]{4,}", "anchors": r"A[0-9]{3,}", "flows": r"F[0-9]{3,}",
        "hypotheses": r"H[0-9]{3,}", "blockers": r"B[0-9]{3,}", "contexts": r"CTX-[0-9]{3,}",
    }
    raw = state.get("stable_identities")
    if raw is None:
        return
    if not isinstance(raw, dict) or any(name not in formats for name in raw):
        raise ValueError("Protocol v5 stable identity registry has an invalid namespace")
    registry: dict[str, dict[str, str]] = {}
    for name, value in raw.items():
        if not isinstance(value, dict):
            raise ValueError("Protocol v5 stable identity registry namespace is invalid")
        normalized = {str(identity): str(public) for identity, public in value.items()}
        if (
            any(not identity or not re.fullmatch(formats[name], public) for identity, public in normalized.items())
            or len(set(normalized.values())) != len(normalized)
        ):
            raise ValueError("Protocol v5 stable identity registry contains invalid or duplicate public IDs")
        registry[name] = normalized

    def require(name: str, identity: object, public: object) -> None:
        identity_value, public_value = str(identity or ""), str(public or "")
        if identity_value and public_value and (registry.get(name) or {}).get(identity_value) != public_value:
            raise ValueError("Protocol v5 persisted public ID does not match its stable identity")

    runtime = state.get("investigation_runtime") if isinstance(state.get("investigation_runtime"), dict) else {}
    for item in (runtime.get("anchors") or {}).get("candidates") or []:
        if isinstance(item, dict):
            require("anchors", item.get("identity"), item.get("anchor_id"))
    generation_identity = str(state.get("atlas_generation_id") or "")
    for name, key in (("execution", "execution_flow"), ("integration", "integration_flow")):
        flow = runtime.get(key) if isinstance(runtime.get(key), dict) else {}
        if flow.get("flow_id") and generation_identity:
            require("flows", _hash("ticket-flow", generation_identity, name), flow.get("flow_id"))
    for item in (runtime.get("hypothesis_ledger") or {}).get("items") or []:
        if isinstance(item, dict):
            require("hypotheses", item.get("identity"), item.get("hypothesis_id"))
    for item in (runtime.get("evidence_frontier") or {}).get("items") or []:
        if isinstance(item, dict):
            require("blockers", item.get("identity"), item.get("blocker_id"))
    evidence_values = set((registry.get("evidence") or {}).values())
    for record in state.get("evidence_records") or []:
        if isinstance(record, dict) and record.get("public_id") and str(record["public_id"]) not in evidence_values:
            raise ValueError("Protocol v5 persisted evidence public ID is not registered")
    context_registry = registry.get("contexts") or {}
    context_values = set(context_registry.values())
    if state.get("last_context_id") and str(state["last_context_id"]) not in context_values:
        raise ValueError("Protocol v5 persisted context ID is not registered")
    lineage = state.get("context_lineage") or []
    if not isinstance(lineage, list) or len(lineage) > 100 or not all(isinstance(item, dict) for item in lineage):
        raise ValueError("Protocol v5 context lineage is invalid or exceeds its bound")
    seen_ids: set[str] = set()
    seen_contexts: set[str] = set()
    seen_checkpoints: set[str] = set()
    previous_number = 0
    pinned_generation = state.get("generation")
    progressive = state.get("progressive_checkpoint") if isinstance(state.get("progressive_checkpoint"), dict) else {}
    for item in lineage:
        context_id = str(item.get("context_id") or "")
        base_id = item.get("base_context_id")
        base = str(base_id) if base_id is not None else None
        kind = str(item.get("kind") or "")
        try:
            number = int(item.get("number") or 0)
            generation = int(item.get("generation"))
            protocol_version = int(item.get("protocol_version"))
        except (TypeError, ValueError):
            raise ValueError("Protocol v5 context lineage metadata is invalid") from None
        if (
            not context_id or context_id in seen_ids or number < 1 or protocol_version != 5
            or pinned_generation is None or generation != int(pinned_generation)
            or (base is not None and base not in seen_contexts)
        ):
            raise ValueError("Protocol v5 context lineage order or generation is invalid")
        if kind == "first_useful_checkpoint":
            match = re.fullmatch(r"(CTX-[0-9]{3,})-P1", context_id)
            pending_parent = progressive.get("continuation_status") in {"pending", "failed"}
            if (
                match is None
                or (match.group(1) not in context_values and not pending_parent)
                or progressive.get("checkpoint_id") != context_id
                or progressive.get("context_id") != match.group(1)
                or progressive.get("content_hash") != item.get("content_hash")
            ):
                raise ValueError("Protocol v5 first-useful checkpoint lineage is invalid")
            seen_checkpoints.add(context_id)
        elif kind in {"checkpoint", "delta"}:
            content_hash = str(item.get("content_hash") or "")
            if context_registry.get(content_hash) != context_id or number <= previous_number:
                raise ValueError("Protocol v5 context lineage identity is invalid")
            progressive_parent = item.get("progressive_parent_id")
            if progressive_parent is not None and str(progressive_parent) not in seen_checkpoints:
                raise ValueError("Protocol v5 progressive context parent is invalid")
            seen_contexts.add(context_id)
            previous_number = number
        else:
            raise ValueError("Protocol v5 context lineage kind is invalid")
        seen_ids.add(context_id)
    if state.get("last_context_id") and seen_contexts and str(state["last_context_id"]) != next(
        reversed([str(item.get("context_id")) for item in lineage if item.get("kind") in {"checkpoint", "delta"}])
    ):
        raise ValueError("Protocol v5 last context does not match its lineage")


def _location_evidence_ids(
    bundle: ContextBundle,
    registry: dict[str, dict[str, str]],
    repo: str,
    path: str,
    line: int | None = None,
) -> list[str]:
    identifiers: list[str] = []
    for evidence in bundle.evidence:
        if not _pinned_repository_evidence(bundle, evidence) or evidence.repo != repo or evidence.path != path:
            continue
        if line is not None and not evidence.line_start <= line <= evidence.line_end:
            continue
        identity = _hash(
            "evidence", evidence.repo, evidence.path, evidence.line_start, evidence.line_end, evidence.content,
        )
        identifiers.append(_allocate(registry, "evidence", identity, "E", 4))
        if len(identifiers) >= 8:
            break
    return identifiers


def _validated_prior_evidence_ids(
    settings: Settings,
    generation: AtlasGenerationRef,
    state: dict[str, Any],
    bundle: ContextBundle,
    registry: dict[str, dict[str, str]],
) -> set[str]:
    """Validate only prior IDs that can affect this wave, against pinned exact source."""
    referenced: list[str] = []
    runtime = state.get("investigation_runtime") or {}
    for item in (runtime.get("hypothesis_ledger") or {}).get("items") or []:
        if not isinstance(item, dict):
            continue
        for key in ("candidate_evidence", "supporting_evidence", "contradicting_evidence"):
            referenced.extend(str(value) for value in item.get(key) or [])
    for values in (runtime.get("coverage_proofs") or {}).values():
        referenced.extend(str(value) for value in values or [])
    memory = state.get("investigation_memory") if isinstance(state.get("investigation_memory"), dict) else {}
    for fact in memory.get("verified_facts") or []:
        if isinstance(fact, dict):
            referenced.append(str(fact.get("evidence_id") or ""))
    for item in (runtime.get("evidence_frontier") or {}).get("items") or []:
        if not isinstance(item, dict):
            continue
        for key in ("known", "candidate_evidence", "verified_evidence"):
            referenced.extend(str(value) for value in item.get(key) or [])
    wanted_order = list(dict.fromkeys(
        value for value in referenced if re.fullmatch(r"E[0-9]{4,}", value)
    ))[:200]
    wanted = set(wanted_order)
    if not wanted:
        return set()

    valid: set[str] = set()
    current_regions: dict[tuple[str, str, int, int, str], str] = {}
    for evidence in bundle.evidence:
        if not _pinned_repository_evidence(bundle, evidence):
            continue
        identity = _hash(
            "evidence", evidence.repo, evidence.path, evidence.line_start, evidence.line_end, evidence.content,
        )
        public_id = _allocate(registry, "evidence", identity, "E", 4)
        digest = hashlib.sha256(evidence.content.encode("utf-8")).hexdigest()
        current_regions[(evidence.repo, evidence.path, evidence.line_start, evidence.line_end, digest)] = public_id
        if public_id in wanted:
            valid.add(public_id)

    state_root = settings.state_dir.resolve()
    sources = state.get("sources") if isinstance(state.get("sources"), dict) else {}
    records = [item for item in state.get("evidence_records") or [] if isinstance(item, dict)]
    records_by_public: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        records_by_public.setdefault(str(record.get("public_id") or ""), []).append(record)
    fallback_files: list[tuple[str, str]] = []
    fallback_seen: set[tuple[str, str]] = set()
    for public_id in wanted_order:
        for record in records_by_public.get(public_id, []):
            repo = str(record.get("repo") or "")
            path = str(record.get("path") or "")
            source = sources.get(repo) if isinstance(sources.get(repo), dict) else {}
            key = repo, path
            if (
                public_id not in valid and key not in fallback_seen
                and repo in generation.snapshots and path
                and str(source.get("sha") or "") == generation.snapshots[repo]
                and not str(source.get("snapshot") or "")
            ):
                fallback_seen.add(key)
                fallback_files.append(key)
    indexed_sources: dict[tuple[str, str], str] = {}
    if fallback_files:
        from .index import read_generation_files

        loaded = read_generation_files(
            settings, generation, fallback_files,
            max_bytes=MAX_PRIOR_EVIDENCE_HYDRATION_BYTES,
            max_seconds=MAX_PRIOR_EVIDENCE_HYDRATION_SECONDS,
        )
        if loaded is not None:
            indexed_sources = loaded

    for record in records:
        public_id = str(record.get("public_id") or "")
        if public_id not in wanted or public_id in valid:
            continue
        repo = str(record.get("repo") or "")
        path = str(record.get("path") or "")
        source = sources.get(repo) if isinstance(sources.get(repo), dict) else {}
        try:
            start = int(record.get("line_start"))
            end = int(record.get("line_end"))
            record_generation = record.get("generation")
            if record_generation is not None and int(record_generation) != generation.generation:
                continue
            if not repo or repo not in generation.snapshots or not path or start < 1 or end < start:
                continue
            if str(source.get("sha") or "") != generation.snapshots[repo]:
                continue
            snapshot_value = str(source.get("snapshot") or "")
            if snapshot_value:
                snapshot = Path(snapshot_value).resolve()
                candidate = (snapshot / path).resolve()
                if (
                    not snapshot.is_relative_to(state_root)
                    or not candidate.is_relative_to(snapshot)
                ):
                    continue
                from .core import BrainError, _bounded_regular_file_bytes

                try:
                    source_content = _bounded_regular_file_bytes(
                        candidate, MAX_REFRESH_FILE_BYTES,
                    ).decode("utf-8", errors="replace")
                except BrainError:
                    continue
            else:
                source_content = indexed_sources.get((repo, path))
                if source_content is None or len(source_content.encode("utf-8")) > MAX_REFRESH_FILE_BYTES:
                    continue
            lines = source_content.splitlines()
            content = "\n".join(lines[start - 1:end])
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if digest != str(record.get("content_hash") or ""):
                continue
            identity = _hash("evidence", repo, path, start, end, content)
            if (registry.get("evidence") or {}).get(identity) != public_id:
                continue
            internal = "E-" + hashlib.sha256(
                f"{repo}\0{path}\0{start}\0{end}\0{content}".encode("utf-8")
            ).hexdigest()[:24]
            if str(record.get("evidence_id") or "") != internal:
                continue
            if current_regions.get((repo, path, start, end, digest), public_id) != public_id:
                continue
        except (OSError, TypeError, ValueError):
            continue
        valid.add(public_id)
    return valid


def _hypotheses(
    previous: list[dict[str, Any]], requested: Iterable[object], bundle: ContextBundle,
    registry: dict[str, dict[str, str]], valid_prior_evidence_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    valid_prior = valid_prior_evidence_ids or set()
    ledger = {
        str(item.get("identity")): {
            **dict(item),
            # A still-valid exact-source ID proves only that the cited bytes
            # remain pinned.  It does not by itself preserve the semantic
            # classification of those bytes against a hypothesis.
            "status": "untested",
            "candidate_evidence": [
                str(value) for value in item.get("candidate_evidence") or []
                if str(value) in valid_prior
            ][:10],
            "supporting_evidence": [],
            "contradicting_evidence": [],
            "verification": "not structurally reverified in this wave",
        }
        for item in previous if isinstance(item, dict) and item.get("identity")
    }
    authoritative = [item for item in bundle.evidence if _pinned_repository_evidence(bundle, item)]
    for raw in _bounded_hypothesis_inputs(requested):
        identity = _hash("hypothesis", _normalize(raw))
        previous_item = ledger.get(identity) or {}
        prior_candidates = [
            str(value) for value in previous_item.get("candidate_evidence") or [] if str(value) in valid_prior
        ]
        terms = [term for term in _compound_terms(raw) if len(term) >= 4]
        candidate_evidence: list[str] = []
        supporting: list[str] = []
        contradicting: list[str] = []
        conflicting_assignments = False
        boolean_claim = re.search(
            r"(?i)\b([A-Za-z_$][\w$.-]*)\s+(?:is|equals?|=)\s+(true|false|enabled|disabled)\b",
            str(raw),
        )
        expected_boolean = None
        claim_key = None
        if boolean_claim:
            claim_key = _normalize(boolean_claim.group(1))
            expected_boolean = boolean_claim.group(2).casefold() in {"true", "enabled"}
        for evidence in authoritative:
            content = evidence.content.casefold()
            if not terms or sum(term in content for term in terms) < min(2, len(terms)):
                continue
            evidence_identity = _hash(
                "evidence", evidence.repo, evidence.path, evidence.line_start, evidence.line_end, evidence.content,
            )
            public_id = _allocate(registry, "evidence", evidence_identity, "E", 4)
            candidate_evidence.append(public_id)
            if claim_key is not None:
                structural_content = evidence.verification_content or (
                    evidence.content if evidence.line_start == 1 else None
                )
                observed_values = (
                    _boolean_assignments(evidence.path, structural_content, claim_key)
                    if structural_content is not None else set()
                )
                if len(observed_values) > 1:
                    conflicting_assignments = True
                elif observed_values:
                    observed = next(iter(observed_values))
                    (supporting if observed == expected_boolean else contradicting).append(public_id)
            if len(candidate_evidence) >= 10:
                break
        if conflicting_assignments or (supporting and contradicting):
            status = "unresolved"
        elif contradicting:
            status = "contradicted"
        elif supporting:
            status = "supported"
        else:
            status = "unresolved" if candidate_evidence else "untested"
        ledger[identity] = {
            "identity": identity, "hypothesis_id": _allocate(registry, "hypotheses", identity, "H", 3),
            "statement": raw, "origin": str(previous_item.get("origin") or "request"), "status": status,
            "candidate_evidence": list(dict.fromkeys([
                *prior_candidates, *candidate_evidence,
            ]))[:10],
            "supporting_evidence": list(dict.fromkeys(supporting))[:10],
            "contradicting_evidence": list(dict.fromkeys(contradicting))[:10],
            "verification": (
                "ambiguous conflicting exact assignments" if conflicting_assignments or (supporting and contradicting)
                else "exact pinned boolean assignment" if supporting or contradicting
                else "not structurally verified"
            ),
        }
    return sorted(ledger.values(), key=lambda item: item["hypothesis_id"])[:100]


def _frontier(
    unresolved: Iterable[object], coverage: dict[str, str], registry: dict[str, dict[str, str]],
    previous: Iterable[dict[str, Any]] = (),
    *,
    bundle: ContextBundle,
    next_best_evidence: dict[str, Any] | None = None,
    hypotheses: Iterable[dict[str, Any]] = (),
    valid_prior_evidence_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    valid_prior = valid_prior_evidence_ids or set()
    values = [str(item) for item in unresolved if str(item).strip()]
    coverage_values = {
        f"Establish {key.replace('_', ' ')}": key
        for key, value in coverage.items() if value not in {"verified", "not_requested"}
    }
    resolved_coverage_statements = {
        f"Establish {key.replace('_', ' ')}"
        for key, value in coverage.items() if value == "verified"
    }
    values.extend(coverage_values)
    active_statements = set(values) | set(coverage_values)
    retained = {
        str(item.get("identity")): dict(item) for item in previous
        if isinstance(item, dict) and item.get("identity")
        and not (
            item.get("coverage_key")
            and coverage.get(str(item.get("coverage_key"))) == "verified"
        )
        and str(item.get("statement") or "") not in resolved_coverage_statements
        and (item.get("coverage_key") or str(item.get("statement") or "") in active_statements)
    }
    exact_evidence: list[tuple[set[str], str]] = []
    for evidence in bundle.evidence:
        if not _pinned_repository_evidence(bundle, evidence):
            continue
        identity = _hash(
            "evidence", evidence.repo, evidence.path, evidence.line_start, evidence.line_end, evidence.content,
        )
        evidence_id = _allocate(registry, "evidence", identity, "E", 4)
        exact_evidence.append((set(_compound_terms(evidence.content + " " + evidence.path)), evidence_id))
    attempted_scopes = [
        f"repo:{repo}" for repo in list(dict.fromkeys(
            str(repo) for repo in bundle.trace.get("final_repo_scope") or []
        ))[:32]
    ]
    action = str(
        (next_best_evidence or {}).get("action")
        or (next_best_evidence or {}).get("query")
        or "retrieve highest-value exact evidence"
    )
    contradicted = [
        item for item in hypotheses if isinstance(item, dict) and item.get("status") == "contradicted"
    ]
    for value in list(dict.fromkeys(values))[:100]:
        identity = _hash("blocker", value)
        value_terms = set(_compound_terms(value))
        verified = [
            evidence_id for evidence_terms, evidence_id in exact_evidence
            if value_terms and len(value_terms & evidence_terms) >= min(2, len(value_terms))
        ][:20]
        contradictions = [
            str(item.get("hypothesis_id")) for item in contradicted
            if value_terms & set(_compound_terms(item.get("statement") or ""))
        ][:20]
        retained[identity] = {
            **retained.get(identity, {}),
            "identity": identity, "blocker_id": _allocate(registry, "blockers", identity, "B", 3),
            "statement": value, "status": "unresolved", "next_action": action,
            "known": list(dict.fromkeys([
                *verified,
            ]))[:20],
            "missing": list((retained.get(identity) or {}).get("missing") or [value]),
            "candidate_evidence": [
                value for value in (retained.get(identity) or {}).get("candidate_evidence") or []
                if str(value) in valid_prior
            ][:20],
            "verified_evidence": list(dict.fromkeys([
                *verified,
            ]))[:20],
            "contradictions": list(dict.fromkeys([
                *((retained.get(identity) or {}).get("contradictions") or []), *contradictions,
            ]))[:20],
            "attempted_scopes": list(dict.fromkeys([
                *((retained.get(identity) or {}).get("attempted_scopes") or []), *attempted_scopes,
            ]))[:50],
            "rejected_scopes": list(dict.fromkeys([
                *((retained.get(identity) or {}).get("rejected_scopes") or []),
                *(attempted_scopes if "no " in value.casefold() or "not found" in value.casefold() else []),
            ]))[:50],
            "estimated_cost": "bounded", "coverage_key": coverage_values.get(value),
            "priority": (
                "high" if coverage_values.get(value) == (next_best_evidence or {}).get("coverage")
                and int((next_best_evidence or {}).get("value") or 0) >= 80 else "normal"
            ),
        }
    return sorted(retained.values(), key=lambda item: str(item.get("blocker_id") or item["identity"]))[:100]


def _flow_cache_identity(schema: str, steps: Iterable[dict[str, Any]]) -> str:
    immutable_keys = (
        "identity", "order", "depth", "edge_type", "source_id", "target_id", "target",
        "kind", "key", "repo", "module_id", "entity_id", "path", "line", "direction",
        "framework", "confidence",
    )
    logical = [
        {key: item.get(key) for key in immutable_keys if key in item}
        for item in steps
    ]
    return _hash(schema, json.dumps(logical, sort_keys=True, separators=(",", ":")))


def _valid_generation_paths(
    settings: Settings, generation: AtlasGenerationRef, pairs: Iterable[tuple[str, str]],
) -> tuple[set[tuple[str, str]], int]:
    values = list(dict.fromkeys((str(repo), str(path)) for repo, path in pairs))[:1_000]
    valid: set[tuple[str, str]] = set()
    operations = 0
    connection = connect(settings)
    try:
        for offset in range(0, len(values), 400):
            batch = values[offset:offset + 400]
            placeholders = ",".join("?" for _ in batch)
            operations += 1
            valid.update(
                (str(row[0]), str(row[1])) for row in connection.execute(
                    f"SELECT DISTINCT e.repo,e.path FROM generation_entities g "
                    f"JOIN atlas_entities e ON e.entity_id=g.entity_id WHERE g.generation=? "
                    f"AND (e.repo || char(0) || e.path) IN ({placeholders})",
                    (generation.generation, *(repo + "\0" + path for repo, path in batch)),
                )
            )
    finally:
        connection.close()
    return valid, operations


def _delta_items(
    current: Iterable[dict[str, Any]], previous: Iterable[dict[str, Any]], key: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    before = {
        str(item.get(key)): item for item in previous
        if isinstance(item, dict) and item.get(key)
    }
    after = {
        str(item.get(key)): item for item in current
        if isinstance(item, dict) and item.get(key)
    }
    changed = [
        item for identity, item in after.items()
        if json.dumps(item, sort_keys=True, default=str) != json.dumps(before.get(identity), sort_keys=True, default=str)
    ]
    return changed, sorted(set(before) - set(after))


def build_ticket_runtime(
    settings: Settings,
    generation: AtlasGenerationRef,
    request: dict[str, Any],
    bundle: ContextBundle,
    state: dict[str, Any],
    *,
    context_id: str,
    next_best_evidence: dict[str, Any] | None = None,
    validated_prior_evidence_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Derive one bounded wave from the ticket's immutable generation."""
    existing = dict(state.get("investigation_runtime") or {})
    runtime_compatibility = _hash(
        INVESTIGATION_RUNTIME_SCHEMA_VERSION, generation.identity, RUNTIME_ANCHOR_SCHEMA_VERSION,
        JAVA_INTELLIGENCE_SCHEMA_VERSION, EXECUTION_FLOW_SCHEMA_VERSION, INTEGRATION_FLOW_SCHEMA_VERSION,
        PROGRAM_SLICE_SCHEMA_VERSION, SURFACE_SCHEMA_VERSION,
    )
    if existing and (
        existing.get("schema_version") != INVESTIGATION_RUNTIME_SCHEMA_VERSION
        or existing.get("compatibility_identity") != runtime_compatibility
    ):
        existing = {}
    pinned = existing.get("generation")
    if pinned is not None and int(pinned) != generation.generation:
        raise RuntimeError("investigation runtime generation changed inside a pinned ticket")
    wave = int(existing.get("wave") or 0) + 1
    if wave > HARD_MAX_WAVES:
        raise RuntimeError("investigation wave limit exceeded")
    values: list[object] = [
        *(item for item in request.get("anchors") or [] if isinstance(item, dict)),
        *(request.get("resolve") or []), *(request.get("runtime_facts") or []), request.get("objective"),
    ]
    ablations = set(str(value) for value in request.get("_evaluation_ablation") or [])
    if "anchors" in ablations:
        resolved = {
            "status": "degraded", "reason": "anchors disabled by evaluation ablation",
            "generation": generation.generation, "schema_version": RUNTIME_ANCHOR_SCHEMA_VERSION,
            "compatibility_identity": _component_identity(
                generation, "runtime_anchors", RUNTIME_ANCHOR_SCHEMA_VERSION,
            ),
            "candidates": [], "inputs": [value for _, value in _bounded_anchor_queries(values)],
        }
    else:
        resolved = resolve_runtime_anchors(
            settings, generation, values, use_cache="generation_cache" not in ablations,
        )
        combined = {str(item.get("identity")): item for item in resolved.get("candidates") or []}
        prefetch_anchor_ids = list(dict.fromkeys(
            str(value) for value in ((request.get("_prefetch") or {}).get("anchor_ids") or []) if value
        ))[:MAX_ANCHOR_CANDIDATES]
        prefetch_anchor_reused = 0
        poisoned_prior_anchor = False
        if (
            "prefetch" not in ablations
            and prefetch_anchor_ids
            and _valid_prefetch_envelope(generation, request.get("_prefetch"), str(request.get("objective") or ""))
        ):
            placeholders = ",".join("?" for _ in prefetch_anchor_ids)
            connection = connect(settings)
            try:
                rows = connection.execute(
                    f"SELECT a.anchor_id,a.kind,a.value,a.normalized,a.repo,a.module_id,a.entity_id,a.path,a.line,"
                    f"a.blob_sha,a.confidence,a.method,a.provenance_json,a.fingerprint "
                    f"FROM generation_runtime_anchors g JOIN atlas_runtime_anchors a ON a.anchor_id=g.anchor_id "
                    f"JOIN generation_intelligence_files i ON i.generation=g.generation AND i.repo=a.repo "
                    f"AND i.path=a.path AND i.blob_sha=a.blob_sha AND i.schema_version=? WHERE g.generation=? "
                    f"AND a.anchor_id IN ({placeholders})",
                    (GENERATION_INTELLIGENCE_INPUT_SCHEMA_VERSION, generation.generation, *prefetch_anchor_ids),
                )
                for row in rows:
                    candidate = _validated_runtime_anchor_row(
                        tuple(row), method="ticket_prefetch_prior", score=.5,
                    )
                    if candidate is None:
                        poisoned_prior_anchor = True
                        continue
                    identifier = str(candidate["identity"])
                    if identifier in combined:
                        continue
                    combined[identifier] = candidate
                    prefetch_anchor_reused += 1
            finally:
                connection.close()
            resolved["database_operations"] = int(resolved.get("database_operations") or 0) + 1
        resolved["prefetch_reused"] = prefetch_anchor_reused
        for item in _exact_evidence_anchors(request, bundle, generation):
            identifier = str(item["identity"])
            if identifier not in combined or item.get("evidence_authority") == "exact_source":
                combined[identifier] = item
        previous_candidates = [
            item for item in (existing.get("anchors") or {}).get("candidates") or []
            if isinstance(item, dict) and item.get("identity") and str(item.get("identity")) not in combined
        ][:MAX_ANCHOR_CANDIDATES]
        previous_ids = [str(item["identity"]) for item in previous_candidates]
        if previous_ids:
            placeholders = ",".join("?" for _ in previous_ids)
            connection = connect(settings)
            try:
                for row in connection.execute(
                    f"SELECT a.anchor_id,a.kind,a.value,a.normalized,a.repo,a.module_id,a.entity_id,a.path,a.line,"
                    f"a.blob_sha,a.confidence,a.method,a.provenance_json,a.fingerprint "
                    f"FROM generation_runtime_anchors g JOIN atlas_runtime_anchors a ON a.anchor_id=g.anchor_id "
                    f"JOIN generation_intelligence_files i ON i.generation=g.generation AND i.repo=a.repo "
                    f"AND i.path=a.path AND i.blob_sha=a.blob_sha AND i.schema_version=? WHERE g.generation=? "
                    f"AND a.anchor_id IN ({placeholders})",
                    (GENERATION_INTELLIGENCE_INPUT_SCHEMA_VERSION, generation.generation, *previous_ids),
                ):
                    candidate = _validated_runtime_anchor_row(
                        tuple(row), method="retained_ticket_prior", score=.5,
                    )
                    if candidate is None:
                        poisoned_prior_anchor = True
                        continue
                    candidate["provenance"] = {**candidate["provenance"], "exact_source": False}
                    combined[str(candidate["identity"])] = candidate
            finally:
                connection.close()
            resolved["database_operations"] = int(resolved.get("database_operations") or 0) + 1
        if poisoned_prior_anchor:
            resolved["status"] = "degraded"
            resolved["reason"] = "runtime-anchor content identity is incompatible"
        evidence_records = [
            item for item in state.get("evidence_records") or [] if isinstance(item, dict)
        ]
        for item in previous_candidates:
            identifier = str(item["identity"])
            if identifier in combined:
                continue
            method = str(item.get("method") or "")
            expected = _hash(
                "evidence-anchor", generation.identity, method, str(item.get("kind") or ""),
                _normalize(item.get("value")), str(item.get("repo")), str(item.get("path")),
                int(item.get("line") or 1),
            )
            retained = any(
                record.get("repo") == item.get("repo") and record.get("path") == item.get("path")
                and int(record.get("line_start") or 1) <= int(item.get("line") or 1) <= int(record.get("line_end") or 1)
                for record in evidence_records
            )
            if identifier != expected or not retained:
                continue
            combined[identifier] = {
                **item, "confidence": min(.5, float(item.get("confidence") or 0)),
                "evidence_authority": "inferred_candidate",
                "provenance": {**dict(item.get("provenance") or {}), "exact_source": False},
            }
        resolved["candidates"] = sorted(
            combined.values(),
            key=lambda item: (
                0 if item.get("evidence_authority") == "exact_source" else 1,
                -float(item.get("confidence") or 0), str(item.get("repo")), str(item.get("path")), int(item.get("line") or 1),
            ),
        )[:MAX_ANCHOR_CANDIDATES]
        for item in resolved["candidates"]:
            verified = _verified_anchor(bundle, item)
            if item.get("evidence_authority") != "exact_source":
                item["evidence_authority"] = "exact_source" if verified else "atlas_candidate"
                provenance = dict(item.get("provenance") or {})
                provenance["exact_source"] = verified
                item["provenance"] = provenance
                if verified:
                    item["confidence"] = max(.95, float(item.get("confidence") or 0))
        resolved["ambiguous"] = len([
            item for item in resolved["candidates"] if float(item.get("confidence") or 0) >= .9
        ]) > 1
    anchors = list(resolved.get("candidates") or [])
    registry = {
        key: dict(value) for key, value in (state.get("stable_identities") or {}).items() if isinstance(value, dict)
    }
    for evidence in bundle.evidence:
        identity = _hash(
            "evidence", evidence.repo, evidence.path, evidence.line_start, evidence.line_end, evidence.content,
        )
        _allocate(registry, "evidence", identity, "E", 4)
    valid_prior_evidence_ids = (
        set(validated_prior_evidence_ids)
        if validated_prior_evidence_ids is not None
        else _validated_prior_evidence_ids(settings, generation, state, bundle, registry)
    )
    for item in anchors:
        item["anchor_id"] = _allocate(registry, "anchors", str(item["identity"]), "A", 3)
        item["evidence_ids"] = _location_evidence_ids(
            bundle, registry, str(item.get("repo")), str(item.get("path")), int(item.get("line") or 1),
        ) if item.get("evidence_authority") == "exact_source" else []
        if item.get("evidence_authority") == "exact_source" and not item["evidence_ids"]:
            item["evidence_authority"] = "atlas_candidate"
    typed_requests = {
        (str(item.get("kind") or ""), _normalize(item.get("value")))
        for item in request.get("anchors") or [] if isinstance(item, dict)
        and item.get("kind") in {"endpoint", "event", "topic", "queue", "stack_frame"}
    }
    typed_seeds = [
        str(item.get("entity_id")) for item in anchors
        if item.get("entity_id")
        and (str(item.get("kind") or ""), _normalize(item.get("value"))) in typed_requests
        and not is_test_path(str(item.get("path") or ""))
    ]
    seeds = list(dict.fromkeys(typed_seeds or [
        str(item.get("entity_id")) for item in anchors
        if item.get("entity_id") and not is_test_path(str(item.get("path") or ""))
    ]))
    execution_input = _hash("execution-input", *seeds)
    execution_compatibility = _component_identity(generation, "typed_graph", EXECUTION_FLOW_SCHEMA_VERSION)
    # Flow subsets change with the current exact-evidence verification state. Rebuild the
    # bounded traversal from the pinned graph instead of treating ticket/session state as
    # an authoritative cache that could hide an omitted edge.
    execution = _execution_flow(settings, generation, seeds, bundle)
    execution.update({"input_identity": execution_input, "cache_reused": False})
    execution["cache_identity"] = _flow_cache_identity(EXECUTION_FLOW_SCHEMA_VERSION, execution.get("steps") or [])
    integration_input = _hash(
        "integration-input", *(str(item.get("identity") or item.get("value")) for item in anchors),
    )
    integration_compatibility = _component_identity(
        generation, "java_intelligence", INTEGRATION_FLOW_SCHEMA_VERSION,
    )
    integration = _integration_flow(settings, generation, anchors, bundle)
    integration.update({"input_identity": integration_input, "cache_reused": False})
    integration["cache_identity"] = _flow_cache_identity(
        INTEGRATION_FLOW_SCHEMA_VERSION, integration.get("steps") or [],
    )
    if "graph_flow" in ablations:
        execution.update({"status": "degraded", "reason": "graph flow disabled by evaluation ablation", "steps": [], "paths": []})
    for flow_name, flow in (("execution", execution), ("integration", integration)):
        for step in flow.get("steps") or []:
            step["evidence_ids"] = _location_evidence_ids(
                bundle, registry, str(step.get("repo")), str(step.get("path")), int(step.get("line") or 1),
            ) if step.get("state") == "verified" else []
            if step.get("state") == "verified" and not step["evidence_ids"]:
                step["state"] = "candidate"
                step["evidence_authority"] = "atlas_candidate"
        if flow_name == "execution":
            flow["paths"] = _execution_paths(flow.get("steps") or [])
        flow_identity = _hash("ticket-flow", generation.identity, flow_name)
        flow["flow_id"] = _allocate(registry, "flows", flow_identity, "F", 3)
    if "program_slice" in ablations:
        slice_state = {
            "schema_version": PROGRAM_SLICE_SCHEMA_VERSION, "status": "degraded",
            "reason": "Program Slice disabled by evaluation ablation", "statements": [],
        }
    else:
        slice_state = _program_slice(bundle)
    surfaces = _surfaces(bundle, integration)
    retention_database_operations = 0
    for record in state.get("evidence_records") or []:
        if not isinstance(record, dict) or str(record.get("public_id") or "") not in valid_prior_evidence_ids:
            continue
        repo, path = str(record.get("repo") or ""), str(record.get("path") or "")
        suffix = Path(path).suffix.lower()
        name = (
            "test" if is_test_path(path)
            else "config_data" if suffix in _CONFIG_SUFFIXES | {".sql", ".avsc", ".proto", ".graphql", ".graphqls"}
            else "implementation"
        )
        surfaces[name].append({
            "repo": repo, "path": path, "state": "verified", "confidence": 1.0,
            "evidence_authority": "revalidated_pinned_source",
            "evidence_ids": [str(record["public_id"])],
        })
    for name in ("implementation", "test", "impact", "contract", "config_data"):
        merged_surface = {
            (str(item.get("repo")), str(item.get("path"))): item
            for item in surfaces.get(name) or [] if isinstance(item, dict)
        }
        surfaces[name] = sorted(
            merged_surface.values(), key=lambda item: (str(item.get("repo")), str(item.get("path"))),
        )[:200]
        for item in surfaces[name]:
            current_ids = _location_evidence_ids(
                bundle, registry, str(item.get("repo")), str(item.get("path")),
            ) if item.get("state") == "verified" else []
            item["evidence_ids"] = current_ids or [
                str(value) for value in item.get("evidence_ids") or []
                if str(value) in valid_prior_evidence_ids
            ]
            if item.get("state") == "verified" and not item["evidence_ids"]:
                item["state"] = "candidate"
                item["evidence_authority"] = "atlas_candidate"
    merged_slice = {
        str(item.get("identity")): item for item in slice_state.get("statements") or []
        if isinstance(item, dict) and item.get("identity")
    }
    slice_state["statements"] = sorted(
        merged_slice.values(), key=lambda item: (str(item.get("repo")), str(item.get("path")), int(item.get("line") or 1)),
    )[:MAX_SLICE_STATEMENTS]
    slice_state.update({
        "generation": generation.generation,
        "compatibility_identity": _hash(PROGRAM_SLICE_SCHEMA_VERSION, generation.identity),
    })
    surfaces.update({
        "generation": generation.generation,
        "compatibility_identity": _hash(SURFACE_SCHEMA_VERSION, generation.identity),
    })
    coverage = {
        str(key): "candidate" if str(value) == "verified" else str(value)
        for key, value in (state.get("coverage_map") or {}).items()
    }
    coverage_proofs: dict[str, list[str]] = {}

    def exact_ids(items: Iterable[dict[str, Any]]) -> list[str]:
        return list(dict.fromkeys(
            str(identifier)
            for item in items if isinstance(item, dict)
            for identifier in item.get("evidence_ids") or []
            if re.fullmatch(r"E[0-9]{4,}", str(identifier))
        ))[:50]

    verified_entry_anchors = [item for item in anchors if (
        item.get("evidence_authority") == "exact_source"
        and float(item.get("confidence") or 0) >= .9
        and _is_runtime_entry_anchor(item)
        and not is_test_path(str(item.get("path") or ""))
    )]
    if entry_ids := exact_ids(verified_entry_anchors):
        coverage["production_entry_point"] = "verified"
        coverage_proofs["production_entry_point"] = entry_ids
    elif anchors:
        coverage["production_entry_point"] = "candidate"
    flow_edge_types = {
        "CALLS", "CALLS_ENDPOINT", "EXPOSES_ENDPOINT", "PUBLISHES", "CONSUMES", "READS_CONFIG",
        "WRITES_TABLE", "READS_TABLE", "DEPENDS_ON_REPO",
    }
    flow_steps_by_id = {
        str(item.get("identity")): item for item in execution.get("steps") or [] if item.get("identity")
    }
    verified_static_paths = [
        path for path in execution.get("paths") or []
        if path.get("state") == "verified" and int(path.get("length") or 0) >= 2
        and all(flow_steps_by_id.get(str(identifier), {}).get("edge_type") in flow_edge_types
                for identifier in path.get("step_ids") or [])
        and all(not is_test_path(str(flow_steps_by_id.get(str(identifier), {}).get("path") or ""))
                for identifier in path.get("step_ids") or [])
    ]
    if verified_static_paths:
        verified_step_ids = {
            str(identifier) for path in verified_static_paths for identifier in path.get("step_ids") or []
        }
        if flow_ids := exact_ids(
            item for identity, item in flow_steps_by_id.items() if identity in verified_step_ids
        ):
            coverage["main_execution_flow"] = "verified"
            coverage_proofs["main_execution_flow"] = flow_ids
    elif execution.get("steps"):
        coverage["main_execution_flow"] = "candidate"
    verified_integrations: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in integration.get("steps") or []:
        if item.get("state") != "verified":
            continue
        key = (str(item.get("kind") or ""), _normalize(item.get("key")))
        verified_integrations.setdefault(key, []).append(item)
    established_pair: tuple[dict[str, Any], dict[str, Any]] | None = None
    for (kind, _), values in verified_integrations.items():
        for index, first in enumerate(values):
            for second in values[index + 1:]:
                if str(first.get("repo")) == str(second.get("repo")):
                    continue
                by_direction = {str(first.get("direction")): first, str(second.get("direction")): second}
                if set(by_direction) != {"inbound", "outbound"}:
                    continue
                if kind == "topic":
                    established_pair = first, second
                    break
                if kind == "endpoint":
                    outbound = by_direction["outbound"]
                    inbound = by_direction["inbound"]
                    target_service = (outbound.get("provenance") or {}).get("target_service")
                    if target_service and _normalize(target_service) == _normalize(inbound.get("repo")):
                        established_pair = first, second
                        break
            if established_pair is not None:
                break
        if established_pair is not None:
            break
    if established_pair is not None:
        if integration_ids := exact_ids(established_pair):
            coverage["cross_repo_integration"] = "verified"
            coverage_proofs["cross_repo_integration"] = integration_ids
    elif integration.get("steps"):
        coverage["cross_repo_integration"] = "candidate"
    if impact_ids := exact_ids(
        item for item in surfaces.get("impact") or [] if item.get("state") == "verified"
    ):
        coverage["impact_surface"] = "verified"
        coverage_proofs["impact_surface"] = impact_ids
    elif surfaces.get("impact"):
        coverage["impact_surface"] = "candidate"
    if contract_ids := exact_ids(
        item for item in surfaces.get("contract") or [] if item.get("state") == "verified"
    ):
        coverage["contract_surface"] = "verified"
        coverage_proofs["contract_surface"] = contract_ids
    elif surfaces.get("contract"):
        coverage["contract_surface"] = "candidate"
    if test_ids := exact_ids(
        item for item in surfaces.get("test") or [] if item.get("state") == "verified"
    ):
        coverage["tests"] = "verified"
        coverage_proofs["tests"] = test_ids
    verified_config = [
        item for item in surfaces.get("config_data") or [] if item.get("state") == "verified"
    ]
    if config_ids := exact_ids(
        item for item in verified_config if Path(str(item.get("path") or "")).suffix.lower() in _CONFIG_SUFFIXES
    ):
        coverage["configuration"] = "verified"
        coverage_proofs["configuration"] = config_ids
    if data_ids := exact_ids(
        item for item in verified_config
        if Path(str(item.get("path") or "")).suffix.lower() in {".sql", ".avsc", ".proto", ".graphql", ".graphqls"}
    ):
        coverage["data_schema"] = "verified"
        coverage_proofs["data_schema"] = data_ids
    from .atlas import next_best_evidence as plan_next_best_evidence

    next_best_evidence = plan_next_best_evidence(
        coverage, request, int(state.get("no_progress_rounds") or 0),
    )
    for evidence in bundle.evidence:
        identity = _hash("evidence", evidence.repo, evidence.path, evidence.line_start, evidence.line_end, evidence.content)
        _allocate(registry, "evidence", identity, "E", 4)
    previous_hypotheses = [
        item for item in (existing.get("hypothesis_ledger") or {}).get("items") or []
        if isinstance(item, dict) and item.get("statement")
    ]
    requested_hypotheses = list(dict.fromkeys([
        *(str(item.get("statement")) for item in previous_hypotheses),
        *(str(value) for value in request.get("hypotheses") or []),
    ]))
    hypothesis_identities = {
        _hash("hypothesis", _normalize(item.get("statement"))) for item in previous_hypotheses
    } | {
        _hash("hypothesis", _normalize(value)) for value in requested_hypotheses
    }
    if len(hypothesis_identities) > MAX_HYPOTHESES:
        raise RuntimeError(
            f"investigation hypothesis ledger exceeds its {MAX_HYPOTHESES}-item cross-wave bound"
        )
    hypotheses = _hypotheses(
        previous_hypotheses,
        requested_hypotheses, bundle, registry, valid_prior_evidence_ids,
    )
    frontier = _frontier(
        bundle.unresolved, coverage, registry,
        (existing.get("evidence_frontier") or {}).get("items") or [],
        bundle=bundle, next_best_evidence=next_best_evidence, hypotheses=hypotheses,
        valid_prior_evidence_ids=valid_prior_evidence_ids,
    )
    first_useful = existing.get("first_useful_checkpoint")
    published_checkpoint = state.get("progressive_checkpoint")
    if isinstance(published_checkpoint, dict) and published_checkpoint.get("status") == "published":
        first_useful = {
            key: value for key, value in published_checkpoint.items()
            if key != "internal_evidence_ids"
        }
    if not frontier:
        stop_reason = "coverage_satisfied"
    elif int(state.get("no_progress_rounds") or 0) >= 2:
        stop_reason = "no_progress"
    elif wave >= DEFAULT_MAX_WAVES:
        stop_reason = "default_wave_limit"
    else:
        stop_reason = "continue"
    database_operations = retention_database_operations + sum(
        int(value.get("database_operations") or 0) for value in (resolved, execution, integration)
    )
    if database_operations > MAX_RUNTIME_DB_OPERATIONS:
        raise RuntimeError("investigation runtime exceeded its database operation contract")
    prior_physical_operations = sum(
        int((item.get("retrieval") or {}).get("physical_backend_operations") or 0)
        for item in state.get("request_history") or [] if isinstance(item, dict)
    )
    total_physical_operations = prior_physical_operations + int(bundle.trace.get("physical_backend_operations") or 0)
    runtime = {
        "schema_version": INVESTIGATION_RUNTIME_SCHEMA_VERSION,
        "compatibility_identity": runtime_compatibility,
        "generation": generation.generation, "atlas_generation_id": generation.identity,
        "wave": wave, "max_waves": DEFAULT_MAX_WAVES, "hard_max_waves": HARD_MAX_WAVES,
        "coverage": coverage,
        "coverage_proofs": coverage_proofs,
        "anchors": resolved, "execution_flow": execution, "integration_flow": integration,
        "program_slice": slice_state, "surfaces": surfaces,
        "hypothesis_ledger": {
            "schema_version": HYPOTHESIS_LEDGER_SCHEMA_VERSION,
            "compatibility_identity": _hash(HYPOTHESIS_LEDGER_SCHEMA_VERSION, generation.identity),
            "generation": generation.generation, "status": "ready", "items": hypotheses,
            "bounds": {"item_limit": MAX_HYPOTHESES, "items": len(hypotheses)},
        },
        "evidence_frontier": {
            "schema_version": EVIDENCE_FRONTIER_SCHEMA_VERSION,
            "compatibility_identity": _hash(EVIDENCE_FRONTIER_SCHEMA_VERSION, generation.identity),
            "generation": generation.generation, "status": "ready", "items": frontier,
        },
        "progressive_checkpoint": {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "compatibility_identity": _hash(CHECKPOINT_SCHEMA_VERSION, generation.identity),
            "generation": generation.generation, "status": "ready", "first_useful": first_useful,
        },
        "protocol": {
            "schema_version": PROTOCOL_V5_SCHEMA_VERSION,
            "compatibility_identity": _hash(PROTOCOL_V5_SCHEMA_VERSION, generation.identity),
            "generation": generation.generation, "status": "ready",
        },
        "serving_state": {
            "atlas": "ready", "generation": generation.generation,
            "atlas_generation_id": generation.identity, "source_signature": generation.source_signature,
            "source_snapshots": len(generation.snapshots),
            **{
                name: str(generation.component(name).get("status") or "unavailable")
                for name in ("lexical", "structural", "relationships", "runtime_anchors", "java_intelligence")
            },
            "semantic": (
                str(bundle.trace.get("semantic_status"))
                if bundle.trace.get("semantic_status") not in {None, "not_requested"}
                else str(generation.component("semantic").get("status") or "unavailable")
            ),
            "reranker": (
                "used" if any("local reranker" in item.found_by for item in bundle.evidence) else "not_used"
            ),
        },
        "first_useful_checkpoint": first_useful,
        "next_best_evidence": next_best_evidence,
        "stop_reason": stop_reason,
        "degraded": [
            {"component": name, "reason": value.get("reason")}
            for name, value in (("anchors", resolved), ("execution_flow", execution), ("integration_flow", integration), ("program_slice", slice_state))
            if value.get("status") != "ready"
        ],
        "bounds": {
            "anchor_inputs": MAX_ANCHOR_INPUTS, "anchor_candidates": MAX_ANCHOR_CANDIDATES,
            "flow_depth": MAX_FLOW_DEPTH, "flow_branch": MAX_FLOW_BRANCH, "flow_steps": MAX_FLOW_STEPS,
            "slice_input_bytes": MAX_SLICE_INPUT_BYTES, "slice_statements": MAX_SLICE_STATEMENTS,
            "database_operations": database_operations,
            "database_operation_limit": MAX_RUNTIME_DB_OPERATIONS,
            "physical_operations_used": total_physical_operations,
            "physical_operation_limit": settings.max_backend_operations * HARD_MAX_WAVES,
            "context_byte_limit_per_wave": settings.hard_context_chars,
            "context_byte_limit_all_waves": settings.hard_context_chars * HARD_MAX_WAVES,
            "repo_scope_count": int(bundle.metrics.get("repo_scope_count") or 0),
            "repo_scope_limit": int(bundle.metrics.get("repo_scope_limit") or settings.widen_repo_limit),
            "candidate_count": int(bundle.metrics.get("raw_candidates") or 0),
            "candidate_limit": settings.pre_rerank_candidate_limit,
            "rerank_input_count": int(bundle.metrics.get("rerank_input_count") or 0),
            "hydrated_region_count": int(bundle.metrics.get("hydrated_regions") or 0),
            "semantic_repo_count": int(bundle.metrics.get("semantic_repo_count") or 0),
            "subprocess_count": int(bundle.metrics.get("subprocess_count") or 0),
            "bytes_scanned": int(bundle.metrics.get("bytes_scanned") or 0),
            "bytes_read": int(bundle.metrics.get("bytes_read") or 0),
        },
        "evaluation_ablation": sorted(ablations),
        "evidence_authority": "Only exact source regions from the pinned generation are final evidence authority.",
    }
    delta_state: dict[str, Any] = {"removed": {}}
    for name, key, current_items, previous_items in (
        ("anchors", "identity", resolved.get("candidates") or [], (existing.get("anchors") or {}).get("candidates") or []),
        ("execution_flow", "identity", execution.get("steps") or [], (existing.get("execution_flow") or {}).get("steps") or []),
        ("integration_flow", "identity", integration.get("steps") or [], (existing.get("integration_flow") or {}).get("steps") or []),
        ("program_slice", "identity", slice_state.get("statements") or [], (existing.get("program_slice") or {}).get("statements") or []),
        ("hypothesis_ledger", "identity", hypotheses, (existing.get("hypothesis_ledger") or {}).get("items") or []),
        ("evidence_frontier", "identity", frontier, (existing.get("evidence_frontier") or {}).get("items") or []),
    ):
        changed, removed = _delta_items(current_items, previous_items, key)
        delta_state[name] = changed
        if removed and name in {"execution_flow", "integration_flow", "evidence_frontier"}:
            delta_state["removed"][name] = removed
    delta_surfaces: dict[str, list[dict[str, Any]]] = {}
    for name in ("implementation", "test", "impact", "contract", "config_data"):
        current_items = surfaces.get(name) or []
        previous_items = (existing.get("surfaces") or {}).get(name) or []
        changed, removed = _delta_items(
            ({**item, "surface_key": f"{item.get('repo')}:{item.get('path')}"} for item in current_items),
            ({**item, "surface_key": f"{item.get('repo')}:{item.get('path')}"} for item in previous_items),
            "surface_key",
        )
        delta_surfaces[name] = [
            {key: value for key, value in item.items() if key != "surface_key"} for item in changed
        ]
    delta_state["surfaces"] = delta_surfaces
    runtime["delta_state"] = delta_state
    state["stable_identities"] = registry
    return runtime


def stable_evidence_id(state: dict[str, Any], item: Any) -> str:
    identity = _hash("evidence", item.repo, item.path, item.line_start, item.line_end, item.content)
    registry = state.setdefault("stable_identities", {}).setdefault("evidence", {})
    return _allocate({"evidence": registry}, "evidence", identity, "E", 4)


def render_protocol_v5(runtime: dict[str, Any], *, delta: bool = False) -> str:
    """Render bounded derived navigation state; source remains separately rendered."""
    sections = [
        "## Protocol v5 investigation state", "",
        f"- Runtime schema: `{runtime.get('schema_version')}`",
        f"- Pinned generation: `{runtime.get('generation')}`",
        f"- Wave: `{runtime.get('wave')}` / `{runtime.get('max_waves')}` (hard `{runtime.get('hard_max_waves')}`)",
        f"- Stop reason: `{runtime.get('stop_reason')}`",
        f"- Context mode: `{'delta' if delta else 'full checkpoint'}`", "",
    ]
    serving = runtime.get("serving_state") or {}
    sections.extend(["### Serving state", ""])
    for name in (
        "atlas", "lexical", "structural", "relationships", "semantic",
        "runtime_anchors", "java_intelligence",
    ):
        sections.append(f"- `{name}`: `{serving.get(name, 'unavailable')}`")
    sections.append("")
    delta_state = runtime.get("delta_state") or {}
    anchors = (delta_state.get("anchors") if delta else None) or (
        [] if delta else (runtime.get("anchors") or {}).get("candidates") or []
    )
    sections.extend(["### Runtime anchors", ""])
    sections.extend(
        f"- `{item.get('anchor_id')}` `{item.get('kind')}` `{item.get('value')}` → "
        f"`{item.get('repo')}:{item.get('path')}:{item.get('line')}` "
        f"({item.get('method')}, {item.get('confidence')}; evidence "
        f"{', '.join(item.get('evidence_ids') or []) or 'candidate-only'})"
        for item in anchors[:MAX_ANCHOR_CANDIDATES]
    )
    if not anchors:
        sections.append("- None resolved")
    for title, key in (("ExecutionFlow", "execution_flow"), ("IntegrationFlow", "integration_flow")):
        flow = runtime.get(key) or {}
        flow_steps = (delta_state.get(key) if delta else flow.get("steps")) or []
        sections.extend(["", f"### {title} `{flow.get('flow_id', 'unassigned')}`", ""])
        for item in flow_steps:
            label = item.get("edge_type") or item.get("kind")
            sections.append(
                f"- {item.get('order')}. `{label}` `{item.get('repo')}:{item.get('path')}:{item.get('line')}` "
                f"— `{item.get('state')}` / `{item.get('evidence_authority')}` / evidence "
                f"`{', '.join(item.get('evidence_ids') or []) or 'candidate-only'}`"
            )
        if not flow_steps:
            sections.append(f"- {('No changed steps' if delta else flow.get('reason')) or 'No bounded flow found'}")
    slice_items = (delta_state.get("program_slice") if delta else (runtime.get("program_slice") or {}).get("statements")) or []
    sections.extend(["", "### Program Slice Lite", ""])
    for item in slice_items[:MAX_SLICE_STATEMENTS]:
        sections.append(
            f"- `{item.get('kind')}` `{item.get('repo')}:{item.get('path')}:{item.get('line')}` "
            f"— `{item.get('evidence_authority')}`"
        )
    if not slice_items:
        sections.append("- No changed statements" if delta else "- No bounded slice available")
    sections.extend(["", "### Hypothesis Ledger", ""])
    hypothesis_items = (delta_state.get("hypothesis_ledger") if delta else (runtime.get("hypothesis_ledger") or {}).get("items")) or []
    for item in hypothesis_items:
        sections.append(f"- `{item['hypothesis_id']}` `{item['status']}` — {item['statement']}")
    if not hypothesis_items:
        sections.append("- No changes" if delta else "- None")
    sections.extend(["", "### Evidence Frontier", ""])
    frontier_items = (delta_state.get("evidence_frontier") if delta else (runtime.get("evidence_frontier") or {}).get("items")) or []
    for item in frontier_items:
        sections.append(f"- `{item['blocker_id']}` `{item['status']}` — {item['statement']}")
    if not frontier_items:
        sections.append("- No changes" if delta else "- None")
    sections.extend(["", "### Impact / Test / Contract / Config-Data Surfaces", ""])
    surfaces = delta_state.get("surfaces") if delta else runtime.get("surfaces") or {}
    for name in ("implementation", "test", "impact", "contract", "config_data"):
        rows = surfaces.get(name) or []
        rendered = ", ".join(
            f"{item['repo']}:{item['path']} [{item['state']}; "
            f"{', '.join(item.get('evidence_ids') or []) or 'candidate-only'}]" for item in rows[:20]
        )
        sections.append(f"- `{name}`: {rendered or 'none'}")
    removed = delta_state.get("removed") or {}
    if delta and removed:
        sections.extend(["", "### Superseded investigation state", ""])
        for name, identifiers in sorted(removed.items()):
            sections.append(f"- `{name}`: {', '.join(f'`{value}`' for value in identifiers[:100])}")
    sections.extend([
        "", "### Evidence authority", "",
        str(runtime.get("evidence_authority") or "Exact pinned source is authoritative."), "",
    ])
    return "\n".join(sections)

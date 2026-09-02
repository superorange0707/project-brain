from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from .locks import workspace_exclusive
from .platforms import atomic_managed_text_write, logical_path, read_managed_text

if TYPE_CHECKING:
    from .core import Repository, Settings


MAX_FACTS = 20_000
MAX_RELATIONSHIPS = 10_000
MAX_RELATIONSHIPS_PER_KEY = 512
MAX_RELATIONSHIP_ARTIFACT_BYTES = 16_000_000
MAX_RELATIONSHIP_REPOSITORIES = 100
MAX_RELATIONSHIP_DOCUMENTS = 50_000
MAX_RELATIONSHIP_SOURCE_BYTES = 256 * 1024 * 1024
MAX_RELATIONSHIP_FILE_BYTES = 3 * 1024 * 1024
MAX_RELATIONSHIP_SOURCE_SECONDS = 30.0
MAX_RELATIONSHIP_FILESYSTEM_ENTRIES = 500_000
MAX_CONFIG_VALUES = 20_000
MAX_FACTS_PER_ANALYZER = 8_000
_RELATIONSHIP_CACHE: dict[tuple[str, str, int, int], list[Relationship]] = {}
_RELATIONSHIP_RENDER_HASH_CACHE: dict[tuple[str, int, int], str] = {}


@dataclass(frozen=True)
class Fact:
    repo: str
    kind: str
    key: str
    path: str
    line: int
    detail: str = ""


@dataclass(frozen=True)
class Relationship:
    source: str
    target: str
    kind: str
    key: str
    source_evidence: str
    target_evidence: str
    confidence: str = "high"

    def summary(self) -> str:
        return f"{self.source} --{self.kind}({self.key})--> {self.target} [{self.confidence}]"


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _files(repo: Repository, suffixes: set[str], deadline: float) -> Iterable[Path]:
    root = repo.scan_path
    entries = 0
    for path in root.rglob("*") if root.is_dir() else []:
        entries += 1
        if entries > MAX_RELATIONSHIP_FILESYSTEM_ENTRIES or time.monotonic() >= deadline:
            raise RuntimeError("relationship filesystem source budget exceeded")
        if not path.is_symlink() and path.is_file() and path.suffix.lower() in suffixes and not any(
            part in {".git", "target", "build", "node_modules", ".venv"} for part in path.parts
        ) and path.name.lower() not in {".env", ".envrc", "credentials", "credentials.json", "service-account.json", "id_rsa", "id_ed25519", "keystore"} and path.suffix.lower() not in {".key", ".pem", ".p12", ".pfx", ".jks"}:
            yield path


def _relative(repo: Repository, path: Path) -> str:
    return logical_path(path.relative_to(repo.scan_path))


def _bounded_relationship_source(path: Path) -> str:
    """Read one direct regular source file through a stable bounded handle."""
    try:
        expected = path.lstat()
        if not stat.S_ISREG(expected.st_mode):
            raise OSError("relationship source is not a direct regular file")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (expected.st_dev, expected.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                raise OSError("relationship source identity changed while opening")
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                raw = source.read(MAX_RELATIONSHIP_FILE_BYTES + 1)
        finally:
            os.close(descriptor)
    except OSError:
        raise
    if len(raw) > MAX_RELATIONSHIP_FILE_BYTES:
        raise RuntimeError("relationship source file budget exceeded")
    return raw.decode("utf-8", errors="replace")


def _relationship_documents(
    settings: Settings, snapshots: dict[str, str], suffixes: set[str],
) -> list[tuple[str, str, str]]:
    try:
        from .index import indexed_snapshot_documents

        return indexed_snapshot_documents(
            settings, snapshots, suffixes,
            max_repositories=MAX_RELATIONSHIP_REPOSITORIES,
            max_items=MAX_RELATIONSHIP_DOCUMENTS,
            max_bytes=MAX_RELATIONSHIP_SOURCE_BYTES,
            max_file_bytes=MAX_RELATIONSHIP_FILE_BYTES,
            max_seconds=MAX_RELATIONSHIP_SOURCE_SECONDS,
        )
    except sqlite3.DataError as error:
        raise RuntimeError("relationship authoritative source budget exceeded") from error
    except sqlite3.Error as error:
        if any(snapshot != "working-tree" for snapshot in snapshots.values()):
            raise RuntimeError(
                "relationship source is unavailable from the authoritative lexical snapshot"
            ) from error
    if len(settings.repositories) > MAX_RELATIONSHIP_REPOSITORIES:
        raise RuntimeError("relationship repository budget exceeded")
    deadline = time.monotonic() + MAX_RELATIONSHIP_SOURCE_SECONDS
    documents: list[tuple[str, str, str]] = []
    total_bytes = 0
    for repo in settings.repositories:
        for path in _files(repo, suffixes, deadline):
            try:
                content = _bounded_relationship_source(path)
            except OSError:
                continue
            total_bytes += len(content.encode("utf-8"))
            if len(documents) >= MAX_RELATIONSHIP_DOCUMENTS or total_bytes > MAX_RELATIONSHIP_SOURCE_BYTES:
                raise RuntimeError("relationship source budget exceeded")
            documents.append((repo.name, _relative(repo, path), content))
    return documents


def _config_values(
    documents: Iterable[tuple[str, str, str]],
) -> dict[str, dict[str, tuple[str, str, int]]]:
    by_repo: dict[str, dict[str, tuple[str, str, int]]] = {}
    item_count = 0
    for repo, path, text in documents:
        values = by_repo.setdefault(repo, {})
        lines = text.splitlines()
        if Path(path).suffix.lower() == ".properties":
            for number, line in enumerate(lines, 1):
                match = re.match(r"\s*([^#!\s][^=:\s]*)\s*[=:]\s*(.*?)\s*$", line)
                if match and item_count < MAX_CONFIG_VALUES:
                    values[match.group(1)] = (match.group(2).strip("\"'"), path, number)
                    item_count += 1
            continue
        stack: list[tuple[int, str]] = []
        for number, line in enumerate(lines, 1):
            match = re.match(r"^(\s*)([A-Za-z0-9_.-]+):\s*(.*?)\s*$", line)
            if not match or line.lstrip().startswith("#"):
                continue
            indent = len(match.group(1))
            while stack and stack[-1][0] >= indent:
                stack.pop()
            key = ".".join([item[1] for item in stack] + [match.group(2)])
            raw = match.group(3).strip("\"'")
            if raw and item_count < MAX_CONFIG_VALUES:
                values[key] = (raw, path, number)
                item_count += 1
            else:
                stack.append((indent, match.group(2)))
    return by_repo


def _resolve(value: str, config: dict[str, tuple[str, str, int]]) -> str:
    match = re.fullmatch(r"\$\{([^}:]+)(?::([^}]*))?}", value.strip())
    if not match:
        return value
    configured = config.get(match.group(1))
    return configured[0] if configured else (match.group(2) or value)


def _xml_text(element: ET.Element, name: str) -> str | None:
    child = element.find(f"{{*}}{name}")
    return child.text.strip() if child is not None and child.text else None


def _maven_facts(documents: Iterable[tuple[str, str, str]]) -> tuple[list[Fact], list[Fact]]:
    provided: list[Fact] = []
    required: list[Fact] = []
    for repo, path, text in documents:
        if Path(path).name != "pom.xml":
            continue
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            continue
        parent = root.find("{*}parent")
        group = _xml_text(root, "groupId") or (_xml_text(parent, "groupId") if parent is not None else None)
        artifact = _xml_text(root, "artifactId")
        evidence = path
        if artifact and len(provided) + len(required) < MAX_FACTS_PER_ANALYZER:
            provided.append(Fact(repo, "maven-producer", f"{group or '?'}:{artifact}", evidence, 1))
        for dependency in root.findall(".//{*}dependencies/{*}dependency"):
            if len(provided) + len(required) >= MAX_FACTS_PER_ANALYZER:
                break
            dep_group = _xml_text(dependency, "groupId") or "?"
            dep_artifact = _xml_text(dependency, "artifactId")
            if dep_artifact:
                required.append(Fact(repo, "maven-consumer", f"{dep_group}:{dep_artifact}", evidence, 1))
    return provided, required


def _literal_values(value: str, config: dict[str, tuple[str, str, int]]) -> list[str]:
    return [_resolve(match.group(1), config) for match in islice(re.finditer(r'["\']([^"\']+)["\']', value), 256)]


def _structure_mask(text: str) -> str:
    """Mask comments and literal bodies while preserving offsets, quotes, and newlines."""
    output = list(text)
    index = 0
    state = "code"
    quote = ""
    while index < len(text):
        pair = text[index:index + 2]
        triple = text[index:index + 3]
        if state == "code":
            if pair == "//":
                output[index:index + 2] = "  "
                state, index = "line", index + 2
                continue
            if pair == "/*":
                output[index:index + 2] = "  "
                state, index = "block", index + 2
                continue
            if triple == '\"\"\"':
                state, quote = "triple", '\"'
                index += 3
                continue
            if text[index] in {'\"', "'"}:
                state, quote = "string", text[index]
            index += 1
            continue
        if state == "line":
            if text[index] == "\n":
                state = "code"
            else:
                output[index] = " "
            index += 1
            continue
        if state == "block":
            if pair == "*/":
                output[index:index + 2] = "  "
                state, index = "code", index + 2
            else:
                if text[index] != "\n":
                    output[index] = " "
                index += 1
            continue
        if state == "triple":
            if triple == '\"\"\"':
                state, index = "code", index + 3
            else:
                if text[index] != "\n":
                    output[index] = " "
                index += 1
            continue
        if text[index] == "\\" and index + 1 < len(text):
            output[index:index + 2] = "  "
            index += 2
        elif text[index] == quote:
            state = "code"
            index += 1
        else:
            if text[index] != "\n":
                output[index] = " "
            index += 1
    return "".join(output)


def _argument_value(text: str, offset: int) -> str:
    match = re.match(r'\s*(?:(["\'])((?:\\.|(?!\1).)*)\1|([A-Za-z_$][\w$]*))', text[offset:], re.S)
    return (match.group(2) if match and match.group(1) else match.group(3) if match else "") or ""


def _kafka_facts(
    documents: Iterable[tuple[str, str, str]],
    configs: dict[str, dict[str, tuple[str, str, int]]],
) -> tuple[list[Fact], list[Fact]]:
    producers: list[Fact] = []
    consumers: list[Fact] = []
    for repo, path, text in documents:
        if len(producers) + len(consumers) >= MAX_FACTS_PER_ANALYZER:
            break
        config = configs.get(repo, {})
        structure = _structure_mask(text)
        constants: dict[str, str] = {}
        constant_pattern = re.compile(
            r"\b(?:static\s+final\s+|const\s+val\s+)(?:String\s+)?([A-Za-z_$][\w$]*)\s*=\s*"
        )
        for match in constant_pattern.finditer(structure):
            if len(constants) >= MAX_CONFIG_VALUES:
                break
            value = _argument_value(text, match.end())
            if value:
                constants[str(match.group(1))] = _resolve(value, config)
        for match in re.finditer(r"@KafkaListener\s*\((.*?)\)", structure, re.S):
            body = text[match.start(1):match.end(1)]
            topic_value = re.search(r"\btopics?\s*=\s*(.*?)(?:,\s*\w+\s*=|$)", body, re.S)
            values = _literal_values(topic_value.group(1), config) if topic_value else _literal_values(body, config)
            if topic_value:
                values.extend(constants[name] for name in constants if re.search(rf"\b{re.escape(name)}\b", topic_value.group(1)))
            for topic in sorted(set(values)):
                if len(producers) + len(consumers) >= MAX_FACTS_PER_ANALYZER:
                    break
                consumers.append(Fact(repo, "kafka-consumer", topic, path, _line(text, match.start()), "@KafkaListener"))
        producer_patterns = (
            (r"\b\w*[Kk]afka\w*\.send\s*\(\s*", "KafkaTemplate.send"),
            (r"\bstreamBridge\.send\s*\(\s*", "StreamBridge.send"),
        )
        for pattern, detail in producer_patterns:
            for match in re.finditer(pattern, structure):
                if len(producers) + len(consumers) >= MAX_FACTS_PER_ANALYZER:
                    break
                raw = _argument_value(text, match.end())
                topic = _resolve(constants.get(raw, raw), config)
                if topic and not topic.startswith("${"):
                    producers.append(Fact(repo, "kafka-producer", topic, path, _line(text, match.start()), detail))
    binding = re.compile(r"^spring\.cloud\.stream\.bindings\.([^.]+)\.destination$")
    for repo, config in configs.items():
        for key, (topic, path, line) in config.items():
            if len(producers) + len(consumers) >= MAX_FACTS_PER_ANALYZER:
                break
            match = binding.match(key)
            if not match:
                continue
            fact = Fact(repo, "kafka-binding", topic, path, line, match.group(1))
            if re.search(r"(?:^|-)out(?:-|$)", match.group(1)):
                producers.append(fact)
            elif re.search(r"(?:^|-)in(?:-|$)", match.group(1)):
                consumers.append(fact)
    return producers, consumers


def _annotation_value(body: str, attribute: str = "value") -> str:
    named = re.search(rf"\b{attribute}\s*=\s*[\"']([^\"']*)[\"']", body)
    if named:
        return named.group(1)
    unnamed = re.search(r"[\"']([^\"']*)[\"']", body)
    return unnamed.group(1) if unnamed else ""


def _route(path: str) -> str:
    value = "/" + path.strip().strip("/") if path.strip().strip("/") else "/"
    value = re.sub(r"\{[^}]+}", "{}", value)
    return re.sub(r"/+", "/", value)


def _http_facts(
    documents: Iterable[tuple[str, str, str]],
    configs: dict[str, dict[str, tuple[str, str, int]]],
) -> tuple[list[Fact], list[Fact]]:
    clients: list[Fact] = []
    controllers: list[Fact] = []
    mapping = re.compile(r"@(Request|Get|Post|Put|Patch|Delete)Mapping\s*(?:\((.*?)\))?", re.S)
    for repo, path, text in documents:
        if len(clients) + len(controllers) >= MAX_FACTS_PER_ANALYZER:
            break
        config = configs.get(repo, {})
        structure = _structure_mask(text)
        class_match = re.search(r"\b(?:class|interface)\s+[A-Za-z_$][\w$]*", structure)
        if not class_match:
            continue
        header = structure[: class_match.start()]
        request_mappings = list(re.finditer(r"@RequestMapping\s*(?:\((.*?)\))?", header, re.S))
        prefix_body = (
            text[request_mappings[-1].start(1):request_mappings[-1].end(1)]
            if request_mappings and request_mappings[-1].group(1) is not None else ""
        )
        prefix = _resolve(_annotation_value(prefix_body), config) if request_mappings else ""
        feign = list(re.finditer(r"@FeignClient\s*\((.*?)\)", header, re.S))
        service = ""
        feign_prefix = prefix
        if feign:
            body = text[feign[-1].start(1):feign[-1].end(1)]
            service = _resolve(_annotation_value(body, "name") or _annotation_value(body), config)
            feign_prefix = _resolve(_annotation_value(body, "path"), config) or prefix
        controller = bool(re.search(r"@(RestController|Controller)\b", header))
        body_start = class_match.end()
        for match in mapping.finditer(structure, body_start):
            if len(clients) + len(controllers) >= MAX_FACTS_PER_ANALYZER:
                break
            # A class-level RequestMapping is already represented by `prefix`.
            between = structure[body_start:match.start()]
            if match.group(1) == "Request" and not re.search(r"\b(?:public|private|protected|fun)\b[^{};]*$", between[-300:], re.S):
                continue
            method = "ANY" if match.group(1) == "Request" else match.group(1).upper()
            mapping_body = text[match.start(2):match.end(2)] if match.group(2) is not None else ""
            suffix = _resolve(_annotation_value(mapping_body), config)
            if service:
                route = _route(f"{feign_prefix}/{suffix}")
                clients.append(Fact(repo, "http-client", f"{method} {route}", path, _line(text, match.start()), service))
            if controller:
                route = _route(f"{prefix}/{suffix}")
                controllers.append(Fact(repo, "http-server", f"{method} {route}", path, _line(text, match.start())))
    return clients, controllers


def _evidence(fact: Fact) -> str:
    return f"{fact.repo}:{fact.path}:{fact.line}"


def _service_matches(service: str, repo: str) -> bool:
    def normalize(value: str) -> str:
        compact = re.sub(r"[^a-z0-9]", "", value.lower())
        return compact.removesuffix("service")

    return bool(service) and normalize(service) == normalize(repo)


def analyze_relationships(settings: Settings) -> tuple[list[Fact], list[Relationship]]:
    facts: list[Fact] = []
    maven_producers: list[Fact] = []
    maven_consumers: list[Fact] = []
    kafka_producers: list[Fact] = []
    kafka_consumers: list[Fact] = []
    http_clients: list[Fact] = []
    http_servers: list[Fact] = []
    snapshots = {repo.name: str(repo.source_sha or "working-tree") for repo in settings.repositories}
    documents = _relationship_documents(
        settings, snapshots, {".properties", ".yml", ".yaml", ".xml", ".java", ".kt", ".kts"},
    )
    config_documents: list[tuple[str, str, str]] = []
    xml_documents: list[tuple[str, str, str]] = []
    kafka_documents: list[tuple[str, str, str]] = []
    http_documents: list[tuple[str, str, str]] = []
    for document in documents:
        suffix = Path(document[1]).suffix.lower()
        if suffix in {".properties", ".yml", ".yaml"}:
            config_documents.append(document)
        if suffix == ".xml":
            xml_documents.append(document)
        if suffix in {".java", ".kt", ".kts"}:
            kafka_documents.append(document)
        if suffix in {".java", ".kt"}:
            http_documents.append(document)
    configs = _config_values(config_documents)
    provided, required = _maven_facts(xml_documents)
    produced, consumed = _kafka_facts(kafka_documents, configs)
    clients, servers = _http_facts(http_documents, configs)
    maven_producers.extend(provided)
    maven_consumers.extend(required)
    kafka_producers.extend(produced)
    kafka_consumers.extend(consumed)
    http_clients.extend(clients)
    http_servers.extend(servers)
    facts.extend((maven_producers + maven_consumers + kafka_producers + kafka_consumers + http_clients + http_servers)[:MAX_FACTS])

    relationships: set[Relationship] = set()
    per_key: dict[tuple[str, str], int] = {}

    def add(relationship: Relationship) -> bool:
        key = (relationship.kind, relationship.key)
        if len(relationships) >= MAX_RELATIONSHIPS or per_key.get(key, 0) >= MAX_RELATIONSHIPS_PER_KEY:
            return False
        before = len(relationships)
        relationships.add(relationship)
        if len(relationships) != before:
            per_key[key] = per_key.get(key, 0) + 1
        return True

    maven_by_artifact: dict[str, list[Fact]] = {}
    for producer in sorted(maven_producers, key=lambda item: (item.key, item.repo, item.path, item.line)):
        maven_by_artifact.setdefault(producer.key.rsplit(":", 1)[-1], []).append(producer)
    for consumer in sorted(maven_consumers, key=lambda item: (item.key, item.repo, item.path, item.line)):
        artifact = consumer.key.rsplit(":", 1)[-1]
        for producer in maven_by_artifact.get(artifact, [])[:MAX_RELATIONSHIPS_PER_KEY]:
            if consumer.repo != producer.repo and (consumer.key == producer.key or artifact == producer.key.rsplit(":", 1)[-1]):
                if not add(Relationship(consumer.repo, producer.repo, "MAVEN_DEPENDS_ON", producer.key, _evidence(consumer), _evidence(producer))):
                    break
    kafka_by_key: dict[str, list[Fact]] = {}
    for consumer in kafka_consumers:
        kafka_by_key.setdefault(consumer.key, []).append(consumer)
    for producer in sorted(kafka_producers, key=lambda item: (item.key, item.repo, item.path, item.line)):
        for consumer in sorted(kafka_by_key.get(producer.key, []), key=lambda item: (item.repo, item.path, item.line)):
            if producer.repo != consumer.repo and not add(Relationship(
                producer.repo, consumer.repo, "KAFKA", producer.key, _evidence(producer), _evidence(consumer),
            )):
                break
    http_by_key: dict[str, list[Fact]] = {}
    for server in http_servers:
        http_by_key.setdefault(server.key, []).append(server)
    for client in sorted(http_clients, key=lambda item: (item.key, item.repo, item.path, item.line)):
        matched = [server for server in http_by_key.get(client.key, []) if server.repo != client.repo]
        for server in sorted(matched, key=lambda item: (item.repo, item.path, item.line)):
            if not add(Relationship(client.repo, server.repo, "HTTP", client.key, _evidence(client), _evidence(server))):
                break
        if not matched:
            for repo in settings.repositories:
                if repo.name != client.repo and _service_matches(client.detail, repo.name):
                    add(Relationship(client.repo, repo.name, "FEIGN_TARGET", client.detail, _evidence(client), "repository name", "medium"))
                    break
    return facts, sorted(relationships, key=lambda item: (item.source, item.target, item.kind, item.key))


def _source_signature(settings: Settings) -> list[list[str | None]]:
    return [[repo.name, repo.source_ref, repo.source_sha] for repo in settings.repositories]


def _relationship_payload_hash(value: dict[str, object]) -> str:
    payload = {key: item for key, item in value.items() if key != "payload_hash"}
    return "sha256:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def valid_relationship_payload(value: object, expected_snapshots: dict[str, str]) -> bool:
    if not isinstance(value, dict):
        return False
    if len(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > MAX_RELATIONSHIP_ARTIFACT_BYTES:
        return False
    sources = value.get("sources")
    relationships = value.get("relationships")
    if not isinstance(sources, list) or not isinstance(relationships, list):
        return False
    source_rows = [item for item in sources if isinstance(item, list) and len(item) >= 3]
    source_projection = {str(item[0]): str(item[2] or "working-tree") for item in source_rows}
    if len(source_rows) != len(sources) or len(source_projection) != len(source_rows):
        return False
    if (
        value.get("version") != 2
        or source_projection != expected_snapshots
        or value.get("payload_hash") != _relationship_payload_hash(value)
        or len(relationships) > MAX_RELATIONSHIPS
        or not all(
            isinstance(item, dict)
            and set(item) == {
                "source", "target", "kind", "key", "source_evidence", "target_evidence", "confidence",
            }
            and all(isinstance(item.get(key), str) for key in item)
            for item in relationships
        )
    ):
        return False
    rendered_hash = value.get("rendered_sha256")
    return isinstance(rendered_hash, str) and bool(re.fullmatch(r"[0-9a-f]{64}", rendered_hash))


def _cached_relationships(settings: Settings, generation: object | None = None) -> list[Relationship] | None:
    if generation is not None:
        component = generation.component("relationships")  # type: ignore[attr-defined]
        if component.get("status") != "ready" or not component.get("artifact_ref"):
            return []
        path = (settings.state_dir / str(component["artifact_ref"])).resolve()
        if not path.is_relative_to(settings.state_dir.resolve()):
            return []
    else:
        if getattr(settings, "atlas_generation_mode", "current") == "legacy_source_pin":
            return []
        path = settings.state_dir / "relationships.json"
    if path.is_symlink() or not path.is_file():
        return None
    try:
        metadata = path.stat()
        if metadata.st_size > MAX_RELATIONSHIP_ARTIFACT_BYTES:
            return None
        expected_identity = str(component.get("content_hash") or "") if generation is not None else "current"
        cache_key = (str(path), expected_identity, metadata.st_mtime_ns, metadata.st_size)
        if cache_key in _RELATIONSHIP_CACHE:
            return list(_RELATIONSHIP_CACHE[cache_key])
        value = json.loads(read_managed_text(
            settings.state_dir, path, max_bytes=MAX_RELATIONSHIP_ARTIFACT_BYTES,
        ))
        if generation is not None:
            from .catalog import _content_hash

            if _content_hash(value) != component.get("content_hash"):
                return None
        expected_snapshots = (
            {repo.name: str(generation.snapshots.get(repo.name) or "working-tree") for repo in settings.repositories}  # type: ignore[attr-defined]
            if generation is not None
            else {repo.name: str(repo.source_sha or "working-tree") for repo in settings.repositories}
        )
        if (
            not valid_relationship_payload(value, expected_snapshots)
        ):
            return None
        parsed = [Relationship(**item) for item in value.get("relationships") or []]
        if len(_RELATIONSHIP_CACHE) >= 64:
            _RELATIONSHIP_CACHE.clear()
        _RELATIONSHIP_CACHE[cache_key] = parsed
        _RELATIONSHIP_RENDER_HASH_CACHE[(str(path), metadata.st_mtime_ns, metadata.st_size)] = str(
            value["rendered_sha256"]
        )
        return list(parsed)
    except (AttributeError, OSError, json.JSONDecodeError, TypeError):
        return None


def related_relationships(
    settings: Settings,
    queries: Iterable[str],
    evidence_paths: set[tuple[str, str]],
    *,
    limit: int = 12,
    generation: object | None = None,
) -> list[Relationship]:
    """Return a bounded, evidence-backed contract-graph slice for the current retrieval."""
    relationships = _cached_relationships(settings, generation)
    if relationships is None and generation is None and getattr(settings, "atlas_generation_mode", "current") != "legacy_source_pin":
        _, relationships = analyze_relationships(settings)
    elif relationships is None:
        relationships = []
    terms = {
        value.lower()
        for query in queries
        for value in re.findall(r"[A-Za-z0-9_.:/{}-]{3,}", query)
        if value.lower() not in {
            "behavior", "change", "configuration", "current", "determine", "exact", "find", "implementation",
            "inspect", "locate", "repository", "service", "source", "the",
        }
    }

    def location(value: str) -> tuple[str, str] | None:
        match = re.fullmatch(r"([^:]+):(.+):(\d+)", value)
        return (match.group(1), match.group(2)) if match else None

    scored: list[tuple[int, Relationship]] = []
    for relationship in relationships:
        haystack = " ".join(
            [relationship.source, relationship.target, relationship.kind, relationship.key]
        ).lower()
        path_match = location(relationship.source_evidence) in evidence_paths or location(relationship.target_evidence) in evidence_paths
        term_matches = sum(1 for term in terms if term in haystack)
        if not path_match and not term_matches:
            continue
        scored.append(((100 if path_match else 0) + term_matches * 10, relationship))
    scored.sort(key=lambda item: (-item[0], item[1].source, item[1].target, item[1].kind, item[1].key))
    return [item for _, item in scored[:limit]]


def _runtime_workflows(relationships: list[Relationship]) -> list[str]:
    runtime = [item for item in relationships if item.kind in {"KAFKA", "HTTP", "FEIGN_TARGET"}]
    adjacency: dict[str, list[Relationship]] = {}
    for item in runtime:
        adjacency.setdefault(item.source, []).append(item)
    workflows: set[str] = set()

    def walk(node: str, parts: list[str], seen: set[str], depth: int) -> None:
        if len(workflows) >= 1_000:
            return
        outgoing = [item for item in adjacency.get(node, []) if item.target not in seen][:8]
        if not outgoing and len(parts) > 1:
            workflows.add(" ".join(parts))
        if depth >= 4:
            return
        for edge in outgoing:
            walk(edge.target, [*parts, f"--{edge.kind}:{edge.key}-->", edge.target], seen | {edge.target}, depth + 1)

    for source in sorted(adjacency):
        walk(source, [source], {source}, 0)
        if len(workflows) >= 1_000:
            break
    return sorted(workflows)


@workspace_exclusive
def generate_relationship_map(settings: Settings) -> str:
    state_path = settings.state_dir / "relationships.json"
    rendered_path = settings.generated_dir / "PROJECT_RELATIONSHIPS.md"
    cached = _cached_relationships(settings)
    if cached is not None and rendered_path.is_file():
        try:
            metadata = state_path.stat()
            rendered = read_managed_text(
                settings.generated_dir, rendered_path, max_bytes=MAX_RELATIONSHIP_ARTIFACT_BYTES,
            )
            expected_render_hash = _RELATIONSHIP_RENDER_HASH_CACHE.get(
                (str(state_path), metadata.st_mtime_ns, metadata.st_size)
            )
            if expected_render_hash == hashlib.sha256(rendered.encode("utf-8")).hexdigest():
                return rendered
        except (OSError, ValueError, UnicodeDecodeError):
            pass
    facts, relationships = analyze_relationships(settings)
    workflows = _runtime_workflows(relationships)
    output = [
        "# Generated Cross-Repository Relationships",
        "",
        f"Generated: {datetime.now(UTC).isoformat()}",
        "",
        "Every edge below comes from exact static evidence. Runtime configuration, reflection, and dynamic topic/URL construction still require logs or tests.",
        "",
        "## Snapshot inputs",
        "",
    ]
    for repo in settings.repositories:
        output.append(
            f"- `{repo.name}` — `{repo.source_ref or 'working tree'}` at "
            f"`{(repo.source_sha or 'unknown')[:12]}` ({repo.source_status})"
        )
        if repo.source_warning:
            output.append(f"  - Freshness warning: {repo.source_warning}")
    output.extend(["", "## Runtime workflows", ""])
    output.extend(f"- `{workflow}`" for workflow in workflows) if workflows else output.append("- None detected")
    output.extend(["", "## Relationship edges", ""])
    if relationships:
        for item in relationships:
            output.extend(
                [
                    f"- **{item.source} → {item.target}** — `{item.kind}` `{item.key}` ({item.confidence})",
                    f"  - source: `{item.source_evidence}`",
                    f"  - target: `{item.target_evidence}`",
                ]
            )
    else:
        output.append("- None detected")
    output.extend(["", "## Detected endpoints, topics, and artifacts", ""])
    for fact in sorted(facts, key=lambda item: (item.kind, item.key, item.repo, item.path, item.line)):
        output.append(f"- `{fact.kind}` `{fact.key}` — `{_evidence(fact)}`" + (f" — `{fact.detail}`" if fact.detail else ""))
    if not facts:
        output.append("- None detected")
    text = "\n".join(output).rstrip() + "\n"
    payload: dict[str, object] = {
        "version": 2,
        "sources": _source_signature(settings),
        "relationships": [item.__dict__ for item in relationships],
        "rendered_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    payload["payload_hash"] = _relationship_payload_hash(payload)
    payload_text = json.dumps(payload, indent=2) + "\n"
    if (
        len(text.encode("utf-8")) > MAX_RELATIONSHIP_ARTIFACT_BYTES
        or len(payload_text.encode("utf-8")) > MAX_RELATIONSHIP_ARTIFACT_BYTES
    ):
        raise RuntimeError("relationship artifact budget exceeded")
    from .core import _atomic_generated_text_write

    _atomic_generated_text_write(
        settings, settings.generated_dir / "PROJECT_RELATIONSHIPS.md", text,
    )
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    atomic_managed_text_write(
        settings.state_dir,
        state_path,
        payload_text,
    )
    _RELATIONSHIP_CACHE.clear()
    _RELATIONSHIP_RENDER_HASH_CACHE.clear()
    return text

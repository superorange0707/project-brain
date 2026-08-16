from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from .core import Repository, Settings


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


def _files(repo: Repository, suffixes: set[str]) -> Iterable[Path]:
    root = repo.scan_path
    for path in root.rglob("*") if root.is_dir() else []:
        if path.is_file() and path.suffix.lower() in suffixes and not any(
            part in {".git", "target", "build", "node_modules", ".venv"} for part in path.parts
        ):
            yield path


def _relative(repo: Repository, path: Path) -> str:
    return str(path.relative_to(repo.scan_path))


def _config_values(repo: Repository) -> dict[str, tuple[str, str, int]]:
    values: dict[str, tuple[str, str, int]] = {}
    for path in _files(repo, {".properties", ".yml", ".yaml"}):
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if path.suffix == ".properties":
            for number, line in enumerate(lines, 1):
                match = re.match(r"\s*([^#!\s][^=:\s]*)\s*[=:]\s*(.*?)\s*$", line)
                if match:
                    values[match.group(1)] = (match.group(2).strip("\"'"), _relative(repo, path), number)
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
            if raw:
                values[key] = (raw, _relative(repo, path), number)
            else:
                stack.append((indent, match.group(2)))
    return values


def _resolve(value: str, config: dict[str, tuple[str, str, int]]) -> str:
    match = re.fullmatch(r"\$\{([^}:]+)(?::([^}]*))?}", value.strip())
    if not match:
        return value
    configured = config.get(match.group(1))
    return configured[0] if configured else (match.group(2) or value)


def _xml_text(element: ET.Element, name: str) -> str | None:
    child = element.find(f"{{*}}{name}")
    return child.text.strip() if child is not None and child.text else None


def _maven_facts(repo: Repository) -> tuple[list[Fact], list[Fact]]:
    provided: list[Fact] = []
    required: list[Fact] = []
    for path in _files(repo, {".xml"}):
        if path.name != "pom.xml":
            continue
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError):
            continue
        parent = root.find("{*}parent")
        group = _xml_text(root, "groupId") or (_xml_text(parent, "groupId") if parent is not None else None)
        artifact = _xml_text(root, "artifactId")
        evidence = _relative(repo, path)
        if artifact:
            provided.append(Fact(repo.name, "maven-producer", f"{group or '?'}:{artifact}", evidence, 1))
        for dependency in root.findall(".//{*}dependencies/{*}dependency"):
            dep_group = _xml_text(dependency, "groupId") or "?"
            dep_artifact = _xml_text(dependency, "artifactId")
            if dep_artifact:
                required.append(Fact(repo.name, "maven-consumer", f"{dep_group}:{dep_artifact}", evidence, 1))
    return provided, required


def _literal_values(value: str, config: dict[str, tuple[str, str, int]]) -> list[str]:
    return [_resolve(match.group(1), config) for match in re.finditer(r'["\']([^"\']+)["\']', value)]


def _kafka_facts(repo: Repository, config: dict[str, tuple[str, str, int]]) -> tuple[list[Fact], list[Fact]]:
    producers: list[Fact] = []
    consumers: list[Fact] = []
    for path in _files(repo, {".java", ".kt", ".kts"}):
        text = path.read_text(encoding="utf-8", errors="replace")
        relative = _relative(repo, path)
        constants = {
            name: _resolve(value, config)
            for name, value in re.findall(r"\b(?:static\s+final\s+|const\s+val\s+)(?:String\s+)?([A-Za-z_$][\w$]*)\s*=\s*[\"']([^\"']+)[\"']", text)
        }
        constants.update(
            {
                name: _resolve(value, config)
                for value, name in re.findall(r"@Value\s*\(\s*[\"']([^\"']+)[\"']\s*\)\s*(?:private\s+)?(?:String\s+)?([A-Za-z_$][\w$]*)", text)
            }
        )
        for match in re.finditer(r"@KafkaListener\s*\((.*?)\)", text, re.S):
            body = match.group(1)
            topic_value = re.search(r"\btopics?\s*=\s*(.*?)(?:,\s*\w+\s*=|$)", body, re.S)
            values = _literal_values(topic_value.group(1), config) if topic_value else _literal_values(body, config)
            if topic_value:
                values.extend(constants[name] for name in constants if re.search(rf"\b{re.escape(name)}\b", topic_value.group(1)))
            for topic in sorted(set(values)):
                consumers.append(Fact(repo.name, "kafka-consumer", topic, relative, _line(text, match.start()), "@KafkaListener"))
        producer_patterns = (
            (r"\b\w*[Kk]afka\w*\.send\s*\(\s*([\"'][^\"']+[\"']|[A-Za-z_$][\w$]*)", "KafkaTemplate.send"),
            (r"\bstreamBridge\.send\s*\(\s*([\"'][^\"']+[\"']|[A-Za-z_$][\w$]*)", "StreamBridge.send"),
        )
        for pattern, detail in producer_patterns:
            for match in re.finditer(pattern, text):
                raw = match.group(1).strip("\"'")
                topic = _resolve(constants.get(raw, raw), config)
                if topic and not topic.startswith("${"):
                    producers.append(Fact(repo.name, "kafka-producer", topic, relative, _line(text, match.start()), detail))
    binding = re.compile(r"^spring\.cloud\.stream\.bindings\.([^.]+)\.destination$")
    for key, (topic, path, line) in config.items():
        match = binding.match(key)
        if not match:
            continue
        fact = Fact(repo.name, "kafka-binding", topic, path, line, match.group(1))
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


def _http_facts(repo: Repository, config: dict[str, tuple[str, str, int]]) -> tuple[list[Fact], list[Fact]]:
    clients: list[Fact] = []
    controllers: list[Fact] = []
    mapping = re.compile(r"@(Request|Get|Post|Put|Patch|Delete)Mapping\s*(?:\((.*?)\))?", re.S)
    for path in _files(repo, {".java", ".kt"}):
        text = path.read_text(encoding="utf-8", errors="replace")
        relative = _relative(repo, path)
        class_match = re.search(r"\b(?:class|interface)\s+[A-Za-z_$][\w$]*", text)
        if not class_match:
            continue
        header = text[: class_match.start()]
        request_mappings = list(re.finditer(r"@RequestMapping\s*(?:\((.*?)\))?", header, re.S))
        prefix = _resolve(_annotation_value(request_mappings[-1].group(1) or ""), config) if request_mappings else ""
        feign = list(re.finditer(r"@FeignClient\s*\((.*?)\)", header, re.S))
        service = ""
        feign_prefix = prefix
        if feign:
            body = feign[-1].group(1)
            service = _resolve(_annotation_value(body, "name") or _annotation_value(body), config)
            feign_prefix = _resolve(_annotation_value(body, "path"), config) or prefix
        controller = bool(re.search(r"@(RestController|Controller)\b", header))
        body_start = class_match.end()
        for match in mapping.finditer(text, body_start):
            # A class-level RequestMapping is already represented by `prefix`.
            between = text[body_start:match.start()]
            if match.group(1) == "Request" and not re.search(r"\b(?:public|private|protected|fun)\b[^{};]*$", between[-300:], re.S):
                continue
            method = "ANY" if match.group(1) == "Request" else match.group(1).upper()
            suffix = _resolve(_annotation_value(match.group(2) or ""), config)
            if service:
                route = _route(f"{feign_prefix}/{suffix}")
                clients.append(Fact(repo.name, "http-client", f"{method} {route}", relative, _line(text, match.start()), service))
            if controller:
                route = _route(f"{prefix}/{suffix}")
                controllers.append(Fact(repo.name, "http-server", f"{method} {route}", relative, _line(text, match.start())))
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
    for repo in settings.repositories:
        config = _config_values(repo)
        provided, required = _maven_facts(repo)
        produced, consumed = _kafka_facts(repo, config)
        clients, servers = _http_facts(repo, config)
        maven_producers.extend(provided)
        maven_consumers.extend(required)
        kafka_producers.extend(produced)
        kafka_consumers.extend(consumed)
        http_clients.extend(clients)
        http_servers.extend(servers)
    facts.extend(maven_producers + maven_consumers + kafka_producers + kafka_consumers + http_clients + http_servers)

    relationships: set[Relationship] = set()
    for consumer in maven_consumers:
        artifact = consumer.key.rsplit(":", 1)[-1]
        for producer in maven_producers:
            if consumer.repo != producer.repo and (consumer.key == producer.key or artifact == producer.key.rsplit(":", 1)[-1]):
                relationships.add(Relationship(consumer.repo, producer.repo, "MAVEN_DEPENDS_ON", producer.key, _evidence(consumer), _evidence(producer)))
    for producer in kafka_producers:
        for consumer in kafka_consumers:
            if producer.repo != consumer.repo and producer.key == consumer.key:
                relationships.add(Relationship(producer.repo, consumer.repo, "KAFKA", producer.key, _evidence(producer), _evidence(consumer)))
    for client in http_clients:
        matched = [server for server in http_servers if server.repo != client.repo and server.key == client.key]
        for server in matched:
            relationships.add(Relationship(client.repo, server.repo, "HTTP", client.key, _evidence(client), _evidence(server)))
        if not matched:
            for repo in settings.repositories:
                if repo.name != client.repo and _service_matches(client.detail, repo.name):
                    relationships.add(Relationship(client.repo, repo.name, "FEIGN_TARGET", client.detail, _evidence(client), "repository name", "medium"))
    return facts, sorted(relationships, key=lambda item: (item.source, item.target, item.kind, item.key))


def _runtime_workflows(relationships: list[Relationship]) -> list[str]:
    runtime = [item for item in relationships if item.kind in {"KAFKA", "HTTP", "FEIGN_TARGET"}]
    adjacency: dict[str, list[Relationship]] = {}
    for item in runtime:
        adjacency.setdefault(item.source, []).append(item)
    workflows: set[str] = set()

    def walk(node: str, parts: list[str], seen: set[str]) -> None:
        outgoing = [item for item in adjacency.get(node, []) if item.target not in seen]
        if not outgoing and len(parts) > 1:
            workflows.add(" ".join(parts))
        for edge in outgoing:
            walk(edge.target, [*parts, f"--{edge.kind}:{edge.key}-->", edge.target], seen | {edge.target})

    for source in sorted(adjacency):
        walk(source, [source], {source})
    return sorted(workflows)


def generate_relationship_map(settings: Settings) -> str:
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
    settings.generated_dir.mkdir(parents=True, exist_ok=True)
    (settings.generated_dir / "PROJECT_RELATIONSHIPS.md").write_text(text, encoding="utf-8")
    return text

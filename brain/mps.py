"""Read standard MPS XML as evidence, without executing MPS or editing models.

The v9 registry indexes belong to one model. References are resolved only by
model identity and node ID; display names never establish a reference target.
"""
from __future__ import annotations

import hashlib
import io
import json
import posixpath
import re
import stat
import time
import uuid
import zipfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from xml.parsers import expat


SPLIT_SUFFIXES = {".model", ".mpsr"}
MODEL_SUFFIXES = {".mps", ".mpl", ".msd", ".mpr", ".devkit"} | SPLIT_SUFFIXES
MAX_FILE_BYTES = 3 * 1024 * 1024
MAX_PROJECT_BYTES = 16 * 1024 * 1024
MAX_PROJECT_FILES = 128
MAX_XML_ELEMENTS = 50_000
MAX_XML_DEPTH = 128
MAX_SUMMARY_BYTES = 32_000
MAX_CONNECTIONS = 8192
MAX_TRACE_ROOTS = 128
MAX_TRACE_BRANCH = 16
MAX_TRACE_DEPTH = 8
MAX_DEFINITION_USES = 512
# Stable MPS structure identities, verified against MetaIdByDeclaration and the
# structure language at JetBrains/MPS 3e609c4accef48708d1892631dadcff5826881d6.
STRUCTURE_LANGUAGE = "c72da2b9-7cce-4447-8389-f407dc1158b7"
ABSTRACT_CONCEPT = "1169125787135"
CONCEPT_DECLARATIONS = {"1071489090640", "1169125989551"}
LINK_DECLARATION = "1071489288298"
PROPERTY_DECLARATION = "1071489288299"
CONCEPT_ID_PROPERTY = "6714410169261853888"
LANGUAGE_ID_PROPERTY = "9005308665739310115"
LINK_ID_PROPERTY = "241647608299431140"
PROPERTY_ID_PROPERTY = "241647608299431129"
META_CLASS_PROPERTY = "1071599937831"


def _node_label(node: dict) -> str:
    return next((str(prop["value"]) for prop in node["properties"]
                 if prop["role"].get("name") == "name" and prop.get("value") is not None),
                node["concept"].get("name") or node["key"])


def _language_id(value: str | None) -> str | None:
    if value is None or re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value) is None:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def _numeric_id(value: str | None) -> str | None:
    if value is None or re.fullmatch(r"[+-]?[0-9]+", value) is None:
        return None
    digits = value.lstrip("+-").lstrip("0") or "0"
    if len(digits) > 19:
        return None
    number = int(digits) * (-1 if value.startswith("-") else 1)
    return str(number) if -(1 << 63) <= number < (1 << 63) else None


def _language_model_owners(project: dict) -> dict[str, list[dict]]:
    """Bind supplied models to exact descriptor UUID/source-root declarations."""
    roots = []
    generator_roots = set()

    def source_roots(section: dict, module: str):
        for root in section.get("children", []):
            attrs = root["attributes"]
            content = attrs.get("contentPath", "")
            if root["tag"] != "modelRoot" or attrs.get("type") != "default" or not (
                    content == "${module}" or content.startswith("${module}/")):
                continue
            base = posixpath.join(module, content[len("${module}"):].lstrip("/"))
            for source in root.get("children", []):
                location = source["attributes"].get("location", "")
                if source["tag"] != "sourceRoot" or any(char in location for char in ("$", "\\", ":")) or location.startswith("/"):
                    continue
                path = posixpath.normpath(posixpath.join(base, location))
                if path == ".." or path.startswith("../") or "$" in path or "\\" in path:
                    continue
                yield path, source["line"]

    for descriptor in project["files"]:
        if descriptor.get("descriptor_kind") != "language":
            continue
        language_id = _language_id(descriptor["attributes"].get("uuid"))
        module = posixpath.dirname(descriptor["path"])
        # Generator models belong to generator modules, not the language UUID.
        for section in descriptor["sections"]:
            if section["tag"] == "generators":
                for generator in section.get("children", []):
                    if generator["tag"] == "generator":
                        for models in generator.get("children", []):
                            if models["tag"] == "models":
                                generator_roots.update(path for path, _ in source_roots(models, module))
            elif section["tag"] == "models" and language_id is not None:
                for path, line in source_roots(section, module):
                    roots.append((path, {"language_id": language_id, "descriptor_path": descriptor["path"],
                                         "sha256": descriptor["sha256"], "line": line, "source_root": path}))
    owners = {}
    for model in project["files"]:
        if model["kind"] != "model":
            continue
        path = posixpath.normpath(model["path"])
        owners[model["path"]] = [] if any(root == "." or path.startswith(root + "/") for root in generator_roots) else [
            proof for root, proof in roots if root == "." or path.startswith(root + "/")]
    return owners


def _role_identity(role: dict) -> tuple[str | None, str | None, str | None]:
    return (_language_id(role.get("language", {}).get("id")),
            _numeric_id(role.get("declared_on_id")), _numeric_id(role.get("id")))


def _declaration_property(node: dict, owner: str, role: str) -> tuple[bool, str | None]:
    values = [prop.get("value") for prop in node["properties"]
              if _role_identity(prop["role"]) == (STRUCTURE_LANGUAGE, owner, role)]
    return len(values) > 1, values[0] if len(values) == 1 else None


def _regular_node_id(serialized: str | None) -> str | None:
    """Decode only canonical v9 regular IDs for identity fallback, not providers."""
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz$_ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    if (not serialized or len(serialized) > 11 or any(char not in alphabet for char in serialized)
            or len(serialized) > 1 and serialized.startswith("0")
            or len(serialized) == 11 and alphabet.index(serialized[0]) > 15):
        return None
    number = 0
    for char in serialized:
        number = (number << 6) | alphabet.index(char)
    if number >= 1 << 63:
        number -= 1 << 64
    return str(number)


def _declaration_id(node: dict, owner: str, role: str) -> tuple[str | None, str]:
    duplicate, value = _declaration_property(node, owner, role)
    if duplicate:
        return None, "duplicate_identity_property"
    if value is not None and not value.isascii():
        return None, "unsupported_numeric_property"
    explicit = _numeric_id(value)
    if explicit is not None:
        return explicit, "explicit_property"
    # The official reader permits overflow aliases; identity fallback accepts
    # only values that its standard regular-node writer could produce.
    fallback = _regular_node_id(node["id"])
    if fallback is None:
        return None, "unsupported_node_identity"
    return fallback, "regular_node_id_fallback"


def _declaration_link_kind(node: dict) -> str | None:
    duplicate, value = _declaration_property(node, LINK_DECLARATION, META_CLASS_PROPERTY)
    if duplicate:
        return None
    # LinkMetaclass's default reference member and aggregation member IDs are
    # declared in the official structure model; /suffix is only presentation.
    if value is None:
        return "reference"
    member = _regular_node_id(value.partition("/")[0])
    return {"1084199179704": "reference", "1084199179705": "child"}.get(member)


def definition_anchors(project: dict, selected: list[tuple[str, str]]) -> dict:
    """Link used stable IDs to supplied structure declarations, never to behavior."""
    owners = _language_model_owners(project)
    index: dict[tuple, list[tuple[dict, dict, dict]]] = {}
    selected = set(selected)
    uses: dict[tuple, dict] = {}
    seen_uses = set()

    def add_use(kind: str, identity: dict) -> None:
        if kind == "concept":
            key = (kind, _language_id(identity.get("language", {}).get("id")), _numeric_id(identity.get("id")))
        else:
            key = (kind, *_role_identity(identity))
        seen_uses.add(key)
        if len(uses) < MAX_DEFINITION_USES:
            uses.setdefault(key, {"kind": kind, "language_id": key[1], "concept_id": key[2],
                                  "role_id": key[3] if len(key) == 4 else None, "name": identity.get("name")})

    for model in project["files"]:
        if model["kind"] != "model":
            continue
        root_by_key = {}
        root_identity = {}
        for node in model["nodes"]:
            root = root_by_key[node["parent"]] if node["parent"] is not None else node
            root_by_key[node["key"]] = root
            concept = node["concept"]
            meta = (_language_id(concept.get("language", {}).get("id")), _numeric_id(concept.get("id")))
            if node["parent"] is None and meta[0] == STRUCTURE_LANGUAGE and meta[1] in CONCEPT_DECLARATIONS:
                duplicate, language_override = _declaration_property(node, ABSTRACT_CONCEPT, LANGUAGE_ID_PROPERTY)
                proof = {"method": "explicit_language_property"}
                if language_override:
                    language = _language_id(language_override)
                else:
                    declarations = owners.get(model["path"], [])
                    language = declarations[0]["language_id"] if len(declarations) == 1 else None
                    proof = {"method": "descriptor_source_root", "descriptor": declarations[0]} if len(declarations) == 1 else {}
                stable_id, method = _declaration_id(node, ABSTRACT_CONCEPT, CONCEPT_ID_PROPERTY)
                if not duplicate and language is not None and stable_id is not None:
                    root_identity[node["key"]] = (language, stable_id)
                    index.setdefault(("concept", language, stable_id), []).append((model, node, {**proof, "id_method": method}))
            owner_identity = root_identity.get(root["key"])
            if (owner_identity and meta[0] == STRUCTURE_LANGUAGE and meta[1] in {LINK_DECLARATION, PROPERTY_DECLARATION}
                    and node["parent"] == root["key"] and _role_identity(node.get("role", {})) == (
                        STRUCTURE_LANGUAGE, ABSTRACT_CONCEPT, "1071489727084" if meta[1] == PROPERTY_DECLARATION else "1071489727083")):
                kind = "property" if meta[1] == PROPERTY_DECLARATION else _declaration_link_kind(node)
                stable_id, method = _declaration_id(node, meta[1], PROPERTY_ID_PROPERTY if kind == "property" else LINK_ID_PROPERTY)
                if kind is not None and stable_id is not None:
                    index.setdefault((kind, *owner_identity, stable_id), []).append((model, node, {"id_method": method, "owner_anchor": f"{model['path']}#{root['key']}"}))
            if (model["path"], node["key"]) in selected:
                add_use("concept", concept)
                if node.get("role"):
                    add_use("child", node["role"])
                for prop in node["properties"]:
                    add_use("property", prop["role"])
                for ref in node["references"]:
                    add_use("reference", ref["role"])
    result = []
    for key, use in uses.items():
        matches = index.get(key, []) if all(part is not None for part in key) else []
        status = "unrecognized_identity" if any(part is None for part in key) else "resolved" if len(matches) == 1 else "ambiguous_declaration" if matches else "missing_declaration"
        targets = [{"path": model["path"], "sha256": model["sha256"], "model_ref": model["reference"],
                    "node_id": node["id"], "key": node["key"], "anchor": f"{model['path']}#{node['key']}",
                    "line_start": node["line_start"], "line_end": node["line_end"], "identity_proof": proof}
                   for model, node, proof in matches[:16]]
        result.append({**use, "status": status, "target": targets[0] if status == "resolved" else None,
                       "candidates": targets if status == "ambiguous_declaration" else [], "candidate_count": len(matches),
                       "semantics": "Identity link to supplied structure source; constraints, generators and runtime behavior require further evidence."})
    return {"bindings": result, "omitted_uses": len(seen_uses) - len(uses)}


def model_connections(project: dict) -> dict:
    """Lift explicit references to their enclosing model roots, preserving callsites.

    A root is a structural boundary, not an assumed executable flow. References
    keep their actual role and target node, including jumps into a nested node.
    """
    models = {entry["path"]: entry for entry in project["files"] if entry["kind"] == "model"}
    nodes: dict[tuple[str, str], dict] = {}
    persistent: dict[tuple[str, str], dict] = {}
    owners: dict[tuple[str, str], tuple[str, str]] = {}
    roots: dict[tuple[str, str], dict] = {}

    def location(model: dict, node: dict) -> dict:
        return {"path": model["path"], "model_ref": model["reference"], "node_id": node["id"],
                "model_identity": model["identity"],
                "key": node["key"], "concept": node["concept"].get("name"), "label": _node_label(node),
                "concept_identity": node["concept"], "sha256": model["sha256"],
                "anchor": f"{model['path']}#{node['key']}",
                "line_start": node["line_start"], "line_end": node["line_end"]}

    for path, model in models.items():
        for node in model["nodes"]:  # Parser order is parent before children.
            key = (path, node["key"])
            nodes[key] = node
            if node["id"] is not None:
                persistent[(path, node["id"])] = node
            owners[key] = owners[(path, node["parent"])] if node["parent"] is not None else key
            if owners[key] == key:
                roots[key] = location(model, node)
    connections = []
    omitted = 0
    for path, model in models.items():
        for node in model["nodes"]:
            for ref in node["references"]:
                if len(connections) >= MAX_CONNECTIONS:
                    omitted += 1
                    continue
                target = persistent.get((ref.get("target_path"), ref.get("target_node"))) if ref["status"] == "resolved" else None
                target_key = (ref["target_path"], target["key"]) if target is not None else None
                source_root = owners[(path, node["key"])]
                target_root = owners[target_key] if target_key is not None else None
                ancestry = []
                current = node
                while current["parent"] is not None:
                    ancestry.append({"key": current["key"], "role": current.get("role"),
                                     "concept": current["concept"].get("name"), "line": current["line_start"]})
                    current = nodes[(path, current["parent"])]
                connections.append({
                    "identity": hashlib.sha256(json.dumps([path, node["key"], ref["line"], ref["serialized"]], sort_keys=True).encode()).hexdigest(),
                    "source_root": roots[source_root], "source_node": location(model, node),
                    "containment_path": list(reversed(ancestry)), "role": ref["role"], "line": ref["line"],
                    "status": ref["status"], "scope": ref.get("scope"), "serialized": ref["serialized"],
                    "target_model": ref.get("target_model"), "target_node_id": ref.get("target_node"),
                    "target_node": location(models[ref["target_path"]], target) if target is not None else None,
                    "target_root": roots[target_root] if target_root is not None else None,
                    "within_root": source_root == target_root,
                })
    return {"roots": list(roots.values()), "connections": connections, "omitted_connections": omitted,
            "semantics": "Explicit model-reference connections; executable flow, branch order and return behavior require language evidence."}


def trace_model_connections(project: dict, queries: list[str], *, direction: str = "outgoing", depth: int = MAX_TRACE_DEPTH,
                            query_paths: dict[str, set[str]] | None = None) -> dict:
    """Bounded multi-root navigation and reverse impact, with continuation anchors."""
    if direction not in {"outgoing", "incoming"} or not 1 <= depth <= MAX_TRACE_DEPTH:
        raise MpsError("MPS trace requires outgoing/incoming direction and depth 1..8")
    graph = model_connections(project)
    roots = {(item["path"], item["key"]): item for item in graph["roots"]}
    by_root: dict[tuple[str, str], list[dict]] = {}
    for connection in graph["connections"]:
        origin = connection["source_root"] if direction == "outgoing" else connection["target_root"]
        if origin is not None:
            by_root.setdefault((origin["path"], origin["key"]), []).append(connection)
    wanted = {query.strip() for query in queries[:16] if query.strip()}
    seed_keys = set()
    matched_nodes = []
    matched_count = 0
    query_roots: dict[str, set[tuple[str, str]]] = {}
    for model in project["files"]:
        owner = {}
        for node in model.get("nodes", []):
            owner[node["key"]] = owner[node["parent"]] if node["parent"] is not None else node["key"]
            identities = {model["path"], model["reference"], f"{model['path']}#{node['key']}"}
            if model["identity"] is not None:
                identities.add(model["identity"])
            if node["id"] is not None:
                identities.add(f"{model['reference']}#{node['id']}")
                if model["identity"] is not None:
                    identities.add(f"{model['identity']}#{node['id']}")
            names = {_node_label(node).casefold(), str(node["concept"].get("name") or "").casefold()}
            matches = {query for query in wanted if (query_paths is None or model["path"] in query_paths.get(query, set()))
                       and (query in identities or query.casefold() in names)}
            if matches:
                root_key = (model["path"], owner[node["key"]])
                seed_keys.add(root_key)
                matched_count += 1
                for query in matches:
                    query_roots.setdefault(query, set()).add(root_key)
                if len(matched_nodes) < MAX_TRACE_ROOTS:
                    matched_nodes.append({"path": model["path"], "key": node["key"], "node_id": node["id"],
                                          "anchor": f"{model['path']}#{node['key']}", "line": node["line_start"]})
    seeds = [key for key in roots if key in seed_keys]
    queue = deque((key, 0) for key in seeds[:MAX_TRACE_ROOTS])
    seen = set(key for key, _ in queue)
    steps = []
    frontier = []
    truncated = bool(graph["omitted_connections"] or len(seeds) > MAX_TRACE_ROOTS or len(queries) > 16)
    while queue:
        key, hops = queue.popleft()
        edges = by_root.get(key, [])
        if hops >= depth:
            if edges:
                frontier.append(roots[key])
                truncated = True
            continue
        if len(edges) > MAX_TRACE_BRANCH:
            frontier.append(roots[key])
            truncated = True
        for connection in edges[:MAX_TRACE_BRANCH]:
            neighbor = connection["target_root"] if direction == "outgoing" else connection["source_root"]
            neighbor_key = (neighbor["path"], neighbor["key"]) if neighbor is not None else None
            revisit = neighbor_key in seen if neighbor_key is not None else False
            steps.append({**connection, "depth": hops, "direction": direction, "revisited_root": revisit})
            if neighbor_key is not None and not revisit:
                if len(seen) >= MAX_TRACE_ROOTS:
                    frontier.append(neighbor)
                    truncated = True
                else:
                    seen.add(neighbor_key)
                    queue.append((neighbor_key, hops + 1))
    return {"seeds": [roots[key] for key in seeds[:MAX_TRACE_ROOTS]], "matched_nodes": matched_nodes,
            "omitted_seed_roots": max(0, len(seeds) - MAX_TRACE_ROOTS),
            "omitted_matches": matched_count - len(matched_nodes),
            "ambiguous_queries": [query for query in sorted(query_roots) if len(query_roots[query]) > 1],
            "unmatched_queries": sorted(wanted - query_roots.keys()), "steps": steps,
            "frontier": list({(root["path"], root["key"]): root for root in frontier}.values()),
            "truncated": truncated, "omitted_connections": graph["omitted_connections"], "semantics": graph["semantics"],
            "bounds": {"depth": depth, "branch": MAX_TRACE_BRANCH, "roots": MAX_TRACE_ROOTS}}


class MpsError(ValueError):
    """Unsupported persistence, invalid source, or an exceeded read limit."""


@dataclass
class _Element:
    tag: str
    attrs: dict[str, str]
    line: int
    end: int = 0
    text: str = ""
    children: list[_Element] = field(default_factory=list)


def _read_xml(source: bytes) -> _Element:
    if len(source) > MAX_FILE_BYTES:
        raise MpsError("MPS XML exceeds the 3 MB file limit")
    try:
        source.decode("utf-8")
    except UnicodeDecodeError as error:
        raise MpsError("Only UTF-8 MPS XML exports are supported") from error
    parser = expat.ParserCreate()
    stack: list[_Element] = []
    root: _Element | None = None
    count = 0

    def start(tag: str, attrs: dict[str, str]) -> None:
        nonlocal root, count
        count += 1
        if count > MAX_XML_ELEMENTS or len(stack) >= MAX_XML_DEPTH:
            raise MpsError("MPS XML exceeds the element or nesting limit")
        element = _Element(tag, attrs, parser.CurrentLineNumber)
        if stack:
            stack[-1].children.append(element)
        else:
            root = element
        stack.append(element)

    def end(_tag: str) -> None:
        stack.pop().end = parser.CurrentLineNumber

    def characters(value: str) -> None:
        if stack and value.strip():
            stack[-1].text += value

    def forbidden(*_args: object) -> None:
        raise MpsError("DTD and entity declarations are not supported")

    def declaration(_version: str, encoding: str | None, _standalone: int) -> None:
        if encoding and encoding.lower().replace("-", "") not in {"utf8", "usascii"}:
            raise MpsError("Only UTF-8 MPS XML exports are supported")

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = characters
    parser.XmlDeclHandler = declaration
    parser.StartDoctypeDeclHandler = forbidden
    parser.EntityDeclHandler = forbidden
    parser.ExternalEntityRefHandler = forbidden
    try:
        parser.Parse(source, True)
    except expat.ExpatError as error:
        raise MpsError(f"Invalid XML at line {error.lineno}: {expat.ErrorString(error.code)}") from error
    if root is None:
        raise MpsError("Empty MPS XML")
    return root


def _children(element: _Element, tag: str) -> list[_Element]:
    return [child for child in element.children if child.tag == tag]


def _section(element: _Element, tag: str) -> list[_Element]:
    return [child for section in _children(element, tag) for child in section.children]


def _tree(element: _Element) -> dict:
    """Preserve descriptor/generator nesting without inferring its semantics."""
    result = {"tag": element.tag, "attributes": element.attrs, "line": element.line}
    if element.text:
        result["text"] = element.text
    if element.children:
        result["children"] = [_tree(child) for child in element.children]
    return result


def _unescape_reference(value: str) -> str:
    # MPS StringUtil decodes each %xx as one character, not URL/UTF-8 bytes.
    if re.search(r"%(?![0-9a-fA-F]{2})", value):
        raise MpsError("Invalid model-reference escape")
    return re.sub(r"%([0-9a-fA-F]{2})", lambda match: chr(int(match[1], 16)), value)


def parse_model_reference(reference: str) -> dict:
    """Match SModelReference grammar before unescaping separator characters."""
    encoded = reference.strip()
    left, right = encoded.find("("), encoded.rfind(")")
    presentation = None
    if left > 0 and right == len(encoded) - 1:
        encoded, presentation = encoded[:left], encoded[left + 1:right]
    if "(" in encoded or ")" in encoded:
        raise MpsError("Invalid model-reference parentheses")
    module_id, model_id = encoded.split("/", 1) if "/" in encoded else (None, encoded)
    module_id = _unescape_reference(module_id) if module_id is not None else None
    model_id = _unescape_reference(model_id)
    if not model_id or module_id == "":
        raise MpsError("Empty model/module identity")
    module_name, model_name = None, None
    if presentation is not None:
        module_name, model_name = presentation.split("/", 1) if "/" in presentation else (None, presentation)
        module_name = _unescape_reference(module_name) if module_name is not None else None
        model_name = _unescape_reference(model_name)
    prefix = model_id.partition(":")[0]
    globalness = True if prefix in {"r", "f", "m", "path"} else False if prefix == "i" else None
    if prefix == "r":
        try:
            model_id = "r:" + str(uuid.UUID(model_id[2:]))
        except ValueError:
            pass  # Opaque source IDs are retained; this reader is not an MPS runtime validator.
    if prefix == "i":
        if module_id is None:
            raise MpsError("Module-private integer model IDs require a module identity")
        try:
            model_id = "i:" + format(int(model_id[2:], 16), "04x")
        except ValueError as error:
            raise MpsError("Invalid integer model identity") from error
    identity = model_id if globalness else json.dumps([module_id, model_id], separators=(",", ":")) if globalness is False else None
    return {"raw": reference, "module_id": module_id, "model_id": model_id,
            "module_name": module_name, "model_name": model_name,
            "globally_unique": globalness, "identity": identity}


def _model(root: _Element) -> dict:
    if "content" in root.attrs:
        raise MpsError("Split/file-per-root persistence requires stream grouping or an MPS runtime export")
    persistence = _children(root, "persistence")
    if len(persistence) != 1 or persistence[0].attrs.get("version") != "9":
        raise MpsError("Only standard single-file MPS XML persistence version 9 is supported")
    reference = root.attrs.get("ref", "")
    if not reference or not _children(root, "registry"):
        raise MpsError("MPS v9 requires a model reference and a registry")
    reference_parts = parse_model_reference(reference)
    concepts: dict[str, dict] = {}
    roles: dict[str, dict[str, dict]] = {"property": {}, "reference": {}, "child": {}}
    for language in _section(root, "registry"):
        if language.tag != "language":
            continue
        for concept in _children(language, "concept"):
            index = concept.attrs.get("index", "")
            if not index or index in concepts:
                raise MpsError("Missing or duplicate concept registry index")
            concepts[index] = {**concept.attrs, "language": language.attrs}
            for role in concept.children:
                if role.tag not in roles:
                    continue
                role_index = role.attrs.get("index", "")
                if not role_index or role_index in roles[role.tag]:
                    raise MpsError("Missing or duplicate role registry index")
                roles[role.tag][role_index] = {**role.attrs, "declared_on": concept.attrs.get("name"),
                                              "declared_on_id": concept.attrs.get("id"), "language": language.attrs}

    warnings: set[str] = set()

    def decode(kind: str, index: str) -> dict:
        value = roles[kind].get(index)
        if value is None:
            warnings.add(f"Unknown {kind} registry index: {index}")
            return {"index": index, "name": None}
        return value

    imports: dict[str, str] = {}
    import_details = []
    for entry in _section(root, "imports"):
        if entry.tag != "import":
            continue
        index = entry.attrs.get("index", "")
        if not index or index in imports or not entry.attrs.get("ref"):
            raise MpsError("Missing or duplicate model import index/reference")
        imports[index] = entry.attrs["ref"]
        import_details.append({"attributes": entry.attrs, "reference_parts": parse_model_reference(entry.attrs["ref"])})
    nodes: list[dict] = []
    roots = _children(root, "node")
    pending = [(element, None) for element in reversed(roots)]
    ordered: list[tuple[_Element, _Element | None]] = []
    keys: dict[int, str] = {}
    ids: set[str] = set()
    while pending:
        element, parent = pending.pop()
        node_id = element.attrs.get("id")
        if node_id == "" or (node_id is not None and node_id in ids):
            raise MpsError("Empty or duplicate persistent node ID in model")
        if node_id is not None:
            ids.add(node_id)
        keys[id(element)] = f"id:{node_id}" if node_id is not None else f"source:{len(ordered)}"
        ordered.append((element, parent))
        pending.extend((child, element) for child in reversed(_children(element, "node")))
    for element, parent in ordered:
        node_id = element.attrs.get("id")
        concept_index = element.attrs.get("concept", "")
        concept = concepts.get(concept_index)
        if concept is None:
            warnings.add(f"Unknown concept registry index: {concept_index}")
        children = _children(element, "node")
        node = {
            "id": node_id, "key": keys[id(element)],
            "identity_kind": "persistent" if node_id is not None else "source_location",
            "concept": concept or {"index": concept_index, "name": None},
            "parent": keys[id(parent)] if parent is not None else None,
            "children": [keys[id(child)] for child in children],
            "line_start": element.line, "line_end": element.end,
            "properties": [], "references": [],
        }
        if "role" in element.attrs:
            node["role"] = decode("child", element.attrs["role"])
        for prop in _children(element, "property"):
            node["properties"].append({"role": decode("property", prop.attrs.get("role", "")),
                                       "value": prop.attrs.get("value"), "line": prop.line})
        for ref in _children(element, "ref"):
            item = {"role": decode("reference", ref.attrs.get("role", "")),
                    "serialized": ref.attrs, "line": ref.line}
            local, external = ref.attrs.get("node"), ref.attrs.get("to")
            if local is not None and external is not None:
                item["status"] = "invalid_target"
            elif local is not None:
                item.update(target_model=reference, target_node=local, scope="local")
            elif external is not None and ":" in external:
                index, target = external.split(":", 1)
                item.update(target_model=imports.get(index), target_node=target, scope="imported")
                if index not in imports:
                    item["status"] = "unknown_import"
            else:
                item["status"] = "invalid_target"
            if item.get("target_node", "").startswith("^"):
                item["status"] = "dynamic"
            node["references"].append(item)
        nodes.append(node)
    return {
        "kind": "model", "reference": reference, "reference_parts": reference_parts,
        "identity": reference_parts["identity"],
        "attributes": root.attrs, "model_attributes": [_tree(child) for child in _children(root, "attribute")],
        "persistence": "9", "imports": imports, "import_details": import_details,
        "languages": [_tree(child) for child in _section(root, "languages")],
        "registry": list(concepts.values()), "roots": [keys[id(node)] for node in roots],
        "nodes": nodes, "warnings": sorted(warnings),
    }


def parse_document(source: bytes) -> dict:
    """Decode an XML model or module descriptor; retain source line locations."""
    root = _read_xml(source)
    if root.tag == "model":
        return _model(root)
    if root.tag not in {"language", "solution", "dev-kit", "project"}:
        raise MpsError(f"Unsupported MPS XML root: {root.tag}")
    return {"kind": "descriptor", "descriptor_kind": root.tag, "attributes": root.attrs,
            "sections": [_tree(child) for child in root.children]}


def parse_project(documents: dict[str, bytes], *, deadline: float | None = None) -> dict:
    """Resolve the project's reference graph without traversing local paths."""
    if len(documents) > MAX_PROJECT_FILES or sum(map(len, documents.values())) > MAX_PROJECT_BYTES:
        raise MpsError("MPS project exceeds the 128 file or 16 MB decoded-source limit")
    files = []
    targets: dict[tuple[str, str], list[str]] = {}
    for path, source in sorted(documents.items()):
        _check_deadline(deadline)
        entry = {"path": path, "sha256": hashlib.sha256(source).hexdigest()}
        try:
            if PurePosixPath(path).suffix.lower() in SPLIT_SUFFIXES:
                raise MpsError("Split/file-per-root streams require grouping or an MPS runtime export")
            entry.update(parse_document(source))
        except MpsError as error:
            entry.update(kind="unsupported", reason=str(error))
        files.append(entry)
        if entry["kind"] == "model" and entry["identity"] is not None:
            for node in entry["nodes"]:
                if node["id"] is not None:
                    targets.setdefault((entry["identity"], node["id"]), []).append(path)
    counts: dict[str, int] = {}
    for model in files:
        _check_deadline(deadline)
        local_ids = {node["id"] for node in model.get("nodes", []) if node["id"] is not None}
        for node in model.get("nodes", []):
            for ref in node["references"]:
                if "status" not in ref:
                    if ref["scope"] == "local":
                        matches = [model["path"]] if ref["target_node"] in local_ids else []
                    else:
                        identity = parse_model_reference(ref["target_model"])["identity"]
                        if identity is None:
                            ref["status"] = "unsupported_identity"
                            counts[ref["status"]] = counts.get(ref["status"], 0) + 1
                            continue
                        matches = targets.get((identity, ref["target_node"]), [])
                    ref["status"] = "resolved" if len(matches) == 1 else "ambiguous" if matches else "missing_target"
                    if len(matches) == 1:
                        ref["target_path"] = matches[0]
                counts[ref["status"]] = counts.get(ref["status"], 0) + 1
    return {"files": files, "reference_counts": counts}


def documents_from_attachment(filename: str, source: bytes) -> dict[str, bytes] | None:
    """Read bounded MPS attachment members in memory, without extracting paths."""
    suffix = PurePosixPath(filename).suffix.lower()
    if suffix in MODEL_SUFFIXES:
        if len(source) > MAX_FILE_BYTES:
            raise MpsError("MPS XML exceeds the 3 MB file limit")
        return {filename: source}
    if suffix != ".zip":
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(source)) as archive:
            members = archive.infolist()
            if len(members) > 2048:
                raise MpsError("ZIP exceeds the 2048 member limit")
            selected = [item for item in members if PurePosixPath(item.filename).suffix.lower() in MODEL_SUFFIXES]
            if not selected:
                return None
            if len(selected) > MAX_PROJECT_FILES or sum(item.file_size for item in selected) > MAX_PROJECT_BYTES:
                raise MpsError("MPS ZIP exceeds the 128 file or 16 MB decoded-source limit")
            seen = set()
            documents = {}
            for item in selected:
                path = PurePosixPath(item.filename)
                if (item.orig_filename != item.filename or path.is_absolute() or ".." in path.parts or "\\" in item.filename
                        or ":" in item.filename or not item.filename or item.filename.casefold() in seen):
                    raise MpsError("MPS ZIP contains an unsafe or duplicate member path")
                seen.add(item.filename.casefold())
                if (item.flag_bits & 1 or stat.S_ISLNK(item.external_attr >> 16)
                        or item.file_size > MAX_FILE_BYTES):
                    raise MpsError("MPS ZIP contains an encrypted, symlink, or oversized model")
                with archive.open(item) as stream:
                    content = stream.read(MAX_FILE_BYTES + 1)
                if len(content) > MAX_FILE_BYTES:
                    raise MpsError("MPS ZIP model exceeds the 3 MB file limit")
                documents[item.filename] = content
            return documents
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError, EOFError) as error:
        raise MpsError("Unable to read the MPS ZIP archive") from error


def project_from_attachment(filename: str, source: bytes) -> dict | None:
    """Recognize and decode MPS attachments using the same bounded member reader."""
    documents = documents_from_attachment(filename, source)
    return parse_project(documents) if documents is not None else None


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.perf_counter() >= deadline:
        raise MpsError("MPS navigation time budget exceeded; repeat a focused model/node request")


def focused_navigation(documents: dict[str, bytes], queries: list[str], *, incoming: bool = False,
                       query_paths: dict[str, set[str]] | None = None, deadline: float | None = None,
                       project: dict | None = None) -> str | None:
    """Hydrate a bounded requested route from original attachment member bytes."""
    _check_deadline(deadline)
    project = project if project is not None else parse_project(documents, deadline=deadline)
    _check_deadline(deadline)
    trace = trace_model_connections(project, queries, direction="incoming" if incoming else "outgoing", query_paths=query_paths)
    _check_deadline(deadline)
    requested_regions = []
    source_files = {}
    file_entries = {entry["path"]: entry for entry in project["files"]}
    for query in queries[:16]:
        file_path = query.partition("#")[0]
        entry = file_entries.get(file_path)
        if entry is not None and entry["kind"] != "model" and (
                query_paths is None or file_path in query_paths.get(query, set())):
            source_files[file_path] = {key: entry[key] for key in ("path", "kind", "descriptor_kind", "reason", "sha256") if key in entry}
            source_files[file_path]["status"] = "source_only"
            if "#lines:" not in query:
                requested_regions.append((file_path, 1, 20))
        path, marker, span = query.rpartition("#lines:")
        match = re.fullmatch(r"([0-9]{1,8})-([0-9]{1,8})", span) if marker and path in documents and (
            query_paths is None or path in query_paths.get(query, set())) else None
        if match:
            start, end = map(int, match.groups())
            if 1 <= start <= end and end - start < 80:
                requested_regions.append((path, start, end))
    if not trace["seeds"] and not requested_regions:
        return None
    result = {"format": "mps-focused-navigation", "authority": "user-supplied external source, not pinned repository proof",
              "route_status": "structural" if trace["seeds"] else "source_only",
              "file_statuses": list(source_files.values()),
              "source_only_semantics": "Descriptors and unsupported files provide declaration/raw source only; no model flow is established." if source_files else None,
              "semantics": trace["semantics"], "bounds": trace["bounds"],
              "seeds": trace["seeds"][:16], "matched_nodes": trace["matched_nodes"][:16],
              "steps": [], "frontier": trace["frontier"][:16],
              "truncated": bool(trace["truncated"] or trace["omitted_matches"] or len(trace["seeds"]) > 16 or len(trace["matched_nodes"]) > 16),
              "omitted_seed_roots": trace["omitted_seed_roots"] + max(0, len(trace["seeds"]) - 16),
              "ambiguous_queries": trace["ambiguous_queries"],
              "unmatched_queries": [query for query in trace["unmatched_queries"] if not any(
                  query == f"{path}#lines:{start}-{end}" for path, start, end in requested_regions) and query not in source_files],
              "omitted_matches": trace["omitted_matches"] + max(0, len(trace["matched_nodes"]) - 16),
              "unrendered_steps": len(trace["steps"]), "omitted_connections": trace["omitted_connections"],
              "unrendered_frontier_roots": max(0, len(trace["frontier"]) - 16),
              "definition_anchors": [], "omitted_definition_anchors": 0,
              "source_regions": [], "omitted_source_regions": 0,
              "selection": "Names return all matching candidates; an exact path/node anchor disambiguates them. Source regions are bounded windows."}

    def encoded() -> str:
        return json.dumps(result, ensure_ascii=True, indent=2)

    # Compact locations keep exact root/callsite/target identity without repeating
    # every registry entry and XML attribute in both graph and source regions.
    def compact(edge: dict) -> dict:
        return {key: edge[key] for key in ("identity", "source_root", "source_node", "target_root", "target_node",
                                          "role", "line", "status", "within_root", "depth", "revisited_root")}

    locations = [(item["path"], max(1, item["line"] - 3), item["line"] + 16)
                 for item in trace["matched_nodes"][:16]]
    definition_nodes = [(item["path"], item["key"]) for item in trace["matched_nodes"][:16]]
    for edge in trace["steps"]:
        result["steps"].append(compact(edge))
        if len(encoded().encode()) > MAX_SUMMARY_BYTES // 2:
            result["steps"].pop()
            result["truncated"] = True
            break
        result["unrendered_steps"] -= 1
        definition_nodes.append((edge["source_node"]["path"], edge["source_node"]["key"]))
        locations.append((edge["source_node"]["path"], max(1, edge["line"] - 3), edge["line"] + 16))
        if edge["target_node"] is not None:
            definition_nodes.append((edge["target_node"]["path"], edge["target_node"]["key"]))
            line = edge["target_node"]["line_start"]
            locations.append((edge["target_node"]["path"], max(1, line - 3), line + 16))
    definitions = definition_anchors(project, definition_nodes)
    _check_deadline(deadline)
    result["truncated"] = bool(result["truncated"] or definitions["omitted_uses"])
    result["omitted_definition_anchors"] = len(definitions["bindings"]) + definitions["omitted_uses"]
    for definition in definitions["bindings"]:
        result["definition_anchors"].append(definition)
        if len(encoded().encode()) > MAX_SUMMARY_BYTES * 3 // 4:
            result["definition_anchors"].pop()
            result["truncated"] = True
            break
        result["omitted_definition_anchors"] -= 1
        if definition["target"] is not None:
            target = definition["target"]
            line = target["line_start"]
            locations.append((target["path"], max(1, line - 3), line + 16))
    locations = list(dict.fromkeys([*requested_regions, *locations]))
    result["omitted_source_regions"] = len(locations)
    for path, start, end in locations:
        source = documents[path]
        lines = source.decode("utf-8").splitlines()
        if start > len(lines):
            continue
        end = min(len(lines), end)
        region = {"path": path, "sha256": hashlib.sha256(source).hexdigest(),
                  "line_start": start, "line_end": end, "content": "\n".join(lines[start - 1:end]),
                  "continuation_anchor": f"{path}#lines:{end + 1}-{min(len(lines), end + 20)}" if end < len(lines) else None,
                  "complete_file": start == 1 and end == len(lines)}
        result["source_regions"].append(region)
        if len(encoded().encode()) > MAX_SUMMARY_BYTES - 1024:
            result["source_regions"].pop()
            result["truncated"] = True
            break
        result["omitted_source_regions"] -= 1
    result["truncated"] = bool(result["truncated"] or result["omitted_source_regions"])
    # A single long model label/property can exceed the route budget before any
    # step is added. Return the exact focused anchor, without a clipped identity.
    if len(encoded().encode()) > MAX_SUMMARY_BYTES:
        return json.dumps({"format": result["format"], "truncated": True,
                           "reason": "MPS anchors exceed the focused navigation byte limit"})
    return encoded()


def render_navigation(project: dict) -> str:
    """Produce valid bounded JSON; omissions are explicit and never source proof."""
    clipped_strings = 0

    def clip(value: object) -> object:
        nonlocal clipped_strings
        if isinstance(value, str) and len(value) > 1024:
            clipped_strings += 1
            return value[:1024] + " [value truncated]"
        if isinstance(value, dict):
            return {key: clip(item) for key, item in value.items()}
        if isinstance(value, list):
            return [clip(item) for item in value]
        return value

    result = {
        "format": "mps-v9-navigation", "reference_counts": project["reference_counts"],
        "limitations": [
            "Containment and source order are AST structure; they do not establish runtime execution order.",
            "Registry names decode used concepts/roles; language aspects define their business semantics.",
            "Descriptor/generator sections are declarations, not observed generated code or a generation trace.",
            "Only supplied standard v9 XML model files are decoded; references resolve only among supplied files.",
            "Node keys identify containment within this source; source_location keys are not persistent MPS node IDs.",
            "Known built-in model ID factories follow MPS global/module-private identity rules; custom factory identity stays unresolved.",
        ],
        "connections": [], "omitted_connections": 0,
        "files": [], "omitted_files": 0, "truncated_values": 0,
    }

    def encoded() -> str:
        result["truncated_values"] = clipped_strings
        return json.dumps(result, ensure_ascii=True, indent=2)

    graph = model_connections(project)
    result["omitted_connections"] = len(graph["connections"]) + graph["omitted_connections"]
    # Reserve most of the handoff for registry/node evidence, but expose nested
    # cross-root jumps even when their source node would otherwise be omitted.
    for connection in graph["connections"]:
        compact = {key: connection[key] for key in ("source_node", "target_node", "role", "line", "status", "within_root", "containment_path")}
        compact.update(source_root_anchor=connection["source_root"]["anchor"],
                       target_root_anchor=connection["target_root"]["anchor"] if connection["target_root"] else None)
        result["connections"].append(clip(compact))
        if len(encoded().encode()) > MAX_SUMMARY_BYTES // 3:
            result["connections"].pop()
            break
        result["omitted_connections"] -= 1

    # Keep per-file counts even if the bounded context cannot carry all nodes.
    for entry in project["files"]:
        base = {key: clip(value) for key, value in entry.items()
                if key not in {"nodes", "registry", "sections", "languages", "imports", "roots"}}
        base.update(node_count=len(entry.get("nodes", [])), omitted_nodes=len(entry.get("nodes", [])),
                    omitted_sections=len(entry.get("sections", [])), omitted_registry=len(entry.get("registry", [])))
        result["files"].append(base)
        if len(encoded().encode()) > MAX_SUMMARY_BYTES - 1024:
            result["files"].pop()
            result["omitted_files"] += 1
            continue
        for key in ("roots", "imports", "languages", "registry", "sections", "nodes"):
            values = entry.get(key)
            if values is None:
                continue
            if isinstance(values, dict):
                base[key] = clip(values)
                if len(encoded().encode()) > MAX_SUMMARY_BYTES - 1024:
                    del base[key]
                    base[f"omitted_{key}"] = len(values)
                continue
            base[key] = []
            for value in values:
                base[key].append(clip(value))
                if len(encoded().encode()) > MAX_SUMMARY_BYTES - 1024:
                    base[key].pop()
                    base[f"omitted_{key}"] = len(values) - len(base[key])
                    break
                if key in {"nodes", "sections", "registry"}:
                    base[f"omitted_{key}"] -= 1
    return encoded()

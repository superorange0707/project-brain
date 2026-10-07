"""Optional immutable MPS sources, separate from lexical snapshot membership."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote

from . import mps
from .platforms import atomic_managed_text_write, read_managed_bytes, read_managed_text

SCHEMA_VERSION = "mps-models-v1"
PARSER_VERSION = "mps-v9-v2"
SUPPORTED_PARSERS = {"mps-v9-v1", "mps-v9-v2"}
MAPPING_PATH = "mps-flow-mappings.json"
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024


def _hash(value: object) -> str:
    from .catalog import _content_hash
    return _content_hash(value)


def _safe_path(path: str) -> bool:
    from .core import IGNORED_DIRS, SENSITIVE_FILE_NAMES, SENSITIVE_SUFFIXES
    parts = PurePosixPath(path).parts
    return bool(parts) and PurePosixPath(path).as_posix() == path and not path.startswith("/") and "\\" not in path and ":" not in path and all(
        part not in {".", ".."} and part not in IGNORED_DIRS for part in parts
    ) and parts[-1].lower() not in SENSITIVE_FILE_NAMES and PurePosixPath(path).suffix.lower() not in SENSITIVE_SUFFIXES


def is_model_path(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in mps.MODEL_SUFFIXES


def _mapping_entries(source: str) -> list[dict]:
    try:
        value = json.loads(source) if len(source.encode()) <= 64_000 else None
        entries = value.get("mappings") if isinstance(value, dict) and value.get("version") == 1 else None
        return entries if isinstance(entries, list) and len(entries) <= 64 and all(isinstance(item, dict) for item in entries) else []
    except ValueError:
        return []


def build_component(settings, state: dict) -> dict:
    """Capture Git objects or sealed exports; never read an unsealed working tree."""
    from .index import _git_blob_contents, _git_manifest
    from .sync import _sealed_snapshot_is_intact, _snapshot_seal_path, MAX_GIT_SNAPSHOT_SEAL_BYTES

    snapshots = {name: str(raw.get("sha") or "working-tree") for name, raw in state.items() if isinstance(raw, dict)}
    files = []
    limitations = []
    source_bytes = 0
    model_count = 0
    artifact_bytes = 0
    failed = False
    for name, sha in sorted(snapshots.items()):
        repo = replace(settings.repo(name), source_sha=sha)
        if repo.source_warning and "MPS discovery incomplete:" in repo.source_warning:
            limitations.append({"repo": name, "reason": repo.source_warning.split("MPS discovery incomplete:", 1)[1].strip()})
        try:
            manifest = _git_manifest(repo)
            seal = None
            if manifest is None:
                target = repo.source_path
                if target is None or not target.resolve().is_relative_to(settings.state_dir.resolve()):
                    raise ValueError("no immutable Git or managed snapshot source")
                seal_path = _snapshot_seal_path(target.parent, sha)
                if not _sealed_snapshot_is_intact(target, seal_path, sha):
                    raise ValueError("managed source snapshot seal is unavailable or changed")
                seal = json.loads(read_managed_text(settings.state_dir, seal_path, max_bytes=MAX_GIT_SNAPSHOT_SEAL_BYTES))["files"]
                manifest = {path: ("100644", "sha256:" + details["sha256"]) for path, details in seal.items()}
            selected = [path for path, (mode, _) in sorted(manifest.items())
                        if mode in {"100644", "100755"} and _safe_path(path) and (is_model_path(path) or path == MAPPING_PATH)]
            omitted = max(0, len(selected) - (mps.MAX_PROJECT_FILES - len(files)))
            selected = selected[:max(0, mps.MAX_PROJECT_FILES - len(files))]

            def load(paths: list[str], remaining: int) -> dict[str, bytes]:
                if not paths or remaining <= 0:
                    return {}
                if seal is not None:
                    values = {}
                    for path in paths:
                        size = seal[path]["size"]
                        if size <= min(mps.MAX_FILE_BYTES, remaining):
                            values[path] = read_managed_bytes(settings.state_dir, repo.source_path / path, max_bytes=size)
                            remaining -= size
                    return values
                blobs = dict(_git_blob_contents(repo, {manifest[path][1] for path in paths}, max_total_bytes=remaining))
                return {path: blobs[manifest[path][1]] for path in paths if manifest[path][1] in blobs}

            sources = load(selected, mps.MAX_PROJECT_BYTES - source_bytes)
            mapping = sources.get(MAPPING_PATH, b"").decode("utf-8", errors="replace")
            evidence_paths = sorted({str((entry.get("evidence") or {}).get("path") or "")
                                     for entry in _mapping_entries(mapping) if isinstance(entry.get("evidence"), dict)})
            evidence_paths = [path for path in evidence_paths if path in manifest and path not in sources
                              and manifest[path][0] in {"100644", "100755"} and _safe_path(path)]
            available = max(0, mps.MAX_PROJECT_FILES - len(files) - len(sources))
            omitted += max(0, len(evidence_paths) - available)
            evidence_sources = load(evidence_paths[:available], max(0, mps.MAX_PROJECT_BYTES - source_bytes - sum(map(len, sources.values()))))
            omitted += len(evidence_paths[:available]) - len(evidence_sources)
            sources.update(evidence_sources)
            for path, raw in sorted(sources.items()):
                if source_bytes + len(raw) > mps.MAX_PROJECT_BYTES:
                    omitted += 1
                    continue
                try:
                    source = raw.decode("utf-8")
                except UnicodeDecodeError:
                    limitations.append({"repo": name, "path": path, "reason": "non-UTF-8 persistence is unsupported"})
                    continue
                blob = manifest[path][1]
                digest = hashlib.sha256(raw).hexdigest()
                if blob.startswith("sha256:"):
                    valid = blob == "sha256:" + digest
                else:
                    algorithm = hashlib.sha1 if len(blob) == 40 else hashlib.sha256
                    valid = algorithm(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest() == blob
                if not valid:
                    raise ValueError("immutable source object identity changed")
                record = {"repo": name, "snapshot": sha, "path": path, "blob": blob, "sha256": digest, "source": source}
                encoded_bytes = len(json.dumps(record, ensure_ascii=False).encode())
                if artifact_bytes + encoded_bytes > MAX_ARTIFACT_BYTES - 8_000_000:
                    omitted += 1
                    continue
                files.append(record)
                artifact_bytes += encoded_bytes
                source_bytes += len(raw)
                model_count += int(is_model_path(path))
            omitted += len(selected) - len([path for path in selected if path in sources])
            if omitted:
                limitations.append({"repo": name, "reason": "MPS source file/byte bounds exceeded", "omitted_files": omitted})
        except (OSError, RuntimeError, ValueError, KeyError) as error:
            failed = True
            limitations.append({"repo": name, "reason": str(error)})
            files = [item for item in files if item["repo"] != name]
            source_bytes = sum(len(item["source"].encode()) for item in files)
            model_count = sum(is_model_path(item["path"]) for item in files)
            artifact_bytes = sum(len(json.dumps(item, ensure_ascii=False).encode()) for item in files)
    terms = set()
    discovery_complete = True
    project = mps.parse_project({f"{quote(item['repo'], safe='')}/{item['path']}": item["source"].encode()
                                 for item in files if is_model_path(item["path"])})
    for entry in project["files"]:
        for node in entry.get("nodes", []):
            for value in mps._node_search_values(node) + [str(entry.get("reference") or ""), str(entry.get("identity") or ""), entry["path"]]:
                if len(value.encode()) > 256:
                    discovery_complete = False
                for term in sorted(mps._search_terms(value)):
                    if len(term.encode()) > 256 or len(terms) >= 4096 and term not in terms:
                        discovery_complete = False
                    elif term:
                        terms.add(term)
    payload = {"schema_version": SCHEMA_VERSION, "parser_version": PARSER_VERSION,
               "snapshots": snapshots, "files": files, "limitations": limitations,
               "discovery_terms": sorted(terms), "discovery_complete": discovery_complete}
    payload["source_manifest_hash"] = _hash([{key: item[key] for key in ("repo", "snapshot", "path", "blob", "sha256")} for item in files])
    payload["mapping_hash"] = _hash([item for item in files if item["path"] == MAPPING_PATH])
    component = {"schema_version": SCHEMA_VERSION, "status": "unavailable" if failed else "ready", "content_hash": _hash(payload),
                 "details": {"snapshots": snapshots, "parser_version": PARSER_VERSION, "models": model_count,
                             "source_bytes": source_bytes, "files": len(files), "discovery_terms": sorted(terms),
                             "discovery_complete": discovery_complete,
                             "source_manifest_hash": payload["source_manifest_hash"], "mapping_hash": payload["mapping_hash"],
                             "limitations": limitations}}
    path = settings.state_dir / "mps-models.json"
    atomic_managed_text_write(settings.state_dir, path, json.dumps(payload, ensure_ascii=False) + "\n")
    component["_artifact_source"] = str(path)
    return component


def validate_payload(payload: dict, snapshots: dict, component: dict) -> bool:
    """Validate copied bytes independently of the mutable build artifact."""
    from .index import _blob_identity_valid
    try:
        files = payload["files"]
        terms = payload["discovery_terms"]
        limitations = payload["limitations"]
        if (payload["schema_version"] != SCHEMA_VERSION or component.get("schema_version") != SCHEMA_VERSION
                or payload["parser_version"] not in SUPPORTED_PARSERS | {PARSER_VERSION}
                or payload["snapshots"] != snapshots or component.get("details", {}).get("snapshots") != snapshots
                or _hash(payload) != component.get("content_hash") or not isinstance(files, list)
                or payload["discovery_terms"] != component["details"]["discovery_terms"]
                or payload["discovery_complete"] != component["details"]["discovery_complete"]
                or not isinstance(payload["discovery_complete"], bool)
                or not isinstance(terms, list) or len(terms) > 4096
                or any(not isinstance(term, str) or len(term.encode()) > 256 for term in terms)
                or not isinstance(limitations, list) or len(limitations) > mps.MAX_PROJECT_FILES + 2 * len(snapshots)
                or any(not isinstance(item, dict) or not isinstance(item.get("reason"), str)
                       or item.get("repo") not in snapshots for item in limitations)
                or limitations != component["details"]["limitations"]
                or len(files) > mps.MAX_PROJECT_FILES):
            return False
        seen = set()
        total = 0
        for item in files:
            key = item["repo"], item["path"]
            source = item["source"]
            raw = source.encode("utf-8")
            if (key in seen or not _safe_path(item["path"]) or item["repo"] not in snapshots
                    or item["snapshot"] != snapshots[item["repo"]] or len(raw) > mps.MAX_FILE_BYTES
                    or hashlib.sha256(raw).hexdigest() != item["sha256"]
                    or not _blob_identity_valid(item["blob"], source, len(raw))):
                return False
            seen.add(key)
            total += len(raw)
        manifest = _hash([{key: item[key] for key in ("repo", "snapshot", "path", "blob", "sha256")} for item in files])
        mapping = _hash([item for item in files if item["path"] == MAPPING_PATH])
        return (total <= mps.MAX_PROJECT_BYTES and manifest == payload["source_manifest_hash"]
                == component["details"]["source_manifest_hash"] and mapping == payload["mapping_hash"]
                == component["details"]["mapping_hash"] and payload["parser_version"] == component["details"]["parser_version"]
                and component["details"]["source_bytes"] == total and component["details"]["files"] == len(files)
                and component["details"]["models"] == sum(is_model_path(item["path"]) for item in files))
    except (AttributeError, KeyError, TypeError, ValueError, UnicodeError):
        return False


def retain_aligned_component(settings, state: dict, candidate: dict, generation=None) -> dict:
    """Keep an intact same-source component through every publication entry point."""
    from .catalog import current_generation_ref
    if candidate.get("status") == "ready":
        return candidate
    generation = generation or current_generation_ref(settings)
    snapshots = {name: str(raw.get("sha") or "working-tree") for name, raw in state.items() if isinstance(raw, dict)}
    if generation is not None and generation.snapshots == snapshots and load_component(settings, generation) is not None:
        for item in state.values():
            if isinstance(item, dict):
                item["mps_warning"] = "MPS refresh unavailable; retaining the previous aligned MPS source component"
        return {**generation.component("mps_models"), "_artifact_source": str(settings.state_dir / generation.component("mps_models")["artifact_ref"])}
    return candidate


def validate_membership(settings, payload: dict) -> bool:
    """Publication proves path/blob membership, not just self-consistent hashes."""
    from .index import _git_manifest
    from .sync import _sealed_snapshot_is_intact, _snapshot_seal_path, MAX_GIT_SNAPSHOT_SEAL_BYTES
    try:
        for name, sha in payload["snapshots"].items():
            repo = replace(settings.repo(name), source_sha=sha)
            files = [item for item in payload["files"] if item["repo"] == name]
            if not files:
                continue
            manifest = _git_manifest(repo)
            if manifest is None:
                target = repo.source_path
                if target is None or not target.resolve().is_relative_to(settings.state_dir.resolve()):
                    return False
                seal_path = _snapshot_seal_path(target.parent, sha)
                if not _sealed_snapshot_is_intact(target, seal_path, sha):
                    return False
                seal = json.loads(read_managed_text(settings.state_dir, seal_path, max_bytes=MAX_GIT_SNAPSHOT_SEAL_BYTES))["files"]
                if any(item["path"] not in seal or "sha256:" + seal[item["path"]]["sha256"] != item["blob"] for item in files):
                    return False
            elif any(manifest.get(item["path"]) not in {("100644", item["blob"]), ("100755", item["blob"])} for item in files):
                return False
        return True
    except (OSError, RuntimeError, KeyError, ValueError, TypeError):
        return False


def load_component(settings, generation=None) -> dict | None:
    from .catalog import canonical_atlas_identity, generation_root
    from .core import _ACTIVE_RETRIEVAL_CACHE
    generation = generation or settings.atlas_generation
    if generation is None:
        return None
    component = generation.component("mps_models")
    expected_ref = str(Path("generations") / f"generation-{generation.generation:06d}" / "mps_models.json")
    if (component.get("status") != "ready" or component.get("artifact_ref") != expected_ref
            or canonical_atlas_identity(generation.manifest) != generation.identity):
        return None
    cache = _ACTIVE_RETRIEVAL_CACHE.get()
    key = ("mps-models", generation.identity, component.get("content_hash"))
    if cache is not None and key in cache:
        return cache[key]
    try:
        payload = json.loads(read_managed_text(generation_root(settings), settings.state_dir / component["artifact_ref"], max_bytes=MAX_ARTIFACT_BYTES))
        if not isinstance(payload, dict) or not validate_payload(payload, generation.snapshots, component):
            payload = None
    except (OSError, ValueError, UnicodeError):
        payload = None
    if cache is not None:
        cache[key] = payload
    return payload


def read_source(settings, repo: str, path: str, generation=None) -> str | None:
    payload = load_component(settings, generation)
    return next((item["source"] for item in payload["files"] if (item["repo"], item["path"]) == (repo, path)), None) if payload else None


def _query_value(value) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or value.get("query") or value.get("glob") or value.get("path") or "")
    return str(value or "")


def may_match(generation, request: dict) -> bool:
    """Cheap candidate gate; terms select work and never establish node identity."""
    details = generation.component("mps_models").get("details") or {}
    values = [*(request.get("resolve") or []), *(request.get("symbols") or []), *(request.get("paths") or []), *(request.get("searches") or [])]
    values.extend(item.get("value", "") for item in request.get("anchors") or [] if item.get("kind") in {"symbol", "file_hint"})
    terms = set(details.get("discovery_terms") or [])
    return any(is_model_path(value.partition("#")[0]) or "#id:" in value or "#lines:" in value
               or mps._query_matches(value, terms) for value in map(_query_value, values)) or details.get("discovery_complete") is False


def navigation(settings, request: dict, *, deadline: float | None = None) -> tuple[dict | None, list]:
    """Return separate derived navigation and exact repository source evidence."""
    from .core import Evidence
    payload = load_component(settings)
    if payload is None:
        return None, []
    records = {f"{quote(item['repo'], safe='')}/{item['path']}": item for item in payload["files"] if is_model_path(item["path"])}
    documents = {path: item["source"].encode() for path, item in records.items()}
    queries = []
    query_paths = {}
    scope = request.get("repos") or []
    terms = set(payload["discovery_terms"])

    def add_query(value: str, repos: list[str]) -> None:
        if not value.strip():
            return
        allowed = {path for path, item in records.items() if not repos or item["repo"] in repos}
        bare, marker, anchor = value.partition("#")
        matches = [path + (marker + anchor if marker else "") for path, item in records.items()
                   if path in allowed and item["path"] == bare]
        for query in matches or [value]:
            queries.append(query)
            query_paths.setdefault(query, set()).update(allowed)

    for value in [*(request.get("resolve") or []), *(request.get("symbols") or [])]:
        add_query(_query_value(value), value.get("repos") or scope if isinstance(value, dict) else scope)
    for anchor in request.get("anchors") or []:
        if anchor.get("kind") in {"symbol", "file_hint"}:
            value = str(anchor.get("value") or "")
            repo = str(anchor.get("repo") or "")
            add_query(value, [repo] if repo else scope)
    for value in request.get("paths") or []:
        repos = value.get("repos") or ([str(value["repo"])] if value.get("repo") else scope) if isinstance(value, dict) else scope
        add_query(_query_value(value), repos)
    # Exact model reads stay focused. Generic code anchors must not suppress
    # model candidates derived from the same ordinary objective/search request.
    if not any(is_model_path(query.partition("#")[0]) or "#id:" in query or "#lines:" in query for query in queries):
        for value in request.get("searches") or []:
            query = _query_value(value)
            if mps._query_matches(query, terms) or is_model_path(query.partition("#")[0]) or not payload["discovery_complete"]:
                add_query(query, value.get("repos") or scope if isinstance(value, dict) else scope)
    from .core import _ACTIVE_RETRIEVAL_CACHE
    cache = _ACTIVE_RETRIEVAL_CACHE.get()
    key = ("mps-project", settings.atlas_generation.identity, settings.atlas_generation.component("mps_models")["content_hash"])
    project = cache.get(key) if cache is not None else None
    if project is None:
        project = mps.parse_project(documents, deadline=deadline)
        if cache is not None:
            cache[key] = project
    # Preserve explicit model focus, then known model candidates before generic
    # code queries; many code anchors must not exhaust the 16-query model budget.
    queries = list(dict.fromkeys(queries))
    queries.sort(key=lambda query: 0 if is_model_path(query.partition("#")[0]) or "#id:" in query or "#lines:" in query
                 else 1 if mps._query_matches(query, terms) else 2)
    rendered = mps.focused_navigation(documents, queries, incoming=request.get("mode") == "impact_analysis", query_paths=query_paths, deadline=deadline, project=project)
    if rendered is None:
        return None, []
    result = json.loads(rendered)
    result["authority"] = "Derived structural navigation from the pinned MPS source component; source evidence is delivered separately."
    result["generation"] = settings.atlas_generation.generation
    result["component_hash"] = settings.atlas_generation.component("mps_models")["content_hash"]
    result["limitations"] = payload["limitations"]
    evidence = []
    for region in result.get("source_regions", []):
        record = records[region["path"]]
        repo, path = record["repo"], record["path"]
        source = read_source(settings, repo, path)
        if source is None:
            continue
        content = region.pop("content")
        region.update(repo=repo, repository_path=path)
        evidence.append(Evidence(repo, path, region["line_start"], region["line_end"], content, "MPS source", 100,
                                 ["pinned MPS model navigation"], source if len(source.encode()) <= 1_000_000 else None))
    _annotate_mappings(result, payload)
    mapping_sources = set()
    mapping_bytes = 0
    all_sources = {(item["repo"], item["path"]): item for item in payload["files"]}
    for step in result.get("steps", []):
        mapping = step.get("flow_mapping") or {}
        if mapping.get("status") != "project_declared_with_source":
            continue
        proof = mapping["evidence"]
        declarations = [(proof["repo"], MAPPING_PATH, 1, len(all_sources[proof["repo"], MAPPING_PATH]["source"].splitlines())),
                        (proof["repo"], proof["path"], proof["line_start"], proof["line_end"])]
        for repo, path, start, end in declarations:
            identity = repo, path, start, end
            if identity in mapping_sources:
                continue
            source = all_sources[repo, path]["source"]
            content = "\n".join(source.splitlines()[start - 1:end])
            if len(content.encode()) > 4000 or mapping_bytes + len(content.encode()) > 8000:
                continue
            mapping_bytes += len(content.encode())
            mapping_sources.add(identity)
            evidence.append(Evidence(repo, path, start, end, content, "MPS flow mapping source", 100,
                                     ["pinned project flow mapping declaration"], source if len(source.encode()) <= 1_000_000 else None))
        mapping["source_status"] = "delivered" if all(item in mapping_sources for item in declarations) else "not_delivered_request_mapping_and_specification_files"
    mps._check_deadline(deadline)
    _bound_navigation(result, min(mps.MAX_SUMMARY_BYTES, max(1024, settings.hard_context_chars // 3)))
    mps._check_deadline(deadline)
    return result, evidence


def _bound_navigation(result: dict, limit: int) -> None:
    """Preserve complete JSON and exact identities, with explicit omissions."""
    # Original source evidence travels separately. Trim repeated discovery and
    # declaration metadata before losing the actual requested connections.
    for field, counter in (("limitations", "omitted_limitations"), ("definition_anchors", "omitted_definition_anchors"),
                           ("source_regions", "omitted_source_regions"), ("file_statuses", "omitted_file_statuses"),
                           ("matched_nodes", "omitted_matches"), ("seeds", "omitted_seed_roots"),
                           ("steps", "unrendered_steps"), ("frontier", "unrendered_frontier_roots")):
        while result.get(field) and len(json.dumps(result, ensure_ascii=True, indent=2).encode()) > limit:
            omitted = result[field].pop()
            if field == "steps":
                result["next_step_anchor"] = omitted["resume_anchor"]
                result["next_step_direction"] = omitted["direction"]
            result[counter] = result.get(counter, 0) + 1
            result["truncated"] = True
    if len(json.dumps(result, ensure_ascii=True, indent=2).encode()) > limit:
        retained = {key: result[key] for key in ("generation", "component_hash", "route_status", "unrendered_steps") if key in result}
        cursor = {key: result[key] for key in ("next_step_anchor", "next_step_direction") if key in result}
        result.clear()
        result.update(retained, format="mps-focused-navigation",
                      truncated=True, reason="Requested MPS identities exceed the navigation byte budget; request one exact model/node anchor")
        if len(json.dumps({**result, **cursor}, ensure_ascii=True, indent=2).encode()) <= limit:
            result.update(cursor)


def _annotate_mappings(result: dict, payload: dict) -> None:
    """Attach exact project declarations; never turn them into observed execution."""
    sources = {(item["repo"], item["path"]): item for item in payload["files"]}
    mappings = []
    for item in payload["files"]:
        if item["path"] != MAPPING_PATH:
            continue
        for declaration in _mapping_entries(item["source"]):
            if any(not isinstance(declaration.get(key), str) for key in ("language_id", "concept_id", "role_id")):
                continue
            identity = (mps._language_id(declaration.get("language_id")), mps._numeric_id(declaration.get("concept_id")),
                        mps._numeric_id(declaration.get("role_id")))
            proof = declaration.get("evidence")
            if not all(identity) or declaration.get("kind") not in {"flow_call", "transition", "branch", "join", "return"} or not isinstance(proof, dict):
                continue
            source = sources.get((item["repo"], str(proof.get("path") or "")))
            start, end = proof.get("line_start"), proof.get("line_end")
            if (source is None or not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int)
                    or isinstance(end, bool) or not 1 <= start <= end <= len(source["source"].splitlines())
                    or end - start >= 80 or proof.get("sha256", source["sha256"]) != source["sha256"]):
                continue
            mappings.append((item["repo"], identity, {"kind": declaration["kind"], "status": "project_declared_with_source",
                "evidence": {"repo": item["repo"], "path": source["path"], "sha256": source["sha256"],
                             "line_start": start, "line_end": end},
                "mapping_path": MAPPING_PATH, "mapping_sha256": item["sha256"],
                "semantics": "Pinned project declaration, not observed execution order or verified runtime behavior."}))
    for step in result.get("steps", []):
        repo = unquote(step["source_node"]["path"].partition("/")[0])
        matches = [proof for owner, identity, proof in mappings if owner == repo and identity == mps._role_identity(step["role"])]
        if matches:
            step["flow_mapping"] = matches[0] if len(matches) == 1 else {"status": "ambiguous", "candidate_count": len(matches)}
            if len(json.dumps(result, ensure_ascii=True).encode()) > mps.MAX_SUMMARY_BYTES:
                step.pop("flow_mapping")
                result["omitted_flow_mappings"] = result.get("omitted_flow_mappings", 0) + 1
                result["truncated"] = True

#!/usr/bin/env python3
"""Build an auditable Qwen3-Reranker-4B Q6_K Precision release pack.

This is packaging-time tooling, never a Project Brain runtime dependency.  It
accepts a locally converted GGUF, a pinned local llama.cpp runtime, and scores
from the official Qwen Transformers implementation.  The emitted pack contains
only public/synthetic conformance data and can be installed without Hugging
Face access on the target machine.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import secrets
import shutil
import socket
import subprocess
import tarfile
import time
import sys
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from brain.platforms import process_group_kwargs, terminate_process_tree


PACK_ID = "qwen3-reranker-4b-q6k-darwin-arm64"
MODEL_FILE = "model.gguf"
RUNTIME_FILE = "llama-server"
TOKENIZER_FILE = "tokenizer.json"
SUITE_FILE = "conformance.json"
SOURCE_PROVENANCE_FILE = "UPSTREAM_SHA256SUMS.txt"
RERANK_INSTRUCTION = "Given a web search query, retrieve relevant passages that answer the query"
RERANK_INPUT_CONTRACT_VERSION = "qwen3-reranker-web-search-v1"
RERANK_CONTEXT_TOKENS = 4096
RERANK_PHYSICAL_BATCH_TOKENS = RERANK_CONTEXT_TOKENS
# Native llama.cpp backends differ slightly in floating-point reduction order
# (Windows CPU measured 0.00037145). This bounded absolute tolerance remains
# independent of exact ranking and official-reference score checks.
RERANKER_BATCH_PARITY_TOLERANCE = 1e-3
# Q6_K changes the calibrated probability slightly relative to official BF16
# Transformers.  Exact official-reference order, all finite scores, and this
# bounded probability delta are independently checked before publication.
MAXIMUM_REFERENCE_SCORE_DELTA = 0.10


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def runtime_documents(case: dict[str, object]) -> list[str]:
    documents = case["documents"]
    assert isinstance(documents, list)
    truncate_to_chars = case.get("truncate_to_chars")
    return [str(document)[:int(truncate_to_chars)] if truncate_to_chars is not None else str(document) for document in documents]


def standard_cases() -> list[dict[str, object]]:
    long_handler = "\n".join(
        [
            "def invalidate_session_token(session_id: str) -> None:",
            "    audit.log('invalidating expired session')",
            "    session_store.delete(session_id)",
        ]
        * 180
    )
    return [
        {
            "id": "english-positive-negative",
            "query": "Which service consumes CustomerUpdated and recalculates customer eligibility?",
            "documents": [
                "CustomerUpdatedListener consumes CustomerUpdated and calls eligibilityService.recalculate(customerId).",
                "CREATE INDEX customer_region_idx ON customer(region);",
                "The deployment runbook describes how to rotate an application log.",
            ],
            "expected_top_index": 0,
        },
        {
            "id": "multilingual-positive-negative",
            "query": "如何找到处理客户更新事件并重新计算资格的代码？",
            "documents": [
                "客户事件监听器收到 CustomerUpdated 后调用资格服务重新计算 customerId 的资格。",
                "La migration SQL ajoute une colonne de région client.",
                "La guía de despliegue describe una rotación de registros.",
            ],
            "expected_top_index": 0,
        },
        {
            "id": "code-positive-negative",
            "query": "Where does handleCustomerUpdated call eligibilityService.recalculate?",
            "documents": [
                "@KafkaListener(topics = \"customer.updated\")\nvoid handleCustomerUpdated(CustomerUpdated event) {\n  eligibilityService.recalculate(event.customerId());\n}",
                "record CustomerRegionMigration(String region) {}",
                "def render_release_notes(version): return version",
            ],
            "expected_top_index": 0,
        },
        {
            "id": "long-truncated-input",
            "query": "Which function invalidates an expired session token?",
            "documents": [
                long_handler,
                "def refresh_search_index(): return 'completed'",
            ],
            "truncate_to_chars": 4096,
            "expected_top_index": 0,
        },
    ]


def candidate_pool_cases() -> list[dict[str, object]]:
    query = "Where is the CustomerUpdated event used to recalculate eligibility?"
    relevant = "CustomerUpdatedListener handles CustomerUpdated and invokes eligibilityService.recalculate(customerId)."
    cases: list[dict[str, object]] = []
    for size in (10, 20, 40, 80):
        documents = [relevant] + [
            f"Unrelated public synthetic release-note paragraph {number}: rotate logs and publish a build artifact."
            for number in range(1, size)
        ]
        cases.append({
            "id": f"candidate-pool-{size}",
            "query": query,
            "documents": documents,
            "expected_top_index": 0,
        })
    return cases


def _post(endpoint: str, key: str, query: str, documents: list[str]) -> list[float]:
    request = Request(
        endpoint + "/rerank",
        data=json.dumps({"query": query, "documents": documents}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with urlopen(request, timeout=300) as response:
        payload = json.loads(response.read().decode("utf-8"))
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("llama.cpp did not return a rerank response")
    indexed: dict[int, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            indexed[int(row["index"])] = float(row["relevance_score"])
        except (KeyError, TypeError, ValueError):
            continue
    scores = [indexed[index] for index in range(len(documents)) if index in indexed]
    if len(scores) != len(documents) or not scores or any(not math.isfinite(score) for score in scores):
        raise RuntimeError("llama.cpp returned incomplete, empty, or non-finite rerank scores")
    return scores


def _order(scores: list[float]) -> list[int]:
    return sorted(range(len(scores)), key=lambda index: (-scores[index], index))


def _reference_cases(reference: Path) -> tuple[dict[str, list[float]], dict[str, list[float]]]:
    try:
        raw = json.loads(reference.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"invalid official-reference output: {reference}") from error
    if not isinstance(raw, dict) or raw.get("instruction") != RERANK_INSTRUCTION:
        raise SystemExit("official-reference output has the wrong Qwen reranker instruction")

    def collect(name: str) -> dict[str, list[float]]:
        entries = raw.get(name)
        if not isinstance(entries, list):
            raise SystemExit(f"official-reference output is missing {name}")
        values: dict[str, list[float]] = {}
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not isinstance(entry.get("scores"), list):
                raise SystemExit(f"official-reference output has an invalid {name} case")
            try:
                scores = [float(value) for value in entry["scores"]]
            except (TypeError, ValueError) as error:
                raise SystemExit(f"official-reference output has non-numeric {name} scores") from error
            if not scores or any(not math.isfinite(score) for score in scores):
                raise SystemExit(f"official-reference output has non-finite {name} scores")
            values[entry["id"]] = scores
        return values

    return collect("reranker"), collect("candidate_pools")


def _golden_case(case: dict[str, object], reference_scores: dict[str, list[float]]) -> dict[str, object]:
    case_id = str(case["id"])
    scores = reference_scores.get(case_id)
    documents = runtime_documents(case)
    if scores is None or len(scores) != len(documents):
        raise RuntimeError(f"official reference did not return one score per document for {case_id}")
    expected = _order(scores)
    if expected[0] != int(case["expected_top_index"]):
        raise RuntimeError(f"official Qwen reference did not rank the intended public positive first for {case_id}")
    result = {
        "id": case_id,
        "query": str(case["query"]),
        "documents": [str(document) for document in case["documents"]],
        "expected_top_index": int(case["expected_top_index"]),
        "expected_order": expected,
        "reference_scores": scores,
        "maximum_score_delta": MAXIMUM_REFERENCE_SCORE_DELTA,
    }
    if "truncate_to_chars" in case:
        result["truncate_to_chars"] = int(case["truncate_to_chars"])
    return result


def _assert_case(endpoint: str, key: str, case: dict[str, object]) -> None:
    documents = runtime_documents(case)
    scores = _post(endpoint, key, str(case["query"]), documents)
    individual = [_post(endpoint, key, str(case["query"]), [document])[0] for document in documents]
    expected_top = int(case["expected_top_index"])
    reference = [float(score) for score in case["reference_scores"]]
    maximum_delta = float(case["maximum_score_delta"])
    if _order(scores)[0] != expected_top:
        raise RuntimeError(f"local Q6_K did not rank the labelled positive first for {case['id']}")
    parity_deltas = [abs(left - right) for left, right in zip(scores, individual, strict=True)]
    if any(delta > RERANKER_BATCH_PARITY_TOLERANCE for delta in parity_deltas):
        raise RuntimeError(
            f"local Q6_K batch/single rerank parity failed for {case['id']}: "
            f"max_delta={max(parity_deltas):.9g}, batch={scores}, single={individual}"
        )
    if any(abs(left - right) > maximum_delta for left, right in zip(scores, reference, strict=True)):
        raise RuntimeError(f"local Q6_K score delta exceeds official-reference threshold for {case['id']}")


def conformance(runtime: Path, model: Path, reference: Path, output: Path) -> None:
    standard_reference, pool_reference = _reference_cases(reference)
    standard = [_golden_case(case, standard_reference) for case in standard_cases()]
    pools = [_golden_case(case, pool_reference) for case in candidate_pool_cases()]
    port = free_port()
    key = secrets.token_urlsafe(24)
    endpoint = f"http://127.0.0.1:{port}"
    log = output / "llama-server-build.log"
    command = [
        str(runtime), "--model", str(model), "--host", "127.0.0.1", "--port", str(port),
        "--api-key", key, "--offline", "--no-webui", "--reranking", "--pooling", "rank",
        "--ctx-size", str(RERANK_CONTEXT_TOKENS), "-ub", str(RERANK_PHYSICAL_BATCH_TOKENS),
    ]
    with log.open("wb") as stderr:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stderr, stderr=stderr, **process_group_kwargs())
    succeeded = False
    try:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if process.poll() is not None:
                tail = log.read_text(encoding="utf-8", errors="replace")[-6000:] if log.exists() else "(llama.cpp log unavailable)"
                raise RuntimeError(f"llama.cpp exited during Precision pack conformance (exit code {process.returncode}):\n{tail}")
            try:
                with urlopen(endpoint + "/health", timeout=2) as response:
                    if response.status < 400:
                        break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("llama.cpp did not become healthy during Precision pack conformance")
        for case in [*standard, *pools]:
            _assert_case(endpoint, key, case)
        suite = {
            "producer": {
                "reference": "official Qwen/Qwen3-Reranker-4B Transformers implementation",
                "runtime": "pinned local llama.cpp server",
                "public_synthetic_only": True,
            },
            "requirements": {"long_input_min_chars": 4096, "reranker_candidate_pools": [10, 20, 40, 80]},
            "reranker": standard,
            "reranker_candidate_pools": pools,
        }
        (output / SUITE_FILE).write_text(json.dumps(suite, separators=(",", ":")), encoding="utf-8")
        succeeded = True
    finally:
        terminate_process_tree(process, graceful_timeout=10)
        if succeeded:
            log.unlink(missing_ok=True)


def deterministic_tar(source: Path, names: list[str], target: Path) -> None:
    with target.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.GNU_FORMAT) as archive:
                for name in names:
                    path = source / name
                    info = archive.gettarinfo(str(path), arcname=name)
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mtime = 0
                    with path.open("rb") as handle:
                        archive.addfile(info, handle)


def artifact(url: str, path: Path) -> dict[str, object]:
    return {"url": url, "sha256": sha256(path), "size": path.stat().st_size}


def stage_model(source: Path, target: Path) -> None:
    """Avoid a second multi-gigabyte copy when build inputs share a volume."""
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def main() -> int:
    global PACK_ID, RUNTIME_FILE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--model-sha256", required=True)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--runtime-license", type=Path, required=True)
    parser.add_argument("--license", type=Path, required=True)
    parser.add_argument("--source-provenance", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--release-base", default="https://github.com/superorange0707/project-brain/releases/download")
    parser.add_argument("--upstream-revision", required=True)
    parser.add_argument("--tokenizer-revision", required=True)
    parser.add_argument("--runtime-revision", required=True)
    parser.add_argument("--builder-revision", required=True)
    parser.add_argument("--minimum-brain-version", required=True)
    parser.add_argument("--part-bytes", type=int, default=1_900_000_000)
    parser.add_argument(
        "--platform", default="darwin-arm64",
        choices=("darwin-arm64", "darwin-amd64", "linux-arm64", "linux-amd64", "windows-amd64"),
    )
    args = parser.parse_args()

    PACK_ID = f"qwen3-reranker-4b-q6k-{args.platform}"
    RUNTIME_FILE = "llama-server.exe" if args.platform.startswith("windows-") else "llama-server"
    runtime_os, runtime_architecture = args.platform.split("-", 1)
    metal = args.platform == "darwin-arm64"

    for path in (args.model, args.tokenizer, args.runtime, args.runtime_license, args.license, args.source_provenance, args.reference):
        if not path.is_file():
            raise SystemExit(f"missing required local input: {path}")
    if sha256(args.model) != args.model_sha256.lower():
        raise SystemExit("converted Qwen Q6_K GGUF SHA-256 mismatch")
    if args.part_bytes < 1_000_000:
        raise SystemExit("--part-bytes must be at least 1,000,000")
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite output directory: {args.output}")

    pack = args.output / PACK_ID
    pack.mkdir(parents=True)
    stage_model(args.model, pack / MODEL_FILE)
    shutil.copy2(args.tokenizer, pack / TOKENIZER_FILE)
    shutil.copy2(args.runtime, pack / RUNTIME_FILE)
    os.chmod(pack / RUNTIME_FILE, 0o755)
    shutil.copy2(args.license, pack / "LICENSE")
    shutil.copy2(args.runtime_license, pack / "LLAMA_CPP_LICENSE")
    shutil.copy2(args.source_provenance, pack / SOURCE_PROVENANCE_FILE)
    conformance(pack / RUNTIME_FILE, pack / MODEL_FILE, args.reference, pack)
    artifacts = {
        name: sha256(pack / name)
        for name in (RUNTIME_FILE, MODEL_FILE, TOKENIZER_FILE, SUITE_FILE, "LICENSE", "LLAMA_CPP_LICENSE", SOURCE_PROVENANCE_FILE)
    }
    provenance = "\n".join([
        "# Qwen3-Reranker-4B Q6_K provenance",
        "",
        "- upstream_model: Qwen/Qwen3-Reranker-4B",
        f"- upstream_revision: {args.upstream_revision}",
        "- upstream_weights: exact official safetensors listed in UPSTREAM_SHA256SUMS.txt",
        f"- source_provenance_sha256: {artifacts[SOURCE_PROVENANCE_FILE]}",
        "- conversion: llama.cpp convert_hf_to_gguf.py output type f16, then llama-quantize Q6_K",
        "- training_or_fine_tuning: none",
        f"- weight_sha256: {artifacts[MODEL_FILE]}",
        "- tokenizer_upstream: Qwen/Qwen3-Reranker-4B",
        f"- tokenizer_revision: {args.tokenizer_revision}",
        f"- tokenizer_sha256: {artifacts[TOKENIZER_FILE]}",
        "- license: Apache-2.0; included in LICENSE",
        f"- runtime: llama.cpp compiled from the pinned revision below for {args.platform}",
        f"- runtime_revision: {args.runtime_revision}",
        f"- runtime_sha256: {artifacts[RUNTIME_FILE]}",
        f"- builder_revision: {args.builder_revision}",
        f"- converter_revision: llama.cpp@{args.runtime_revision}",
        "- conformance: public/synthetic only; official Qwen Transformers reference and local Q6_K runtime",
        "",
    ])
    (pack / "PROVENANCE.md").write_text(provenance, encoding="utf-8")
    (pack / "NOTICE").write_text(
        "Project Brain Precision model pack: official Qwen Qwen3-Reranker-4B weights converted without training from the recorded Apache-2.0 source revision. The included llama.cpp runtime is covered by its included LLAMA_CPP_LICENSE.\n",
        encoding="utf-8",
    )
    for name in ("PROVENANCE.md", "NOTICE"):
        artifacts[name] = sha256(pack / name)
    manifest = {
        "pack_id": PACK_ID,
        "capability": "reranker",
        "model_family": "Qwen3",
        "upstream_model": "Qwen/Qwen3-Reranker-4B",
        "upstream_revision": args.upstream_revision,
        "license": "Apache-2.0",
        "weight_format": "GGUF",
        "quantization": "Q6_K",
        "weight_sha256": artifacts[MODEL_FILE],
        "tokenizer_file": TOKENIZER_FILE,
        "tokenizer_sha256": artifacts[TOKENIZER_FILE],
        "runtime_name": "llama.cpp",
        "runtime_revision": args.runtime_revision,
        "builder_revision": args.builder_revision,
        "runtime_binary": RUNTIME_FILE,
        "model_file": MODEL_FILE,
        "runtime_args": ["--ctx-size", str(RERANK_CONTEXT_TOKENS), "-ub", str(RERANK_PHYSICAL_BATCH_TOKENS)],
        "pooling": "rank",
        "normalization": "none",
        "query_instruction": RERANK_INSTRUCTION,
        "document_instruction": "",
        "query_instruction_version": RERANK_INPUT_CONTRACT_VERSION,
        "document_card_version": "1",
        "chunk_schema_version": "1",
        "embedding_dimension": 0,
        "converter_revision": f"llama.cpp@{args.runtime_revision}",
        "minimum_brain_version": args.minimum_brain_version,
        "golden_suite": SUITE_FILE,
        "golden_suite_hash": artifacts[SUITE_FILE],
        "artifacts": artifacts,
        "runtime_compatibility": {"os": runtime_os, "architecture": runtime_architecture, "metal": metal, "reranking": True},
    }
    (pack / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    metadata_names = [
        "manifest.json", RUNTIME_FILE, TOKENIZER_FILE, SUITE_FILE, SOURCE_PROVENANCE_FILE,
        "LICENSE", "LLAMA_CPP_LICENSE", "NOTICE", "PROVENANCE.md",
    ]
    metadata = args.output / f"{PACK_ID}-metadata.tar.gz"
    deterministic_tar(pack, metadata_names, metadata)
    parts: list[Path] = []
    with (pack / MODEL_FILE).open("rb") as source:
        index = 0
        while chunk := source.read(args.part_bytes):
            part = args.output / f"{PACK_ID}-model.part-{index:02d}"
            part.write_bytes(chunk)
            parts.append(part)
            index += 1
    base = f"{args.release_base.rstrip('/')}/{args.release_tag}"
    descriptor = {
        "schema": "project-brain-model-pack-v1",
        "pack_id": PACK_ID,
        "capability": "reranker",
        "platform": args.platform,
        "release_tag": args.release_tag,
        "metadata": artifact(f"{base}/{metadata.name}", metadata),
        "model": {"file": MODEL_FILE, "sha256": artifacts[MODEL_FILE], "parts": [artifact(f"{base}/{part.name}", part) for part in parts]},
    }
    descriptor_path = args.output / f"{PACK_ID}-descriptor.json"
    descriptor_path.write_text(json.dumps(descriptor, indent=2) + "\n", encoding="utf-8")
    checksums = [metadata, *parts, descriptor_path]
    (args.output / "SHA256SUMS.txt").write_text("".join(f"{sha256(path)}  {path.name}\n" for path in checksums), encoding="utf-8")
    print(json.dumps({"pack_id": PACK_ID, "descriptor": str(descriptor_path), "descriptor_sha256": sha256(descriptor_path), "artifacts": [path.name for path in checksums]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

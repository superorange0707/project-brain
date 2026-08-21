#!/usr/bin/env python3
"""Build one auditable Qwen3-Embedding-4B GGUF release pack from local inputs.

This is a release-engineering tool, never a Project Brain runtime dependency.
It deliberately accepts only already-downloaded official weights/tokenizer and a
pinned local llama.cpp binary.  It writes public/synthetic conformance evidence,
splits the weight for GitHub Release's per-asset limit, and emits the descriptor
that a released Core version pins by SHA-256.
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
from pathlib import Path
from urllib.request import Request, urlopen


PACK_ID = "qwen3-embedding-4b-q6k-darwin-arm64"
MODEL_FILE = "model.gguf"
RUNTIME_FILE = "llama-server"
TOKENIZER_FILE = "tokenizer.json"
SUITE_FILE = "conformance.json"
QUERY_INSTRUCTION = "Instruct: Given a code search query, retrieve relevant code passages that answer the query\nQuery: "
INPUT_SUFFIX = "<|endoftext|>"


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


def _post(endpoint: str, key: str, inputs: list[str]) -> list[list[float]]:
    request = Request(
        endpoint + "/v1/embeddings",
        data=json.dumps({"input": inputs}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    # Public CI runners can take longer than a workstation for the one required
    # >4K-character card; this remains bounded and entirely loopback-local.
    with urlopen(request, timeout=300) as response:
        payload = json.loads(response.read().decode("utf-8"))
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("llama.cpp did not return an embedding response")
    ordered = sorted(rows, key=lambda row: int(row.get("index", 0)) if isinstance(row, dict) else -1)
    vectors = [row.get("embedding") for row in ordered if isinstance(row, dict)]
    if len(vectors) != len(inputs) or any(not isinstance(vector, list) or not vector for vector in vectors):
        raise RuntimeError("llama.cpp returned an incomplete embedding batch")
    converted = [[float(value) for value in vector] for vector in vectors]
    if any(not all(math.isfinite(value) for value in vector) for vector in converted):
        raise RuntimeError("llama.cpp returned a non-finite embedding")
    return converted


def cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True)) / (
        (math.sqrt(sum(a * a for a in left)) or 1.0) * (math.sqrt(sum(b * b for b in right)) or 1.0)
    )


def _case(case_id: str, texts: list[str], expected_order: list[int], endpoint: str, key: str, instruction: str = "") -> dict[str, object]:
    vectors = _post(endpoint, key, [instruction + text + INPUT_SUFFIX for text in texts])
    if any(len(vector) != 2560 for vector in vectors):
        raise RuntimeError("official Qwen3-Embedding-4B GGUF did not return 2560-dimensional vectors")
    order = sorted(range(1, len(texts)), key=lambda index: (-cosine(vectors[0], vectors[index]), index))
    if order != expected_order:
        raise RuntimeError(f"public synthetic conformance ranking failed for {case_id}: {order} != {expected_order}")
    result: dict[str, object] = {
        "id": case_id,
        "texts": texts,
        "dimension": 2560,
        "reference_vectors": vectors,
        "minimum_cosine_to_reference": 0.9999,
        "expected_similarity_order": expected_order,
        "normalized": False,
    }
    if instruction:
        result["instruction"] = instruction
    return result


def conformance(runtime: Path, model: Path, output: Path) -> None:
    port = free_port()
    key = secrets.token_urlsafe(24)
    endpoint = f"http://127.0.0.1:{port}"
    log = output / "llama-server-build.log"
    command = [
        str(runtime), "--model", str(model), "--host", "127.0.0.1", "--port", str(port),
        "--api-key", key, "--offline", "--no-webui", "--embedding", "--pooling", "last",
        "--ctx-size", "4096", "-ub", "512",
    ]
    with log.open("wb") as stderr:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stderr, stderr=stderr, start_new_session=True)
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if process.poll() is not None:
                try:
                    tail = log.read_text(encoding="utf-8", errors="replace")[-6000:]
                except OSError:
                    tail = "(llama.cpp log was unavailable)"
                raise RuntimeError(
                    "llama.cpp exited during pack conformance "
                    f"(exit code {process.returncode}):\n{tail}"
                )
            try:
                with urlopen(endpoint + "/health", timeout=2) as response:
                    if response.status < 400:
                        break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("llama.cpp did not become healthy during pack conformance")
        long_code = "\n".join("def validate_customer_%04d(customer_id): return customer_id is not None" % number for number in range(64))
        suite = {
            "producer": {
                "reference": "official Qwen/Qwen3-Embedding-4B-GGUF Q6_K artifact",
                "runtime": "pinned local llama.cpp server",
                "public_synthetic_only": True,
            },
            "requirements": {"long_input_min_chars": 4096},
            "embedding": [
                _case(
                    "code-event-routing",
                    [
                        "Find the service that consumes CustomerUpdated and recalculates eligibility.",
                        "@KafkaListener consumes CustomerUpdated and calls eligibilityService.recalculate(customerId).",
                        "CREATE INDEX customer_jurisdiction_idx ON customer(jurisdiction);",
                    ],
                    [1, 2], endpoint, key,
                ),
                _case(
                    "multilingual-code-intent",
                    [
                        "如何查找客户资格重新计算的事件处理器？",
                        "客户事件监听器收到 CustomerUpdated 后调用资格重新计算服务。",
                        "La migration SQL ajoute une colonne de région client.",
                    ],
                    [1, 2], endpoint, key,
                ),
                _case(
                    "query-instruction-contract",
                    [
                        "Find the class that consumes CustomerUpdated and recalculates eligibility.",
                        "CustomerUpdated listener invokes eligibilityService.recalculate(customerId).",
                        "A SQL migration creates a customer region index.",
                    ],
                    [1, 2], endpoint, key, QUERY_INSTRUCTION,
                ),
                _case(
                    "long-code-card",
                    [
                        long_code,
                        "def validate_customer(customer_id): return customer_id is not None",
                    ],
                    [1], endpoint, key,
                ),
            ],
        }
        (output / SUITE_FILE).write_text(json.dumps(suite, separators=(",", ":")), encoding="utf-8")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--model-sha256", required=True)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--runtime-license", type=Path, required=True)
    parser.add_argument("--license", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--release-base", default="https://github.com/superorange0707/project-brain/releases/download")
    parser.add_argument("--upstream-revision", required=True)
    parser.add_argument("--tokenizer-revision", required=True)
    parser.add_argument("--runtime-revision", required=True)
    parser.add_argument("--minimum-brain-version", required=True)
    parser.add_argument("--part-bytes", type=int, default=1_900_000_000)
    args = parser.parse_args()

    for path in (args.model, args.tokenizer, args.runtime, args.runtime_license, args.license):
        if not path.is_file():
            raise SystemExit(f"missing required local input: {path}")
    if sha256(args.model) != args.model_sha256.lower():
        raise SystemExit("official Qwen GGUF SHA-256 mismatch")
    if args.part_bytes < 1_000_000:
        raise SystemExit("--part-bytes must be at least 1,000,000")
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite output directory: {args.output}")
    pack = args.output / PACK_ID
    pack.mkdir(parents=True)
    shutil.copy2(args.model, pack / MODEL_FILE)
    shutil.copy2(args.tokenizer, pack / TOKENIZER_FILE)
    shutil.copy2(args.runtime, pack / RUNTIME_FILE)
    os.chmod(pack / RUNTIME_FILE, 0o755)
    shutil.copy2(args.license, pack / "LICENSE")
    shutil.copy2(args.runtime_license, pack / "LLAMA_CPP_LICENSE")
    conformance(pack / RUNTIME_FILE, pack / MODEL_FILE, pack)
    artifacts = {name: sha256(pack / name) for name in (RUNTIME_FILE, MODEL_FILE, TOKENIZER_FILE, SUITE_FILE, "LICENSE", "LLAMA_CPP_LICENSE")}
    provenance = "\n".join([
        "# Qwen3-Embedding-4B Q6_K provenance",
        "",
        "- upstream_model: Qwen/Qwen3-Embedding-4B-GGUF",
        f"- upstream_revision: {args.upstream_revision}",
        "- upstream_weight: Qwen3-Embedding-4B-Q6_K.gguf (official upstream GGUF; no conversion or training)",
        f"- weight_sha256: {artifacts[MODEL_FILE]}",
        "- tokenizer_upstream: Qwen/Qwen3-Embedding-4B",
        f"- tokenizer_revision: {args.tokenizer_revision}",
        f"- tokenizer_sha256: {artifacts[TOKENIZER_FILE]}",
        "- license: Apache-2.0; included in LICENSE",
        "- runtime: llama.cpp compiled from the pinned revision below with Metal enabled",
        f"- runtime_revision: {args.runtime_revision}",
        f"- runtime_sha256: {artifacts[RUNTIME_FILE]}",
        "- converter_revision: not-applicable (the official Qwen GGUF is used unchanged)",
        "- conformance: public/synthetic only; no private source, ticket, or benchmark data",
        "",
    ])
    (pack / "PROVENANCE.md").write_text(provenance, encoding="utf-8")
    (pack / "NOTICE").write_text("Project Brain semantic model pack: official Qwen Qwen3-Embedding-4B-GGUF Q6_K weights under Apache-2.0. The included llama.cpp runtime is covered by its included LLAMA_CPP_LICENSE.\n", encoding="utf-8")
    for name in ("PROVENANCE.md", "NOTICE"):
        artifacts[name] = sha256(pack / name)
    manifest = {
        "pack_id": PACK_ID,
        "capability": "embedding",
        "model_family": "Qwen3",
        "upstream_model": "Qwen/Qwen3-Embedding-4B-GGUF",
        "upstream_revision": args.upstream_revision,
        "license": "Apache-2.0",
        "weight_format": "GGUF",
        "quantization": "Q6_K",
        "weight_sha256": artifacts[MODEL_FILE],
        "tokenizer_file": TOKENIZER_FILE,
        "tokenizer_sha256": artifacts[TOKENIZER_FILE],
        "runtime_name": "llama.cpp",
        "runtime_revision": args.runtime_revision,
        "runtime_binary": RUNTIME_FILE,
        "model_file": MODEL_FILE,
        "runtime_args": ["--pooling", "last", "--ctx-size", "4096", "-ub", "512"],
        "pooling": "last-token",
        "normalization": "none",
        "input_suffix": INPUT_SUFFIX,
        "query_instruction": QUERY_INSTRUCTION,
        "document_instruction": "",
        "query_instruction_version": "qwen3-code-retrieval-v1",
        "document_card_version": "1",
        "chunk_schema_version": "1",
        "embedding_dimension": 2560,
        "converter_revision": "not-applicable:official-qwen-gguf",
        "minimum_brain_version": args.minimum_brain_version,
        "golden_suite": SUITE_FILE,
        "golden_suite_hash": artifacts[SUITE_FILE],
        "artifacts": artifacts,
        "runtime_compatibility": {"os": "darwin", "architecture": "arm64", "metal": True},
    }
    (pack / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    metadata_names = ["manifest.json", RUNTIME_FILE, TOKENIZER_FILE, SUITE_FILE, "LICENSE", "LLAMA_CPP_LICENSE", "NOTICE", "PROVENANCE.md"]
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
        "capability": "embedding",
        "platform": "darwin-arm64",
        "release_tag": args.release_tag,
        "metadata": artifact(f"{base}/{metadata.name}", metadata),
        "model": {
            "file": MODEL_FILE,
            "sha256": artifacts[MODEL_FILE],
            "parts": [artifact(f"{base}/{part.name}", part) for part in parts],
        },
    }
    descriptor_path = args.output / f"{PACK_ID}-descriptor.json"
    descriptor_path.write_text(json.dumps(descriptor, indent=2) + "\n", encoding="utf-8")
    checksums = [metadata, *parts, descriptor_path]
    (args.output / "SHA256SUMS.txt").write_text("".join(f"{sha256(path)}  {path.name}\n" for path in checksums), encoding="utf-8")
    print(json.dumps({"pack_id": PACK_ID, "descriptor": str(descriptor_path), "descriptor_sha256": sha256(descriptor_path), "artifacts": [path.name for path in checksums]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

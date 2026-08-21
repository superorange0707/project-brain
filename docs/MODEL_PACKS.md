# Offline model packs

Project Brain Core has no model dependency. A Semantic or Precision pack is a separately approved local artifact: it is installed once, verified locally, and then run only through a pack-owned loopback `llama.cpp` process. The runtime does not download weights, call Hugging Face, call a Qwen API, or expose a listener beyond `127.0.0.1`.

This guide is for a release engineer or organization-controlled pack builder. It is not an instruction to bypass company approval. Do not place private source, ticket data, credentials, or a private calibration corpus in a public pack, golden suite, or release asset.

## Supported profiles

| Edition | Capability | Current target |
| --- | --- | --- |
| Semantic Lite | embedding | Qwen3-Embedding-0.6B |
| Semantic flagship | embedding | Qwen3-Embedding-4B |
| Semantic experimental | embedding | Qwen3-Embedding-8B (requires benchmark evidence) |
| Precision | reranker | Qwen3-Reranker-4B |

Qwen is the present implementation choice, not an architectural requirement: the manifest carries family, runtime, source revision, and capability. The official Qwen weights are the source of truth. When Qwen publishes the selected embedding model as an official GGUF, use that artifact directly rather than converting it again. Qwen documents both its official GGUF releases and local conversion path; its embedding repository also publishes the official embedding/reranking evaluation contract. See the [Qwen llama.cpp guide](https://github.com/QwenLM/Qwen3/blob/main/docs/source/run_locally/llama.cpp.md), [Qwen quantization guide](https://github.com/QwenLM/Qwen3/blob/main/docs/source/quantization/llama.cpp.md), and [Qwen3 Embedding evaluation repository](https://github.com/QwenLM/Qwen3-Embedding/tree/main/evaluation).

## Pack layout and manifest

An unpacked pack has no symlinks and contains all artifacts it declares:

```text
qwen3-reranker-4b-q8/
  manifest.json
  llama-server
  model.gguf
  tokenizer.json
  conformance.json
  LICENSE
  PROVENANCE.md
```

Production manifests must contain checksums for the runtime, weights, tokenizer, and golden suite. `weight_sha256` and `tokenizer_sha256` must equal the matching entries in `artifacts`, so a pack cannot silently swap a model after review. `document_card_version` and `chunk_schema_version` are required for every pack; for rerankers, they version bounded candidate-card serialization rather than an embedding dimension. Use `embedding_dimension: 0` for a reranker.

```json
{
  "pack_id": "qwen3-reranker-4b-q8",
  "capability": "reranker",
  "model_family": "Qwen3",
  "upstream_model": "Qwen/Qwen3-Reranker-4B",
  "upstream_revision": "<official-commit-or-revision>",
  "license": "Apache-2.0",
  "weight_format": "GGUF",
  "quantization": "Q8_0",
  "runtime_name": "llama.cpp",
  "runtime_revision": "<pinned-commit>",
  "converter_revision": "llama.cpp@<pinned-commit>",
  "runtime_binary": "llama-server",
  "model_file": "model.gguf",
  "tokenizer_file": "tokenizer.json",
  "weight_sha256": "<64-hex-sha256-of-model.gguf>",
  "tokenizer_sha256": "<64-hex-sha256-of-tokenizer.json>",
  "pooling": "rank",
  "normalization": "none",
  "query_instruction_version": "qwen3-reranker-v1",
  "document_card_version": "1",
  "chunk_schema_version": "1",
  "embedding_dimension": 0,
  "minimum_brain_version": "0.6.1",
  "golden_suite": "conformance.json",
  "golden_suite_hash": "<64-hex-sha256>",
  "artifacts": {
    "llama-server": "<64-hex-sha256>",
    "model.gguf": "<same-as-weight_sha256>",
    "tokenizer.json": "<same-as-tokenizer_sha256>",
    "conformance.json": "<same-as-golden_suite_hash>"
  }
}
```

For an embedding pack, use its actual output dimension plus the upstream pooling and normalization contract. A mismatched semantic schema is rejected with the explicit semantic-index rebuild command; it never invalidates Core.

## Reproducible reranker conversion

Conversion runs on an approved packaging machine, not on the enterprise runtime machine. Start from a preapproved local checkout of official Qwen weights and a source-pinned `llama.cpp` checkout. Record every placeholder in the manifest and `PROVENANCE.md`:

```bash
git -C /approved-src/llama.cpp checkout <pinned-llama.cpp-commit>
python /approved-src/llama.cpp/convert_hf_to_gguf.py \
  /approved-weights/Qwen3-Reranker-4B \
  --outtype bf16 \
  --outfile /approved-build/qwen3-reranker-4b-bf16.gguf
/approved-src/llama.cpp/build/bin/llama-quantize \
  /approved-build/qwen3-reranker-4b-bf16.gguf \
  /approved-build/model.gguf Q8_0
shasum -a 256 /approved-build/model.gguf /approved-weights/Qwen3-Reranker-4B/tokenizer.json
```

Older pinned `llama.cpp` revisions name the converter `convert-hf-to-gguf.py`; use the name present in the exact checked-out revision and record it in `converter_revision`. Copy the matching tokenizer and upstream license into the pack. Do not use an unverified community GGUF as the final Precision pack. A community conversion is suitable only for development comparison against an official-reference suite.

## Conformance and local measurement

Create the golden suite from public/synthetic texts and the official Qwen reference implementation on the approved pack-builder machine. It must cover embedding dimensions, finite vectors, normalization, batch/single parity, reference-vector cosine, expected similarity order, and a long input; reranker cases must cover positive/negative ordering, multilingual and code-oriented pairs, batch/single score parity, long/truncated inputs, and finite non-empty scores.

`brain model verify PACK` checks declared files then runs the suite through the exact local runtime. `brain model benchmark PACK` adds public synthetic embedding batch throughput or 10/20/40/80 candidate-pool latency. Neither is a private-repository Recall/MRR claim. `brain model autotune PACK` stores the result and conservative batch/pool recommendation only under private Brain state; it does not publish model or source data.

## Controlled installation

For a copied or internal-share pack:

```bash
brain model install /approved-share/qwen3-reranker-4b-q8.tar
brain model verify qwen3-reranker-4b-q8
brain model benchmark qwen3-reranker-4b-q8
brain model autotune qwen3-reranker-4b-q8 --latency-budget-ms 3000
```

An approved GitHub Release asset can be staged once only when its expected hash is supplied. It is an installation operation, not a runtime dependency:

```bash
brain model install https://github.com/ORG/REPO/releases/download/vX/pack.tar --sha256 <release-sha256>
```

For an organization-managed HTTPS host, add an exact host or parent domain to the local `brain.toml` before installation:

```toml
[models]
approved_install_hosts = ["models.example.internal"]
```

The downloader accepts only credential-free HTTPS, approved hosts, a declared content length, and a caller-provided SHA-256. It does not support Hugging Face as a Project Brain runtime source. Precision automatically falls back to Semantic, then Core, if verification, startup, health, timeout, or reranking fails; the deterministic verified-evidence path remains authoritative.

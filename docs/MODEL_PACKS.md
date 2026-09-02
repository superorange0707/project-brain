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

## Official Semantic pack v1.0.6

The first Core-catalogued Project Brain Semantic release is a separately
versioned macOS Apple Silicon pack, not part of the Core wheel, standalone
archive, or Homebrew formula. It contains the unchanged official
`Qwen/Qwen3-Embedding-4B-GGUF` artifact
`Qwen3-Embedding-4B-Q6_K.gguf` at revision
`4eb3b8293ac9b642f61ece63459fae31e82d6669`, SHA-256
`0c04b2b5e9b039dd01fd1e6d757968855fd5e2523bb3e9a4a03fa6454973a1af`.
The included official tokenizer is pinned to
`Qwen/Qwen3-Embedding-4B` revision
`5cf2132abc99cad020ac570b19d031efec650f2b`, SHA-256
`83cdf8c3a34f68862319cb1810ee7b1e2c0a44e0864ae930194ddb76bb7feb8d`.

The immutable [v1.0.6 pack release](https://github.com/superorange0707/project-brain/releases/tag/semantic-pack-v1.0.6)
is selected through its [descriptor](https://github.com/superorange0707/project-brain/releases/download/semantic-pack-v1.0.6/qwen3-embedding-4b-q6k-darwin-arm64-descriptor.json),
whose SHA-256 is `cbd09af575fb1b2e036abc17ed3e693e5bab4807af19efd2c1a9b5cd75ae8afc`.
It includes the checked GGUF, a relocatable source-pinned macOS ARM64
`llama.cpp` runtime, Apache-2.0 and runtime notices, a provenance record,
runtime arguments, 2560-dimensional last-token pooling metadata, the query
instruction/card/chunk versions, and public/synthetic batch, multilingual,
code, long-input, finite-vector, and ranking conformance cases. Its release
gate rejects Homebrew-linked and unresolved `@rpath` dependencies. It makes no
M3 Pro performance or private-repository relevance claim.

After installing a compatible Core release, use the controlled alias:

```bash
brain model install semantic
brain model verify semantic
brain model benchmark semantic
brain model autotune semantic
brain edition set semantic
brain refresh
```

The Core catalog pins the release descriptor SHA-256. The installer verifies
that descriptor, each GitHub Release part, and the final assembled model before
installing the normal local pack layout. Hugging Face is only a packaging-time
upstream source; enterprise runtime inference has no Hugging Face dependency.
The Core catalog is populated only after a clean temporary installation verifies
the published runtime as well as the descriptor and model checksums.

## Pack layout and manifest

An unpacked pack has no symlinks and contains all artifacts it declares:

```text
qwen3-reranker-4b-q6k-darwin-arm64/
  manifest.json
  llama-server
  model.gguf
  tokenizer.json
  conformance.json
  LICENSE
  LLAMA_CPP_LICENSE
  NOTICE
  PROVENANCE.md
```

Production manifests must contain checksums for the runtime, weights, tokenizer, and golden suite. `weight_sha256` and `tokenizer_sha256` must equal the matching entries in `artifacts`, so a pack cannot silently swap a model after review. `document_card_version` and `chunk_schema_version` are required for every pack; for rerankers, they version bounded candidate-card serialization rather than an embedding dimension. Use `embedding_dimension: 0` for a reranker.

Native runtime packs are platform-specific. A Windows amd64 pack uses
`llama-server.exe`, declares `platform: windows-amd64` in its release descriptor,
and declares `runtime_compatibility.os: windows` plus
`runtime_compatibility.architecture: amd64` in its manifest. Core rejects a
foreign native runtime before starting or registering it. The Windows builders
reuse the exact pinned model identity but rebuild and rerun public conformance
with a source-pinned portable Windows CPU runtime. Each Windows pack is first
published from a new draft Windows-specific pack tag; immutable Darwin releases
are never reopened or appended. A Windows alias is not added
to the Core catalog until the published descriptor SHA-256, reconstructed pack,
clean install, and actual Windows conformance have all been verified.

```json
{
  "pack_id": "qwen3-reranker-4b-q6k-darwin-arm64",
  "capability": "reranker",
  "model_family": "Qwen3",
  "upstream_model": "Qwen/Qwen3-Reranker-4B",
  "upstream_revision": "<official-commit-or-revision>",
  "license": "Apache-2.0",
  "weight_format": "GGUF",
  "quantization": "Q6_K",
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
  "minimum_brain_version": "0.6.4",
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

Conversion runs on the Project Brain model-pack builder, not on the enterprise
runtime machine. The `precision-pack-v*` workflow pins the exact official
`Qwen/Qwen3-Reranker-4B` revision, downloads and verifies both official
safetensor shards and the tokenizer, then uses the recorded `llama.cpp`
revision to make F16 GGUF followed by Q6_K. It records the original weight
hashes in `UPSTREAM_SHA256SUMS.txt`, the converted GGUF hash in the manifest,
and never trains or fine-tunes the model.

The equivalent reproducible commands are:

```bash
git -C /approved-src/llama.cpp checkout <pinned-llama.cpp-commit>
python /approved-src/llama.cpp/convert_hf_to_gguf.py \
  /approved-weights/Qwen3-Reranker-4B \
  --outtype f16 \
  --outfile /approved-build/qwen3-reranker-4b-f16.gguf
/approved-src/llama.cpp/build/bin/llama-quantize \
  /approved-build/qwen3-reranker-4b-f16.gguf \
  /approved-build/model.gguf Q6_K
shasum -a 256 /approved-build/model.gguf /approved-weights/Qwen3-Reranker-4B/tokenizer.json
```

Older pinned `llama.cpp` revisions name the converter `convert-hf-to-gguf.py`; use the name present in the exact checked-out revision and record it in `converter_revision`. Copy the matching tokenizer and upstream license into the pack. Do not use an unverified community GGUF as the final Precision pack. A community conversion is suitable only for development comparison against an official-reference suite.

## Conformance and local measurement

## Official Precision pack v1.0.2

The [v1.0.2 Precision release](https://github.com/superorange0707/project-brain/releases/tag/precision-pack-v1.0.2)
is the Core-catalogued macOS Apple Silicon reranker pack. Its immutable
descriptor SHA-256 is
`9070626e90b0306237bdf208ce0991cbf3804ee1bbee4ddca28c93df288f7df7` and
its Q6_K model SHA-256 is
`2fd4a7bbb61400e65bb3849f8d367759232be2206e1bb467b2b3d7ff42e79aeb`.
It derives without training from official `Qwen/Qwen3-Reranker-4B` revision
`ff536d3f82cc9ef977ea312094eb103d8446116a`, with pinned `llama.cpp`
`d775b8967a46d8beb110d444aa3b8938179e0dd8`. The release includes Apache-2.0
and runtime notices, source hashes, a local runtime, and public/synthetic
official-reference, long-input, batch/single, multilingual, code, and
10/20/40/80 pool conformance. It makes no M3 Pro or private-corpus claim.

Create the golden suite from public/synthetic texts and the official Qwen
Transformers reranker implementation on the pack-builder machine. The Precision
workflow uses the exact GGUF chat-template instruction, `Given a web search
query, retrieve relevant passages that answer the query`, so the official
reference and local runtime have the same input contract. It records official
reference scores, requires the same ranking order from Q6_K within a bounded
probability delta, and checks batch/single parity with an absolute tolerance of
`0.001` for native floating-point reduction differences. Every candidate is
checked in deterministic physical batches of at most 10 against the official
reference; batch/single parity uses deterministic top, midpoint, and endpoint
samples so 80-item pools do not require 80 redundant model passes. The suite
covers positive and negative pairs, multilingual and code-oriented pairs,
long/truncated input, finite non-empty scores, and 10/20/40/80 candidate pools.

`brain model verify PACK` checks declared files then runs the suite through the exact local runtime. `brain model benchmark PACK` adds public synthetic embedding batch throughput or 10/20/40/80 candidate-pool latency. Neither is a private-repository Recall/MRR claim. `brain model autotune PACK` stores the result and conservative batch/pool recommendation only under private Brain state; it does not publish model or source data.

Portable Windows Precision packs declare conservative pre-autotune defaults of
10 documents per physical request and a 20-candidate shortlist. Verification,
benchmarking, and production reranking share that physical bound; local
autotuning may lower or raise it within the global 80-candidate ceiling.

## Controlled installation

For a copied or internal-share pack:

```bash
brain model install /approved-share/qwen3-reranker-4b-q6k-darwin-arm64
brain model verify qwen3-reranker-4b-q6k-darwin-arm64
brain model benchmark qwen3-reranker-4b-q6k-darwin-arm64
brain model autotune qwen3-reranker-4b-q6k-darwin-arm64 --latency-budget-ms 3000
```

After the separate pack release has passed a clean release-asset installation,
a subsequent Core catalog release enables the controlled alias:

```bash
brain model install precision
brain model verify precision
brain model benchmark precision
brain model autotune precision
brain edition set precision
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

### Corporate TLS inspection

Model installation preserves full TLS certificate and hostname verification. The
Core downloader uses native operating-system trust through `truststore`: the
macOS Keychain on macOS, the Windows certificate store, and platform OpenSSL
trust on Linux. Therefore an
enterprise inspection root already trusted by the operating system is consumed
without adding that certificate to Project Brain or committing it to a pack.

`brain doctor` reports whether installation is using system trust or an
explicit bundle, without exposing CA contents, the bundle path, proxy
credentials, or environment values. If enterprise policy requires a local PEM
fallback, set the standard `SSL_CERT_FILE` for the command or add the following
to the local workspace configuration:

```toml
[models]
ca_bundle = "/approved/path/enterprise-ca.pem"
```

The bundle is added to the download trust context; it never enables insecure TLS, suppresses
hostname checks, expands approved redirect hosts, or bypasses descriptor and
artifact SHA-256 verification.

Corporate proxy settings remain available to the one-time HTTPS descriptor and
artifact downloader. They do not apply to an installed, verified pack's own
runtime: Project Brain launches that process on fixed `127.0.0.1` and uses a
dedicated no-proxy loopback transport for health, embedding, and reranking
calls. This is deliberately narrower than a process-wide proxy override and
does not require `NO_PROXY`; non-loopback URLs are rejected for this transport.
`brain doctor` reports this enforcement and only a safe yes/no proxy-configured
indicator, never a proxy URL, credential, certificate, or environment value.

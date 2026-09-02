# Release and distribution procedure

Project Brain Core releases and model packs are separate artifacts. Neither the
Core wheel/standalone archives nor the Homebrew formula contain model weights.
Core release validation must also include the packaged `truststore` dependency:
the standalone binary uses native macOS Keychain trust, the Windows certificate
store, and platform OpenSSL trust on Linux for one-time verified model-pack
downloads. Do not replace it
with an insecure context or a Project Brain-managed corporate CA bundle.
For an already-installed verified pack, separately smoke-test the Brain-owned
`127.0.0.1` runtime boundary with enterprise proxy configuration present. Its
health, embedding, and reranking calls must use direct loopback transport;
remote descriptor/artifact downloads must remain proxy-aware and checksum-
verified. Never solve a loopback proxy failure by changing global proxy settings
or weakening TLS.

## Core release and Homebrew

1. Run the full public/synthetic test suite on Python 3.11–3.14 for Linux and
   native Windows, build the wheel/sdist, and verify the macOS/Linux archives
   plus the native Windows amd64 ZIP from a clean tagged commit.
2. Smoke-test `install-project-brain.sh` against each macOS/Linux archive and
   `install-project-brain.ps1` against the Windows ZIP before publishing both
   installers as release assets. Each installer must consume the final
   `SHA256SUMS.txt` and preserve existing Brain-owned state.
3. Push a `vX.Y.Z` tag. The release workflow builds all artifacts, writes
   `SHA256SUMS.txt`, creates signed GitHub build-provenance attestations for
   every asset, and creates the GitHub Release. Verify the downloaded Windows
   ZIP (and any other asset) with
   `gh attestation verify <asset> --repo superorange0707/project-brain` in
   addition to checking `SHA256SUMS.txt`; provenance verification is a stable
   v1 release gate, not an optional installer reset or workspace migration.
4. Only after that release succeeds, the `homebrew-tap` job downloads the
   published checksum asset and compares it with the build checksum file.
5. If both match and the repository has a narrowly scoped
   `HOMEBREW_TAP_TOKEN` Actions secret with write access only to
   `superorange0707/homebrew-tap`, it renders, syntax-checks, commits, and
   pushes the formula.

The Homebrew job depends on `github-release`, so a failed build, checksum step,
or release creation cannot change the tap. If the token is absent, Core release
publication still succeeds and the tap remains an explicit release-engineering
follow-up; do not put a personal token in source or a release artifact.

## Semantic model-pack release

The first Core-catalogued official Semantic pack,
[v1.0.6](https://github.com/superorange0707/project-brain/releases/tag/semantic-pack-v1.0.6),
was published by an independent `semantic-pack-v*` tag. Its immutable
descriptor SHA-256 is
`cbd09af575fb1b2e036abc17ed3e693e5bab4807af19efd2c1a9b5cd75ae8afc`.
The workflow runs only on macOS Apple Silicon and:

1. downloads the exact official Qwen GGUF and tokenizer revisions and verifies
   their upstream SHA-256 values;
2. builds the recorded relocatable static `llama.cpp` runtime locally with
   Metal support, rejecting Homebrew-linked and unresolved `@rpath` binaries;
3. runs public/synthetic conformance through that exact local runtime;
4. emits the manifest, provenance, Apache-2.0 and runtime notices, descriptor,
   model parts, and `SHA256SUMS.txt`;
5. verifies the emitted checksums before creating the model-pack GitHub Release.

The Core catalog is updated in a subsequent Core release with the descriptor
SHA-256. This ordering prevents `brain model install semantic` from resolving an
unpinned `latest` asset. The target work machine downloads only the
Project Brain-controlled release during installation; all later inference is
local and offline.

Do not add an entry to the Core catalog until a clean temporary installation
also verifies the packaged local runtime can start and complete its golden
suite.

The manually dispatched Windows Semantic and Precision workflows default to
build-only mode. They use immutable upstream/model descriptors, build a native
portable `llama-server.exe`, run the same bounded public conformance, and upload
Actions artifacts. Setting their `publish` input is a separate protected release
action from the default branch: it requires a new `*-pack-windows-vX.Y.Z` tag,
creates a draft release at the reviewed builder commit, uploads and re-downloads
the complete checksummed asset set, and only then publishes it as immutable. It
never appends Windows assets to an already-published Darwin pack release. After
publication, verify the release assets on a clean Windows 11 x64 host and only
then pin the exact Windows descriptor SHA-256 in a later reviewed Core change.
Never copy the Darwin descriptor into the Windows catalog.

No release workflow claims target-machine latency, M3 Pro memory usage, or
private-ticket replay quality until those local validations exist. A verified
Precision pack is an offline capability; its target-machine performance and
private-local-data quality remain separate measurements.

## Precision model-pack release

Precision pack v1.0.2 was built on a controlled 32 GB Apple Silicon pack
builder because the standard GitHub `macos-15` runner has only 7 GB RAM while
the official F16 reference weights are about 7.7 GiB. The checked-in workflow
is therefore manually dispatched only; it remains the reproducible recipe for
an approved builder with sufficient memory and does not create a failed release
candidate automatically on an under-provisioned tag push.

The official Precision pack follows the same separate-release and
descriptor-pinning sequence. A `precision-pack-v*` tag runs on macOS Apple
Silicon and first downloads the pinned official
`Qwen/Qwen3-Reranker-4B` safetensors/tokenizer source. It checks the source
SHA-256 values, converts F16 GGUF with the recorded `llama.cpp` commit,
quantizes that result to Q6_K, and records both source and derived hashes.
There is no training, fine tuning, or community GGUF dependency.

Before the model-pack release is created, the workflow compares public/
synthetic scores from the official Qwen Transformers reranker against the exact
local Q6_K runtime. It checks ranking order, bounded score delta, multilingual
and code pairs, long/truncated input, finite scores, batch/single parity within
the shared `0.001` absolute native-runtime tolerance, and 10/20/40/80 candidate
pools. It then performs a clean temporary local-pack installation, verification,
and public synthetic benchmark. The release asset
descriptor is only added to the Core catalog in a later Core tag after a fresh
downloaded-release installation verifies the descriptor, parts, assembled GGUF,
provenance, and runtime conformance.

The pack, benchmark report, and release notes must not claim target-machine M3
performance or private-repository/ticket quality. Those are intentionally local
target-machine and private-data measurements.

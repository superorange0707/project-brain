# Release and distribution procedure

Project Brain Core releases and model packs are separate artifacts. Neither the
Core wheel/standalone archives nor the Homebrew formula contain model weights.

## Core release and Homebrew

1. Run the full public/synthetic test suite, build the wheel/sdist, and verify
   the standalone archives from a clean tagged commit.
2. Push a `vX.Y.Z` tag. The release workflow builds all artifacts, writes
   `SHA256SUMS.txt`, and creates the GitHub Release.
3. Only after that release succeeds, the `homebrew-tap` job downloads the
   published checksum asset and compares it with the build checksum file.
4. If both match and the repository has a narrowly scoped
   `HOMEBREW_TAP_TOKEN` Actions secret with write access only to
   `superorange0707/homebrew-tap`, it renders, syntax-checks, commits, and
   pushes the formula.

The Homebrew job depends on `github-release`, so a failed build, checksum step,
or release creation cannot change the tap. If the token is absent, Core release
publication still succeeds and the tap remains an explicit release-engineering
follow-up; do not put a personal token in source or a release artifact.

## Semantic model-pack release

The first official Semantic pack is published by an independent
`semantic-pack-v*` tag. Its workflow runs only on macOS Apple Silicon and:

1. downloads the exact official Qwen GGUF and tokenizer revisions and verifies
   their upstream SHA-256 values;
2. builds the recorded `llama.cpp` revision locally with Metal support;
3. runs public/synthetic conformance through that exact local runtime;
4. emits the manifest, provenance, Apache-2.0 and runtime notices, descriptor,
   model parts, and `SHA256SUMS.txt`;
5. verifies the emitted checksums before creating the model-pack GitHub Release.

The Core catalog is updated in a subsequent Core release with the descriptor
SHA-256. This ordering prevents `brain model install semantic` from resolving an
unpinned `latest` asset. The target work machine downloads only the
Project Brain-controlled release during installation; all later inference is
local and offline.

No release workflow claims target-machine latency, M3 Pro memory usage, private
ticket replay quality, or Precision capability until those local validations
exist.

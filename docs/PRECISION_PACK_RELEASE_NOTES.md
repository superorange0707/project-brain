# Project Brain Precision model pack

This release is a separately versioned, macOS Apple Silicon-only offline
reranker pack. It does not change Project Brain Core and it does not bundle a
model into the Core wheel, standalone archive, or Homebrew formula.

It is built from the exact official `Qwen/Qwen3-Reranker-4B` revision recorded
in the embedded provenance. The release workflow verifies the source
safetensors and tokenizer SHA-256 values before converting the source with the
recorded `llama.cpp` revision to Q6_K. No training or fine tuning occurs.

The release includes only public/synthetic conformance data. Its local runtime
is loopback-only and offline after installation. The release checks final
checksums, an official-Qwen Transformers reference comparison, local Q6_K
ordering/score/batch parity, long truncated input, multilingual/code pairs, and
10/20/40/80 candidate-pool cases.

It does not claim M3 Pro latency or memory figures, or private-ticket or
private-repository relevance results. Those remain local target-machine and
private-local-data validations.

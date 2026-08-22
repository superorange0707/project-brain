# Project Brain v0.6.4

Project Brain v0.6.4 catalogs the independently released offline Precision
pack without changing the Core product boundary. The Qwen3-Reranker-4B Q6_K
weight is never bundled into Core or Homebrew. `brain model install precision`
pins Precision pack v1.0.2 descriptor SHA-256
`9070626e90b0306237bdf208ce0991cbf3804ee1bbee4ddca28c93df288f7df7`.

The Precision pack workflow checks exact official source weights/tokenizer,
uses a pinned llama.cpp conversion/quantization toolchain, and compares public
synthetic results with the official Qwen Transformers reranker before it can
publish a Q6_K model pack. The installed local verifier repeats order, bounded
score delta, long-input, batch/single, and 10/20/40/80 candidate-pool checks.

## Release qualification

- The exact tagged source runs the full public/synthetic regression suite and
  builds wheel, source, and standalone artifacts in GitHub Actions.
- Core requires no model, hosted index, API key, or cloud service.
- This release does not ship model weights and does not turn Project Brain into
  a coding agent or source-editing tool.
- `brain model install precision` remains intentionally unavailable until the
  immutable model-pack release and post-release clean installation are verified
  and its descriptor is pinned in the Core catalog.

## Deliberately not claimed

- No Apple M3 Pro / 36 GB latency, memory, Semantic, or Precision performance
  numbers are claimed.
- No private-enterprise repository, ticket-replay, Recall, MRR, nDCG, or
  target-machine benchmark results are claimed.
- The release makes no Qwen3-Reranker-4B target-machine performance claim.
  Precision remains unavailable until its separately verified reranker pack is
  installed. Organization approval remains a local policy decision;
  Core stays fully usable without model packs.

See `MILESTONE_REPORT.md` and `docs/MODEL_PACKS.md` in the tagged source for the
verified scope, offline-pack procedure, and target-machine/private-data follow-up.

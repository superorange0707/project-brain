# Project Brain v0.6.1

Project Brain v0.6.1 strengthens the local, read-only code-intelligence Core:
persistent indexed retrieval, immutable snapshot verification, compact
candidate-first context packing, structural/history relationships, and explicit
Core fallback behavior.

It also introduces optional on-device Semantic and Precision infrastructure:
auditable offline model packs, snapshot-filtered semantic retrieval, bounded
protected reranking, local conformance, model benchmarking, and autotuning.
Semantic models and rerankers can only discover or reorder candidates; final
source evidence is always re-read from the investigation's pinned Git snapshot.

## Release qualification

- The exact tagged source runs the full public/synthetic regression suite and
  builds wheel, source, and standalone artifacts in GitHub Actions.
- Core requires no model, hosted index, API key, or cloud service.
- This release does not ship model weights and does not turn Project Brain into
  a coding agent or source-editing tool.

## Deliberately not claimed

- No Apple M3 Pro / 36 GB latency, memory, Semantic, or Precision performance
  numbers are claimed.
- No private-enterprise repository, ticket-replay, Recall, MRR, nDCG, or
  target-machine benchmark results are claimed.
- Organization approval and installation of Qwen3 embedding/reranker model packs
  remain local policy decisions; Core remains fully usable without them.

See `MILESTONE_REPORT.md` and `docs/MODEL_PACKS.md` in the tagged source for the
verified scope, offline-pack procedure, and target-machine/private-data follow-up.

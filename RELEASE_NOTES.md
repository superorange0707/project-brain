# Project Brain v0.6.6

Project Brain v0.6.6 fixes a corporate-proxy compatibility issue affecting
already-installed Semantic and Precision packs. When Project Brain launches a
verified pack-owned `llama.cpp` process, its health, embedding, and reranking
calls now use dedicated direct transport to the process's fixed `127.0.0.1`
endpoint. A proxy rule that bypasses `localhost` but not numeric loopback can
therefore no longer intercept model verification or local inference.

The bypass is deliberately limited to a verified, Project Brain-managed
loopback process. It does not change system proxy settings or require
`NO_PROXY`. One-time GitHub model descriptor and artifact downloads remain
proxy-aware and continue using operating-system trust, certificate and hostname
verification, approved-host validation, and descriptor/release-part/assembled-
model SHA-256 checks. `brain doctor` reports the enforced boundary and only a
safe proxy-configured indicator; it never displays proxy URLs, credentials,
certificate material, or environment values.

## Release qualification

- The exact tagged source runs the full public/synthetic regression suite and
  builds wheel, source, and standalone artifacts in GitHub Actions.
- Core requires no model, hosted index, API key, or cloud service.
- This release does not ship model weights and does not turn Project Brain into
  a coding agent or source-editing tool.
- The independent Semantic and Precision model packs remain hash-pinned,
  separately released artifacts; Core and Homebrew never bundle their weights.

## Deliberately not claimed

- No Apple M3 Pro / 36 GB latency, memory, Semantic, or Precision performance
  numbers are claimed.
- No private-enterprise repository, ticket-replay, Recall, MRR, nDCG, or
  target-machine benchmark results are claimed.
- The release makes no Qwen3-Reranker-4B target-machine performance claim.
  Organization approval remains a local policy decision; Core stays fully
  usable without model packs.

See `MILESTONE_REPORT.md` and `docs/MODEL_PACKS.md` in the tagged source for the
verified scope, offline-pack procedure, and target-machine/private-data follow-up.

# Project Brain v0.6.7

Project Brain v0.6.7 hardens Semantic indexing for real codebases with large
or multilingual semantic cards. It bounds each complete embedding input and
the exact UTF-8 JSON body sent to the local runtime, including the document
instruction and pack input suffix. When a card exceeds that bound, Brain keeps
its repository/path/symbol identity and deterministically truncates only its
code section.

If a verified pack-owned local embedding runtime disconnects during indexing,
Brain restarts it and retries bounded smaller batches. Successful sub-batches
remain in the content-addressed cache; a persistent single-card failure emits
only safe size diagnostics. Semantic USearch shards are written as a new
generation and published only after the complete build succeeds, preserving the
previous generation after a failure. An unchanged subsequent refresh reuses the
published semantic generation instead of rebuilding it.

This release retains the v0.6.6 network-security boundary unchanged. One-time
GitHub model descriptor and artifact downloads remain proxy-aware and use
operating-system trust, certificate and hostname verification, approved-host
validation, and descriptor/release-part/assembled-model SHA-256 checks.
Verified pack-owned `127.0.0.1` inference remains direct no-proxy transport
with an ephemeral API key. No setting requires `NO_PROXY`, disables TLS, or
exposes source content, credentials, proxies, or certificates.

## Release qualification

- The exact tagged source runs the full public/synthetic regression suite,
  wheel/sdist clean-install check, and four-platform standalone workflow.
- Core requires no model, hosted index, API key, or cloud service.
- This release does not ship model weights and does not turn Project Brain into
  a coding agent or source-editing tool.
- The independent Semantic and Precision model packs remain hash-pinned,
  separately released artifacts; Core and Homebrew never bundle their weights.

## Deliberately not claimed

- Passing release gates does not by itself prove the prior enterprise-machine
  disconnect fixed; the affected machine must run `brain refresh --no-fetch
  --no-discover` and report `semantic_chunks > 0`.
- No Apple M3 Pro / 36 GB latency, memory, Semantic, or Precision performance
  numbers are claimed.
- No private-enterprise repository, ticket-replay, Recall, MRR, nDCG, or
  target-machine benchmark results are claimed.
- The release makes no Qwen3-Reranker-4B target-machine performance claim.
  Organization approval remains a local policy decision; Core stays fully
  usable without model packs.

See `MILESTONE_REPORT.md` and `docs/MODEL_PACKS.md` in the tagged source for the
verified scope, offline-pack procedure, and target-machine/private-data follow-up.

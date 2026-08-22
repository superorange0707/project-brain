# Project Brain v0.6.5

Project Brain v0.6.5 fixes enterprise TLS compatibility for one-time Semantic
and Precision model-pack downloads. The packaged downloader now uses native
operating-system trust through `truststore`: the macOS Keychain on macOS and
platform OpenSSL trust on Linux. A corporate inspection root already trusted by
the operating system is therefore recognized by the standalone/Homebrew binary.

TLS certificate and hostname verification remain mandatory. `brain doctor`
reports the safe trust mode without disclosing certificate material, CA paths,
proxy credentials, or environment values. Administrators may add a local PEM
bundle through `models.ca_bundle` or the standard `SSL_CERT_FILE` when policy
requires it; the approved-host and descriptor/release-part/assembled-model
SHA-256 gates remain unchanged.

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

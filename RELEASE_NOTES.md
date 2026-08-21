# Project Brain v0.6.3

Project Brain v0.6.3 completes distribution readiness for the optional offline
Semantic edition without changing the Core product boundary. The separately
versioned Qwen3-Embedding-4B Q6_K pack is never bundled into Core or Homebrew;
Core pins its Project Brain release descriptor and verifies every downloaded
part, the assembled GGUF, pack manifest, provenance, and public/synthetic
conformance before local use.

The release also adds a final-release-only Homebrew automation gate. The tap is
updated only after the GitHub Release and its published `SHA256SUMS.txt` exist
and match the build output. Failed candidates cannot update the tap. The
authorization check is evaluated inside the post-release job, so an absent tap
token safely skips that update rather than invalidating the Core release.

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
- Precision remains unavailable until a separately verified reranker pack is
  installed. Organization approval remains a local policy decision; Core stays
  fully usable without model packs.

See `MILESTONE_REPORT.md` and `docs/MODEL_PACKS.md` in the tagged source for the
verified scope, offline-pack procedure, and target-machine/private-data follow-up.

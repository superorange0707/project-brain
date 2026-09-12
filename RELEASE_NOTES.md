# Project Brain v1.0.25 — Requested Test Evidence

This stable patch improves requested test-source retrieval while preserving existing
workspaces and ticket-pinned generations.

## What changed

- Deliver actual test source for protocol-v5 `test_surface` and explicit test
  evidence requests with symbol anchors, rather than stopping at a definition.
- Search test paths through the existing pinned lexical index before applying
  source candidate limits. Separate test repositories remain discoverable when
  the request has no explicit repository restriction.
- Avoid unused definition queries and optional model startup for qualified-only
  requests. Mixed investigations retain their broader retrieval paths.
- Preserve old-ticket generation pins, scope-specific caches, source integrity
  checks and bounded work. Regression fixtures cover 10/50/100 repositories,
  incomplete-result cache safety and old/new-ticket source isolation.

Test references provide source evidence for investigation; they do not establish
runtime test coverage or infer exact receiver types for ambiguous method names.

Includes earlier Atlas parse-timeout diagnostics/recovery, reduced repeated
call-graph parsing, Semantic shard/vector reuse and per-ticket handoffs.

## Upgrade

Finish any active refresh or investigation before upgrading. On macOS:

```sh
brain ui stop
brew update
brew upgrade project-brain
brain --version
brain ui
```

The version must be `brain 1.0.25`. Windows and Linux users can use the matching
native release archive and existing installer. All supported native archives,
Python distributions, installers and `SHA256SUMS.txt` accompany the release.

No Atlas/Semantic schema or embedding-input changes. Model packs, caches,
published generations and ticket sessions remain reusable. No reset, model
reinstall or refresh is required solely for this upgrade. If a refresh
previously failed, retry Refresh Brain or `brain refresh`; do not delete indexes.

Agent Kit remains v4 and the request protocol remains v5. No new Agent is
required. Existing installations already using these versions need no kit
update for this patch.

## Scope

Target source remains read-only. Exact ticket-pinned source is the evidence
authority; unavailable or corrupt old generations never substitute newer ones.
Parsing and input limits remain enforced, with explicit failures rather than
publishing incomplete state as ready.

Regression fixtures are not a guarantee of universal search quality or fixed
enterprise refresh times. Existing opt-in PyPI publication policy is unchanged.

See [CHANGELOG.md](CHANGELOG.md) for version history.

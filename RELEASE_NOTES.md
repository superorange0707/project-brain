# Project Brain v1.0.24 — Qualified Source Retrieval

This stable patch improves precise source lookup while preserving existing
workspaces and ticket-pinned generations.

## What changed

- Find methods in deep source paths using registered Atlas entities, including
  Java class/package-qualified symbols and Python module-qualified declarations
  in flat and src layouts. Incorrect packages and owners do not silently expand
  to unrelated same-named methods. Python dynamic imports and re-exports are
  not inferred.
- Deliver the requested definition through the existing exact-source verifier.
  Exact-symbol-only requests avoid unrelated discovery and model startup;
  mixed investigations keep their broader retrieval paths.
- Validate scope on warm-cache reads and preserve old-ticket generation pins.
  Regression checks cover actual protocol-v5 handoffs, corruption degradation,
  and bounded query work at 10/50/100 repositories.

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

The version must be `brain 1.0.24`. Windows and Linux users can use the matching
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

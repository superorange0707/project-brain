# Project Brain v1.0.22 — Method-Scoped Symbol Tracing

This stable patch improves symbol tracing and investigation evidence
while preserving existing workspaces and generation-pinned tickets.

## What changed

- Attribute direct callees to the requested method, not neighboring methods,
  comments or quoted text. Include resolved callee locations in symbol-request
  candidates so they can be delivered through the existing source verifier.
- Read validated call edges from the ticket's pinned Atlas generation instead
  of rescanning source during symbol tracing. Legacy or unavailable graph data
  falls back to bounded exact-source extraction; old tickets never substitute
  a newer generation. Physical work and returned callees remain bounded.
- Regression checks cover method ownership, pinned-generation isolation,
  corrupt-data fallback, resource cleanup and constant graph-query work at
  10/50/100 repositories.

## Included from recent patches

- Find method implementations beyond repositories that only call or mention
  them. Recognize camelCase, acronym and snake_case identifiers; do not confuse
  Python control flow or quoted text with function declarations.
- Keep converging call branches and links between investigation starting points.
  Recursive edges remain visible without presenting cyclic execution paths.
- Reuse bounded Java source transformations within one investigation request.
  Source authority and delivered line ranges are still checked on every use;
  temporary parsed results are released when the request finishes.
- Includes v1.0.19's reduced Atlas call-graph scanning, actionable parse-timeout
  recovery, broader Semantic routing and per-ticket investigation handoffs.

## Upgrade

Finish any active refresh or investigation before upgrading. On macOS:

```sh
brain ui stop
brew update
brew upgrade project-brain
brain --version
brain ui
```

The version must be `brain 1.0.22`. Windows and Linux users can use the matching
native release archive and the existing installer. The wheel, source
distribution, installers and `SHA256SUMS.txt` accompany the release.

No Atlas/Semantic schema or embedding-input contract changes. Existing model
packs, embedding cache, index generations and ticket sessions remain reusable.
No reset, model reinstall or refresh is required solely for this upgrade. If a
refresh previously failed, retry Refresh Brain or `brain refresh`; do not delete
prior indexes or caches.

Agent Kit remains v4 and the request protocol remains v5. No new Agent is
required. When upgrading from an older Agent Kit, run
`brain agent-kit m365 --json` and update the existing Agent's instructions and
project knowledge.

## Scope

Target source remains read-only and exact ticket-pinned source remains the
evidence authority. This patch does not bypass parsing or input limits and does
not publish an incomplete Atlas generation as ready.

Regression checks cover implementation discovery at 10/50/100 repositories,
converging and recursive call graphs, generation isolation and bounded source
reuse. They are not a claim of universal search quality or fixed enterprise
refresh times. The existing opt-in PyPI publication policy is unchanged.

See [CHANGELOG.md](CHANGELOG.md) for version history.

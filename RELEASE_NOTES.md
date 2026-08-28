# Project Brain v0.9.0 — Workspace Intelligence Atlas

Project Brain v0.9 evolves the v0.8.0-rc2 retrieval engine into one immutable,
catalog-authoritative Workspace Intelligence Atlas while preserving exact
pinned-source evidence, Core fallback, and v1/v2/v3 compatibility.

- Immutable Atlas generations now publish multi-snapshot lexical serving,
  Repo/Module/Entity hierarchy and semantic cards, typed provenance
  relationships, Change Intelligence, legacy indexes, and component status in
  one transaction. Tickets pin the resulting identity, and GC follows live pins.
- Blob-level refresh reuses unchanged entity/region/edge identities and embedding
  cache entries; generation caches contain routing IDs only and validate every
  hit against generation membership.
- Retrieval begins with Investigation Memory and similar-ticket priors, then
  performs hierarchical Repo → Module → Entity routing, progressive widening,
  graph expansion, targeted lexical/path/semantic fallback, bounded reranking,
  deterministic Next-Best-Evidence planning, and exact source hydration.
- Generation-scoped cross-ticket caches and Ticket Prefetch warm only routing
  state. Session-authoritative Coverage Maps, stable IDs, CONTEXT/INVESTIGATION
  protocol v4 full/delta checkpoints, recovery, and M365 Agent Kit v3 reduce
  repeated rounds without weakening evidence or session isolation.
- Incremental Atlas refresh reuses unchanged content safely, multi-investigation
  UI coordination and Auto Refresh When Idle preserve workspace mutation locks,
  and a filesystem model lane serializes local inference across processes.
- Local evaluation includes Atlas routing/graph/evidence recall, timing,
  physical-operation, cache/prefetch, delta, late-candidate, and convergence
  metrics. Adversarial correctness hardening preserves exact pinned source as
  final evidence authority and all Core/Semantic/Precision fallbacks.

Field performance will continue to be measured on large enterprise workspaces.
The remaining identified opportunities concern performance and serving
optimization, not known release-blocking correctness defects. This release does
not publish model packs or begin v1.0 work.

---

# Project Brain v0.8.0 Release Candidate 2 (historical)

Project Brain v0.8.0 turns retrieval into an interactive, bounded code-
intelligence workflow while preserving exact pinned-source evidence and Core
fallback.

- `CONTEXT_REQUEST` v3 accepts an objective by itself plus optional bounded
  repository/literal/symbol/path/file/history hints and evidence coverage.
  Unknown keys and unsafe/unbounded inputs fail closed; v1/v2 remain supported.
- The deterministic planner performs cheap global discovery, ranks an initial
  six-repository scope, fuses duplicate/shared-symbol work, enforces 15 logical
  and 200 physical-operation defaults, and widens to 16/all only when required.
- Candidates are fused and pruned to 200 before optional Precision reranking;
  direct paths and definitions remain protected and exact pinned source is
  re-read before evidence publication.
- One shared four-worker repository pool and bounded parallel Semantic shard
  search reduce serial waiting. A single model lane prevents simultaneous local
  4B embedding/reranking runtimes.
- Shared workspace retrieval leases plus per-ticket locks allow two independent
  investigations while serializing one ticket. Refresh, Semantic publication,
  edition/model changes, and GC remain workspace-exclusive across UI/CLI
  processes.
- The UI now starts from the current Brain snapshot by default, runs retrieval
  as ticket background jobs, shows an investigation board, and exposes safe
  progress/profiler fields for requested/effective/physical operations,
  routing, pruning, stage timings, and stop reason.
- Opt-in **Auto Refresh: When idle** checks only selected commits, Core/Semantic
  alignment, and repository discovery; it debounces changes, waits behind active
  ticket retrievals, and invokes the existing authoritative refresh exactly
  once. `brain watch` uses the same detector and scheduler.
- The M365 Agent Kit teaches objective-first v3, one bounded follow-up, and
  `FINAL_SOLUTION` convergence. `AGENT_KIT.json` records Brain 0.8.0, kit
  version 2, and protocol 3.
- Retrieval traces use schema version 2 while old traces/sessions remain
  readable. The synthetic 50-repository fixture reduces 80 requested operations
  (4,000 repository-backend calls in the unfused v0.7 execution shape) to 2
  effective operations and 56 physical backend calls.

This release candidate is intended for target-machine field validation before
stable v0.8.0 promotion. It does not publish model packs or change their schema.

# Project Brain v0.7.0

Project Brain v0.7.0 upgrades `brain ui` into the normal local operations
cockpit while keeping CLI commands available for automation and advanced
workflows.

- CLI and UI now share one full-refresh operation: repository discovery,
  allowed fetches, immutable snapshots, Core indexes, maps, relationships,
  experience/graph state, and Semantic indexing when Semantic or Precision is
  selected.
- The UI reports Core, Semantic, Precision, model-pack, freshness, and managed
  runtime state explicitly. A Semantic pack being installed does not imply it
  is indexed or active.
- A synchronized UI ticket start verifies that the requested Semantic or
  Precision edition is actually active before it pins the investigation. If
  snapshot alignment fails or Precision lacks a verified compatible reranker,
  the ticket does not start unless the user explicitly chooses the visible
  degraded path.
- Local refresh/model/edition work uses bounded status-tracked jobs. A small
  re-entrant workspace lock now extends the UI's in-process single-writer rule
  across CLI and UI processes for refresh, Semantic publication, edition,
  model, and GC mutations. A concurrent operation fails safely before it can
  publish state; errors expose no source contents, credentials, proxy data, or
  certificate material.
- Refresh jobs now retain structured, source-free progress from the real
  Semantic indexing loop: repository/card/cache/new-embedding/batch/shard
  counters, generation reuse or rebuild, and elapsed time. The CLI renders the
  same events as concise progress lines; no second refresh implementation was
  introduced.
- Retrieval results record requested/effective edition, local semantic/reranker
  participation, candidate/evidence counts, pinned generation, and safe timing
  metadata for UI inspection.

The v0.6.6/v0.6.7 TLS, proxy, checksum, loopback-runtime, atomic-Semantic, and
read-only product boundaries are unchanged. This release ships no model weights
and introduces no coding-agent, shell, source-editing, or cloud behavior.

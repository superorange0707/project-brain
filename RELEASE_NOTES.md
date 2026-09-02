# Project Brain v1.0.0 — Investigation Runtime

Project Brain v1.0 turns the v0.9 Workspace Intelligence Atlas into a bounded,
generation-pinned investigation runtime while preserving exact source as the
final repository evidence authority.

- Runtime Anchor Resolver handles symbols, stack frames, endpoints, events,
  topics, configuration, persistence, constants, packages, and file hints with
  bounded generation-scoped cache validation and explicit ambiguity.
- Enterprise Java/Spring refresh intelligence covers MVC, Feign, Kafka, Spring
  components/cache/configuration, JPA, and test references through additive,
  immutable Atlas-generation components.
- Ordered ExecutionFlow, cross-repository IntegrationFlow, Program Slice Lite,
  and implementation/test/impact/contract/config-data surfaces remain candidate
  navigation intelligence until their pinned source locations are verified.
- The multi-wave controller preserves one Atlas/Semantic serving generation,
  stable evidence/anchor/flow/blocker/context identities, Hypothesis Ledger,
  Evidence Frontier, progressive metadata, and first-useful checkpoints for the
  life of a ticket. It never substitutes a newer generation.
- Investigation Protocol v5 adds strict byte-aware inputs, modes, runtime
  anchors, stable lineage, full/delta checkpoints, and stale-base recovery while
  retaining v1–v4 compatibility.
- M365 Agent Kit v4 adds the coding-agent-style investigation state machine,
  evidence labels, challenge/stop rules, and `INVESTIGATION_PROTOCOL.md`.
- The Investigation Cockpit and local evaluation expose serving state, waves,
  flows, surfaces, blockers, first-useful timing, M365 contract quality, and
  bounded physical-operation metrics.

The v1 schema migration is additive and transactional. Existing `brain.toml`,
snapshots, v0.9 Atlas/Semantic generations, embedding cache, model packs, and
ticket sessions are preserved. Optional capability degradation never weakens
exact pinned-source correctness. No hosted inference, source upload, source
editing, target-code execution, or autonomous implementation behavior is added.

This implementation is prepared for final supervisory review only; this work
does not tag, publish, deploy, create a GitHub Release, or update Homebrew.

Native Windows 11 x64 is now a first-class v1 target. The implementation adds
the permanent Python 3.11–3.14 Windows matrix, native process-tree and
cross-process locking behavior, stable separator-independent evidence paths,
PowerShell clipboard integration, adjacent `.exe` backend discovery, a pinned
four-executable Windows standalone ZIP workflow, and separately gated Windows
Semantic/Precision pack builders. No Windows artifact or pack is treated as
official until its Windows runner, conformance, checksum, and clean-archive
gates have actually passed and the resulting immutable descriptors are pinned.

---

# Project Brain v0.9.2 — Python 3.11 Compatibility Hotfix

Project Brain v0.9.2 restores the declared Python 3.11 compatibility of the
v0.9 Workspace Intelligence Atlas.

- Investigation Memory evidence identity no longer uses nested f-string syntax
  that Python 3.11 rejects at parse time.
- The exact UTF-8, NUL-delimited content identity and resulting Atlas evidence
  IDs are unchanged.
- Stable release publication now waits for tests and source compilation on
  Python 3.11, 3.12, 3.13, and 3.14.

This is a syntax-compatibility-only patch. It requires no Atlas schema
migration, Semantic or model reset, embedding-cache deletion, Atlas rebuild, or
ticket-session migration. A normal `brain refresh` remains optional workspace
operation rather than an upgrade requirement. This release does not begin v1.0
implementation.

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
  alignment, and repository scope; it debounces recoverable changes, waits behind
  active ticket retrievals, and invokes the existing authoritative refresh
  exactly once. An unconfigured repository becomes **Action Required** instead
  of entering a failing refresh loop. `brain watch` uses the same detector and scheduler.
- Repository discovery is read-only during refresh. Newly cloned repositories
  are reported as an explicit action and require a user-applied `brain.toml`
  block, preventing concurrent editor saves or partial writes from being
  overwritten by automatic configuration mutation.
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

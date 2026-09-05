# Project Brain v1.0.10 — Large-Workspace Reliability and Recovery

Project Brain v1 turns the Workspace Intelligence Atlas into a bounded,
generation-pinned investigation runtime. v1.0.10 focuses on reliable daily use
in large enterprise workspaces and makes operational failures actionable from
the local UI.

## Highlights

- A ticket-first glass workspace with light/dark appearance, generation/wave
  cards, persistent errors, configuration reload and safe recovery actions.
  Switching tickets clears old previews and guards late asynchronous results.
- Fixed the large-directory `state capacity scan budget exceeded` trap: full
  write/cleanup inventory streams actual file sizes, independently of quick
  status-probe limits. Quota, free disk, safe tree depth and pin-aware GC remain
  enforced; no index deletion or quota-disable workaround is required.
- Verified identical Semantic generations skip source rechunking and embedding.
  Source parsing, bulk cache accounting and pinned context packaging avoid
  repeated scans. Candidate fusion uses actual per-channel ranks, and evaluation
  distinguishes metadata candidates from hydrated source evidence.
- Windows supports both a version-pinned online install and a fully offline
  `-ArchivePath` install using the downloaded ZIP and `SHA256SUMS.txt`. Both
  paths validate before activation and preserve workspace/model/session state.
  The script never changes Execution Policy or requests credentials.
- Optional query-cache writes cannot make valid Semantic retrieval unavailable.
  A bounded memory cache reuses queries in long-lived UI sessions, and cache
  persistence has short inventory/SQL budgets. Capacity and GC accounting use
  current file sizes, not potentially stale snapshot seal totals.
- The Brain UI adds **Storage & recovery** with a safe cleanup preview and a
  guarded one-click reclaim action. It preserves current state and every
  ticket-pinned generation; incomplete reachability proof removes nothing.
- `brain ui` reopens an already-running local instance. `brain ui status` and
  `brain ui stop` provide explicit lifecycle control, and refresh progress
  and failed/completed outcomes survive browser reloads or UI restarts without
  claiming false completion. Uncertain health checks preserve the instance;
  local control bypasses proxies and refuses redirects.
- M365 evidence-ID checks no longer credit prefix collisions such as `E00010`
  when a response must cite `E0001`.
- Auto Refresh remote checks share one bounded 45-second workspace deadline,
  preventing repository-count multiplication on slow corporate networks.
- Repository discovery rolls back verified partial `brain.toml` appends and UI
  refresh reloads the authoritative configuration before indexing. Config
  appends verify exact bounded bytes through the same handle, avoiding Windows
  timestamp mismatches without accepting concurrent content changes.
- M365 handoffs are organized as `generated/handoffs/<TICKET>/...`; existing
  flat handoff references remain readable.
- The stable release gate now confirms every uploaded asset is anonymously
  downloadable and checksum-valid before the release remains public.

## Downloads

The official workflow builds and verifies:

- `project-brain-v1.0.10-macos-arm64.tar.gz`
- `project-brain-v1.0.10-macos-amd64.tar.gz`
- `project-brain-v1.0.10-linux-arm64.tar.gz`
- `project-brain-v1.0.10-linux-amd64.tar.gz`
- `project-brain-v1.0.10-windows-amd64.zip`
- `project_brain_context-1.0.10-py3-none-any.whl`
- `project_brain_context-1.0.10.tar.gz`
- `install-project-brain.sh`
- `install-project-brain.ps1`
- `SHA256SUMS.txt`

Every asset is checksum-verified and receives GitHub build-provenance
attestation. Model weights remain separate and are never bundled into Core.

## Upgrade safety

No Atlas/Semantic schema migration, rebuild, model reinstall, embedding-cache
reset, configuration reset, or ticket-session reset is required solely for this
patch. Existing immutable generations and pinned exact-source evidence remain
authoritative. A normal refresh remains optional workspace maintenance, not an
upgrade migration.

macOS: `brew update` then `brew upgrade project-brain`. Windows: use the tagged
repository's `scripts/install-project-brain.ps1`; add `-Version 1.0.10` for an
online install or `-ArchivePath` for a previously downloaded ZIP. Finish active
work and stop an old UI process before upgrading, then reopen `brain ui`.
Agent Kit v4/protocol v5 remain compatible; rerun `brain agent-kit m365 --json`
to regenerate the kit if desired, without creating a new M365 Agent.

Enterprise cold-build latency and private-ticket accuracy remain field
measurements, not promises derived from the public regression fixtures.

Project Brain remains read-only with respect to target repositories. It does
not upload source, use hosted inference, edit target code, execute target tests,
or act as an autonomous coding agent.

---

# Project Brain v1.0.9 — Safe Automatic Repository Discovery

Project Brain v1 turns the Workspace Intelligence Atlas into a bounded,
generation-pinned investigation runtime. It gives ChatGPT, Claude, M365
Copilot, and other chat AIs locally retrieved, exact-source evidence without
granting them permission to edit or execute your code.

## Highlights

- Explicit UI/CLI refresh once again discovers newly cloned repositories,
  safely appends them to the authoritative `brain.toml`, and includes them in
  the same refresh instead of failing with a manual-edit requirement.
- Repository additions run under the existing workspace writer lease, validate
  direct file identity and bounds, avoid predictable temporary paths, preserve
  completed editor saves, and re-parse the config before indexing. Background
  Auto Refresh still requires an explicit manual refresh before widening scope.
- Semantic refresh keeps the verified local embedding runtime resident for the
  complete bounded build instead of repeatedly reloading the same model.
- Embedding-cache capacity checks, commits, and LRU pruning are amortized across
  batches while successful content-addressed checkpoints remain recoverable.
- Precision reranking now receives generation-validated Atlas entity identity
  and Semantic symbol text, and candidate fusion features are applied exactly
  once.
- Refresh progress survives a browser page reload and reports measured
  cards-per-second with an estimated remaining time.
- Native macOS, Linux, and Windows 11 x64 standalone releases.
- Runtime anchors for symbols, stack frames, endpoints, events, configuration,
  persistence, packages, and file hints.
- Java/Spring MVC, Feign, Kafka, configuration, JPA, test, execution-flow, and
  cross-repository integration intelligence.
- Generation-pinned multi-wave investigations with stable evidence, anchor,
  flow, blocker, checkpoint, and context-lineage identities.
- Protocol v5 full/delta checkpoints, Hypothesis Ledger, Evidence Frontier,
  first-useful checkpoints, and explicit stale-base recovery.
- M365 Agent Kit v4 and a local Investigation Cockpit.
- Core exact/lexical/structural fallback when optional local Semantic or
  Precision capability is absent or fails.

## Downloads

This initial publication contains the locally verified Apple Silicon package:

- `project-brain-v1.0.9-macos-arm64.tar.gz`
- Python wheel and source distribution
- verified macOS/Linux installer script
- `SHA256SUMS.txt`

Verify downloads with `SHA256SUMS.txt`; model weights are published separately
and are never bundled into Core. Hosted GitHub Actions builds are currently
disabled at the account level, so this manual tagged build does not claim a
GitHub-hosted provenance attestation. macOS Intel, Linux, and Windows native
packages remain on the fully verified v1.0.7 release until their native builds
can run; they have not been relabelled as v1.0.9.

On managed Windows machines that allow `git clone` but block direct `.ps1`
downloads, obtain the tagged installer from the repository:

```powershell
git clone --depth 1 --branch v1.0.7 https://github.com/superorange0707/project-brain.git project-brain-installer
cd project-brain-installer
.\scripts\install-project-brain.ps1 -Version 1.0.7
```

The explicit version skips the GitHub API lookup; the installer downloads and
verifies only the matching ZIP and checksum file. If organization policy blocks
PowerShell scripts themselves, use the portable ZIP instead.

## Upgrade safety

The v1 migration is additive and transactional. Existing `brain.toml`, source
snapshots, Atlas/Semantic generations, embedding cache, model packs, and ticket
sessions are preserved. No Atlas/Semantic rebuild, schema migration, model
reinstall, or cache reset is required solely for the v1.0.9 patch. The internal
generation-scoped route cache is safely recomputed on first use.

Exact pinned source remains the final evidence authority. Project Brain adds no
hosted inference, cloud source upload, source editing, target-code execution, or
autonomous implementation behavior.

See the [README](https://github.com/superorange0707/project-brain#install)
for installation instructions and the
[changelog](https://github.com/superorange0707/project-brain/blob/v1.0.9/CHANGELOG.md)
for the complete version-by-version record.

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

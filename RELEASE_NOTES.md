# Project Brain v1.0.14 — Continue Gathering Evidence

This stable release provides macOS Apple Silicon/Intel, Linux arm64/amd64,
Windows x64 and Python distributions from the unchanged v1.0.14 tag. Windows
and Linux packages were added after the Mac-first publication; the already
published Mac executables are unchanged. All platforms now use v1.0.14.

This stable patch removes the lifetime four-wave investigation rejection.
The automatic allowance still pauses work, but the user can explicitly approve
one more bounded wave on the same ticket. Existing generations, evidence IDs,
hypotheses and context lineage remain intact.

## Continue an existing ticket

In **Continue with AI**, paste a new focused request, choose **Classify reply**,
then **Continue gathering evidence**. Each confirmation covers one wave, not an
automatic loop. CLI users can pass `--continue-investigation` to `brain continue`
or `brain ctx`. Omit `wave`, or continue sequentially with 5, 6 and later.
No new ticket, configuration edit, index refresh or reset is required.

Checkpoint retries retain their already promised context ID even when cache
warmth or query budgets change which additional evidence is retrieved. Exact
pinned proofs and artifact hashes remain mandatory; completed contexts cannot
be rebound. Pauses guide the user back to investigation rather than a refresh.

This release also includes v1.0.13's durable Semantic-vector reuse improvements:
unchanged compatible shards are reused and changed repositories recover exact
inputs from registered parent shards before re-embedding. No model pack,
embedding cache, Semantic/Atlas generation or ticket state is deleted.

## Upgrade

Finish active work and stop the old UI process, then run:

```sh
brew update
brew upgrade project-brain
brain --version
brain agent-kit m365 --json
```

The version must be `brain 1.0.14`. Replace your existing M365 Agent's generated
INSTRUCTIONS.md and PROJECT_KNOWLEDGE.md with the new kit. Keep the same ticket
and conversation; no new Agent or mandatory `brain refresh` is required.

Both tagged Mac archives passed native CLI/installer checks. Python 3.11–3.14,
package builds, five-platform deterministic parity and independent full CI passed
on commit `9a2a2b7b4696c450567832cd56938851608596ef`. The all-platform release run
hit an optional Node UI smoke-test timeout on Windows 3.12. The independent
full CI on the identical tag passed all Windows Python 3.11–3.14 jobs, including
that test. Publication combines those successful tests with the tagged native
artifact, installer and parity jobs; it does not relabel the failed job as passed.
Unchanged model-pack contracts were verified against successful v1.0.12
qualification. All published assets carry checksums and attestations and
are downloaded again for integrity and anonymous-availability checks.

Target repositories remain read-only. No target source editing/test execution,
hosted inference or automatic source upload is introduced. Model weights are
not bundled and the existing opt-in PyPI publication policy is unchanged.

## Known legacy-request compatibility issues

Agent Kit v4 uses request protocol v5; these are different version numbers.
Although plain JSON requests for protocols v1–v4 remain parseable, existing
v4 ticket context lineage is not safely migrated to v5. Sending v4 into an
existing v5 ticket can also leave later v5 requests rejected by identity
validation. This is a compatibility defect, not a requirement to delete evidence
or rebuild indexes. Keep an existing ticket on its current protocol while a
managed compatibility/recovery fix is prepared; do not manually rewrite session
state or change JSON version numbers to bypass the error.

JSON wrapped in a Markdown code fence may also be classified as ordinary chat.
Pasting only the JSON object avoids this separate formatting issue. These issues
were reproduced during post-publication compatibility diagnosis and are not
fixed by the v1.0.14 artifact set.

---

# Project Brain v1.0.13 — Preserve Semantic Work Across Refreshes

This stable hotfix prevents avoidable re-embedding during incremental refresh
when the disposable embedding cache has evicted computations that still exist
in a compatible, published Semantic shard.

## What changed

- Unchanged compatible repositories retain their validated shards. Changed
  repositories first use the embedding cache, then recover identical bounded
  model inputs' vectors from the registered parent shard. Only new, changed or
  unverifiable inputs need model work; a changed file's untouched methods can
  reuse their vectors even when their source chunk IDs change.
- Reuse validates pack/schema identities, immutable source and Atlas card
  identities, sealed shard membership, vector dimensions and artifact hashes.
  A missing or stale compatibility projection cannot override the registered
  parent. A damaged shard does not discard other repositories' valid work.
- Recovery is bounded and fails explicitly into the existing managed build.
  Failed publication leaves the old registered generation authoritative;
  ticket pins and reachability-based retention remain unchanged.
- UI/CLI progress distinguishes whole-shard/cache reuse, recovered old vectors
  and new embeddings, with safe reuse/rebuild reason codes. Embedding ETA does
  not treat unchecked repositories as inevitable model work. Git freshness
  probe failures are reported separately from published index readiness and
  retried with backoff.

## Upgrade safely

**Let any running refresh finish before upgrading.** Stop the old UI process
after its job completes. On macOS, run `brew update` then
`brew upgrade project-brain`; `brain --version` must report `brain 1.0.13`.
Windows users can install the official ZIP using the tagged repository's
installer with `-Version 1.0.13` or `-ArchivePath` and its published checksums.

No Atlas/Semantic schema migration, cache deletion, model reinstall,
configuration reset or ticket reset is required. Compatible completed v1.0.7
and v1.0.12 state remains reusable. A refresh is not required solely because
the executable upgraded; the next normal refresh uses the corrected reuse path.
Agent Kit v4 / Investigation Protocol v5 are unchanged.

Public regressions use real native USearch shards with counted deterministic
embedding inputs, including cache eviction, corruption, rollback, pinned
generations and 10/50/100-repository fixtures. They verify avoided model work,
not a five-minute cold-build promise for private enterprise workspaces.
Field latency and private-ticket accuracy remain local measurements.

The normal five-platform standalone archives, wheel, sdist, installers and
`SHA256SUMS.txt` are produced from the tagged source with build provenance.
Model weights are not bundled. Target repositories remain read-only: no source
editing, target test execution, hosted inference or automatic source upload.

---

# Project Brain v1.0.12 — More Reliable Evidence and Investigation Flows

This stable patch improves query-time investigation without changing Atlas,
Semantic or ticket-state schemas. It builds on v1.0.11's large-workspace recovery,
ticket-first UI and verified Windows/macOS installation paths.

## Highlights

- Explicit file requests are read from their pinned source before optional
  discovery or model work can consume the query deadline. Duplicate file
  operations are read once and still obey the compiled operation budget.
- Execution-flow discovery and canonical edge/entity validation are batched by
  depth. Per-seed branch limits and deterministic order are retained, allowing
  later investigation entry points to be explored within the SQL budget.
- A canonical unresolved external call, such as logging, remains a candidate
  leaf instead of discarding the verified internal call chain. Unresolved
  dispatch is never promoted to verified evidence or traversed; corrupted graph
  identities still fail closed.
- Progressive widening no longer repeats already evaluated, explicitly scoped
  history, path or symbol operations.
- Homebrew release verification uses the official installed tap and its fully
  qualified formula name, preserving Homebrew's trust checks.

## Downloads and upgrade

The release includes macOS arm64/amd64 and Linux arm64/amd64 standalone archives,
the native Windows amd64 ZIP, wheel, sdist, both installers and `SHA256SUMS.txt`.
Every asset has a checksum and signed GitHub build-provenance attestation.
Model weights are never bundled into Core.

On macOS, run `brew update` then `brew upgrade project-brain`; `brain --version`
must report `brain 1.0.12`. On Windows, use the tagged repository installer with
`-Version 1.0.12`, or `-ArchivePath` with the downloaded official ZIP and matching
`SHA256SUMS.txt`. Offline installation does not require GitHub authentication,
administrator access or an Execution Policy change; company security policy
still applies. Finish active work and stop the old UI process before upgrading.

No Atlas/Semantic migration, index rebuild, model reinstall, cache deletion,
configuration reset or ticket reset is required solely for this patch. Existing
generations remain pinned, and Agent Kit v4 / Investigation Protocol v5 remain
compatible. `brain refresh` is normal workspace maintenance, not a migration
requirement for these query-time fixes.

Project Brain remains read-only toward target repositories: no target editing,
target test execution, hosted inference or automatic source upload. Public
regressions exercise generation isolation, graph corruption, bounded traversal
and Python 3.11–3.14 compatibility; enterprise latency and private-ticket accuracy
remain field measurements, not blanket performance guarantees.

---

# Project Brain v1.0.11 — Large-Workspace Reliability and Recovery

Project Brain v1 turns the Workspace Intelligence Atlas into a bounded,
generation-pinned investigation runtime. v1.0.11 focuses on reliable daily use
in large enterprise workspaces and makes operational failures actionable from
the local UI.

## Highlights

- Fixed a Windows first-use lock race discovered by native release validation.
  Locks no longer write initialization bytes into another handle's locked range;
  reader concurrency and writer/model exclusion keep the same lock identities.
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

- `project-brain-v1.0.11-macos-arm64.tar.gz`
- `project-brain-v1.0.11-macos-amd64.tar.gz`
- `project-brain-v1.0.11-linux-arm64.tar.gz`
- `project-brain-v1.0.11-linux-amd64.tar.gz`
- `project-brain-v1.0.11-windows-amd64.zip`
- `project_brain_context-1.0.11-py3-none-any.whl`
- `project_brain_context-1.0.11.tar.gz`
- `install-project-brain.sh`
- `install-project-brain.ps1`
- `SHA256SUMS.txt`

Every asset is checksum-verified and receives GitHub build-provenance
attestation. Model weights remain separate and are never bundled into Core.

## Upgrade safety

The earlier v1.0.10 tag was not published as a GitHub Release after its native
lock regression failed. It remains unchanged; v1.0.11 carries the corrected
implementation and the complete workspace refinement release.

No Atlas/Semantic schema migration, rebuild, model reinstall, embedding-cache
reset, configuration reset, or ticket-session reset is required solely for this
patch. Existing immutable generations and pinned exact-source evidence remain
authoritative. A normal refresh remains optional workspace maintenance, not an
upgrade migration.

macOS: `brew update` then `brew upgrade project-brain`. Windows: use the tagged
repository's `scripts/install-project-brain.ps1`; add `-Version 1.0.11` for an
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

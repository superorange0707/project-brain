# Changelog

All notable changes are documented here. This project follows Semantic Versioning.

## [1.0.10] - 2026-09-04

### Fixed

- Large v1 workspaces no longer fail managed writes merely because capacity
  accounting walks hundreds of thousands of files. Healthy immutable source
  snapshots are accounted from their existing bounded v3 seals; damaged or
  unsealed trees still fall back to the strict fail-closed physical scan.
- The Brain UI now turns capacity, quota, and free-disk refusals into an
  actionable **Storage & recovery** card with a preview and a guarded one-click
  cleanup. It uses the existing reachability-aware GC, never removes current or
  ticket-pinned evidence, removes nothing when reachability is incomplete, and
  exposes no local paths to the browser.
- Closing a browser tab no longer makes a running UI look lost. `brain ui`
  reopens the registered instance, while `brain ui status` and `brain ui stop`
  provide explicit lifecycle control. Interrupted refresh progress is restored
  without falsely claiming completion.
- Auto Refresh remote freshness probes now share one bounded 45-second
  workspace deadline instead of multiplying a network timeout by repository.
- Repository-discovery config appends roll back verified partial writes, and a
  UI refresh reloads the authoritative `brain.toml` before indexing newly
  discovered repositories.
- Generated M365 handoffs are organized under one directory per ticket while
  existing flat handoff references remain readable.
- Stable release publication now verifies every asset through the anonymous
  public download URL before allowing the release to remain published.

### Upgrade safety

- No Atlas or Semantic schema migration, rebuild, model reinstall, embedding
  cache reset, or ticket-session reset is required solely for this patch.
- Existing immutable generations and pinned ticket evidence remain authoritative
  and compatible. The new handoff layout applies to newly published handoffs.

## [1.0.9] - 2026-09-04

### Fixed

- Restored explicit refresh-time repository discovery: newly cloned Git
  repositories are safely appended to the authoritative `brain.toml` and
  included in the same refresh instead of failing with a manual-edit error.
- Kept background Auto Refresh from silently widening repository scope; it
  reports Action Required until the user starts an explicit refresh.
- Renamed the cockpit option to **Find and add new repositories** so the
  configuration change is clear before refresh starts.

### Safety

- Repository additions share the existing cross-process workspace writer
  lease, enforce repository/config count and byte bounds, validate the direct
  config-file identity before append, avoid predictable temporary paths,
  preserve completed editor saves, and re-parse the result before indexing.
- Preserved target-repository read-only behavior, Atlas/Semantic generation
  identity, model packs, embedding caches, and ticket sessions. No index rebuild
  is required solely for this patch unless a newly added repository must be
  indexed.

## [1.0.8] - 2026-09-03

### Improved

- Kept the verified embedding runtime resident for one bounded Semantic refresh
  instead of reloading it after every 64 requests, while preserving explicit
  pack limits, disconnect recovery, final integrity validation, and guaranteed
  shutdown.
- Reduced bulk embedding-cache work from per-batch capacity scans and commits
  to one tracked capacity measurement, bounded commit checkpoints, and
  amortized LRU eviction headroom.
- Passed generation-validated Atlas entity identity and Semantic symbol text to
  the Precision reranker, and removed a duplicate candidate-fusion pass that
  could apply lexical features twice.
- Restored an active refresh display after a UI page reload and added measured
  cards-per-second and estimated-remaining-time feedback.

### Compatibility

- Preserved Atlas and Semantic generation identity, card/embedding schemas,
  shard formats, exact-source authority, model packs, embedding cache,
  workspace configuration, and ticket sessions. Existing indexes do not need
  to be rebuilt solely for this upgrade; only the small generation-scoped route
  cache is refreshed on first use for its added candidate-text contract.

## [1.0.7] - 2026-09-03

### Fixed

- Kept macOS stable-release qualification strict on installed model-pack
  conformance, Semantic generation alignment, native Semantic recall, bounded
  reranker invocation, and explicit fallback, while no longer treating a slow
  hosted runner's normal online-timeout fallback as a correctness failure.
- Preserved production model timeouts, fallback behavior, Atlas/Semantic
  identities, model packs, caches, workspace state, and ticket sessions
  unchanged.

## [1.0.6] - 2026-09-03

### Fixed

- Gave the native Windows cross-process shared-lock regression a bounded
  30-second startup window and retained child-process diagnostics when startup
  fails on a heavily loaded runner.
- Preserved production locking, runtime behavior, schemas, Atlas/Semantic
  identity, model packs, workspace state, caches, and ticket sessions unchanged.

## [1.0.5] - 2026-09-03

### Fixed

- Made the native Windows UI release smoke tolerate the brief empty-log window
  between redirected standard-output creation and the flushed startup token.
- Preserved runtime behavior, UI protocol, Atlas/Semantic identity, model packs,
  workspace state, caches, and ticket sessions unchanged.

## [1.0.4] - 2026-09-03

### Fixed

- Configured native Windows CLI standard streams as UTF-8 before command
  execution so Unicode workspace paths remain printable under redirected
  legacy code pages.
- Preserved CLI schemas, Atlas identity, retrieval, generation, cache,
  model-pack, workspace, and ticket-session behavior unchanged.

## [1.0.3] - 2026-09-03

### Fixed

- Kept the native Windows release fixture's Git working directory below the
  platform boundary while exercising a real greater-than-260-character source
  path inside the repository. This preserves the intended long-path release
  gate without mistaking a test-harness working-directory limit for a product
  failure.
- Avoided repeatedly scanning comment-masked trailing whitespace during Java
  Atlas and Spring-intelligence extraction. Exact offsets, identities, the
  two-second per-file budget, and the one-megabyte input bound are unchanged.
- Preserved v1.0.2 runtime, schema, generation, cache, model-pack, workspace,
  and ticket-session behavior unchanged.

## [1.0.2] - 2026-09-03

### Fixed

- Rebuilt the Darwin arm64 Precision runtime with a macOS 15 deployment target
  while preserving the immutable model, tokenizer, reference vectors, ranking
  gates, and public pack provenance.
- Gave legacy production packs a bounded verification-only timeout without
  changing normal online model request limits.
- Aligned managed reranker startup with the pack builder's verified
  `--reranking --pooling rank` contract and added bounded verification-only
  retries with source-free case-index diagnostics.

### Verified

- Passed native macOS and GitHub-hosted Windows Semantic/Precision installation,
  conformance, Atlas alignment/freshness, and Precision retrieval gates. The
  Windows standalone remains installable without Python or WSL.
- Preserved all Atlas/Semantic schemas, generation and cache identities, model
  packs, workspace configuration, ticket sessions, and exact-source authority.

## [1.0.1] - 2026-09-02

### Fixed

- Preserved exact compatibility with the immutable pre-v1 Darwin Semantic and
  Precision pack capability arguments while continuing to reject conflicting
  model runtime overrides.
- Bounded native embedding batch/single floating-point drift independently of
  model hashes, dimensions, official-reference cosine, and ranking gates.
- Initialized the native Windows release fixture with Git long-path support so
  the packaged executable is exercised through Unicode, spaced, long paths.

## [1.0.0] - 2026-09-02

### Added

- Native Windows 11 x64 support with Python 3.11–3.14 CI, native process-tree
  cleanup, PowerShell clipboard integration, `.exe` backend discovery, and a
  four-executable standalone ZIP release gate.
- Source-pinned Windows Semantic and Precision model-pack build/conformance
  workflows whose publication must be explicitly enabled after verification.
- Generation-scoped Runtime Anchor Resolver and deterministic Java/Spring MVC,
  Feign, Kafka, component/cache/configuration, JPA, and test intelligence,
  published atomically as additive Atlas components.
- Bounded ordered ExecutionFlow, cross-repository IntegrationFlow, Program Slice
  Lite, and implementation/test/impact/contract/config-data surfaces with
  candidate-versus-exact-source authority labels.
- Ticket-session Hypothesis Ledger, Evidence Frontier, stable evidence/anchor/
  flow/blocker/context IDs, first-useful checkpoint, and a three-wave default /
  four-wave hard-limit controller pinned to one immutable serving generation.
- Investigation Protocol v5, M365 Agent Kit v4 and protocol guide, progressive
  metadata events, Investigation Cockpit state, and integrated Brain/M365 local
  evaluation metrics and ablations.

### Changed

- Windows workspace readers now use native byte-range leases so different
  tickets can retrieve concurrently while publication remains exclusive;
  repository-relative evidence paths are separator-independent.
- Runtime-anchor and flow reuse now validates generation, component schema,
  compatibility identity, membership, and cache payloads before use; corrupt or
  missing pinned state degrades explicitly without newer-generation substitution.
- All new request and source-derived inputs are bounded by deterministic item,
  UTF-8 byte, candidate, traversal, statement, context, and physical-operation
  limits. Python 3.11–3.14 remains the mandatory compatibility matrix.
- Refresh detects unconfigured repositories but no longer mutates user-owned
  `brain.toml`; users explicitly add repository blocks, eliminating concurrent
  editor overwrite and partial-append failure modes.

### Compatibility

- The catalog migration is additive and transactional. Existing v0.9 snapshots,
  Atlas/Semantic generations, model packs, embedding cache, and ticket sessions
  remain readable and reachable; protocols v1–v4 remain accepted.

## [0.9.2] - 2026-08-28

### Fixed

- Replace one Python-3.12-only nested f-string in Investigation Memory evidence
  identity construction with Python 3.11-compatible syntax while preserving the
  exact NUL-delimited hash input and resulting Atlas evidence IDs.
- Gate stable release publication on the complete declared Python 3.11–3.14
  test and compilation matrix.

## [0.9.1] - 2026-08-28

### Fixed

- Register only fully validated Semantic artifacts in the current Atlas
  generation, including source snapshots, input/card schema, verified embedding
  pack, vector dimension, shard manifest, and component content identity.
- Reuse a compatible Semantic generation without re-embedding, or rebuild an
  incompatible v0.8/v0.9 projection through the managed pipeline while reusing
  source-card embedding cache entries and retaining old pinned generations.
- Bound hierarchical Atlas cards with the existing safe Semantic input contract,
  and report Precision ready only after atomic Atlas component publication.

## [0.9.0] - 2026-08-28

### Added

- One catalog-authoritative Workspace Intelligence Atlas generation that binds
  repository snapshots to module/file/entity/region hierarchy, provenance-backed
  typed relationships, change intelligence, Repo/Module/Entity semantic cards,
  and generation-scoped routing caches.
- Blob-level incremental Atlas refresh with unchanged entity/edge/region reuse,
  card/embedding content reuse, deterministic added/modified/deleted/renamed
  deltas, atomic publication, ticket pinning, and reachability GC.
- Ticket-prefetched hierarchical Repo → Module → Entity routing, bounded graph
  expansion, generation-validated similar-investigation priors, and a deterministic
  Next-Best-Evidence value/cost decision.
- Session-authoritative Investigation Memory and Coverage Map, stable evidence and
  candidate IDs, protocol v4 `INVESTIGATION_REQUEST`, full/delta context lineage,
  stale-base checkpoint recovery, and M365 Agent Kit v3.
- Atlas-aware local evaluation metrics for Repo Recall@4/6/8/16, module/entity/
  graph/evidence recall, first-result timings, physical operations, cache/prefetch,
  late candidates, delta reduction, next-evidence usefulness, and rounds to final.

### Changed

- Retrieval now routes through the Atlas before targeted lexical/path/semantic
  fallback while preserving global exact lookup, progressive widening, optional
  bounded reranking, and exact pinned-source hydration as the evidence boundary.
- The local model lane now uses a filesystem lock as well as a thread lock, so
  embedding and reranking are serialized across Brain CLI/UI processes.
- v1/v2/v3 request parsing and old sessions remain compatible; legacy state is
  migrated lazily without changing the v0.8 release candidate.

## [0.8.0] - 2026-08-27

### Added

- Objective-first CONTEXT_REQUEST v3 with bounded hints/coverage and a minimal
  v3 repair response, while preserving v1/v2.
- Deterministic repository routing/widening, operation fusion, request-local
  memoization, physical/logical/candidate budgets, trace schema v2, and safe
  retrieval progress/profiling.
- Shared bounded repository workers, bounded parallel Semantic shard search,
  one local-model lane, shared workspace retrieval leases, per-ticket locking,
  two-ticket UI background jobs, and an investigation board.
- M365 Agent Kit version metadata and instructions for v3, one focused
  follow-up, no-progress handling, and FINAL_SOLUTION convergence.
- Opt-in idle auto-refresh with shared UI/CLI freshness detection, debounced
  coalescing behind active retrievals, bounded cooldown/backoff, safe local
  status, and the existing authoritative refresh pipeline.

### Changed

- The UI starts new tickets from the current Brain snapshot by default; refresh
  before start is an explicit workspace-exclusive option.
- Candidate fusion/pruning now happens before optional Precision reranking and
  exact source hydration.

## [0.6.6] - 2026-08-22

### Fixed

- Make health, embedding, and reranking calls to a verified, pack-owned
  `llama.cpp` process use a dedicated direct transport to its fixed
  `127.0.0.1` endpoint. Corporate proxy rules can no longer divert numeric
  loopback requests while ordinary one-time GitHub model downloads retain
  proxy-aware system-trust handling and all existing checksum gates.
- Report the safe pack-runtime loopback boundary in `brain doctor`, including
  only whether proxy configuration is present. Startup diagnostics now separate
  process launch, early exit, unavailable health endpoint, and transport
  failures without exposing proxy or certificate material.

## [0.6.5] - 2026-08-22

### Fixed

- Use the native operating-system trust store for one-time model-pack downloads:
  the macOS Keychain through `truststore` and platform OpenSSL trust on Linux.
  Enterprise TLS-inspection roots already trusted by the OS now work in the
  standalone/Homebrew binary without weakening certificate or hostname checks.
- Honor an administrator-supplied `models.ca_bundle` or standard
  `SSL_CERT_FILE` as an additive local CA source, without logging certificate
  contents, paths, proxy credentials, or environment values.
- Add safe model-download trust diagnostics to `brain doctor`; redirect host,
  descriptor, release-part, and assembled-artifact SHA-256 gates remain
  mandatory.

## [0.6.3] - 2026-08-21

### Fixed

- Restore Python 3.11 compatibility for content-addressed semantic shard names.
- Run CI and release-suite tests with the Semantic vector extra installed.
- Move Homebrew tap-token evaluation into the post-release job so an absent
  secret safely skips the tap update while a successful release remains valid.

## [0.6.2] - 2026-08-21

### Added

- Add a separately versioned official Semantic-pack release pipeline for the
  unchanged Qwen3-Embedding-4B Q6_K GGUF, with pinned upstream inputs, local
  llama.cpp runtime, Apache/runtime notices, provenance, public/synthetic
  conformance, multipart GitHub Release assets, and a pinned descriptor format.
- Add `brain model install semantic` alias support for a Core-pinned Project
  Brain release descriptor; descriptor, part, assembled-model, manifest, and
  normal pack verification all remain local and checksum-gated.
- Add document/query embedding contracts, Qwen input suffix/runtime arguments,
  and semantic index refresh after a selected Semantic edition refresh.
- Add a final-release-only Homebrew update job guarded by a scoped tap token.

### Fixed

- Do not build or query a Zoekt shard from a working tree that differs from its
  declared pinned Git SHA; fall back to the blob-backed Core index instead.

## [0.6.1] - 2026-08-21

### Added

- Add stable retrieval contracts, deterministic narrow-first query planning and
  rank fusion, streaming bounded `rg` fallback, and a trace JSON artifact for
  every context request.
- Add catalog schema/version checks, atomic generation pointers, session
  generation pinning, generation-aware status/freshness/storage/GC commands,
  and changed-range ticket-history refreshes.
- Add Core/Semantic/Precision capability profiles, local-only verified model
  pack management, deterministic semantic-card chunking, vector-shard snapshot
  filtering, and conformance/bakeoff report commands.
- Add local hand-labelled golden replay evaluation with calibration/validation/
  holdout splits, suite hashing, ranking metrics, and no request text telemetry.
- Add an optional Zoekt local-shard adapter with snapshot checks and deterministic
  SQLite/ripgrep fallback, plus LRU-bounded vector/query caches and session-pinned
  snapshot storage GC.
- Build source-pinned `zoekt` and `zoekt-index` commands into the standalone
  release archive, preserving upstream license and revision provenance.
- Require production model conformance suites to cover reference-vector cosine
  and similarity-order parity, batch/single reranker parity, and a declared
  long-input exercise.
- Reject incompatible verified embedding-pack/card schemas with an explicit
  semantic-index rebuild path, while retaining Core retrieval as the fallback.
- Always shut down Brain-owned embedding and reranker runtimes after semantic
  indexing, semantic search, and precision reranking, including failed calls.
- Add `brain benchmark --machine`, public-synthetic model batch/candidate-pool
  measurements, and `brain model autotune` profiles that apply only to the exact
  verified local pack.
- Add a complete offline pack guide covering official-Qwen provenance,
  reproducible reranker conversion, conformance, and target-machine validation.
- Require production pack provenance for model/tokenizer artifacts, format,
  quantization, pooling, normalization, instructions, converter, and semantic
  card/chunk versions.

### Fixed

- Decode the current Zoekt JSONL base64 line representation before literal
  verification, so real local-shard matches are not dropped.
- Keep the configurable free-disk safety guard while using a portable 5 GiB
  default for new and legacy workspaces.

### Security

- Restrict model runtime endpoints to loopback; permit one-time pack staging
  only from GitHub Release or configured HTTPS hosts with a caller-provided
  SHA-256; validate archive paths and checksum-verify all declared artifacts.
- Create Brain-owned state, session, and generated-handoff directories with
  owner-only permissions on POSIX hosts.
- Require production managed llama.cpp packs to contain checksummed runtime and
  model artifacts; recheck them before local offline launch and always shut down
  the pack-owned process. Reject `runtime_url` delegation for production packs,
  so an unverified external daemon cannot substitute its binary or weights.

### Core retrieval additions

- Build an atomic SQLite FTS5 trigram generation for exact content and path
  retrieval, with file membership keyed to Git blob SHA and content-hash fallback
  for non-Git sources.
- Record local index and retrieval timings and summarize p50/p95 latency with
  `brain benchmark`.
- Preserve non-hydrated ranked candidates as compact candidate IDs in context.

### Core retrieval changes

- Resolve common symbol, caller, implementation, and test candidates from one
  indexed literal search plus deterministic line verification.
- Merge overlapping hits, apply file/repository diversity, and rank candidates
  before reading source. New workspaces hydrate at most 18 source regions by
  default while retaining `rg` and the built-in scanner as fallbacks.

## [0.6.0] - 2026-08-17

### Added

- Build an incremental, local experience index from ticket identifiers in Git
  commit subjects, grouping the same ticket across repositories and recording
  changed production, test, and configuration paths.
- Rank similar historical tickets from the new ticket description, with
  bounded, credential-redacted patch evidence available as an explicit opt-in.
- Add deterministic `brain evaluate` retrieval recall reports by comparing old
  Brain evidence with the files later changed by matching ticket commits.
- Add verified filename/path retrieval through `paths:` requests and the
  `brain paths` command.
- Add bounded evidence-backed contract expansion for relevant Kafka, HTTP,
  Feign, and Maven relationships.
- Cache the relationship graph by analyzed source snapshot so repeated ticket
  rounds do not rescan every repository.
- Add cumulative implementation-readiness coverage to every context response.
- Add `brain evidence` for explicit local document, note, log, and runtime
  artifacts; text is reused in later contexts while binary files are archived
  without claiming they were parsed.
- Show indexed committed-ticket memory in the local cockpit and project status.

### Security

- Exclude sensitive file types and names from automatic historical patch
  excerpts and redact common private-key, access-key, token, password, and
  secret patterns before a patch reaches a handoff.

## [0.5.4] - 2026-08-17

### Fixed

- Treat an AI-requested file that does not exist as an `Unresolved` operation,
  allowing the rest of the batched searches and the numbered context handoff to
  complete.
- Commit request/context artifacts only after retrieval succeeds, and remove
  orphaned artifacts when a genuinely fatal or unsafe operation aborts a round.
  Retrying an old half-written round now safely reuses its expected number.
- Instruct chat agents to use direct file retrieval only for previously verified
  paths and to search first whenever the exact location is unknown.

## [0.5.3] - 2026-08-17

### Fixed

- Keep Start, AI evidence, implementation review, and selected history outputs
  separate in the local cockpit instead of mirroring the latest handoff into
  every panel.
- Show immutable request/context artifacts as a chronological investigation
  history while hiding the moving `current-handoff.md` transport alias from the
  history list.
- Add a confirmed **Delete history** action that removes only the selected
  ticket's Brain session and generated handoffs, never repositories or branches.

## [0.5.2] - 2026-08-17

### Fixed

- Execute the last `CONTEXT_REQUEST` in a complete or accidentally appended AI
  reply instead of re-reading an earlier request from the same text.
- Prefer a later `FINAL_SOLUTION` over quoted older repository requests.
- Clear the cockpit's processed AI reply so the next response cannot be appended
  to stale input by accident.
- Give every M365 handoff a round-specific filename such as
  `TICKET-context-010.md`, preventing file-attachment caches from showing the
  previous round; `TICKET-current.md` remains available as a stable alias.

## [0.5.1] - 2026-08-17

### Added

- Automatically discover newly cloned Git repositories during `brain refresh`,
  normal ticket startup, and **Discover & sync** in the local cockpit.
- Append only new repository blocks to `brain.toml`, preserving every existing
  description, tag, branch override, comment, and custom setting.
- Add `--no-discover` to `brain refresh` and `brain start` for intentionally
  narrow workspaces.
- Generate four ready-to-paste M365 Agent Builder prompts in
  `SUGGESTED_PROMPTS.md`.

### Changed

- Deliver M365 handoffs through the Finder-visible
  `generated/handoffs/TICKET-current.md` path while retaining internal history
  under `.runs/TICKET/`.
- Stop repository discovery at each Git root so refreshing a large multi-repo
  parent does not walk every source file.

## [0.5.0] - 2026-08-16

### Added

- Generate a ready-to-paste Microsoft 365 Copilot Agent kit with permanent
  Instructions, stable project knowledge, setup guidance, and a UI preview.
- Add `brain continue` and a unified AI inbox that distinguish repository tool
  requests, direct AI/user conversation, and `FINAL_SOLUTION` without another
  model or credential.
- Maintain one stable `.runs/TICKET/current-handoff.md` for M365 delivery instead
  of requiring users to create and rename a file for every investigation turn.
- Track retrieval signatures, unique evidence gained, prior objectives, and
  consecutive no-progress turns in every ticket session and returned context.
- Persist `waiting_for_ai`, `ready_to_implement`, and implementation-review states
  for existing and new investigations.

### Changed

- Make the chat AI the sole user-facing investigator: it asks users directly for
  business, documentation, runtime, and environment facts and invokes Brain only
  for local repository evidence.
- Rename the cockpit's misleading Execution Plan to Retrieval Plan and preserve
  the active ticket across navigation and UI restarts.

### Fixed

- Detect and reject an identical retrieval plan against the same pinned source
  snapshots instead of repeatedly returning the same evidence.

## [0.4.1] - 2026-08-16

### Fixed

- Show each repository's analyzed branch/ref next to its sync status in the
  local cockpit instead of showing only `current`.

## [0.4.0] - 2026-08-16

### Added

- Prefer fresh `origin/develop` or `origin/development` snapshots before a
  repository's release/default branch, with configurable project priorities.
- Support per-repository `branch` configuration and temporary
  `--branch REPO=BRANCH` overrides for ticket feature branches.
- Include the exact branch, commit, sync status, and freshness warnings in AI
  start/context packs and cross-repository relationship maps.

### Fixed

- Enforce a hard limit of one interactive SSH fetch per endpoint: every later
  repository uses `BatchMode` and cannot request another passphrase.
- Explicitly disable macOS `UseKeychain` during Project Brain fetches and retain
  compatible company `core.sshCommand` options when adding safe SSH controls.
- Kill the complete Git/SSH process group when a fetch times out, preventing an
  orphaned SSH process from continuing to request a passphrase.
- Allow non-interactive fetches up to five minutes for slower company networks
  while keeping the single interactive attempt bounded.

## [0.3.2] - 2026-08-16

### Fixed

- Reuse one temporary OpenSSH connection per host during multi-repository sync,
  avoiding a separate private-key passphrase prompt for every repository.
- Stop retrying the same SSH host after authentication fails once in a sync,
  while preserving locally available source snapshots for the remaining repos.

## [0.3.1] - 2026-08-16

### Fixed

- Stop the standalone local UI cleanly when a packaged executable receives an
  interrupt during socket shutdown, without printing a PyInstaller traceback.

## [0.3.0] - 2026-08-16

### Added

- A token-protected, loopback-only `brain ui` investigation cockpit for project
  health, ticket startup, AI request preview, evidence delivery, session history,
  and implementation/test feedback.
- Versioned `CONTEXT_REQUEST` protocol with whole-response YAML extraction, JSON
  input, deterministic dry-run plans, repository validation, and copyable repair
  prompts when a chat model breaks the schema.
- Stable JSON output for ticket startup, context fulfilment, project status,
  request preview, and implementation feedback commands.
- `brain feedback` packages tracked diffs and human-observed test output for an
  AI review without running commands or editing code.
- `brain demo` creates a self-contained four-repository Java/Spring/Kafka/Feign
  investigation that new users can explore immediately.
- Loopback API authentication, strict browser security headers, request-size
  limits, and artifact traversal protection.

## [0.2.1] - 2026-08-16

### Fixed

- Parse the `cols`/`rows` structured JSON emitted by the pinned graph backend,
  and send CLI arguments over its non-deprecated stdin JSON interface.

## [0.2.0] - 2026-08-16

### Added

- Safe multi-repository synchronization: fetch `origin` without pulling,
  checking out, resetting, cleaning, or changing a working branch.
- Immutable snapshots of the latest locally available remote default branch, so
  stale clones can be analyzed while uncommitted work remains untouched.
- One-command initialization that discovers, syncs, indexes, maps, and checks
  every nested Git repository below a project root.
- Evidence-backed Maven, Kafka, Spring REST, and Feign relationships plus derived
  cross-repository runtime workflows.
- Optional `codebase-memory-mcp` v0.10.5 structural graph integration with a
  deterministic lexical fallback.
- Prebuilt macOS and Linux release archives containing `brain` and the pinned
  structural backend; no local Python or Xcode compilation is required.

### Fixed

- Give each repository its own search result budget so an early noisy repository
  cannot hide evidence from later repositories.

## [0.1.2] - 2026-08-15

### Added

- Recursively discover every nested Git repository when `brain init` is run
  without explicit repository paths.

## [0.1.1] - 2026-08-14

### Fixed

- Give the GitHub CLI an explicit repository context when publishing release assets.

## [0.1.0] - 2026-08-14

### Added

- Zero-dependency, installable `brain` CLI.
- Portable multi-repository TOML configuration and `brain init`.
- Exact/regex search with ripgrep and standard-library fallback.
- Symbol, implementation, test, and static call-site discovery.
- Git history, repository freshness, direct source, and working-diff retrieval.
- Spring, Kafka, Feign, persistence, scheduling, route, and Maven fact extraction.
- `CONTEXT_REQUEST` parsing, evidence ranking/deduplication, and Markdown packing.
- Claude clipboard chunking and M365 file delivery.
- Per-ticket sessions and reusable project/ticket knowledge.
- CI, release packaging, security policy, user guide, and contribution guide.

[1.0.10]: https://github.com/superorange0707/project-brain/releases/tag/v1.0.10
[1.0.9]: https://github.com/superorange0707/project-brain/releases/tag/v1.0.9
[1.0.8]: https://github.com/superorange0707/project-brain/releases/tag/v1.0.8
[1.0.7]: https://github.com/superorange0707/project-brain/releases/tag/v1.0.7
[1.0.6]: https://github.com/superorange0707/project-brain/releases/tag/v1.0.6
[1.0.5]: https://github.com/superorange0707/project-brain/releases/tag/v1.0.5
[1.0.4]: https://github.com/superorange0707/project-brain/releases/tag/v1.0.4
[1.0.3]: https://github.com/superorange0707/project-brain/releases/tag/v1.0.3
[1.0.2]: https://github.com/superorange0707/project-brain/releases/tag/v1.0.2
[1.0.1]: https://github.com/superorange0707/project-brain/releases/tag/v1.0.1
[1.0.0]: https://github.com/superorange0707/project-brain/releases/tag/v1.0.0
[0.9.2]: https://github.com/superorange0707/project-brain/releases/tag/v0.9.2
[0.9.1]: https://github.com/superorange0707/project-brain/releases/tag/v0.9.1
[0.9.0]: https://github.com/superorange0707/project-brain/releases/tag/v0.9.0
[0.8.0]: https://github.com/superorange0707/project-brain/releases/tag/v0.8.0
[0.6.0]: https://github.com/superorange0707/project-brain/releases/tag/v0.6.0
[0.5.4]: https://github.com/superorange0707/project-brain/releases/tag/v0.5.4
[0.5.3]: https://github.com/superorange0707/project-brain/releases/tag/v0.5.3
[0.5.2]: https://github.com/superorange0707/project-brain/releases/tag/v0.5.2
[0.5.1]: https://github.com/superorange0707/project-brain/releases/tag/v0.5.1
[0.5.0]: https://github.com/superorange0707/project-brain/releases/tag/v0.5.0
[0.4.1]: https://github.com/superorange0707/project-brain/releases/tag/v0.4.1
[0.4.0]: https://github.com/superorange0707/project-brain/releases/tag/v0.4.0
[0.3.2]: https://github.com/superorange0707/project-brain/releases/tag/v0.3.2
[0.3.1]: https://github.com/superorange0707/project-brain/releases/tag/v0.3.1
[0.3.0]: https://github.com/superorange0707/project-brain/releases/tag/v0.3.0
[0.2.1]: https://github.com/superorange0707/project-brain/releases/tag/v0.2.1
[0.2.0]: https://github.com/superorange0707/project-brain/releases/tag/v0.2.0
[0.1.2]: https://github.com/superorange0707/project-brain/releases/tag/v0.1.2
[0.1.1]: https://github.com/superorange0707/project-brain/releases/tag/v0.1.1
[0.1.0]: https://github.com/superorange0707/project-brain/releases/tag/v0.1.0

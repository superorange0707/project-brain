# Changelog

All notable changes are documented here. This project follows Semantic Versioning.

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

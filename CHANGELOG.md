# Changelog

All notable changes are documented here. This project follows Semantic Versioning.

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

[0.4.0]: https://github.com/superorange0707/project-brain/releases/tag/v0.4.0
[0.3.2]: https://github.com/superorange0707/project-brain/releases/tag/v0.3.2
[0.3.1]: https://github.com/superorange0707/project-brain/releases/tag/v0.3.1
[0.3.0]: https://github.com/superorange0707/project-brain/releases/tag/v0.3.0
[0.2.1]: https://github.com/superorange0707/project-brain/releases/tag/v0.2.1
[0.2.0]: https://github.com/superorange0707/project-brain/releases/tag/v0.2.0
[0.1.2]: https://github.com/superorange0707/project-brain/releases/tag/v0.1.2
[0.1.1]: https://github.com/superorange0707/project-brain/releases/tag/v0.1.1
[0.1.0]: https://github.com/superorange0707/project-brain/releases/tag/v0.1.0

# Changelog

All notable changes are documented here. This project follows Semantic Versioning.

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

[0.2.1]: https://github.com/superorange0707/project-brain/releases/tag/v0.2.1
[0.2.0]: https://github.com/superorange0707/project-brain/releases/tag/v0.2.0
[0.1.2]: https://github.com/superorange0707/project-brain/releases/tag/v0.1.2
[0.1.1]: https://github.com/superorange0707/project-brain/releases/tag/v0.1.1
[0.1.0]: https://github.com/superorange0707/project-brain/releases/tag/v0.1.0

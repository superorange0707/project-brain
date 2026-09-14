# Project Brain v1.0.26 — Reliable Investigations and Source Retrieval

This release improves investigation continuation, source retrieval and the local
web workspace while preserving ticket-pinned source and existing model packs.

## What changed

- Handle complete AI replies containing JSON/YAML requests, code fences and
  surrounding explanation. When Copilot returns plain text, **Create request
  from text** prepares a validated request for review before retrieval.
- Continue beyond earlier investigation round limits with a fresh resource
  budget for each request. Recover saved requests and evidence without resetting
  tickets or changing their source generation.
- Resume running UI jobs after temporary polling failures or page reloads.
  Preserve drafts edited during retrieval, prevent duplicate submission, and
  retry failed automatic refresh with backoff.
- Improve complete method-body delivery, explicit symbol/relationship requests,
  Python import-aware navigation and HTTP/configuration/event source navigation.
  Reuse bounded parsing, anchor lookup and source reads without hiding incomplete
  results or treating ambiguous navigation as verified source.
- Fix narrow-screen layouts, long result/path wrapping, form labels and the
  readiness display for indexed non-Git snapshots.

## Upgrade

Finish active refresh/retrieval work before upgrading. On macOS:

```sh
brain ui stop
brew update
brew upgrade project-brain
brain --version
brain ui
```

The version must be `brain 1.0.26`. Native macOS, Linux and Windows archives and
the existing installers are also available as release assets.

Regenerate the M365 Agent Kit with `brain agent-kit m365 --json` and replace the
existing Agent Builder instructions and project knowledge. Protocol v5 and Agent
Kit v4 remain in use; no new Agent or ticket is required. Existing agents do not
automatically receive local template changes.

Run **Refresh Brain** to build updated relationship projections for new
investigations. Old tickets keep their original generations. Unchanged Semantic
cards can reuse their embeddings; do not delete indexes, model packs or ticket
history as an upgrade step.

## Validation scope

Local acceptance passed 843 tests, with five native Windows cases deferred to
Windows CI. The installed UI passed 48 layout checks across three widths and
both themes; the macOS arm64 Core executable matched source behavior in the
deterministic release fixture. Publication requires fresh model qualification,
the full native test/build matrix, installer checks, checksums and provenance.

These bounded public/synthetic and local Core checks do not promise universal
enterprise search quality or fixed model latency. Target repositories remain
read-only, and unavailable pinned evidence never substitutes newer source.

See [CHANGELOG.md](CHANGELOG.md) for version history.

---

# Project Brain v1.0.25 — Requested Test Evidence

This stable patch improves requested test-source retrieval while preserving existing
workspaces and ticket-pinned generations.

## What changed

- Deliver actual test source for protocol-v5 `test_surface` and explicit test
  evidence requests with symbol anchors, rather than stopping at a definition.
- Search test paths through the existing pinned lexical index before applying
  source candidate limits. Separate test repositories remain discoverable when
  the request has no explicit repository restriction.
- Avoid unused definition queries and optional model startup for qualified-only
  requests. Mixed investigations retain their broader retrieval paths.
- Preserve old-ticket generation pins, scope-specific caches, source integrity
  checks and bounded work. Regression fixtures cover 10/50/100 repositories,
  incomplete-result cache safety and old/new-ticket source isolation.

Test references provide source evidence for investigation; they do not establish
runtime test coverage or infer exact receiver types for ambiguous method names.

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

The version must be `brain 1.0.25`. Windows and Linux users can use the matching
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

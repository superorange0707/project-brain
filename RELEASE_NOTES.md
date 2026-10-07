# Project Brain v1.0.33 — Unified MPS Retrieval and Nested Flows

Ordinary ticket questions can retrieve code and pinned MPS model evidence in the
same context. Model/concept names and decoded property values provide bounded
lexical candidates; exact model/node identities establish structural references.

## What changed

- Code anchors no longer suppress matching model terms from the same objective.
  CamelCase and supported word separators share candidate matching. Ordinary
  discovery retains short uppercase terms such as PE, while qualified code
  anchors retain their existing precision rules.
- Follow exact nested-node subtrees across repositories. Reverse impact retains
  precise caller nodes and marks references to enclosing nodes as ancestor
  context. Shared targets retain each callsite while expansion is deduplicated.
- Page bounded connection lists with the returned source anchor and direction.
  Byte-omitted steps retain a continuation cursor; depth/node frontiers start a
  new focused traversal. Stable connection identities support ticket deduplication.
- Refresh publishes discovery profile v2 for new tickets. Existing v1 model
  components and original ticket generations remain readable. Source and lexical
  membership proofs retain their existing boundaries.
- Avoid an unnecessary Java method regex scan when there is no owning class or
  opening parenthesis. Existing parser time and row bounds remain enforced.
- AI guidance requires exact authoring-node edits, before/after changes, affected
  usages and acceptance assertions. Structural references and supplied change
  blocks still require project language, generator, source and test validation.

## Upgrade

On macOS, finish active operations, then run:

```sh
brain ui stop
brew update
brew trust --formula superorange0707/tap/project-brain
brew upgrade superorange0707/tap/project-brain
brain --version
```

The version must be `brain 1.0.33`. If you use the macOS login service, run
`brain service install` from the initialized workspace to refresh its executable
reference, then `brain service status`. Otherwise restart with `brain ui`.
Windows and Linux users can use the matching native archives and existing
checksum-verifying installers.

Run **Refresh Brain** to publish the richer model discovery metadata for new
tickets. Regenerate the M365 Agent Kit to receive the updated delivery guidance.
Keep existing indexes, model packs and ticket history; no ticket reset or model
reinstall is required. Existing tickets keep their original discovery metadata.

Publication retains the full Python/native test matrix, model qualification,
standalone and installer checks, cross-platform parity, checksums and provenance.
Private IPF business correctness and unsupported persistence remain outside the
generic structural reader's guarantees.

---

# Project Brain v1.0.32 — Mac Background Service and MPS Ticket Delivery

Brain can now run independently of Terminal and the browser. On macOS, an
optional per-workspace login service starts Brain when you sign in and restarts
it after an unexpected exit. This release also brings the MPS model/navigation
and concrete implementation-delivery changes into the same package.

## What changed

- `brain ui` starts a detached local process, waits for authenticated readiness
  and reuses an existing instance. `--foreground` remains available for diagnosis.
- `brain service install` enables a macOS user LaunchAgent with safe
  start/stop/status/uninstall commands, private logs and stable Homebrew links.
  Existing idle-refresh preferences are preserved. No administrator service or
  extra runtime is required; company background and folder-access policies apply.
- Read bounded MPS persistence-v9 models and descriptors from repositories or
  local attachments. Retrieve exact pinned source/node anchors, follow static
  cross-model references and inspect reverse impact. Model names in request
  objectives can start navigation without a known file path.
- Keep optional MPS source projections immutable and generation-scoped. Exact
  project-declared flow mappings require source evidence; structural references
  alone do not establish execution order or private IPF business meaning.
- Ask AI for per-file production diffs/snippets, exact model editor operations,
  acceptance criteria and test assertions. The UI distinguishes a saved plan
  from a supplied change block without claiming that either is validated work.

## Upgrade on macOS

Finish active refresh/retrieval work, then run:

```sh
brain ui stop
brew update
brew trust --formula superorange0707/tap/project-brain
brew upgrade superorange0707/tap/project-brain
brain --version
brain service install
brain service status
brain ui
```

The version must be `brain 1.0.32`. Run the service commands from your initialized
Brain workspace. Keep **Auto Refresh: When idle** enabled. Terminal and browser
can then be closed; sleep pauses processing and logout stops the user service.
`brain service stop` stops safely while idle and preserves login startup;
`brain service uninstall` also removes login startup without deleting workspace
data. Private startup logs are in the configured state's `ui.log`.

Windows and Linux users receive the detached UI and MPS/delivery changes through
their native archives and checksum-verifying installers. The login-service
commands in this release are specific to macOS.

## Existing workspaces and AI agents

Run **Refresh Brain** to build the optional MPS projection for new tickets.
Existing tickets retain their original generations and cannot silently read
newer model source. No deletion of indexes, caches, model packs or ticket history
is required. Semantic model weights and input contracts remain unchanged.

Regenerate the M365 Agent Kit with `brain agent-kit m365 --json` and replace the
existing Agent Builder instructions and project knowledge to receive the new
delivery guidance. Request Protocol v5 remains compatible. Brain provides
evidence and proposals; applying model edits, generation and project-specific
validation remain developer steps.

## Validation

Publication requires the remote Python/native build and test matrix, model-pack
qualification, installer checks, checksums and provenance verification. The Mac
service lifecycle check runs on a macOS runner with a GUI login domain. Local
runtime experiments are not part of this release's acceptance evidence.

---

# Project Brain v1.0.28 — Reliable Idle Refresh

Auto Refresh now checks the published Atlas generation and lexical readiness.
Matching legacy index metadata can no longer hide a missing or outdated
generation while the web UI says a refresh is needed.

The visible web UI updates status every five seconds and immediately when you
return to its tab. Background status updates preserve drafts and pending form
choices, stop while the tab is hidden, and retry temporary connection failures.

## Upgrade

Finish active refresh/retrieval work before upgrading. On macOS:

```sh
brain ui stop
brew update
brew trust --formula superorange0707/tap/project-brain
brew upgrade superorange0707/tap/project-brain
brain --version
brain ui
```

The version must be `brain 1.0.28`. Windows and Linux users can use the matching
native release archive and the existing checksum-verifying installers.

No Atlas/Semantic schema, model-pack, embedding-input or Agent Kit changes.
Published generations, caches and ticket history remain reusable. Upgrading
alone does not require a manual refresh, re-embedding, model reinstall or reset.

Keep **Auto Refresh: When idle** enabled and the `brain ui` service running.
Repository checks use the configured interval (180 seconds by default); the
five-second UI poll only reads status. Existing tickets retain their pinned
generations. Adding newly discovered repositories still requires an explicit
refresh, and source working trees are never merged or checked out by Refresh.

Publication is gated by the native test/build matrix, model-pack qualification,
installer checks, checksums and provenance verification. These checks do not
promise a fixed refresh duration for every workspace.

---

# Project Brain v1.0.27 — Reliable Investigations and Source Retrieval

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
- Keep symbol-reference fallback available from the pinned lexical index when
  the optional Zoekt backend is absent, preserving repository scopes and exact
  symbol word boundaries.
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

The version must be `brain 1.0.27`. Native macOS, Linux and Windows archives and
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

Local acceptance of the investigation/UI changes passed 843 tests, with five
native Windows cases deferred to Windows CI. The optional-backend correction
also passed the 17-test symbol-trace suite, including a new regression test.
The installed UI passed 48 layout checks across three widths and
both themes; the macOS arm64 Core executable matched source behavior in the
deterministic release fixture. Publication requires fresh model qualification,
the full native test/build matrix, installer checks, checksums and provenance.

These bounded public/synthetic and local Core checks do not promise universal
enterprise search quality or fixed model latency. Target repositories remain
read-only, and unavailable pinned evidence never substitutes newer source.

The v1.0.26 candidate was not published after cross-platform validation found
the optional-backend dependency corrected here.

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

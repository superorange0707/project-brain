# How Project Brain works

This page collects the behaviour details behind the [README](../README.md):
how retrieval, pinning, refresh and the local UI behave. Day-to-day usage is in
the [User Guide](USER_GUIDE.md); MPS specifics are in [MPS and IPF evidence](MPS_IPF.md).

## Retrieval and evidence

```text
                      INVESTIGATION_REQUEST v5
ChatGPT / Claude / M365 ─────────────────────────┐
                                                ▼
Repositories ── immutable snapshots ── Workspace Intelligence Atlas
                                                │
Project map + history + ticket memory ──────────┤
                                                ▼
                              bounded retrieval + exact verification
                                                │
                                                ▼
                                  source-backed context checkpoint
```

The retrieval cascade starts with similar investigations and generation-pinned
Repo, Module, and Entity cards; expands typed relationships; uses targeted
lexical, path, structural, and optional semantic retrieval; optionally reranks
candidates; and finally hydrates exact source. Missing optional components cause
an explicit Core fallback, never fabricated evidence or newer-generation
substitution.

The first response is a full checkpoint. Follow-ups normally return bounded
deltas identified by stable `context_id` / `base_context_id` lineage. Duplicate
plans are rejected instead of repeating expensive search. There is no fixed
ticket round limit or extra continuation approval: each submitted request has
its own resource budget. No-progress and coverage signals guide the next
evidence request; they do not lock the ticket or change its pinned generation.
If retrieval saves a context but its AI handoff cannot be written, the UI offers
**Open saved evidence** so you can copy the existing result without repeating
retrieval. The internal `context-NNN.md` remains available in that ticket's history.

## Generation pinning and MPS discovery

Every ticket pins one immutable Atlas/Semantic serving generation. A refresh may
publish a newer generation for later tickets, but it cannot silently change the
source or Semantic component used by an investigation already in progress.
Heuristic flows and slices are navigation candidates until their exact pinned
source locations are verified.

MPS is part of the ordinary context retrieval pipeline: model/concept names and
decoded property values supply bounded lexical candidates, including CamelCase
and phrase matches. A code anchor does not suppress model candidates from the
same objective. One context can contain code and pinned model evidence, without
a separate MPS request format or entry mode. Names remain discovery candidates;
model/node IDs establish reference targets. Existing tickets keep their pinned
component; refresh publishes the richer discovery terms for new tickets.

MPS navigation preserves each callsite and actual target node. An exact nested
anchor follows its subtree; incoming impact follows precise callers and labels
references to enclosing nodes as `ancestor_context`. Shared targets and cycles
retain distinct connections while deduplicating expansion. `via_connection`
records one discovery chain, rather than every possible execution path.
Continue byte-omitted steps with `next_step_anchor`, then use the frontier's
`continuation_anchor`: `#refs:offset` pages connections in the same mode and
pinned ticket; depth/node `refocus` starts another bounded traversal. Connection
pages resume a scope's direct connections; downstream scopes can be rediscovered
in later requests and keep stable connection identities for ticket deduplication.
Structural connections need language/aspect/generator/test evidence to establish business
execution, branch guards, data bindings, retries and transactions.

## Local UI, background service and refresh

`brain ui` starts the local service in the background and returns to the prompt.
You can close both the terminal and browser; **Auto Refresh: When idle** keeps
running while the computer is awake. Run `brain ui` again
to reopen the same local instance, `brain ui status` to inspect it, or
`brain ui stop` to stop it safely while idle. After a reboot or logout, run
`brain ui` once to start it again. Use `brain ui --foreground` for terminal
diagnostics; background startup output is saved in the workspace's `state/ui.log`
(or the configured state directory).

This uses a user LaunchAgent and needs no `sudo`. Keep **Auto Refresh: When idle**
enabled; its existing preference is preserved. Terminal and browser can both be
closed. `brain service stop` (or `brain ui stop`) stops safely while idle and
stays stopped until you start it again or next sign in. `brain service start`
resumes it; `brain service uninstall` removes login startup while preserving
workspace data. `brain service install --port 0` supports another workspace on
an automatically selected port. Company-managed Macs may require approval for
background activity or access to the project folder in System Settings.

The Brain view also includes **Storage & recovery**. It previews or safely
reclaims only unpinned old generations, snapshots, and shards; current state and
every ticket-pinned evidence generation remain protected. Capacity checks measure
actual file sizes without reading source contents. Optional query-cache writes
have a short scan budget and never make valid Semantic retrieval unavailable.
Full write/cleanup inventory streams large directories instead of failing at
the quick-probe item/time ceiling. Quota and free-disk checks still apply;
status remains a bounded estimate, not permission to write from a partial sum.
The latest refresh result, including failures and recovery guidance, survives
browser reloads and UI restarts. A temporary UI health-check failure preserves
the registered instance instead of starting a competing server.

The investigation workspace puts tickets first: each card shows its pinned
generation and wave, and opening it exposes that ticket's history and handoff
folder. Light/dark glass surfaces are bundled locally, with keyboard and
reduced-motion support. **Health & recovery** provides configuration reload,
diagnostics, model verification and safe cleanup. Configuration reload validates
your edit without rewriting the file; it does not retarget an existing ticket.

An unchanged, compatible registered Semantic generation is validated and reused
without rechunking source or calling the embedding model. During an incremental
refresh, unchanged repositories reuse their validated shards. Changed repositories
first reuse the compatible embedding cache, then recover identical model inputs'
vectors from the registered parent shard—even after cache eviction. Recovery
validates the pack/input contract, pinned source, card identities and shard hash;
only new/changed inputs or inputs that cannot be validated need model work.
This also works with compatible v1.0.7 state, without a schema migration, model
reinstall, cache reset or prerequisite full rebuild. Recovery is repo-scoped and
bounded; unavailable/corrupt prior data or an exhausted recovery budget falls back
explicitly to the managed cache/model pipeline. Old ticket generations remain
immutable and protected by existing reachability GC.

Refresh progress separates intact-shard/cache reuse, recovered old vectors and
new embeddings, with the current repository's reuse/rebuild reason. Overall
embedding ETA is shown only after the remaining repository reuse checks; it uses
model time, not cached-card counts or Git time. Git freshness being unverified
does not mean the published index is broken: automatic checks retry with backoff,
while actual missing/incompatible components still require attention. This is not
a promise that a cold enterprise workspace can be embedded in five minutes.

`brain init` recursively discovers Git repositories, fetches allowed remote
refs, creates immutable snapshots, builds local intelligence, and writes Brain
state beside—not inside—the repositories. It never checks out, resets, cleans,
edits, builds, tests, or commits target code.

After initialization, an explicit `brain refresh` or cockpit **Refresh Brain**
safely adds newly cloned repositories to `brain.toml` and includes them in that
same refresh. Use `--no-discover`, or clear **Find and add new repositories**,
when the workspace intentionally excludes another Git repository below its root.

The local cockpit opens on `127.0.0.1` with a random session token. Paste a
ticket, review the selected edition and freshness, then start an investigation.

## Limits of static evidence

Project Brain cannot prove behavior created by reflection, dynamic dependency
injection, generated code, external data, or runtime feature flags. It reports
those limits and asks for runtime evidence rather than pretending static
analysis is certain.

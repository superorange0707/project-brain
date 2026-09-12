# Project Brain v1.0.19 — More Useful Evidence, Less Repeated Work

This stable patch improves source retrieval, investigation handoffs and Atlas
refresh reliability while preserving existing workspaces and pinned tickets.

## What changed

- Broader Semantic repository routing can find evidence beyond the initial
  shortlist. It reuses compatible existing vectors instead of re-embedding
  repository cards during a search.
- Precision ranks bounded, verified source previews rather than navigation
  labels alone. Explicit file requests and definitions remain protected.
- Keep distinct matching branches in large files, reduce repeated search
  processes, and retain already-verified evidence when optional model work
  reaches the query deadline.
- Reduce repeated scans while Atlas builds call relationships. Parsing failures
  identify the repository, file and limit, with a normal refresh retry in the UI.
- Skip unnecessary serial Git remote probes during no-fetch refreshes. Local
  model failures close their HTTP responses and report safe, bounded details.
- Keep both chat targets' handoffs under `generated/handoffs/<TICKET>/`.
  `current.md` is the latest prepared handoff; `brain resume TICKET` prepares a
  new-conversation handover without resetting the ticket or its generation.
- Accept complete Markdown-fenced JSON requests, preserve supported mixed
  legacy/v5 lineage, and keep valid v5 follow-ups incremental. Previewing an
  early checkpoint no longer assumes it was already sent to the chat AI.

## Upgrade and continue working

Finish any active refresh or investigation operation before upgrading. From
the Brain workspace containing `brain.toml`:

```sh
brain ui stop
brew update
brew upgrade project-brain
brain --version
brain agent-kit m365 --json
brain ui
```

The version must be `brain 1.0.19`. Update the existing M365 Agent with the
generated `INSTRUCTIONS.md` and `PROJECT_KNOWLEDGE.md`; optionally update
`SUGGESTED_PROMPTS.md`. Agent Kit remains v4 and the current request protocol
remains v5. No new Agent is required. Use `brain resume TICKET` when a long chat
needs a fresh conversation; do not manually change a ticket's stored protocol
or generation.

This release changes no Atlas/Semantic schema or embedding-input contract.
Existing model packs, embedding cache, index generations and ticket sessions
remain reusable. No reset, model reinstall or refresh is required solely for
the executable upgrade. If a refresh previously failed, retry it through
Refresh Brain or `brain refresh`; do not delete prior indexes or caches.

Windows users can use the versioned native ZIP with the existing online or
offline installer. macOS and Linux retain their matching standalone archives.
The wheel, source distribution, installers and `SHA256SUMS.txt` are published
with the same release.

## Scope

Target source remains read-only, and exact ticket-pinned source is the evidence
authority. No model weights, private source or hosted inference are introduced.
Parsing, source and model-input bounds remain enforced; a failed Atlas build
does not publish an incomplete ready generation.

Public/synthetic checks establish regression safety, not universal search
quality or target-machine latency. Large enterprise workspaces and real ticket
outcomes still require field measurement; this release makes no fixed
minutes-to-refresh or coding-agent-equivalence claim. The existing opt-in PyPI
publication policy is unchanged.

## Earlier versions

See [CHANGELOG.md](CHANGELOG.md) and the
[GitHub releases](https://github.com/superorange0707/project-brain/releases)
for version history and downloads.

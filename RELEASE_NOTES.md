# Project Brain v1.0.34 — One-Command Upgrades, MCP Agents and Joint Java/MPS Evidence

Upgrading no longer takes a checklist, AI agents that support the Model Context
Protocol can call Brain directly, Windows gains sign-in startup, and code
evidence is retrieved beside the MPS model nodes that reference it.

## What changed

- **`brain upgrade`** upgrades the installation in one step: Homebrew installs
  are delegated to Homebrew, standalone installs are downloaded from this
  release page, verified against `SHA256SUMS.txt` and swapped in place with
  automatic rollback. It stops and restarts the local UI for you.
- **No more manual Refresh Brain after upgrades.** Each index generation
  records the schemas it was built with; when a new version changes them, the
  next idle refresh (or `brain upgrade`) republishes the index once. Existing
  tickets keep their pinned generation.
- **The M365 Agent Kit is re-pasted only when it changes.** `brain status` and
  the UI Health panel report when the rendered instructions or knowledge
  changed. The knowledge file is now `PROJECT_KNOWLEDGE.txt`, a format Agent
  Builder accepts.
- **`brain mcp`** lets Claude Desktop, Claude Code, Cursor and JetBrains/VS Code
  agents start tickets, request evidence and read handoffs themselves.
  `brain mcp config --host <host> --write` sets it up. Microsoft 365 Copilot
  keeps the file handoff because it cannot reach a laptop.
- **Windows sign-in startup**: `brain service install` now works on Windows
  through a per-user startup entry, without administrator rights.
- **Joint Java/MPS retrieval**: identifiers found in matched MPS node properties
  are searched as code in the same request; files under `source_gen` and
  `classes_gen` are labelled generated source; start handoffs include an MPS
  model catalog; ticket history includes model changes; golden suites can
  score expected model nodes.
- Faster, cheaper project CI and a cleaner repository layout.

## Upgrade from v1.0.33

v1.0.33 does not have `brain upgrade` yet, so this upgrade uses the previous
steps one last time. On macOS, finish active work, then run:

```sh
brain ui stop
brew update
brew trust --formula superorange0707/tap/project-brain
brew upgrade superorange0707/tap/project-brain
brain --version
brain ui
```

The version must be `brain 1.0.34`. If you use the macOS login service, run
`brain service install` instead of `brain ui`. On Windows, rerun the installer
with `-Version 1.0.34`. From now on, `brain upgrade` is the only step.

After this upgrade the index is republished once: automatically when
**Auto Refresh** is set to *When idle*, otherwise click **Refresh Brain** once
(`brain status` says when it is needed). If `brain status` reports that the
M365 Agent Kit changed, regenerate it
with `brain agent-kit m365` and replace the Instructions and the knowledge file
(now `PROJECT_KNOWLEDGE.txt`) in Agent Builder. Workspaces, model packs and
ticket history are kept; no model reinstall or ticket reset is needed.

---

Earlier releases are described in the [Changelog](CHANGELOG.md) and on the
[Releases page](https://github.com/superorange0707/project-brain/releases).

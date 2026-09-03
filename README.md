<div align="center">

# 🧠 Project Brain

**Give the chat AI your company already allows a safe, local codebase tool loop.**

Project Brain turns tickets and follow-up questions into verified source context
across multiple repositories. Retrieval runs locally, source repositories stay
read-only, and exact pinned source remains the final evidence authority.

[![CI](https://github.com/superorange0707/project-brain/actions/workflows/ci.yml/badge.svg)](https://github.com/superorange0707/project-brain/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/superorange0707/project-brain?display_name=tag)](https://github.com/superorange0707/project-brain/releases/latest)
[![Python 3.11–3.14](https://img.shields.io/badge/Python-3.11%E2%80%933.14-3776AB?logo=python&logoColor=white)](https://github.com/superorange0707/project-brain/actions/workflows/ci.yml)
[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

[Download](#install) · [Quick start](#quick-start) · [How it works](#how-it-works) · [User guide](docs/USER_GUIDE.md) · [Security](SECURITY.md)

</div>

---

```text
Ticket + docs ↔ Chat AI ↔ you
                 │
                 ├── business/runtime question → answer directly
                 ├── INVESTIGATION_REQUEST → Brain → verified source evidence
                 └── enough evidence → implementation guidance
```

Project Brain expands an investigation request into source, symbols, callers,
tests, configuration, Git history, and cross-repository relationships. You stay
in control and apply the resulting solution in your normal IDE.

## Install

The current stable release is **v1.0.8**. Every standalone download contains
`brain`, `codebase-memory-mcp`, `zoekt`, and `zoekt-index`. Model weights are
never bundled.

### macOS — Homebrew

```bash
brew install superorange0707/tap/project-brain
```

Already using Project Brain?

```bash
brew update
brew upgrade project-brain
brain --version
```

Homebrew installs a prebuilt package; it does not compile Python or require
Xcode. Existing workspaces, model packs, caches, Atlas generations, and ticket
sessions are preserved during upgrades.

### Windows 11 x64

On managed machines that allow `git clone` but block direct `.ps1` downloads,
get the installer from the tagged repository and run it directly:

```powershell
git clone --depth 1 --branch v1.0.8 https://github.com/superorange0707/project-brain.git project-brain-installer
cd project-brain-installer
.\scripts\install-project-brain.ps1 -Version 1.0.8
```

Specifying `-Version` skips the GitHub API lookup. The installer downloads only
the matching Windows ZIP and `SHA256SUMS.txt`, verifies the archive, installs it
under `%LOCALAPPDATA%\ProjectBrain\bin`, updates the user `PATH`, and checks the
installed version. No execution-policy change is required when the environment
already permits repository scripts.

Run this installer clone from a downloads or temporary directory, not inside the
directory that contains the repositories you will configure as a Brain
workspace. The clone is not needed after installation.

If company policy also blocks PowerShell script execution, use the portable ZIP:

**[Download `project-brain-v1.0.8-windows-amd64.zip`](https://github.com/superorange0707/project-brain/releases/download/v1.0.8/project-brain-v1.0.8-windows-amd64.zip)**

Download the ZIP in a browser and extract its complete contents into
`%LOCALAPPDATA%\ProjectBrain\bin` (or another folder you control) so the four
`.exe` files and their notices remain together. Then run:

```powershell
cd "$env:LOCALAPPDATA\ProjectBrain\bin"
.\brain.exe --version
.\brain.exe --help
```

Add that folder to your user `PATH` if you want to run `brain.exe` from any
directory. Python, WSL, Go, Git Bash, and Homebrew are not required. Git for
Windows is recommended when `brain refresh` needs to fetch remote refs and
create immutable Git snapshots.

Verify the download against
[`SHA256SUMS.txt`](https://github.com/superorange0707/project-brain/releases/download/v1.0.8/SHA256SUMS.txt):

```powershell
Get-FileHash .\project-brain-v1.0.8-windows-amd64.zip -Algorithm SHA256
```

On unrestricted machines, the same installer is also available directly from
the [v1.0.8 Release assets](https://github.com/superorange0707/project-brain/releases/tag/v1.0.8).

### Linux — verified user-level installer

```bash
curl -fsSLO https://github.com/superorange0707/project-brain/releases/latest/download/install-project-brain.sh
sh install-project-brain.sh
brain --version
```

The installer selects Linux amd64 or arm64, verifies the published checksum,
and installs the four executables in `~/.local/bin`.

### Python package

Python 3.11–3.14 is supported:

```bash
uv tool install "project-brain-context @ git+https://github.com/superorange0707/project-brain.git@v1.0.8"
```

Or install the release wheel directly:

```bash
python -m pip install https://github.com/superorange0707/project-brain/releases/download/v1.0.8/project_brain_context-1.0.8-py3-none-any.whl
```

All macOS, Linux, Windows, wheel, sdist, installer, checksum, and provenance
artifacts are available on the
**[v1.0.8 Release page](https://github.com/superorange0707/project-brain/releases/tag/v1.0.8)**.

## Quick start

Run `brain init` from the directory that contains your repositories:

```bash
cd ~/code/payments-platform
brain init --name payments-platform
brain ui
```

`brain init` recursively discovers Git repositories, fetches allowed remote
refs, creates immutable snapshots, builds local intelligence, and writes Brain
state beside—not inside—the repositories. It never checks out, resets, cleans,
edits, builds, tests, or commits target code.

The local cockpit opens on `127.0.0.1` with a random session token. Paste a
ticket, review the selected edition and freshness, then start an investigation.

Prefer the CLI?

```bash
brain start ABC-1234 --ticket-file ticket.md --target claude
brain continue ABC-1234 --clipboard --target claude
```

For Microsoft 365 Copilot, create the permanent Agent setup kit once:

```bash
brain agent-kit m365
```

See the [M365 setup guide](docs/USER_GUIDE.md#6-create-a-persistent-m365-copilot-agent)
for the files to add to Agent Builder and the Protocol v5 multi-wave workflow.

Try the full flow without using your own repositories:

```bash
brain demo
cd project-brain-demo
brain ui
```

## Editions

| Edition | Local capability | Network or hosted inference |
|---|---|---|
| **Core** | Exact, lexical, structural, graph, history, and ticket experience | None |
| **Semantic** | Core plus local semantic-card retrieval | None after the optional pack is installed |
| **Precision** | Semantic plus local candidate reranking | None after the optional packs are installed |

Optional model packs are separately published, checksum-pinned, verified before
registration, and restricted to a Brain-managed loopback runtime. Install them
only when your environment permits:

```bash
brain model install semantic
brain model install precision
brain capabilities
```

Read [Model Pack Operations](docs/MODEL_PACKS.md) for provenance, corporate TLS,
offline installation, verification, benchmarking, and removal. Core does not
need a model pack, vector service, API key, or cloud index.

## What Project Brain investigates

- exact content and path search across many repositories;
- class, interface, method, function, implementation, and caller evidence;
- Java/Spring MVC, Feign, Kafka, configuration, persistence, and test links;
- ordered execution and cross-repository integration flows;
- impact, test, contract, and configuration/data surfaces;
- Git pickaxe/history and ticket-labelled local experience;
- bounded runtime anchors, hypotheses, evidence frontier, and multi-wave state;
- full and delta Protocol v5 checkpoints with stable evidence lineage.

Every ticket pins one immutable Atlas/Semantic serving generation. A refresh may
publish a newer generation for later tickets, but it cannot silently change the
source or Semantic component used by an investigation already in progress.
Heuristic flows and slices are navigation candidates until their exact pinned
source locations are verified.

## How it works

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
plans and no-progress waves are rejected instead of repeating expensive search.

## Privacy and safety

- Target repositories remain read-only.
- Project Brain never executes target code or runs target tests.
- There is no hosted Project Brain inference or source-indexing service.
- Runtime model calls, when enabled, stay on `127.0.0.1`.
- Brain-generated source evidence, local repository paths, `.env`, keys,
  workspace configuration, indexes, model packs, and ticket runs are excluded
  by the default `.gitignore`.
- The local UI binds only to IPv4 loopback and uses a per-process bearer token.
- Generated context can still contain proprietary source or secrets already in
  a repository; review it before sharing it outside your approved environment.

Project Brain cannot prove behavior created by reflection, dynamic dependency
injection, generated code, external data, or runtime feature flags. It reports
those limits and asks for runtime evidence rather than pretending static
analysis is certain.

Read the complete [Security Policy](SECURITY.md) and
[third-party notices](THIRD_PARTY.md) before using Project Brain with private
code.

## Documentation

| Document | Use it for |
|---|---|
| [User Guide](docs/USER_GUIDE.md) | Installation, configuration, workflows, commands, and troubleshooting |
| [Model Pack Operations](docs/MODEL_PACKS.md) | Pack provenance, verification, offline use, and tuning |
| [Release Notes](RELEASE_NOTES.md) | Current release overview and historical release notes |
| [Changelog](CHANGELOG.md) | Version-by-version changes |
| [Release Procedure](docs/RELEASING.md) | Maintainer build, provenance, and publishing gates |
| [Security Policy](SECURITY.md) | Supported versions, threat model, and private reporting |

## Contributing

Issues and focused pull requests are welcome. Start with
[CONTRIBUTING.md](CONTRIBUTING.md). Report vulnerabilities through a private
GitHub Security Advisory, not a public issue.

## License

[MIT](LICENSE) © Project Brain contributors.

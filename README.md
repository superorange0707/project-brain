<div align="center">

# 🧠 Project Brain

**Give any chat AI the codebase exploration loop of a coding agent.**

Turn a ticket into verified, up-to-date multi-repository source context—without
cloud indexing, API keys, or giving an agent permission to edit your code.

[![CI](https://github.com/superorange0707/project-brain/actions/workflows/ci.yml/badge.svg)](https://github.com/superorange0707/project-brain/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Zero runtime dependencies](https://img.shields.io/badge/runtime_dependencies-0-2ea44f)](pyproject.toml)
[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

[Quick start](#quick-start) · [Local cockpit](#local-investigation-cockpit) · [How it works](#how-it-works) · [User guide](docs/USER_GUIDE.md) · [Security](SECURITY.md)

</div>

---

Project Brain fills the missing tool loop between a normal chat window and your
local repositories:

```text
Ticket → Chat AI → CONTEXT_REQUEST → Project Brain → source evidence
                    ↑                                  ↓
                    └──────── investigation ───────────┘
                                      ↓
                               FINAL_SOLUTION
```

The AI decides what it still needs to understand. Project Brain expands one
request into exact search, symbol resolution, callers, implementations, tests,
configuration, Git history, knowledge, and complete source retrieval across all
configured repositories. You apply the final solution in your IDE.

## Why Project Brain?

| | Project Brain |
|---|---|
| 🔒 **Safe source snapshots** | Fetches remote refs, but never pulls, checks out, resets, cleans, or edits a repository |
| 🧭 **Multi-repository workflows** | Connects Maven, Kafka, Spring REST, and Feign evidence across repos |
| 🤖 **Model-independent** | Works with Claude, ChatGPT, M365 Copilot, or any text model—no MCP required |
| 🖥️ **Local investigation cockpit** | Paste tickets and AI replies, preview every operation, inspect evidence, and copy results |
| 📚 **Evidence-complete** | Retrieves production source, tests, config, relationships, and history |
| 🧠 **Structural + exact** | Pinned code graph when bundled; deterministic exact/lexical fallback everywhere |
| 🔍 **Honest uncertainty** | Missing evidence and static-analysis limits are reported, never hidden |

No vector database, hosted indexing service, or model/API credential.

## Install

### Homebrew — recommended on macOS

```bash
brew install superorange0707/tap/project-brain
```

The Homebrew package uses a prebuilt release: it does not compile Python or
require Xcode. It also includes the tested structural backend. The small,
separate [Homebrew tap](https://github.com/superorange0707/homebrew-tap) is
Homebrew's standard index for third-party formulae; the application source stays
in this repository.

### Standalone archive — macOS or Linux

Download the archive for your CPU from the
[latest release](https://github.com/superorange0707/project-brain/releases/latest),
extract it, and place both executables on `PATH`:

```bash
tar -xzf project-brain-v0.4.1-macos-arm64.tar.gz
mkdir -p ~/.local/bin
install brain codebase-memory-mcp ~/.local/bin/
```

The Python installs below require Python 3.11+ and use the exact/lexical fallback
unless `codebase-memory-mcp` is also present on `PATH`.

### uv tool (recommended)

```bash
uv tool install "project-brain-context @ git+https://github.com/superorange0707/project-brain.git@v0.4.1"
```

### pipx

```bash
pipx install "project-brain-context @ git+https://github.com/superorange0707/project-brain.git@v0.4.1"
```

### pip / release wheel

```bash
python -m pip install https://github.com/superorange0707/project-brain/releases/download/v0.4.1/project_brain_context-0.4.1-py3-none-any.whl
```

Then verify:

```bash
brain --version
```

## Quick start

Run `brain init` from the folder that contains your repositories. It recursively
discovers nested Git repositories, so you do not need to list them:

```bash
cd ~/code/payments-platform
brain init --name payments-platform
```

That one command discovers every nested Git repo, fetches `origin`, creates
immutable snapshots, prepares lazy structural indexing, and generates the project
relationship map. By default it prefers each repo's fresh `origin/develop` or
`origin/development`, then falls back to that repo's remote default branch. A
fetch failure is reported per repo and falls back to the newest locally available
commit; it never blocks the other repositories.

For SSH remotes, one temporary multiplexed connection is reused per host during
the sync. Only the first fetch may interactively request a passphrase; every later
fetch is forced into non-interactive mode. The socket expires after the sync, and
macOS Keychain access is explicitly disabled. Project Brain never reads or stores
the passphrase. When reuse is unavailable, later repos use their local remote refs
instead of repeating the prompt. A timed-out fetch also terminates its SSH child
processes, so no abandoned prompt is left running in the terminal.

Private intranet GitLab works without an API integration: Project Brain invokes
the installed Git against each existing `origin`, inheriting the machine's VPN,
SSH config, host aliases, and proxy rules. It never sends a remote URL, key, or
passphrase to a hosted Project Brain service—there is no such service.

Open the local investigation cockpit:

```bash
brain ui
```

The browser page is served only on `127.0.0.1` and protected by a random session
token. Paste a ticket, click **Start investigation**, and copy the generated
context to your chat AI. Paste the AI's complete reply into **AI Request**, review
the exact local operations, and click **Approve and run**.

Prefer the terminal? Start a ticket investigation directly:

```bash
brain start ABC-1234 --ticket-file ticket.md --target claude
```

If one repo already has the ticket branch on `origin`, select it for this session
without changing any checkout:

```bash
brain start ABC-1234 --branch payment-service=feature/ABC-1234 --ticket-file ticket.md
```

All other repositories still use their development/default branches. Paste the
generated start context into the chat. When the AI responds with a
`CONTEXT_REQUEST`, copy it and run:

```bash
brain ctx ABC-1234 --clipboard --target claude
```

Paste the result back. Repeat until the AI returns `FINAL_SOLUTION`. For M365
Copilot, use `--target m365` and upload the printed Markdown path instead.

### Try the complete workflow without your own repositories

```bash
brain demo
cd project-brain-demo
brain ui
```

The demo creates four local Git repositories with Spring, Kafka, Feign, tests,
and a realistic eligibility bug. It contains no network remote or credential.

## Local investigation cockpit

The GUI is a thin, local layer over the same tested retrieval core as the CLI:

- **Project health** shows snapshot SHAs, fetch state, and structural/lexical index status.
- **Start ticket** optionally synchronizes every repo, accepts per-repo feature
  branches, and pins the investigation to exact commits.
- **AI Request Inbox** extracts YAML or JSON from the complete chat response, validates repository names, and previews every operation before execution.
- **Evidence context** provides chunk navigation and one-click clipboard delivery.
- **Review changes** packages tracked diffs, developer notes, and observed test output; it never runs the command or claims success itself.
- **Investigation history** reopens ticket, request, context, and feedback artifacts under `.runs/TICKET/`.

There is no embedded model, MCP client, hosted service, or automatic code editor.

## A context request

```yaml
CONTEXT_REQUEST:
  version: 1
  objective: >
    Determine how jurisdiction changes reach trading eligibility recalculation.

  searches:
    - query: "JURISDICTION_CHANGED"
      repos: []

  symbols:
    - name: "TradingEligibilityService.recalculate"
      repos: [trading-service]
      include: [definition, callers, callees, implementations, tests]

  files: []

  history:
    - query: "JURISDICTION_CHANGED"
      repos: [trading-service]
```

One request can trigger dozens of local operations. The returned context includes
the objective, exact analyzed commit/ref, freshness warnings, structural and
framework relationships, ranked source evidence, Git history, and an explicit
unresolved section.

## What it can explore

- Safe fetch plus exact, immutable remote-commit snapshots
- Cross-repository literal and regular-expression search
- Classes, interfaces, methods, functions, and lexical symbol fallback
- Interface implementations and inheritance
- Static callers and likely outbound calls
- Related unit and integration tests
- Direct file and line-range retrieval
- Git pickaxe/history and optional working-tree diffs
- Human project maps, glossaries, flows, and solved-ticket memory
- Evidence-linked Spring REST ↔ Feign, Kafka producer ↔ consumer, and Maven
  producer ↔ consumer relationships with multi-hop workflow summaries
- Java, Kotlin, Python, JavaScript/TypeScript, Go, Rust, Ruby, C/C++, C#, Swift,
  PHP, shell, SQL, XML, YAML, TOML, properties, and other text through `rg`

## How it works

```text
                     ┌─────────────────────┐
                     │ Claude / ChatGPT /  │
                     │ M365 Copilot        │
                     └──────────┬──────────┘
                                │ CONTEXT_REQUEST
                                ▼
┌──────────────┐      ┌─────────────────────┐      ┌─────────────────┐
│ Project map  │─────▶│    Project Brain    │◀─────│ Git repositories│
│ + glossary   │      │ deterministic local │      │ repo A/B/C/...  │
└──────────────┘      │ retrieval           │      └─────────────────┘
                      └──────────┬──────────┘
                                 │ Markdown / clipboard chunks
                                 ▼
                           verified evidence
```

The bundled release uses `codebase-memory-mcp` for tree-sitter/Hybrid-LSP
structural queries, indexed lazily only for repositories implicated by a symbol
request, and Project Brain's deterministic scanners for cross-repo
framework wiring. If the backend is absent or fails, `rg` or the standard-library
scanner remains available. Every heuristic is labelled; this is useful static
evidence, not a claim to compiler-perfect runtime behavior.

## Commands

```text
brain init              Discover, sync, index, and map a project root
brain demo              Create a safe four-repository example investigation
brain sync              Safely fetch all repos and rebuild remote snapshots
brain doctor            Check config, repositories, git, rg, and freshness
brain status            Show project health and ticket sessions (optionally JSON)
brain refresh           Sync and regenerate all project intelligence
brain search            Search all configured repositories
brain symbol            Resolve symbol definitions with lexical fallback
brain trace             Find static callers and likely outbound calls
brain history           Search Git history
brain map               Generate Spring/Maven/project facts
brain start             Start a ticket investigation
brain preview           Validate and dry-run an AI request
brain ctx               Fulfil a CONTEXT_REQUEST
brain feedback          Package diffs and observed test output for AI review
brain ui                Open the token-protected local investigation cockpit
brain next / prev       Navigate Claude clipboard chunks
brain delivery-status   Show the current chunk
brain learn             Create a solved-ticket memory template
```

See the [complete user guide](docs/USER_GUIDE.md) for configuration, workflows,
request syntax, troubleshooting, and operational guidance.

## Privacy and safety

Project Brain invokes `git fetch` using your existing Git configuration and SSH
environment. For SSH fetches on macOS it explicitly passes `UseKeychain=no`; it
never reads, stores, logs, or asks for a token, and never puts a remote URL in
generated state. HTTPS remotes remain under Git's configured credential-helper
policy. Project Brain makes no model/API requests. Generated context can still
contain proprietary source or secrets already present in a repo. Review context
before pasting or uploading it outside your organization.

`brain ui` binds to IPv4 loopback only, uses a random per-process API token,
rejects non-local Host headers, limits request bodies, sends a restrictive browser
security policy, and serves no repository file directly. Closing the process
invalidates the URL immediately.

Generated source evidence, local repository paths, configs, keys, `.env` files,
knowledge, and ticket runs are ignored by the tool repository's default
`.gitignore`. Releases use GitHub/PyPI short-lived OIDC credentials—no publishing
token belongs in this repository.

The prebuilt archive includes the pinned open-source structural backend; see
[third-party notices](THIRD_PARTY.md).

Read [SECURITY.md](SECURITY.md) before using Project Brain with private code.

## Accuracy boundary

Project Brain cannot prove runtime behavior created by reflection, dynamic DI,
generated code, external data, feature flags, or string-built routes/topics. The AI
should request logs or test output when static evidence cannot settle a question.

That is intentional: useful evidence beats false certainty.

## Contributing

Issues and focused pull requests are welcome. Start with
[CONTRIBUTING.md](CONTRIBUTING.md). Please report vulnerabilities through a private
GitHub Security Advisory, not a public issue.

## License

[MIT](LICENSE) © Project Brain contributors.

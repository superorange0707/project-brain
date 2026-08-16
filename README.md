<div align="center">

# 🧠 Project Brain

**Give any chat AI the codebase exploration loop of a coding agent.**

Turn a ticket into verified, up-to-date multi-repository source context—without
cloud indexing, API keys, or giving an agent permission to edit your code.

[![CI](https://github.com/superorange0707/project-brain/actions/workflows/ci.yml/badge.svg)](https://github.com/superorange0707/project-brain/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Zero runtime dependencies](https://img.shields.io/badge/runtime_dependencies-0-2ea44f)](pyproject.toml)
[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

[Quick start](#quick-start) · [How it works](#how-it-works) · [User guide](docs/USER_GUIDE.md) · [Security](SECURITY.md)

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
| 🤖 **Model-independent** | Works with Claude, ChatGPT, M365 Copilot, or any text model |
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
tar -xzf project-brain-v0.2.1-macos-arm64.tar.gz
mkdir -p ~/.local/bin
install brain codebase-memory-mcp ~/.local/bin/
```

The Python installs below require Python 3.11+ and use the exact/lexical fallback
unless `codebase-memory-mcp` is also present on `PATH`.

### uv tool (recommended)

```bash
uv tool install "project-brain-context @ git+https://github.com/superorange0707/project-brain.git@v0.2.1"
```

### pipx

```bash
pipx install "project-brain-context @ git+https://github.com/superorange0707/project-brain.git@v0.2.1"
```

### pip / release wheel

```bash
python -m pip install https://github.com/superorange0707/project-brain/releases/download/v0.2.1/project_brain_context-0.2.1-py3-none-any.whl
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
immutable snapshots of the remote default branches, builds the structural index,
and generates the project relationship map. A fetch failure is reported per repo
and falls back to the newest locally available commit; it never blocks the other
repositories.

Start a ticket investigation:

```bash
brain start ABC-1234 --ticket-file ticket.md --target claude
```

Paste the generated start context into the chat. When the AI responds with a
`CONTEXT_REQUEST`, copy it and run:

```bash
brain ctx ABC-1234 --clipboard --target claude
```

Paste the result back. Repeat until the AI returns `FINAL_SOLUTION`. For M365
Copilot, use `--target m365` and upload the printed Markdown path instead.

## A context request

```yaml
CONTEXT_REQUEST:
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
structural queries and Project Brain's deterministic scanners for cross-repo
framework wiring. If the backend is absent or fails, `rg` or the standard-library
scanner remains available. Every heuristic is labelled; this is useful static
evidence, not a claim to compiler-perfect runtime behavior.

## Commands

```text
brain init              Discover, sync, index, and map a project root
brain sync              Safely fetch all repos and rebuild remote snapshots
brain doctor            Check config, repositories, git, rg, and freshness
brain refresh           Sync and regenerate all project intelligence
brain search            Search all configured repositories
brain symbol            Resolve symbol definitions with lexical fallback
brain trace             Find static callers and likely outbound calls
brain history           Search Git history
brain map               Generate Spring/Maven/project facts
brain start             Start a ticket investigation
brain ctx               Fulfil a CONTEXT_REQUEST
brain next / prev       Navigate Claude clipboard chunks
brain delivery-status   Show the current chunk
brain learn             Create a solved-ticket memory template
```

See the [complete user guide](docs/USER_GUIDE.md) for configuration, workflows,
request syntax, troubleshooting, and operational guidance.

## Privacy and safety

Project Brain invokes `git fetch` using your existing Git configuration and
credential helper. It never reads, stores, logs, or asks for a token, and never
puts a remote URL in generated state. It makes no model/API requests. Generated
context can still contain proprietary source or secrets already present in a repo.
Review context before pasting or uploading it outside your organization.

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

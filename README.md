<div align="center">

# 🧠 Project Brain

**Give any chat AI the codebase exploration loop of a coding agent.**

Turn a ticket into verified, multi-repository source context—without cloud
indexing, API keys, or giving an agent permission to edit your code.

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
| 🔒 **Local and read-only** | Never edits, builds, commits, or uploads a repository |
| 🧭 **Multi-repository** | Every result includes repository, path, and line evidence |
| 🤖 **Model-independent** | Works with Claude, ChatGPT, M365 Copilot, or any text model |
| 📚 **Evidence-complete** | Retrieves production source, tests, config, relationships, and history |
| 🪶 **Tiny footprint** | Python standard library only; `rg` and `git` are optional accelerators |
| 🔍 **Honest uncertainty** | Missing evidence and static-analysis limits are reported, never hidden |

No embeddings. No vector database. No background daemon. No API key.

## Install

Project Brain requires Python 3.11 or newer.

### uv tool (recommended)

```bash
uv tool install "project-brain-context @ git+https://github.com/superorange0707/project-brain.git@v0.1.0"
```

### pipx

```bash
pipx install "project-brain-context @ git+https://github.com/superorange0707/project-brain.git@v0.1.0"
```

### pip / release wheel

```bash
python -m pip install https://github.com/superorange0707/project-brain/releases/download/v0.1.0/project_brain_context-0.1.0-py3-none-any.whl
```

### Homebrew

```bash
brew install superorange0707/tap/project-brain
```

Then verify:

```bash
brain --version
```

## Quick start

Keep the Brain workspace beside your repositories so its generated context never
pollutes them:

```bash
mkdir payments-brain && cd payments-brain

brain init \
  ~/code/customer-service \
  ~/code/trading-service \
  ~/code/risk-service \
  --name payments-platform

brain doctor
brain refresh
```

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
the objective, repository HEADs, freshness warnings, static relationships, ranked
source evidence, Git history, and an explicit unresolved section.

## What it can explore

- Cross-repository literal and regular-expression search
- Classes, interfaces, methods, functions, and lexical symbol fallback
- Interface implementations and inheritance
- Static callers and likely outbound calls
- Related unit and integration tests
- Direct file and line-range retrieval
- Git pickaxe/history and optional working-tree diffs
- Human project maps, glossaries, flows, and solved-ticket memory
- Spring controllers/services/repositories, routes, Feign clients, Kafka listeners,
  scheduled jobs, entities, tables, and Maven dependencies
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

`rg` is used when available and a standard-library scanner takes over when it is
not. Structural relationships use deterministic lexical heuristics: useful static
evidence, not a claim to compiler-perfect call graphs.

## Commands

```text
brain init              Create a portable Brain workspace
brain doctor            Check config, repositories, git, rg, and freshness
brain refresh           Snapshot changed repositories and regenerate facts
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

Project Brain itself makes no network requests and accepts no model/API
credentials. It only reads configured repositories. However, generated context can
contain proprietary source or secrets that already exist in those repositories.
Review context before pasting or uploading it outside your organization.

Generated source evidence, local repository paths, configs, keys, `.env` files,
knowledge, and ticket runs are ignored by the tool repository's default
`.gitignore`. Releases use GitHub/PyPI short-lived OIDC credentials—no publishing
token belongs in this repository.

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

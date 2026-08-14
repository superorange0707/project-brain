# Project Brain User Guide

Project Brain is a read-only bridge between local repositories and a chat AI that
cannot call filesystem tools. This guide covers installation, workspace setup,
ticket investigations, configuration, command reference, and troubleshooting.

## 1. Requirements

- Python 3.11 or newer
- `git` recommended for repository state and history
- `rg` (ripgrep) recommended for fast search
- macOS, Linux, or Windows

There are no Python runtime dependencies. When `rg` is unavailable, Project Brain
uses its built-in scanner. Non-Git directories can still be searched.

## 2. Installation

### uv tool

```bash
uv tool install "project-brain-context @ git+https://github.com/superorange0707/project-brain.git@v0.1.1"
```

Upgrade later with:

```bash
uv tool upgrade project-brain-context
```

### pipx

```bash
pipx install "project-brain-context @ git+https://github.com/superorange0707/project-brain.git@v0.1.1"
```

### Homebrew

```bash
brew install superorange0707/tap/project-brain
```

Homebrew maps `superorange0707/tap` to the separate
[`homebrew-tap`](https://github.com/superorange0707/homebrew-tap) formula index.
Project Brain's application source remains in the main repository.

### From a source checkout

```bash
git clone https://github.com/superorange0707/project-brain.git
cd project-brain
python -m pip install -e .
```

Verify every installation with:

```bash
brain --version
brain --help
```

## 3. Create a Brain workspace

Use a separate directory for Project Brain state. Target repositories remain
untouched.

```text
~/work/
├── customer-service/
├── trading-service/
├── risk-service/
└── payments-brain/
```

Initialize it:

```bash
cd ~/work/payments-brain
brain init \
  ../customer-service \
  ../trading-service \
  ../risk-service \
  --name payments-platform
```

This creates:

```text
payments-brain/
├── brain.toml
└── knowledge/
    ├── PROJECT_MAP.md
    ├── glossary.md
    ├── flows/
    └── tickets/
```

Run diagnostics and generate deterministic facts:

```bash
brain doctor
brain refresh
```

## 4. Configuration

The generated `brain.toml` is portable and uses paths relative to itself where
possible.

```toml
[project]
name = "payments-platform"
runs_dir = ".runs"
state_dir = "state"
generated_dir = "generated"

[search]
max_results = 100

[context]
source_window_lines = 150
full_file_lines = 350
soft_target_chars = 500000

[delivery]
clipboard_chunk_chars = 180000

[knowledge]
path = "knowledge"

[[repositories]]
name = "trading-service"
path = "../trading-service"
description = "Owns trading eligibility and permissions."
tags = ["trading", "eligibility"]
```

Use `-c /path/to/brain.toml` when running a command outside the workspace.
Project Brain also accepts legacy `config.yml` and `config.yaml` files.

### Search settings

- `max_results`: maximum matches gathered per operation.

### Context settings

- `source_window_lines`: source lines around a hit in a large file.
- `full_file_lines`: files up to this size are included in full.
- `soft_target_chars`: emits a warning above this size; evidence is not discarded.

### Delivery settings

- `clipboard_chunk_chars`: maximum size of each Claude clipboard part.

## 5. Teach the Brain your domain

Deterministic search cannot know that a ticket's “PE” means “Permission
Evaluation” or that an apparently active service is obsolete. Put that knowledge
in two concise files.

### `knowledge/PROJECT_MAP.md`

Record ownership and non-obvious flows:

```markdown
# Project Map

## Customer domain

customer-service owns residency and jurisdiction. It publishes CustomerUpdated.

## Trading domain

trading-service consumes CustomerUpdated and recalculates eligibility.

## Main flow

customer-service → CustomerUpdated → trading-service → risk-service
```

### `knowledge/glossary.md`

Map business vocabulary to source vocabulary:

```markdown
# Glossary

## PE

Permission Evaluation. Usually implemented in trading-service.

## EOD

End-of-day batch processing in batch-service.
```

Knowledge files are searched alongside code on every relevant request.

## 6. Start an investigation

Put the ticket body in a text or Markdown file:

```bash
brain start ABC-1234 --ticket-file ticket.md --target claude
```

For a short ticket:

```bash
brain start ABC-1234 --text "Jurisdiction changes only refresh overnight" --target claude
```

The start context contains the coding-agent protocol, ticket, project map,
generated facts, and glossary. For Claude, it is copied automatically. Use
`--no-copy` in a headless terminal.

### M365 Copilot or another file-based frontend

```bash
brain start ABC-1234 --ticket-file ticket.md --target m365
```

Upload the printed `.runs/ABC-1234/start.md` file.

## 7. Fulfil a `CONTEXT_REQUEST`

The AI should either ask for evidence or return a final solution. A full request:

```yaml
CONTEXT_REQUEST:
  objective: Determine the online eligibility recalculation flow.

  searches:
    - query: "JURISDICTION_CHANGED"
      repos: []
    - query: "customer.updated"
      repos: [trading-service]

  symbols:
    - name: "EligibilityEvaluator"
      repos: [trading-service]
      include:
        - definition
        - callers
        - callees
        - implementations
        - tests

  files:
    - repo: trading-service
      path: src/main/resources/application.yml
      lines: 1-160

  history:
    - query: "JURISDICTION_CHANGED"
      repos: [trading-service]
```

From the clipboard:

```bash
brain ctx ABC-1234 --clipboard --target claude
```

From a file:

```bash
brain ctx ABC-1234 --file request.yml --target m365
```

From stdin:

```bash
brain ctx ABC-1234 --target m365 < request.yml
```

When investigating your manual code changes, include all configured repository
diffs:

```bash
brain ctx ABC-1234 --clipboard --include-diff --target claude
```

## 8. Claude chunk navigation

Large contexts are saved as numbered parts. The first is copied automatically.

```bash
brain delivery-status ABC-1234
brain next ABC-1234
brain prev ABC-1234
```

All requests and responses remain under `.runs/ABC-1234/`:

```text
.runs/ABC-1234/
├── ticket.md
├── start.md
├── request-001.yml
├── context-001.md
├── request-002.yml
├── context-002.md
├── delivery/
└── session.json
```

## 9. Exploration commands

### Exact or regular-expression search

```bash
brain search JURISDICTION_CHANGED --fixed
brain search 'class .*Eligibility'
brain search 'customer\.updated' --repo trading-service
```

### Symbol resolution

```bash
brain symbol EligibilityEvaluator
brain symbol TradingEligibilityService.recalculate --repo trading-service
```

### Static call evidence

```bash
brain trace TradingEligibilityService.recalculate
```

Static tracing is intentionally labelled heuristic. Validate runtime DI,
reflection, generated code, and external routing through tests or logs.

### Git history

```bash
brain history JURISDICTION_CHANGED --repo trading-service
```

The command first uses `git log -S`, then a regex history fallback.

### Generated project facts

```bash
brain map
brain refresh
```

`map` scans framework markers and Maven dependencies. `refresh` also records
repository commit snapshots so stale state is visible in later context packs.

### Solved-ticket memory

```bash
brain learn ABC-1234
```

Fill in the generated short template under `knowledge/tickets/`. Future searches
can reuse the root cause, flow, tests, and gotchas.

## 10. Security model

Project Brain does not:

- make network requests;
- ask for AI, GitHub, Jira, or cloud credentials;
- edit target repositories;
- execute repository code or tests;
- run checkout/reset/clean;
- upload generated context.

It does read source and can place that source on your clipboard or in Markdown.
Treat context packs with the same confidentiality as the repositories they came
from. See [SECURITY.md](../SECURITY.md).

## 11. Troubleshooting

### `No brain.toml/config.yml found`

Run commands inside the Brain workspace, pass `-c`, or initialize one with
`brain init`.

### `Unknown repositories`

Names in a request must exactly match `[[repositories]].name` in `brain.toml`.

### Clipboard unavailable

Use `--file` or stdin for input and `--no-copy` for output. On Linux, install
`wl-clipboard` or `xclip` if clipboard delivery is wanted.

### Symbol or caller not found

Request the exact string, interface name, annotation, event/topic, config key, and
known neighboring symbols. Dynamic behavior may not have a static call site.

### Context is very large

Narrow repository lists and request objectives. Increase
`clipboard_chunk_chars` for a frontend that accepts larger pastes. Project Brain
warns at the soft target but never silently removes evidence.

### `rg` missing

The built-in scanner remains functional. Install ripgrep for much faster searches
on large repositories.

## 12. Updating and uninstalling

```bash
uv tool upgrade project-brain-context
uv tool uninstall project-brain-context
```

Or with pipx:

```bash
pipx upgrade project-brain-context
pipx uninstall project-brain-context
```

Removing the CLI does not delete Brain workspaces or their `.runs` evidence.

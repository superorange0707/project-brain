# Project Brain User Guide

Project Brain is a read-only bridge between local repositories and a chat AI that
cannot call filesystem tools. This guide covers installation, workspace setup,
ticket investigations, configuration, command reference, and troubleshooting.

## 1. Requirements

- Homebrew/standalone installs: no Python or Xcode requirement
- Python installs: Python 3.11 or newer
- `git` recommended for repository state and history
- `rg` (ripgrep) recommended for fast search
- macOS, Linux, or Windows

There are no Python package runtime dependencies. The prebuilt package includes
the tested structural engine. When that engine or `rg` is unavailable, Project
Brain uses its built-in scanner. Non-Git directories can still be searched.

## 2. Installation

### Homebrew (recommended on macOS)

```bash
brew install superorange0707/tap/project-brain
```

This installs prebuilt executables and does not compile against the local Xcode
toolchain. Homebrew maps `superorange0707/tap` to the separate
[`homebrew-tap`](https://github.com/superorange0707/homebrew-tap) formula index.

### Standalone macOS/Linux archive

Download the matching archive from the
[latest release](https://github.com/superorange0707/project-brain/releases/latest),
then keep `brain` and `codebase-memory-mcp` in the same directory on `PATH`.

### uv tool

```bash
uv tool install "project-brain-context @ git+https://github.com/superorange0707/project-brain.git@v0.5.0"
```

Upgrade later with:

```bash
uv tool upgrade project-brain-context
```

### pipx

```bash
pipx install "project-brain-context @ git+https://github.com/superorange0707/project-brain.git@v0.5.0"
```

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

To try Project Brain before pointing it at private repositories:

```bash
brain demo
cd project-brain-demo
brain ui
```

The demo has four local Java/Spring repositories, no remote, and no credential.

## 3. Create a Brain workspace

Run Project Brain from the common parent folder. Generated state stays in that
parent folder; target repositories remain untouched.

```text
~/work/payments-platform/
├── customer-service/
├── trading-service/
└── risk-service/
```

Initialize it without listing repositories. Project Brain recursively finds all
nested Git repositories:

```bash
cd ~/work/payments-platform
brain init --name payments-platform
```

This creates:

```text
payments-platform/
├── brain.toml
├── knowledge/
    ├── PROJECT_MAP.md
    ├── glossary.md
    ├── flows/
    └── tickets/
├── customer-service/
├── trading-service/
└── risk-service/
```

Explicit repository paths remain available when you want a narrower scope.

Initialization is the full setup: it discovers all repos, fetches their `origin`
refs, exports immutable source snapshots, records the deterministic index, and
generates project facts and relationships. Structural repositories are indexed
on demand when a symbol request first implicates them, so a 30-repo workspace does
not pay the startup cost for every repo. Run `brain doctor` only when you want the
detailed health report.

### What “latest” means

Project Brain runs `git fetch --prune --quiet origin` for each repo. It then picks
the first available remote development branch from `[sources].branch_priority`
(`origin/develop`, then `origin/development` by default). If neither exists, it
uses `origin/HEAD`, `origin/main`, `origin/master`, or finally the configured
upstream. It exports that exact commit with `git archive` below `state/`.

It deliberately never runs `pull`, `checkout`, `reset`, `clean`, merge, or rebase.
Your current branch, staged files, and uncommitted edits are unchanged. If a fetch
fails, that repo uses its newest locally available remote ref and reports the
failure; other repos continue.

SSH remotes on the same host share a temporary OpenSSH multiplexed connection
during each sync. The first repository can ask for the key passphrase once; all
later fetches are forced into OpenSSH `BatchMode` and cannot prompt. The control
socket is temporary, expires after the operation, and contains no credential.
On macOS, Project Brain also passes `UseKeychain=no`, overriding any inherited
Keychain lookup for its fetches. If connection reuse is unavailable, later repos
fall back to locally available remote refs. When a fetch reaches its timeout,
Project Brain terminates both Git and its SSH child processes so an abandoned
prompt cannot continue in the terminal.

`brain start` performs this sync once at the beginning of a ticket, so every later
context request in that investigation uses a stable source snapshot. Run
`brain refresh` to move all snapshots to newer remote commits, or use
`brain start --no-sync` when offline.

For a ticket branch that already exists on one remote, override only that repo:

```bash
brain start ABC-1234 \
  --branch payment-service=feature/ABC-1234 \
  --ticket-file ticket.md
```

The override does not checkout the branch. Other repos keep using the automatic
development/default policy. The selected ref, SHA, fetch result, and any stale
fallback are recorded in the start pack and every later context pack. To make a
long-lived exception, add `branch = "main"` to that repo's `[[repositories]]`
entry. A branch available only locally can be analyzed, but is explicitly marked
as having unverified remote freshness. The local cockpit exposes the same option
as the **Feature branches** field, one `REPO=BRANCH` entry per line.

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

[graph]
mode = "lazy"

[sources]
branch_priority = ["develop", "development"]

[knowledge]
path = "knowledge"

[[repositories]]
name = "trading-service"
path = "../trading-service"
description = "Owns trading eligibility and permissions."
tags = ["trading", "eligibility"]
# branch = "main" # optional permanent override for this repo
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

### Source branch settings

- `branch_priority`: remote branches preferred for normal cross-repo analysis,
  in order. Repos without one of these branches use their own remote default.
- `repositories.branch`: optional permanent override for a special repo.
- `--branch REPO=BRANCH`: temporary override accepted by `brain sync`,
  `brain refresh`, and `brain start`; useful for a ticket's feature branch.

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

## 6. Create a persistent M365 Copilot Agent

Project Brain can generate the permanent Instructions and stable project
knowledge for Microsoft 365 Copilot Agent Builder:

```bash
brain agent-kit m365
```

This creates:

```text
generated/m365-agent/
├── INSTRUCTIONS.md
├── PROJECT_KNOWLEDGE.md
└── SETUP.md
```

Paste `INSTRUCTIONS.md` into the Agent Builder **Instructions** field. Add
`PROJECT_KNOWLEDGE.md` plus approved IPF, architecture, API, deployment, and
coding-standard documents to **Knowledge**. Keep operational instructions in the
Instructions field rather than hiding them in a knowledge document.

Recommended settings:

- default response mode: **Think deeper**;
- enable **Only use specified sources**;
- disable broad web, email, Teams, and people sources unless the project needs
  them;
- enable code interpreter only when document, log, JSON, or spreadsheet analysis
  is useful.

The M365 Agent remains the user-facing investigator. It asks the developer
directly for business, document, runtime, and environment facts. It emits a
`CONTEXT_REQUEST` only when local repository evidence is required. Project Brain
does not install a connector, request an M365 credential, or call the cloud.

## 7. Local investigation cockpit

Run the editor-independent GUI from a Brain workspace:

```bash
brain ui
```

The command prints and opens a random-token URL on `127.0.0.1`. The page provides:

- repository snapshot, index, and ticket-session health;
- a ticket form that synchronizes repos and creates the AI start context;
- a Continue with AI inbox that distinguishes direct conversation, repository
  requests, duplicate retrievals, and final solutions;
- a deterministic request preview before any search or source read;
- retrieval progress showing unique evidence and no-progress turns;
- evidence chunk navigation and clipboard delivery;
- a one-click M365 Agent setup kit;
- implementation feedback containing tracked diffs and observed test output;
- access to saved ticket, request, context, and feedback artifacts.

No AI model runs inside the page. It does not execute tests or edit code. Closing
`brain ui` invalidates the random URL and stops the local server.

The remaining sections document the equivalent terminal workflow and are useful
for automation, M365 file delivery, or troubleshooting.

## 8. Start an investigation

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

Upload the printed `.runs/ABC-1234/current-handoff.md` file to the dedicated M365
Agent. Brain replaces this same file after every retrieval and implementation
feedback package, so you never create or rename a Markdown file manually.

## 9. Continue an AI investigation

The preferred command accepts the AI's complete reply:

```bash
brain continue ABC-1234 --clipboard --target claude
```

It deterministically routes the reply:

- `CONTEXT_REQUEST`: validate, retrieve, and deliver local evidence;
- normal conversation: tell the developer to answer the AI directly;
- `FINAL_SOLUTION`: archive the plan and mark the ticket ready to implement.

For M365, use:

```bash
brain continue ABC-1234 --file ai-response.txt --target m365
```

Then upload the refreshed `.runs/ABC-1234/current-handoff.md`. Use the same M365
Agent conversation so it retains earlier documents, human answers, and evidence.

The strict `brain ctx` command remains useful for automation when the input is
known to contain a `CONTEXT_REQUEST`.

### `CONTEXT_REQUEST` format

The AI should either ask for evidence or return a final solution. A full request:

```yaml
CONTEXT_REQUEST:
  version: 1
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

Preview a copied request without reading source:

```bash
brain preview --clipboard
brain preview --clipboard --json
```

The preview validates repository names and lists every search, symbol operation,
file read, and history query. JSON request objects are accepted as well as fenced
YAML inside a complete chat response.

An identical retrieval plan against the same pinned repository snapshots is
rejected. Every returned context reports new evidence regions, previously seen
evidence, recent retrieval objectives, and consecutive no-progress requests. A
no-progress result tells the AI to ask for a specific external/runtime fact or
produce `FINAL_SOLUTION`, rather than continuing open-ended repository searches.

## 10. Claude chunk navigation

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
├── current-handoff.md
├── final-solution.md
├── feedback-001.md
├── delivery/
└── session.json
```

## 11. Exploration commands

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

`map` writes both `generated/PROJECT_FACTS.md` and
`generated/PROJECT_RELATIONSHIPS.md`. The relationship map matches exact Maven
coordinates, Kafka topics, Spring routes, and Feign clients, and derives multi-hop
runtime workflows with source and target file/line evidence. `refresh` first
fetches and snapshots all repositories, then rebuilds both maps and the structural
index.

### Structural backend

Homebrew and standalone packages include the tested `codebase-memory-mcp` v0.10.5
binary. Project Brain calls its local JSON CLI for structural symbol and call-path
queries and stores its cache under Brain's ignored `state/` directory. It never
runs the backend installer or changes Claude, Codex, or other agent configuration.
The default `graph.mode = "lazy"` indexes only repositories identified by exact
symbol evidence. Run `brain index` for an eager full-workspace graph, or set
`graph.enabled = false` when deterministic lexical analysis is sufficient.

Python-only installs can add that executable to `PATH` or set
`PROJECT_BRAIN_GRAPH_BIN=/absolute/path/to/codebase-memory-mcp`. `brain doctor`
shows which backend is active. If it is missing or fails, exact search and lexical
analysis continue automatically.

### Solved-ticket memory

```bash
brain learn ABC-1234
```

Fill in the generated short template under `knowledge/tickets/`. Future searches
can reuse the root cause, flow, tests, and gotchas.

## 12. Review your implementation

After applying the AI's solution and running tests yourself, package the observed
result for the same chat:

```bash
brain feedback ABC-1234 \
  --notes "Added the jurisdiction branch and regression test" \
  --test-command "mvn -pl trading-service -Dtest=CustomerChangedListenerTest test" \
  --test-output-file test-output.txt
```

For Claude-style delivery the first part is copied automatically. Add
`--repo trading-service` to limit tracked diffs, or `--no-diff` to send only notes
and test output. Project Brain never runs the command; it labels the output as an
observed human result and asks the AI to find gaps or request more evidence.

## 13. Security model

Project Brain does not:

- ask for AI, GitHub, Jira, or cloud credentials;
- edit target repositories;
- execute repository code or tests;
- run checkout/reset/clean;
- upload generated context.

Its only normal network activity is `git fetch origin`, performed by the user's
installed Git. Private intranet GitLab needs no GitLab API or Project Brain
account: the existing VPN, proxy, SSH config, host alias, and `core.sshCommand`
remain in control. For SSH on macOS, Project Brain adds `UseKeychain=no`; HTTPS
remotes remain subject to Git's configured credential-helper policy. Project
Brain never stores credentials or remote URLs.

The local cockpit binds only to IPv4 loopback, requires a random per-process token
for every API call, rejects non-local Host headers, limits request bodies, and
blocks framing and external page connections with browser security headers. It
does not expose a generic file endpoint: session artifacts are resolved from an
allowlist and cannot escape `.runs/TICKET/`.

It does read source and can place that source on your clipboard or in Markdown.
Treat context packs with the same confidentiality as the repositories they came
from. See [SECURITY.md](../SECURITY.md).

## 14. Troubleshooting

### `No brain.toml/config.yml found`

Run commands inside the Brain workspace, pass `-c`, or initialize one with
`brain init`.

### `Unknown repositories`

Names in a request must exactly match `[[repositories]].name` in `brain.toml`.
Use `brain preview --clipboard` to see the validation error before retrieval. The
GUI also provides a repair prompt that can be copied back to the AI.

### `brain ui` does not open

Open the exact loopback URL printed by the command. Use `brain ui --port 0` if the
default port is busy, or `brain ui --no-open` on a headless machine. Keep the
terminal process running while using the page.

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

### A repository says `fetch-failed`

Run `git fetch origin` in that repository to see Git's full diagnostic. Project
Brain intentionally stores only the exit code so a credential-bearing remote URL
cannot leak into state or context. Fix the normal Git/SSH/VPN access, then run
`brain sync`.

If an SSH key is still locked, you may load the exact company key into a
memory-only agent with `ssh-add /path/to/company_private_key` (without Apple's
`--apple-use-keychain` option), or use your organization's approved key manager,
then rerun `brain sync`. Project Brain does not start an agent, access Keychain,
or persist SSH keys itself. Use
`brain init --no-fetch`, `brain refresh --no-fetch`, or `brain start --no-sync`
when offline.

## 15. Updating and uninstalling

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

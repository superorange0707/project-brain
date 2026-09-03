# Project Brain User Guide

Project Brain is a read-only bridge between local repositories and a chat AI that
cannot call filesystem tools. This guide covers installation, workspace setup,
ticket investigations, configuration, command reference, and troubleshooting.

## 1. Requirements

- Homebrew/standalone installs: no Python, Xcode, Go, or WSL requirement
- Python installs: Python 3.11 or newer
- `git` recommended for repository state and history
- `rg` (ripgrep) recommended for fast search
- macOS, Linux, or Windows

There are no Python package runtime dependencies. The prebuilt package includes
the tested structural engine and a pinned Zoekt command pair. When either engine
or `rg` is unavailable, Project Brain uses its built-in scanner. Non-Git
directories can still be searched.

## 2. Installation

### Homebrew (recommended on macOS)

```bash
brew install superorange0707/tap/project-brain
```

Upgrade an existing stable installation with:

```bash
brew update
brew upgrade project-brain
```

This installs prebuilt executables and does not compile against the local Xcode
toolchain. Homebrew maps `superorange0707/tap` to the separate
[`homebrew-tap`](https://github.com/superorange0707/homebrew-tap) formula index.

### Linux installer

```bash
curl -fsSLO https://github.com/superorange0707/project-brain/releases/latest/download/install-project-brain.sh
sh install-project-brain.sh
```

This selects amd64/arm64, verifies `SHA256SUMS.txt`, and installs the four
adjacent executables in `~/.local/bin` without changing Brain workspace state.

### Standalone macOS/Linux archive

Download the matching archive from the
[latest release](https://github.com/superorange0707/project-brain/releases/latest),
then keep `brain`, `codebase-memory-mcp`, `zoekt`, and `zoekt-index` in the
same directory on `PATH`.

### Native Windows 11 x64 installer

When direct `.ps1` Release Asset downloads are blocked but `git clone` is
allowed, obtain the exact tagged installer from the repository:

```powershell
git clone --depth 1 --branch v1.0.8 https://github.com/superorange0707/project-brain.git project-brain-installer
cd project-brain-installer
.\scripts\install-project-brain.ps1 -Version 1.0.8
brain.exe --version
```

Specifying `-Version` avoids the GitHub API lookup. The installer downloads only
the versioned native ZIP and `SHA256SUMS.txt`, verifies the archive, preserves
workspace/model/session state, and adds its managed binary directory to the
user `PATH`. It does not require an execution-policy change when repository
scripts are already allowed. No WSL or Python runtime is required.

Create this installer clone in a downloads or temporary directory, not inside
the directory that contains repositories configured as a Brain workspace. The
clone is not required after installation.

If PowerShell reports that script execution is disabled by organization policy,
do not bypass that policy; use the portable ZIP below or ask the organization to
approve the installer.

### Manual native Windows 11 x64 standalone

Download `project-brain-v1.0.8-windows-amd64.zip` and the published
`SHA256SUMS.txt` from the same release. Verify the ZIP before extraction, then
keep `brain.exe`, `codebase-memory-mcp.exe`, `zoekt.exe`, and
`zoekt-index.exe` together:

```powershell
Get-FileHash .\project-brain-v1.0.8-windows-amd64.zip -Algorithm SHA256
Expand-Archive .\project-brain-v1.0.8-windows-amd64.zip -DestinationPath "$env:LOCALAPPDATA\ProjectBrain\bin"
$env:PATH = "$env:LOCALAPPDATA\ProjectBrain\bin;$env:PATH"
brain.exe --version
brain.exe --help
```

The archive is native amd64 Windows software. It does not invoke WSL, Git Bash,
Homebrew, Python, or Go. Git for Windows is the only recommended external tool
for the complete synchronized-snapshot workflow: install it before `brain init`
or `brain refresh` if the repositories are Git-backed. `brain doctor` reports
whether Git is discoverable. Without Git, the established read-only non-Git
working-tree fallback remains available.

Windows paths may contain spaces and Unicode. Project Brain stores local paths
in native form but canonicalizes repository-relative evidence and stable IDs to
`/`, so protocol-v5 identities remain portable. Clipboard operations use native
PowerShell. State, Atlas generations, Semantic shards, ticket sessions, and
model packs remain below the configured Brain workspace directories; upgrading
the executable does not reset them.

### uv tool

```bash
uv tool install "project-brain-context @ git+https://github.com/superorange0707/project-brain.git@v1.0.8"
```

Upgrade later with:

```bash
uv tool upgrade project-brain-context
```

### pipx

```bash
pipx install "project-brain-context @ git+https://github.com/superorange0707/project-brain.git@v1.0.8"
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

Explicit repository paths remain available when you want a narrower scope. The
workspace is still created in the current/common parent directory, never inside
an explicitly targeted repository. Use `-c /path/to/brain.toml` when you need a
different workspace location.

Initialization is the full setup: it discovers all repos, fetches their `origin`
refs, exports immutable source snapshots, records the deterministic index, and
generates project facts and relationships. Structural repositories are indexed
on demand when a symbol request first implicates them, so a 30-repo workspace does
not pay the startup cost for every repo. Run `brain doctor` only when you want the
detailed health report.

After initialization, clone new repositories normally. The next `brain refresh`,
synced ticket start, or cockpit **Refresh Brain** reports unconfigured repositories
as an explicit action. Add their `[[repositories]]` blocks to `brain.toml`, then
refresh again. Refresh never mutates this user-owned config implicitly, so an
editor save cannot be overwritten or partially appended. Use `--no-discover`
when a workspace intentionally excludes other Git repos below the same parent
folder and should skip the pending-repository check.

The cockpit's opt-in **Auto Refresh: When idle** mode checks selected commit refs,
Core index alignment, required Semantic generation alignment, and newly cloned
repositories. A newly cloned repository becomes **Action Required** until its
explicit `brain.toml` block is added; it is not placed in a failing refresh loop.
Ordinary working-tree edits are ignored. Recoverable changes are debounced into
one call to the same `refresh_brain()` pipeline used by manual refresh. Active
ticket retrievals leave that refresh pending; new tickets may
still pin the current ready snapshot, and existing tickets retain their original
snapshots. Missing/incompatible model packs, storage guards, invalid config,
Git/network failures, and runtime failures require explicit action and are not
continually retried. The Off/When idle preference and safe timestamps are stored
only under the Brain-owned state directory. `brain watch` uses this same detector
and scheduler rather than a separate unconditional polling implementation.

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
path_result_limit = 12
candidate_limit = 500

[context]
source_window_lines = 150
full_file_lines = 350
soft_target_chars = 120000
hard_context_chars = 180000
hydrate_limit = 18
max_regions_per_file = 2
max_regions_per_repo = 8

[retrieval]
max_concurrent_investigations = 2
repo_workers = 4
initial_repo_limit = 6
widen_repo_limit = 16
max_effective_operations = 15
max_backend_operations = 200
pre_rerank_candidate_limit = 200
semantic_shard_workers = 4

[delivery]
clipboard_chunk_chars = 180000

[graph]
mode = "lazy"

[sources]
branch_priority = ["develop", "development"]
fetch_scope = "selected" # selected | tracked | all-branches
watch_interval_seconds = 180

[knowledge]
path = "knowledge"

[storage]
max_state_gb = 200
minimum_free_disk_gb = 5

[models]
approved_install_hosts = [] # optional organization HTTPS pack hosts
# Optional PEM bundle added to OS trust for model downloads. SSL_CERT_FILE also works.
# ca_bundle = "/path/to/enterprise-ca.pem"

[experience]
enabled = true
ticket_pattern = "(?<![A-Z0-9])([A-Z][A-Z0-9]+-[0-9]+)(?![A-Z0-9])"
commit_limit = 1000
similar_cases = 5
patch_chars = 0

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
- `path_result_limit`: maximum verified filename/path matches read per repository.
- `candidate_limit`: maximum ranked metadata candidates considered before source hydration.

### Retrieval settings (advanced)

The defaults are intended for normal work and usually should not be changed.
They bound concurrent tickets, the shared repository worker pool, initial and
widened repository scopes, logical/physical operations, late candidates, and
Semantic shard search. Values outside Brain's hard safe maxima are rejected.

### Experience settings

- `enabled`: build local committed-ticket experience during refresh/start.
- `ticket_pattern`: regular expression used to extract ticket identifiers from
  Git commit subjects. Change this if your organization does not use `ABC-1234`.
- `commit_limit`: most recent non-merge commits scanned per repository.
- `similar_cases`: historical cases placed in a new ticket handoff.
- `patch_chars`: maximum historical patch characters automatically included for
  each similar case. The safe default is `0` (metadata only). Set a positive
  budget only when the destination AI is approved for repository source.

### Context settings

- `source_window_lines`: source lines around a hit in a large file.
- `full_file_lines`: files up to this size are included in full.
- `soft_target_chars`: emits a warning above this size.
- `hard_context_chars`: source-hydration budget; lower-ranked candidates remain as a compact manifest.
- `hydrate_limit`: maximum source regions hydrated by a normal request.
- `max_regions_per_file` / `max_regions_per_repo`: diversity limits that stop one file or repository from consuming the context.

### Delivery settings

- `clipboard_chunk_chars`: maximum size of each Claude clipboard part.

### Storage settings

- `max_state_gb`: maximum state/index/model-pack bytes Project Brain may own;
  set `0` to disable this quota.
- `minimum_free_disk_gb`: refuse a new index or model-pack write that would
  reduce free disk below this reserve; set `0` to disable the reserve.

### Model-pack installation settings

- `approved_install_hosts`: optional exact host names or parent domains allowed
  for a one-time HTTPS pack download. GitHub Release URLs are also accepted only
  with an explicit SHA-256. These settings never enable hosted inference.
- `ca_bundle`: optional local PEM CA bundle added to the operating-system-backed
  download trust context for one-time model downloads. This is for an
  enterprise-managed root that is not visible to the system store; it never
  disables certificate or hostname verification. The standard `SSL_CERT_FILE`
  environment variable is also honored without logging its value.

### Source branch settings

- `branch_priority`: remote branches preferred for normal cross-repo analysis,
  in order. Repos without one of these branches use their own remote default.
- `repositories.branch`: optional permanent override for a special repo.
- `--branch REPO=BRANCH`: temporary override accepted by `brain sync`,
  `brain refresh`, and `brain start`; useful for a ticket's feature branch.
- `fetch_scope`: defaults to `selected`, which fetches only the chosen source
  branch when it can be identified locally. Use `all-branches` only when a
  workspace deliberately needs every remote branch.
- `watch_interval_seconds`: polling cadence for idle auto-refresh and
  `brain watch` freshness checks (minimum 10).

### Editions and local model packs

`brain edition current` starts at `core`. Core has no model requirement and
always retains SQLite/ripgrep fallbacks. `brain edition set semantic` requires a
verified local embedding pack; `precision` also requires a verified reranker
pack. Semantic indexing additionally uses the optional USearch extra:

```bash
python -m pip install 'project-brain-context[semantic]'
```

The Homebrew and standalone archives include this local vector backend, but do
not include model weights. A plain Python Core installation reports the missing
backend instead of allowing a partially functional Semantic edition.

Pack installation is deliberately local-only:

```bash
brain model install /approved-share/qwen-embedding-pack.tar
brain model verify approved-qwen-embedding
brain model benchmark approved-qwen-embedding
brain model autotune approved-qwen-embedding --latency-budget-ms 3000
brain edition set semantic
brain capabilities
```

On macOS Apple Silicon, the separately released official
[Semantic pack v1.0.6](https://github.com/superorange0707/project-brain/releases/tag/semantic-pack-v1.0.6)
has a short controlled install path. Its descriptor SHA-256 is
`cbd09af575fb1b2e036abc17ed3e693e5bab4807af19efd2c1a9b5cd75ae8afc`.
This download happens once from a Project Brain GitHub Release; its descriptor,
every release part, and the assembled GGUF are checked before the local pack is
registered. It never contacts Hugging Face or any hosted model service while
indexing or querying.

On native Windows amd64 the same commands resolve the independently verified
`semantic-pack-windows-v1.0.0` descriptor, pinned by SHA-256
`69ca378fc2a00f01b23ae047ab46a7137c1b952d3c07a478350aaf2e2c6e2a30`.

```bash
python -m pip install 'project-brain-context[semantic]'
brain model install semantic
brain model verify semantic
brain model benchmark semantic
brain model autotune semantic
brain edition set semantic
brain refresh
```

`brain refresh` builds the snapshot-filtered semantic index after the normal
Core indexes when Semantic is selected. If its local runtime cannot start, the
refresh completes with Core and reports the Semantic warning. Precision becomes
available after the separately verified reranker pack is installed; otherwise it
automatically falls back to Semantic or Core.

Semantic refresh sends bounded requests: the complete document instruction,
semantic card, input suffix, UTF-8 encoding, and JSON escaping are all included
in its limits. A pathological card keeps its repository/path/symbol metadata
and deterministically trims only code. If a verified local runtime disconnects,
Brain restarts it and retries smaller bounded batches before reporting a safe
size-only diagnostic. A failed rebuild leaves the prior published semantic
generation active; successful cached embeddings are retained for the next
local refresh.

The manifest records model/revision/license/runtime/checksum provenance. Every
production embedding or reranker pack must include a hash-pinned local JSON
`golden_suite` (also listed in `artifacts`); `brain model verify` runs it before
marking the pack usable. Embedding cases check finite dimensions, batch parity,
normalization, reference-vector cosine parity, and expected similarity order;
production suites also declare and exercise a minimum long-input length.
Reranker cases pin an expected document order and must produce the same scores
for batch and one-document calls. A minimal embedding case looks like:

```json
{
  "requirements": {"long_input_min_chars": 4096},
  "embedding": [{
    "texts": ["ticket query", "relevant code", "...long negative example..."],
    "dimension": 2,
    "normalized": true,
    "reference_vectors": [[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]],
    "minimum_cosine_to_reference": 0.98,
    "expected_similarity_order": [1, 2]
  }]
}
```

The separately released [Precision pack v1.0.3](https://github.com/superorange0707/project-brain/releases/tag/precision-pack-v1.0.3)
is pinned by this compatible Core release. Its descriptor SHA-256 is
`f780010c883b9ded459f9e4190a262ee76b6a6e9fc20f9e47ab9a1452b438742`:

```bash
brain model install precision
brain model verify precision
brain model benchmark precision
brain model autotune precision --latency-budget-ms 3000
brain edition set precision
```

On native Windows amd64 the `precision` alias resolves
`precision-pack-windows-v1.0.0`, pinned by descriptor SHA-256
`524ac460c07b55891029b1de54120c47664969cdc985df713c19957657150d59`.

The Precision golden suite is public/synthetic only. It compares the official
Qwen Transformers reranker and the local Q6_K runtime for order, bounded score
delta, batch/single parity within a `0.002` absolute native-runtime tolerance,
using deterministic top/midpoint/endpoint samples, multilingual/code cases,
long truncation, and full-batch/reference checks for 10/20/40/80 candidate
pools using physical requests of at most 10 documents. It makes no M3 Pro or
private-corpus performance claim. Portable Windows packs start conservatively
at 10 documents per physical request and a 20-candidate shortlist until local
autotuning records a machine-specific recommendation.

The reference-vector arrays must have the declared dimension; real pack vectors
use the selected embedding dimension. A
managed `llama.cpp` pack declares both `runtime_binary` and `model_file` in its
checksummed `artifacts` mapping. Brain rechecks those hashes before launch, then
starts a short-lived server on `127.0.0.1` with an ephemeral API key,
`--offline`, and no Web UI; it terminates the process after the operation. A
production pack cannot delegate to `runtime_url`: it must own those verified
local artifacts so Brain can audit and terminate the runtime. `runtime_url` is
reserved for test-only loopback conformance fixtures.
Runtime instances that Brain starts for semantic indexing/search or precision
reranking are closed on both success and failure paths; Core continues without
them if an optional model operation fails.
Production embedding packs also declare the current `chunk_schema_version` and
`document_card_version`. A pack from an incompatible schema is disabled with an
explicit recovery path: install a compatible pack, then run `brain index rebuild
--backend semantic`; Core retrieval remains available throughout.
No model download, hosted telemetry, or cloud inference is performed by Project
Brain while it runs. `brain model benchmark` writes a public-synthetic
conformance and local latency report, not a claim that a model has passed your
holdout evaluation. `brain model autotune` stores only local recommendations for
the exact verified pack: embedding batch size, reranker candidate/batch limit,
one query worker, and the current short-lived-runtime policy. It does not invent
M3 Pro numbers or make a model resident. See [offline model packs](MODEL_PACKS.md)
for official-Qwen provenance, reproducible reranker conversion, controlled HTTPS
installation, and the required manifest fields.

### Golden replay evaluation

Keep a sanitized local JSON or YAML suite outside version control when ticket
details are private. Each case has an `id`, one of `calibration`, `validation`,
or `holdout` as `split`, a normal request mapping, and explicit expected paths:

```json
{
  "cases": [{
    "id": "sanitized-eligibility",
    "split": "holdout",
    "request": {"objective": "Find the evaluator", "searches": [{"query": "recalculate", "repos": ["trading-service"]}]},
    "expect": {
      "production_files": ["trading-service:src/main/java/demo/EligibilityEvaluator.java"],
      "test_config_files": ["trading-service:src/test/java/demo/CustomerChangedListenerTest.java"],
      "false_positive_files": ["trading-service:README.md"]
    }
  }]
}
```

Run `brain evaluate --golden /secure/path/suite.json --split holdout`. The
result stores only case IDs, suite hash, and aggregate metrics in
`state/golden-eval.json`; it does not copy request text into telemetry. Metrics
include repository Recall@5/10, file Recall@5/10/20, test recall, MRR@10,
nDCG@10, precision@5/10, context characters, duplicate ratio, candidate/
hydration/total latency, process peak RSS, and semantic-only useful-hit rate
when Semantic candidates are present. The last two are local diagnostic signals,
not a claim about a private corpus or a standalone semantic evidence path.

### Optional Zoekt local shards

If an approved local installation supplies both `zoekt` and `zoekt-index`, a
normal `brain index rebuild --backend lexical` creates a shard under the local
state directory for each repository snapshot. Exact and regular-expression
search uses that shard first and verifies literal lines before they become a
candidate. If either executable, a current shard, or a compatible query is
missing, Project Brain uses SQLite FTS5 or ripgrep automatically. `brain index
status` and `brain freshness` report whether Zoekt is currently available.

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
├── SUGGESTED_PROMPTS.md
└── SETUP.md
```

Paste `INSTRUCTIONS.md` into the Agent Builder **Instructions** field. Add
`PROJECT_KNOWLEDGE.md` plus approved IPF, architecture, API, deployment, and
coding-standard documents to **Knowledge**. Keep operational instructions in the
Instructions field rather than hiding them in a knowledge document.
Add the four title/prompt pairs from `SUGGESTED_PROMPTS.md` to **Suggested
prompts**.

Recommended settings:

- default response mode: **Think deeper**;
- enable **Only use specified sources**;
- disable broad web, email, Teams, and people sources unless the project needs
  them;
- enable code interpreter only when document, log, JSON, or spreadsheet analysis
  is useful.

The M365 Agent remains the user-facing investigator. It asks the developer
directly for business, document, runtime, and environment facts. It emits a
`INVESTIGATION_REQUEST` only when local repository evidence is required. Project Brain
does not install a connector, request an M365 credential, or call the cloud.

## 7. Local investigation cockpit

Run the editor-independent GUI from a Brain workspace:

```bash
brain ui
```

The command prints and opens a random-token URL on `127.0.0.1`. For ordinary
work, stay in the page:

1. Open **Brain** and check overall health, effective retrieval state, model
   verification, Semantic chunk count/alignment, and repository freshness.
2. Click **Refresh Brain**. By default it checks for unconfigured repositories
   and requests an explicit `brain.toml` edit when needed; otherwise it fetches
   allowed remote refs, creates immutable snapshots, updates Core indexes/maps,
   and builds the Semantic generation when the selected edition requires it.
   The single progress surface reports its current phase and safe repository,
   Semantic-card, embedding-cache/new-embedding, batch, shard, generation, and
   elapsed-time counters. Card totals are intentionally indeterminate until the
   real manifest is complete. It never displays source text, absolute paths,
   model credentials, or proxy data.
   Refresh, Semantic rebuild/publication, edition changes, model-pack changes,
   and GC also share an owner-local workspace lock with CLI processes. If a
   different Project Brain process is already performing one of those changes,
   the new operation stops before publishing state; retry after the active
   operation finishes.
3. Select **Core**, **Semantic**, or **Precision**. Semantic requires a verified
   compatible embedding pack and vector backend; Precision additionally requires
   a verified compatible reranker. Installed does not mean indexed or active.
4. Start a ticket from the current Brain snapshot (the default), or explicitly
   select **Check latest code & Refresh before start**. When synchronization is requested in Semantic/Precision,
   Brain verifies that the requested edition is active before it pins ticket
   snapshots: Semantic must be aligned, and Precision must also have its
   verified compatible reranker. If either condition fails, the ticket is not
   started unless you explicitly choose the displayed degraded continuation.
5. Use **Continue with AI** for the complete reply containing a v5
   `INVESTIGATION_REQUEST`. Protocol v4 requests remain accepted only for legacy
   sessions. Retrieval runs as a background ticket job, so a second
   investigation may progress independently. Then inspect Evidence and
   Retrieval transparency.

The page also provides:

- an investigation board for queued, retrieving, waiting, and ready tickets;
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

**Models** accepts only the Project Brain official pack aliases and exposes
install, verify, remove, benchmark, and autotune as explicit local operations.
**Advanced** provides safe diagnostics, freshness, and planner explanation.
Configured/local golden evaluation stays CLI-only (`brain evaluate`) so no
private evaluation data or file paths need to cross a browser surface.

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

Upload the newly printed round-specific file, such as
`generated/handoffs/ABC-1234-context-010.md`, to the dedicated M365 Agent. The
changing filename prevents a stale attachment cache. Brain creates it
automatically, keeps `ABC-1234-current.md` as a stable local alias, and retains
internal history under `.runs/`; you never create or rename a Markdown file.
The direction is deliberate: `.runs/ABC-1234/request-010.yml` is the AI command
sent into Brain, while `ABC-1234-context-010.md` is Brain's evidence sent back to
the AI. Only upload the visible `context-NNN.md` file.

The v1.0.8 performance patch does not change Agent Kit v4 or Investigation
Protocol v5, so an existing M365 Agent does not need to be regenerated. If you
choose to rerun `brain agent-kit m365`, `AGENT_KIT.json` records Brain 1.0.8;
replace the generated files only when you want that metadata refresh.

```bash
brain agent-kit m365 --json
```

## 9. Continue an AI investigation

The preferred command accepts the AI's complete reply:

```bash
brain continue ABC-1234 --clipboard --target claude
```

It deterministically routes the reply:

- `INVESTIGATION_REQUEST` (or legacy `CONTEXT_REQUEST`): validate, retrieve, and deliver local evidence;
- normal conversation: tell the developer to answer the AI directly;
- `FINAL_SOLUTION`: archive the plan and mark the ticket ready to implement.

For M365, use:

```bash
brain continue ABC-1234 --file ai-response.txt --target m365
```

Then upload the newly printed `TICKET-context-NNN.md` handoff. Use the same M365 Agent
conversation so it retains earlier documents, human answers, and evidence. The
`Request: NNN` header and filename must both show the new round. Pass the latest
`context_id` as the next `base_context_id`; normal follow-ups are compact deltas.

The strict `brain ctx` command remains useful for automation when the input is
known to contain an investigation/context request.

If the AI guesses a direct file path that does not exist, Brain records that
operation under `Unresolved`, completes the remaining batched operations, and
still produces the numbered `context-NNN.md`. Unsafe paths remain rejected. A
request number is committed only when its context artifact is also produced.
The generated AI instructions tell the investigator to express one material
unknown through `resolve`, while Brain selects repositories, modules, entities,
graph edges, paths, and literals. Candidate metadata is not evidence until the
returned context contains exact source from the pinned generation.

Old investigations can be removed from **Project overview** with **Delete
history**. The confirmation names the ticket; this deletes only `.runs/TICKET`
and that ticket's generated handoffs. Repositories, branches, and source files
are never deleted.

### `INVESTIGATION_REQUEST` v5 format

The AI should either ask for evidence or return a final solution. The normal
request is objective-first; every optional hint list may be omitted or empty:

```yaml
INVESTIGATION_REQUEST:
  version: 5
  mode: root_cause
  objective: Determine the online eligibility recalculation flow.
  runtime_facts: [The issue is observed only after customer.updated.]
  hypotheses: [The event consumer may bypass EligibilityEvaluator.]
  required: [main execution flow, tests]
  resolve: [Which consumer handles JURISDICTION_CHANGED?]
  anchors:
    - kind: event
      value: JURISDICTION_CHANGED
  base_context_id: CTX-001
  wave: 2
```

This is also valid and does not require a repair round:

```yaml
INVESTIGATION_REQUEST:
  version: 5
  mode: root_cause
  objective: Locate the production flow and tests responsible for this behavior.
  runtime_facts: []
  hypotheses: []
  anchors: []
  wave: 1
```

Brain starts from stored Investigation Memory, the Coverage Map, ticket prefetch,
and generation-validated similar investigations. It routes Repo, Module, and
Entity Cards, expands typed relationships, then performs targeted lexical/path/
semantic fallback and exact source verification. It widens 4/6 → 8/16 → all only
when required coverage is missing. Legacy v1/v2/v3 CONTEXT_REQUEST and v4
INVESTIGATION_REQUEST remain valid for existing conversations; new requests use v5.

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

The preview validates repository names and lists every content search, path
search, symbol operation, file read, and history query. JSON request objects are
accepted as well as fenced YAML inside a complete chat response.

An identical retrieval plan against the same pinned repository snapshots is
rejected. Every returned context reports new evidence regions, previously seen
evidence, recent retrieval objectives, and consecutive no-progress requests. A
no-progress result tells the AI to ask for a specific external/runtime fact or
produce `FINAL_SOLUTION`, rather than continuing open-ended repository searches.
The cumulative **Implementation readiness** section reports whether production
source, tests, configuration, relationships, Git history, and similar tickets
have been seen. It is evidence coverage, not an automatic claim that the change
is safe; the chat AI still decides whether an unknown is blocking.

For a private local before/after replay, keep ticket data on the work machine:

```bash
brain explain TICKET --file request.yml --json
brain continue TICKET --file ai-response.txt --target m365
brain benchmark --json
```

Compare only safe aggregates from `.runs/TICKET/trace-NNN.json`: total/stage
milliseconds, requested/effective/physical operations, routed scope, candidates,
evidence, context size, and stop reason. Do not export the request, source,
repository paths, or ticket text.

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
├── external-001.md
├── external/
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

### Verified filename/path search

```bash
brain paths application.properties
brain paths CachePolicy --repo payment-service
```

Path search walks the same ignored-directory-safe source manifest as repository
search. A matching path is verified before Brain reads it, so the AI does not
need to guess conventional Spring or Maven locations.

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

### Local text and path index

`brain refresh` and `brain index` publish a real SQLite FTS5 trigram search
generation under `state/`. Files are keyed by Git blob SHA, so identical content
is stored and indexed once even when it appears in several snapshots or paths.
The update is one SQLite transaction: readers see the previous complete
generation or the new complete generation, never a partial refresh.

Fixed-string source queries and path queries use this index first. Regex,
missing/stale index, and non-Git working-tree queries retain the `rg`/built-in
scanner fallback. Candidate metadata is merged, ranked, and diversity-limited
before source is read. Run `brain benchmark` (or `brain benchmark --json`) to
inspect locally recorded index/retrieval p50 and p95 latency.

Run `brain benchmark --machine` on each intended target host to persist a
non-identifying local profile (OS, architecture, logical CPUs, and memory only).
It intentionally excludes hostname, serial number, repository paths, and
environment variables. With approved packs installed, `brain model benchmark`
measures embedding batches of 1/8/16 or reranker candidate pools of 10/20/40/80
using only public synthetic cards. `brain model autotune PACK` then stores the
observed recommendation in private Brain state for the exact pack. Run private
ticket replay and final M3 Pro measurements later on that machine; neither is
required to use or validate Core locally.

### Structural backend

Homebrew and standalone packages include the tested `codebase-memory-mcp` v0.10.5
binary and a source-pinned Zoekt command pair. Project Brain calls the structural
backend's local JSON CLI for symbol and call-path queries and Zoekt only for local
immutable shards; it stores both caches under Brain's ignored `state/` directory.
It never runs an installer or changes Claude, Codex, or other agent configuration.
For generation safety, `graph.mode = "eager"` builds structural state during
the authoritative `brain refresh` writer phase. The default `lazy` mode defers
that optional work; retrieval never mutates graph state under its shared
workspace lease. Run `brain index` for an explicit build, or set
`graph.enabled = false` when deterministic lexical analysis is sufficient.

Python-only installs can add that executable to `PATH` or set
`PROJECT_BRAIN_GRAPH_BIN=/absolute/path/to/codebase-memory-mcp`. `brain doctor`
shows which backend is active. If it is missing or fails, exact search and lexical
analysis continue automatically.

### Automatic committed-ticket experience

Every `brain refresh` scans a bounded number of local commits on the selected
source snapshot and extracts ticket identifiers from commit subjects. This is
normally the fresh `origin/develop` snapshot when that branch exists. Ordinary,
squash, and ticket-labelled merge commits are included; merge changes are
attributed against the first parent. Commits with the same ticket are joined
across repositories. Brain records changed paths, tests, configuration,
subjects, and commit SHAs, then ranks similar cases for each new ticket.

```bash
brain experience
brain experience "increase transaction cache duration" --patches
brain evaluate
```

This is retrieval over local Git metadata and patches, not machine learning and
not an upload. `brain evaluate` compares files retrieved during an older Brain
session with files later changed by a commit carrying the same ticket number.
The generated `EXPERIENCE_REPORT.md` reports repository, changed-file, and test
recall plus exact missed paths. Evaluation becomes available after the commit is
visible in the analyzed branch.

Git does not contain the full Jira/GitLab ticket description unless developers
put it in the commit. If a ticket was started in Brain, its local ticket text is
automatically joined to the case once a matching commit appears on the analyzed
snapshot. Add authoritative details for older tickets with the existing
human-maintained memory command:

```bash
brain learn ABC-1234
```

Fill in the generated short template under `knowledge/tickets/`. Future searches
can use its root cause, flow, tests, and gotchas both for similarity ranking and
for the resulting historical evidence.

The normal closed loop is: start and investigate the ticket, implement and test
manually, commit with the same ticket identifier, merge it into the analyzed
branch, then run `brain refresh`. That refresh learns the committed cross-repo
case and regenerates `brain evaluate`; future similar tickets inherit the
observed repo/file/test/config pattern. No model training or source upload occurs.

Historical patch excerpts are disabled by default. `brain experience QUERY
--patches` is an explicit one-time opt-in; configured automatic excerpts are
bounded, skip known key/credential file types, and redact common private-key,
token, password, and secret patterns. This is defense in depth; review a handoff
before moving it outside the project's trust boundary.

### External documents and runtime evidence

```bash
brain evidence ABC-1234 internal-standard.md --kind document --target m365
brain evidence ABC-1234 production.log --kind log --target m365
```

Text evidence is archived under the ticket and automatically included in later
context rounds as explicitly user-supplied evidence. Binary files such as PDFs
are archived locally, but Brain does not claim to parse them; attach the stored
binary directly to M365 Copilot or another AI that supports that format.

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

An optional approved model pack may communicate only with a local loopback
runtime. Pack installation accepts an already-local pack or a one-time,
SHA-256-pinned GitHub Release/configured approved HTTPS source; it validates
archive paths and checksums. Runtime never downloads weights or sends
source/query telemetry to a hosted inference service.

The local cockpit binds only to IPv4 loopback, requires a random per-process token
for every API call, rejects non-local Host headers, limits request bodies, and
blocks framing and external page connections with browser security headers. It
does not expose a generic file endpoint: session artifacts are resolved from an
allowlist and cannot escape `.runs/TICKET/`.

On macOS and Linux, Brain creates its state, session, and generated-handoff
directories with owner-only (`0700`) permissions. Keep any separately exported
handoffs under your organisation's required access controls.

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
warns at the soft target, hydrates source only up to the configured limits, and
lists lower-ranked verified candidates as metadata for explicit follow-up.

### `rg` missing

Indexed fixed-string and path search remains available. The built-in scanner is
used for fallbacks; install ripgrep for faster regex and non-indexed searches.

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

### Model-pack download fails with a certificate error

Project Brain keeps TLS certificate and hostname verification enabled. On macOS
the Homebrew and standalone binaries use the macOS Keychain through
`truststore`; on Linux they use the platform OpenSSL trust store. Run
`brain doctor` to see the active mode without printing certificate contents,
paths, proxy credentials, or environment values.

If `curl` trusts the approved Project Brain GitHub Release through corporate TLS
inspection, a current Project Brain release should do the same. Ask IT to trust
the enterprise root in the operating-system store; do not use `--insecure` or
download a model outside the verified installer. If policy requires an explicit
PEM bundle, use one of these local-only options:

```bash
SSL_CERT_FILE=/approved/path/enterprise-ca.pem brain model install semantic
```

```toml
[models]
ca_bundle = "/approved/path/enterprise-ca.pem"
```

The configured PEM is added to the download trust context and its path or
contents are not reported by `brain doctor`. Project Brain still enforces the
approved redirect hosts plus descriptor, release-part, and assembled-model
SHA-256 checks. A missing, invalid, untrusted, or hostname-mismatched
certificate fails closed.

### Pack verification starts but cannot reach local runtime

Corporate proxy configuration applies to the one-time HTTPS download, but not
to Project Brain's own verified pack runtime. A managed pack is pinned to a
short-lived `127.0.0.1` `llama.cpp` process and all its health, embedding, and
reranking requests use dedicated direct loopback transport. This avoids the
common policy where `localhost` bypasses a proxy but `127.0.0.1` does not; do
not disable TLS or change global proxy settings, and `NO_PROXY` is not required.

Run `brain doctor` to confirm that direct loopback enforcement is active. It
reports only whether external proxy configuration is present, never proxy URLs,
credentials, certificate material, or environment values. A startup error also
distinguishes an executable that failed to start, one that exited, an alive
runtime with an unavailable health endpoint, and a loopback transport failure.
GitHub release descriptor and artifact downloads continue to use configured
enterprise proxy policy plus the verified system-trust and SHA-256 checks above.

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

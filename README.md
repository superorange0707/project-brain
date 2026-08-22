<div align="center">

# 🧠 Project Brain

**Your company gave you a chatbot, not a coding agent.**

Project Brain brings coding-agent-style codebase investigation to the chat AI
your organization already allows.

Use ChatGPT, Claude, M365 Copilot, or another chat AI to investigate your local
multi-repo codebase — with relevant source, callers, tests, config, Git history,
and cross-repo relationships retrieved locally and read-only.

Turn a ticket into verified, up-to-date multi-repository source context—without
cloud indexing, API keys, or giving an agent permission to edit your code.

[![CI](https://github.com/superorange0707/project-brain/actions/workflows/ci.yml/badge.svg)](https://github.com/superorange0707/project-brain/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Small runtime footprint](https://img.shields.io/badge/runtime_dependencies-1-2ea44f)](pyproject.toml)
[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

[Quick start](#quick-start) · [Local cockpit](#local-investigation-cockpit) · [How it works](#how-it-works) · [User guide](docs/USER_GUIDE.md) · [Security](SECURITY.md)

</div>

---

Project Brain fills the missing tool loop between a normal chat window and your
local repositories:

```text
Ticket + docs ↔ Chat AI ↔ you
                 │
                 ├── human/runtime question → answer directly in the chat
                 ├── CONTEXT_REQUEST → Project Brain → source evidence
                 └── enough evidence → FINAL_SOLUTION
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
| 🤖 **Three offline editions** | Core needs no model; optional audited local Semantic and Precision packs stay on-device |
| 🖥️ **Local investigation cockpit** | Paste tickets and AI replies, preview every operation, inspect evidence, and copy results |
| 📚 **Evidence-complete** | Retrieves production source, tests, config, relationships, and history |
| 🧠 **Structural + exact** | Pinned code graph when bundled; deterministic exact/lexical fallback everywhere |
| 🔍 **Honest uncertainty** | Missing evidence and static-analysis limits are reported, never hidden |
| 🎯 **Convergent investigation** | Rejects duplicate retrievals and reports new evidence, prior objectives, and no-progress turns |
| 🧩 **Local ticket experience** | Learns reusable repo/file/test patterns from ticket-labelled Git commits—without a model or upload |
| 🧑‍💻 **Persistent M365 Agent** | Generates permanent Instructions and Knowledge so Copilot always knows when to use Brain or ask you directly |

Core requires no vector database, hosted indexing service, model/API credential,
or cloud service. Optional approved packs are installed from a local path or a
hash-pinned approved release, checksum-verified, and may only use a loopback
runtime; Project Brain never downloads weights while it runs.

One-time model-pack downloads use the operating system's verified trust policy:
the macOS Keychain through `truststore`, and platform OpenSSL trust on Linux.
This includes enterprise roots already trusted by the operating system; TLS and
hostname verification stay enabled. See [corporate TLS troubleshooting](docs/USER_GUIDE.md#model-pack-download-fails-with-a-certificate-error) for the
safe `SSL_CERT_FILE` and `models.ca_bundle` fallback options.

The first Core-catalogued [Semantic pack v1.0.6](https://github.com/superorange0707/project-brain/releases/tag/semantic-pack-v1.0.6)
is separately released for Apple Silicon. It uses the unchanged official
Qwen3-Embedding-4B Q6_K GGUF, is installed only from a Project Brain-controlled
GitHub Release descriptor (SHA-256
`cbd09af575fb1b2e036abc17ed3e693e5bab4807af19efd2c1a9b5cd75ae8afc`),
and remains entirely offline once installed. It is not bundled into Core or
Homebrew.

An official pack enters a Core catalog only after its exact descriptor, release
parts, reconstructed model, provenance, and clean temporary installation have
all been verified. `brain model install semantic` then verifies that descriptor,
the release parts, and the reassembled model before registering the pack.

The separately released [Precision pack v1.0.2](https://github.com/superorange0707/project-brain/releases/tag/precision-pack-v1.0.2)
converts the exact official Qwen3-Reranker-4B source with a pinned local
`llama.cpp` toolchain and compares public/synthetic results with Qwen's local
Transformers reference. Its immutable descriptor SHA-256 is
`9070626e90b0306237bdf208ce0991cbf3804ee1bbee4ddca28c93df288f7df7`.
It is never bundled into Core or Homebrew; after this Core catalog release,
`brain model install precision` resolves only that pinned release.

See [offline model-pack operations](docs/MODEL_PACKS.md) for the audited pack
format, official-Qwen conversion provenance, local conformance, controlled
installation, machine measurement, and autotuning workflow. See
[release distribution](docs/RELEASING.md) for the final-checksum Homebrew and
separate model-pack publishing gates.

## Install

### Homebrew — recommended on macOS

```bash
brew install superorange0707/tap/project-brain
```

Upgrade an existing installation through the same tap:

```bash
brew update
brew upgrade superorange0707/tap/project-brain
```

The Homebrew package uses a prebuilt release: it does not compile Python or
require Xcode. It includes the tested structural backend and pinned Zoekt search
commands plus the local USearch vector backend required by an installed Semantic
model pack. The small,
separate [Homebrew tap](https://github.com/superorange0707/homebrew-tap) is
Homebrew's standard index for third-party formulae; the application source stays
in this repository.

After a GitHub Release has published its final assets and checksums, release
automation renders and commits the matching formula only when the repository
has a scoped `HOMEBREW_TAP_TOKEN` secret. A failed or incomplete release cannot
update the tap.

### Standalone archive — macOS or Linux

Download the archive for your CPU from the
[latest release](https://github.com/superorange0707/project-brain/releases/latest),
extract it, and place all four executables on `PATH`:

```bash
tar -xzf project-brain-v0.6.5-macos-arm64.tar.gz
mkdir -p ~/.local/bin
install brain codebase-memory-mcp zoekt zoekt-index ~/.local/bin/
```

The Python installs below require Python 3.11+ and use the exact/lexical fallback
unless `codebase-memory-mcp` is also present on `PATH`.

### uv tool (recommended)

```bash
uv tool install "project-brain-context @ git+https://github.com/superorange0707/project-brain.git@v0.6.5"
```

### pipx

```bash
pipx install "project-brain-context @ git+https://github.com/superorange0707/project-brain.git@v0.6.5"
```

### pip / release wheel

```bash
python -m pip install https://github.com/superorange0707/project-brain/releases/download/v0.6.5/project_brain_context-0.6.5-py3-none-any.whl
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

The same refresh incrementally indexes ticket identifiers found in ordinary,
squash, and merge commit subjects on the selected snapshot (normally
`origin/develop`). The same ticket is joined across repositories, giving future
tickets local examples of files, tests, configuration, and patch patterns that
changed together. This is deterministic retrieval from Git—not model training—
and no history leaves the machine.

Git can provide the ticket identifier, commit subject, changed files, and diff;
it cannot reconstruct a Jira/GitLab description that was never committed. Add
authoritative old ticket details with `brain learn TICKET` when they materially
improve future investigations. For tickets already investigated through Brain,
the saved `.runs/TICKET/ticket.md` description automatically enriches that case
after a matching commit appears. `brain evaluate` later compares retrieved paths
with the files actually committed under that ticket number.

The feedback loop is deliberately simple and auditable:

```text
brain start IPF-123  → saved ticket + retrieved evidence
          ↓ you implement, test, and commit with IPF-123
brain refresh        → cross-repo committed case is learned locally
          ↓
brain evaluate       → retrieval recall and missed changed paths
          ↓
next ticket          → matching repos/files/tests/config become prior evidence
```

Brain never edits or commits the target repositories. It learns only from the
selected local Git snapshots and ignored local Brain artifacts.

Clone another Git repository anywhere below this project root and the next
`brain refresh` automatically appends it to `brain.toml`, fetches it, snapshots
it, and rebuilds cross-repository intelligence. Existing repository metadata and
custom settings are preserved. Use `--no-discover` only for a deliberately fixed
workspace.

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
context to your chat AI. Paste a complete reply into **Continue with AI**. Brain
routes repository requests, tells you when to answer the AI directly, recognizes a
final plan, and previews every local operation before it runs.
Old ticket history can be deleted from **Project overview** with an explicit
confirmation; repositories and branches are never touched.

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
generated start context into the chat. Route any complete AI reply with:

```bash
brain continue ABC-1234 --clipboard --target claude
```

If it contains `CONTEXT_REQUEST`, Brain retrieves and returns evidence. If the AI
asked a human question, Brain tells you to answer it directly. If it contains
`FINAL_SOLUTION`, Brain marks the ticket ready to implement. The strict legacy
`brain ctx` command remains available for automation.

### Microsoft 365 Copilot Agent — recommended

Generate a one-time setup kit:

```bash
brain agent-kit m365
```

Paste `generated/m365-agent/INSTRUCTIONS.md` into the Agent Builder Instructions
field, add the entries from `SUGGESTED_PROMPTS.md`, and add
`PROJECT_KNOWLEDGE.md` plus approved internal architecture/IPF docs to Knowledge.
The AI then permanently knows that it should talk to you directly
for business, document, runtime, and environment facts, and emit a
`CONTEXT_REQUEST` only for local repository evidence.

Start every M365 ticket with `--target m365`. Upload the newly printed
round-specific file each time, for example:

```text
generated/handoffs/ABC-1234-context-010.md
```

The changing filename prevents M365 from reusing a cached attachment. Upload the
newly printed `context-NNN` file, not the internal `request-NNN.yml`. Brain also
maintains `ABC-1234-current.md` as a stable local alias; you never create or
rename any of these files yourself.

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
- **Continue with AI** distinguishes direct conversation, repository requests,
  duplicate retrievals, and final solutions without invoking another model.
- **Retrieval Plan** validates repository names and previews exactly what Brain
  will inspect; it is deliberately separate from the AI's implementation plan.
- **Evidence context** provides chunk navigation and one-click clipboard delivery.
- **M365 Agent setup** creates and previews the permanent Agent Builder package.
- **Review changes** packages tracked diffs, developer notes, and observed test output; it never runs the command or claims success itself.
- **Investigation history** reopens ticket, request, context, and feedback artifacts under `.runs/TICKET/`.

There is no hosted model, automatic code editor, or autonomous coding loop.
Core has no model requirement; optional audited offline Semantic and Precision
packs only discover or reorder candidates, while pinned-snapshot verification
remains the source of evidence.

## A context request

```yaml
CONTEXT_REQUEST:
  version: 1
  objective: >
    Determine how jurisdiction changes reach trading eligibility recalculation.

  searches:
    - query: "JURISDICTION_CHANGED"
      repos: []

  paths:
    - query: "application.properties"
      repos: [trading-service]

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
unresolved section. It also reports unique evidence gained, prior retrieval
objectives, and no-progress turns. An identical plan against the same pinned
snapshots is rejected instead of wasting another round.

`files:` is for paths already verified by prior evidence. If the exact location
is unknown, the AI uses `paths:` for filename/path fragments or `searches:` for
configuration keys and source literals instead of guessing.

## What it can explore

- Safe fetch plus exact, immutable remote-commit snapshots
- Incremental, blob-deduplicated SQLite trigram content and path indexing
- Cross-repository literal and regular-expression search
- Verified filename and repository-relative path search
- Classes, interfaces, methods, functions, and lexical symbol fallback
- Interface implementations and inheritance
- Static callers and likely outbound calls
- Related unit and integration tests
- Direct file and line-range retrieval
- Ranked late source hydration with compact manifests for deferred candidates
- Candidate expansion (`expand: [C12]`) without repeating broad retrieval
- Atomic SQLite catalog generations, pinned sessions, trace JSON, storage status,
  and safe generation GC
- Optional local semantic cards/vector shards and local reranking contracts;
  neither can become evidence without exact snapshot verification
- Git pickaxe/history and optional working-tree diffs
- Human project maps, glossaries, flows, and solved-ticket memory
- Cross-repository ticket-labelled Git experience, historical patches, and
  retrieval-versus-commit evaluation
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

Project Brain uses a local SQLite trigram generation for fixed-string and path
candidates, and `codebase-memory-mcp` for tree-sitter/Hybrid-LSP structural
queries in bundled releases. Cross-repository framework wiring remains
deterministic. If an index or structural backend is absent, stale, or fails,
`rg` or the standard-library scanner remains available. Every heuristic is
labelled; this is useful static evidence, not a claim to compiler-perfect
runtime behavior.

## Commands

```text
brain init              Discover, sync, index, and map a project root
brain demo              Create a safe four-repository example investigation
brain sync              Safely fetch all repos and rebuild remote snapshots
brain doctor            Check config, repositories, git, rg, and freshness
brain status            Show project health and ticket sessions (optionally JSON)
brain freshness         Compare source snapshots with the current index generation
brain storage           Show local state usage and catalog health
brain gc --dry-run      Preview old, unpinned generation cleanup
brain refresh           Discover new repos, sync, and regenerate intelligence
brain index status      Show atomic generation and index freshness
brain index rebuild     Rebuild lexical, semantic, or all local indexes
brain search --explain  Search and show the deterministic query plan
brain paths --explain   Find verified paths and show the backend plan
brain explain           Compile a CONTEXT_REQUEST without searching
brain symbol            Resolve symbol definitions with lexical fallback
brain trace             Find static callers and likely outbound calls
brain history           Search Git history
brain experience        Inspect or rebuild local ticket-labelled Git experience
brain evaluate          Compare past retrievals with later commits or a local golden suite
brain benchmark --machine Record a non-identifying local benchmark target profile
brain edition           Inspect or set core, semantic, or precision capability profile
brain capabilities      Show installed offline capabilities
brain model             Install, verify, benchmark, autotune, or remove local model packs
brain watch             Refresh selected snapshots at a fixed interval
brain map               Generate Spring/Maven/project facts
brain start             Start a ticket investigation
brain continue          Route a complete AI reply and run only tool requests
brain preview           Classify an AI reply and dry-run repository requests
brain ctx               Fulfil a CONTEXT_REQUEST
brain agent-kit m365    Generate permanent M365 Agent setup files
brain evidence          Add an explicit document, log, note, or runtime artifact
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
policy. Optional model packs may call only a separately supplied loopback
runtime—never a hosted model/API. Managed llama.cpp packs recheck local artifact
hashes, bind only `127.0.0.1`, disable their Web UI, use an ephemeral API key,
and shut down at the end of the operation. Generated context can still contain proprietary
source or secrets already present in a repo. Review context before pasting or
uploading it outside your organization.

`brain ui` binds to IPv4 loopback only, uses a random per-process API token,
rejects non-local Host headers, limits request bodies, sends a restrictive browser
security policy, and serves no repository file directly. Closing the process
invalidates the URL immediately.

Generated source evidence, local repository paths, configs, keys, `.env` files,
knowledge, and ticket runs are ignored by the tool repository's default
`.gitignore`. Releases use GitHub/PyPI short-lived OIDC credentials—no publishing
token belongs in this repository.

The prebuilt archive includes the pinned open-source structural backend and
Zoekt command pair; see [third-party notices](THIRD_PARTY.md).

Historical patch excerpts are off by default. The explicit `brain experience
QUERY --patches` mode skips known credential/key file types and redacts common
secret patterns. Redaction is defense in depth, not a guarantee; review generated
handoffs before sending them outside the repository's trust boundary.

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

# Project Brain milestone readiness report

**Snapshot:** 2026-08-27 · **v0.8.0 implementation gate:** complete locally; target-machine private replay remains · **Published Core release at this snapshot:** 0.6.7

This report separates completed, locally tested engineering from validations
that must run later on the target machine or private local data. It contains no
ticket text, source content, model weights, credentials, or private benchmark
paths. It records only claims supported by local/public-synthetic verification.

## Status vocabulary

`COMPLETE` means the repository contains the capability and public/synthetic
coverage. `DEFERRED TO TARGET MACHINE` and `DEFERRED TO PRIVATE LOCAL DATA` are
intentional local-only validation steps, not engineering blockers.

## Completed delivery slices

| Milestone | Status | Delivered and locally covered |
| --- | --- | --- |
| M00 | COMPLETE | Retrieval traces, sanitized golden replay fixtures with Recall@5/10/20, test recall, MRR/nDCG/precision, context/duplicate/latency/memory/semantic-only diagnostics, `brain benchmark`, non-identifying `brain benchmark --machine`, and local metric storage. |
| M01–M03 | COMPLETE | Typed requests/planner/ranker seams, candidate-first ranking, duplicate/interval merging, exact verification, diversity limits, strict hydration budgets, backend timings, and expansion manifests. |
| M04–M06 | COMPLETE | SQLite catalog migration, immutable generation pointer, Git-blob-aware updates, selected-ref freshness, pinned sessions, refresh fallback, and source-pinned Zoekt shard/query smoke coverage. |
| M07–M08 | COMPLETE | Persisted structural/history/relationship reuse, progressive cost-based plans, deterministic feature ranking, reciprocal-rank fusion, and explainable plans. |
| M09–M12 | COMPLETE | Deterministic structural semantic cards, snapshot-filtered USearch shards, content-addressed bounded-input embedding cache, verified-pack selection, exact UTF-8/JSON request limits, deterministic code-only truncation, adaptive managed-runtime restart/smaller-batch recovery, atomic semantic-generation publication, unchanged-generation reuse, and Core fallback. |
| M10–M14 | COMPLETE | Local-only llama.cpp adapter, checksum/provenance manifest, pack-owned loopback lifecycle, conformance gates for official-reference vectors/order/long input/batch parity, bounded protected reranking, 10/20/40/80 public-synthetic reranker benchmarks, per-pack autotuning, and Semantic/Precision-to-Core fallback. |
| M15 | COMPLETE | One Core codebase with capability profiles, schema incompatibility recovery guidance, controlled local/GitHub-Release/approved-internal pack installation, wheel/sdist build, notices, source-pinned Zoekt release workflow, and a descriptor-pinned official Semantic-pack catalog entry. |
| M16 | COMPLETE | Status/freshness/storage/GC/watch/benchmark/explain commands, pinned-artifact GC protection, pre-write disk guard, local machine-profile capture, and machine-readable model tuning profile. |
| M17 | COMPLETE | Sensitive-path exclusion, owner-only Brain state/session/output directories on POSIX, loopback-only UI/runtime boundaries, checksum/path-traversal protection, catalog/vector corruption fallback, stale-session protection, low-disk fault injection, native system-CA model-download trust with fail-closed hostname/certificate validation, and direct no-proxy transport for verified pack-owned `127.0.0.1` llama.cpp calls. |
| M18 | COMPLETE | One shared CLI/UI full-refresh operation, snapshot-aware Semantic alignment status, explicit requested-Edition validation before UI ticket pinning (including Precision reranker availability), loopback operations dashboard, official-pack-only UI controls, bounded local operation jobs with single-writer rejection, and a re-entrant cross-process workspace lock for refresh/Semantic publication/edition/model/GC mutations. Structured source-free refresh progress comes from the real Semantic manifest/cache/batch/shard loop, with safe diagnostics, planner explanation, and retrieval transparency. |
| v0.8 M00–M10 | COMPLETE | Trace schema v2 and stage accounting; objective-first CONTEXT_REQUEST v3; deterministic repo routing/widening; operation fusion and request-local memoization; physical/effective/candidate budgets; bounded shared repository and Semantic shard parallelism; one model lane; shared-workspace/per-ticket locks; two-ticket UI jobs and investigation board; M365 kit v2/protocol v3; 50-repository synthetic fan-out and concurrency coverage. Exact pinned-source hydration, v1/v2, Core fallback, and workspace mutation exclusion remain intact. |
| v0.8 M11 | COMPLETE | Opt-in Auto Refresh: When idle; read-only selected-ref/Core/Semantic/repository-discovery checks shared with `brain watch`; debounce and one coalesced authoritative refresh after retrieval idle; bounded cooldown/backoff; non-refreshable Action Required latching; pinned-session preservation; and source-free local status/preference persistence. |

## Completed local verification for this snapshot

The full local regression suite (141 public/synthetic tests for this candidate) covers deterministic retrieval, generations,
incremental indexes, semantic/reranker failure fallback, corrupt local state,
pack tampering, production-manifest provenance, public synthetic conformance,
machine-profile privacy, low-disk preflight, UI loopback protection, release
artifact contents, and an enterprise-proxy simulation where numeric loopback
health, embedding, and reranking calls bypass a fake proxy while remote model
downloads retain their standard proxy-eligible transport. The v0.7.0 UI tests
also cover CLI/UI shared refresh delegation, structured progress ordering,
Semantic manifest/card/cache/batch/shard counters, cold rebuild and identical
generation reuse, no false completion after a Semantic failure, progress-payload
sanitization, monotonically advancing UI polling, start-with-sync alignment
refusal, explicit edition validation, model-operation error safety, bounded job
progress, in-process overlapping-write rejection, and cross-process workspace
operation rejection/release for refresh, Semantic rebuild, edition transition,
model removal, and GC.

The current macOS ARM64 development host also builds the pinned Zoekt commands
from source, indexes the demo corpus, and confirms that a literal hit comes from
the Zoekt backend rather than SQLite/ripgrep fallback. Local pack measurements
are deliberately public/synthetic, and never claim private-repository relevance.

Run:

```bash
.venv/bin/python -m unittest discover -s tests -v
uv build
brain benchmark --machine
brain model benchmark PACK
brain model autotune PACK --latency-budget-ms 3000
```

## Deferred validation and authorization boundaries

| Item | Status | What remains |
| --- | --- | --- |
| Official Qwen3-Reranker-4B Q6_K Precision pack | COMPLETE | Public `precision-pack-v1.0.2` contains the official-source-derived Q6_K GGUF (SHA-256 `2fd4a7bbb61400e65bb3849f8d367759232be2206e1bb467b2b3d7ff42e79aeb`), Apache-2.0 notices, provenance, pinned local runtime, and public/synthetic official-reference conformance. Its descriptor SHA-256 is `9070626e90b0306237bdf208ce0991cbf3804ee1bbee4ddca28c93df288f7df7`; Core `v0.6.6` pins the catalog entry. |
| Official Qwen3-Embedding-4B Q6_K Semantic pack | COMPLETE | Public `semantic-pack-v1.0.6` contains the unchanged official Q6_K GGUF, provenance, notices, static local runtime, checksums, and public/synthetic conformance. A clean temporary installation verified the published descriptor, parts, assembled GGUF, runtime conformance, and a 583-card persistent USearch refresh. Core `v0.6.6` pins descriptor SHA-256 `cbd09af575fb1b2e036abc17ed3e693e5bab4807af19efd2c1a9b5cd75ae8afc`. |
| Enterprise model-download TLS compatibility | COMPLETE | Core `v0.6.5` packages `truststore`, uses native system trust for model downloads, preserves certificate/hostname validation and SHA-256 gates, supports additive `models.ca_bundle`/`SSL_CERT_FILE`, and reports only safe trust-mode diagnostics. Public release-descriptor, clean wheel, standalone, and Homebrew smoke checks passed. |
| Pack-owned runtime proxy compatibility | COMPLETE | Core `v0.6.6` restricts direct no-proxy HTTP transport to verified Project Brain-managed `127.0.0.1` llama.cpp processes. Public/synthetic fake-proxy coverage plus clean-wheel and Homebrew Semantic/Precision conformance smoke tests with an unusable proxy and empty `NO_PROXY` confirm health, embedding, and rerank calls do not use the proxy, while one-time remote pack downloads remain standard proxy-eligible system-trust requests. Target-machine validation requires only installing the released Core update; existing packs need not be reinstalled. |
| Semantic refresh workload robustness | COMPLETE | Core `v0.6.7` bounds each complete embedding input and exact UTF-8 JSON body, preserves structural identity while trimming code, retries transport-disconnected batches with deterministic reduction, commits only successful content-addressed cache entries, and atomically switches semantic shards/state only after a complete build. Public/synthetic conformance covers multilingual/escaped cards, instructions/suffixes, tuned 16-item ceilings, 16→8→4→2→1 degradation, prior-generation retention, and identical-refresh reuse. |
| Project Brain v0.7.0 UI parity / operations cockpit | COMPLETE | Release-gate candidate. UI and CLI share the same Core+Semantic refresh operation; UI start-with-sync refuses to pin a requested Semantic/Precision edition that is not active (unaligned Semantic state or unavailable verified Precision reranker) absent an explicit degraded choice. Refresh progress is structured, source-free, and emitted by the actual Semantic manifest/cache/batch/shard loop; dashboard/models/advanced views reuse existing status, capability, model, benchmark, autotune, doctor, and planner services. No model-pack protocol, Semantic schema, TLS/proxy/runtime boundary, source-write capability, tag, GitHub Release, Homebrew tap, deployment, or publication action changed. |
| Public GitHub v0.6.7 Core release | COMPLETE | Published from commit `50cde719af8af4d95a49942490d0fe3f539bfd58` under annotated tag object `59fbbe7d7588a4e9e8017031d403b4e5ff8b20db`. GitHub Actions run `32606349713` passed the public/synthetic distribution suite and all macOS/Linux ARM/AMD standalone builds. The published `SHA256SUMS.txt` (SHA-256 `fc3fca243be51c61b3fa4f3385e8618cd262c419d226d337dcb9e5b404edefea`) was independently checked against the wheel, sdist, and four standalone assets before the official tap was changed. Tap commit `89a74829624c4956049f02a9667bbd9a7f128de4` was rendered exclusively from those published values; strict audit, a real 0.6.6→0.6.7 upgrade, and formula test passed. |
| Company model approval | EXTERNAL POLICY DECISION | The organization chooses which official-source artifact is approved. Project Brain has no bypass mechanism and remains useful as Core without it. |
| Apple M3 Pro / 36 GB measurements | DEFERRED TO TARGET MACHINE | Run the supplied local commands to record embedding p50/p95, batch throughput, 10/20/40/80 rerank latency, retrieval timing, and process/child peak memory. No unverified M3 numbers are claimed here. |
| Linux x86_64 measurements | DEFERRED TO TARGET MACHINE | Run the same local benchmark commands on the selected Linux host. |
| Time-split ticket replay and real enterprise Recall/MRR/nDCG | DEFERRED TO PRIVATE LOCAL DATA | Use the existing local golden/replay and historical Git evaluation infrastructure with private labels retained on the work machine. No private corpus is requested or committed. |
| Public GitHub v0.6.1 release and Homebrew upgrade | COMPLETE | v0.6.1 is published. The official tap points at its final four-platform SHA-256 artifacts; `brew update`, a real 0.6.0→0.6.1 upgrade, formula test, and strict online audit have passed. |
| Public GitHub v0.6.5 Core release | COMPLETE | The public GitHub Release contains final checksum-published wheel/sdist and four standalone assets. GitHub Actions passed Python 3.11–3.14 with the Semantic extra and all four standalone build gates. The official tap was rendered from its published SHA256SUMS, strictly audited, and a real 0.6.4→0.6.5 Homebrew upgrade plus formula test passed. |
| Public GitHub v0.6.6 Core release | COMPLETE | `v0.6.6` is published from commit `429e9214bb0343a0356fd8bf3360883f6ae0c8e0` with final GitHub release SHA256SUMS for wheel, sdist, and four standalone assets. GitHub Actions passed the public/synthetic distribution suite and all macOS/Linux ARM/AMD standalone builds. The official tap commit `806f1d3` was rendered exclusively from that published SHA256SUMS; strict audit, a real 0.6.5→0.6.6 upgrade, and formula test passed. |

## Publication state audit

- `v0.6.7` is the current published Core release. Its official Homebrew formula
  was rendered only from the final GitHub Release SHA-256 values after all
  distribution and four-platform standalone jobs had passed. `brew update`,
  strict audit, a real 0.6.6→0.6.7 upgrade, and formula test passed. The
  release workflow's tap job safely skipped its write phase because no tap
  token was configured; the authorized manual tap commit was made only after
  independently verifying the published assets. `v0.6.6` remains the prior
  published release, and the `v0.6.2` tag was never released: its
  CI/release-workflow defects were discovered before any artifact or tap
  mutation.
- At this snapshot, `v0.8.0` has completed its local implementation gate but has no tag,
  GitHub Release, Homebrew/tap change, deployed artifact, or publication
  action. The v0.6.7 model-pack releases and public Core artifacts remain
  untouched.
- The local development host is Apple Silicon with 32 GB memory, not the stated
  M3 Pro / 36 GB target. Its measurements are development evidence only.
- The Semantic pack's packaging download is a release-engineering operation
  against fixed official upstream revisions. Installed target machines never
  need Hugging Face or hosted model inference.
- Precision-pack publication is intentionally independent from the target M3
  and private-company data. Only the release artifact and its public synthetic
  conformance gate are required before the controlled `precision` catalog alias
  is added.

## Product boundary reaffirmed

Project Brain remains a read-only local code-intelligence bridge for enterprise
chat products. Core requires no model, hosted index, API key, or cloud service.
Optional offline Semantic and Precision packs can discover or reorder a bounded
candidate set only; final source evidence is re-read and verified from the
investigation's pinned Git snapshot. Project Brain does not edit repositories,
run autonomous implementation loops, or act as a coding-agent framework.

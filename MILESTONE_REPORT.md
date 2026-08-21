# Project Brain milestone readiness report

**Snapshot:** 2026-08-21 · **Local build candidate:** 0.6.1

This report separates completed, locally tested engineering from validations
that must run later on the target machine or private local data. It contains no
ticket text, source content, model weights, credentials, or private benchmark
paths. This report is part of the 0.6.1 release commit and records only claims
supported by local/public-synthetic verification.

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
| M09–M12 | COMPLETE | Deterministic structural semantic cards, snapshot-filtered USearch shards, content-addressed embedding cache, verified-pack selection, batch-bounded embedding, and Core fallback. |
| M10–M14 | COMPLETE | Local-only llama.cpp adapter, checksum/provenance manifest, pack-owned loopback lifecycle, conformance gates for official-reference vectors/order/long input/batch parity, bounded protected reranking, 10/20/40/80 public-synthetic reranker benchmarks, per-pack autotuning, and Semantic/Precision-to-Core fallback. |
| M15 | COMPLETE | One Core codebase with capability profiles, schema incompatibility recovery guidance, controlled local/GitHub-Release/approved-internal pack installation, wheel/sdist build, notices, and a source-pinned Zoekt release workflow. |
| M16 | COMPLETE | Status/freshness/storage/GC/watch/benchmark/explain commands, pinned-artifact GC protection, pre-write disk guard, local machine-profile capture, and machine-readable model tuning profile. |
| M17 | COMPLETE | Sensitive-path exclusion, owner-only Brain state/session/output directories on POSIX, loopback-only UI/runtime boundaries, checksum/path-traversal protection, catalog/vector corruption fallback, stale-session protection, and low-disk fault injection. |

## Completed local verification for this snapshot

The full local regression suite covers deterministic retrieval, generations,
incremental indexes, semantic/reranker failure fallback, corrupt local state,
pack tampering, production-manifest provenance, public synthetic conformance,
machine-profile privacy, low-disk preflight, UI loopback protection, and release
artifact contents.

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
| Qwen3-Embedding-4B and Qwen3-Reranker-4B internal pack artifact | DEFERRED TO TARGET MACHINE | Install an organization-approved, checksummed pack locally; run the included verifier, conformance suite, benchmark, and autotune. The pack/runtime interfaces and conversion documentation are complete. |
| Company model approval | EXTERNAL POLICY DECISION | The organization chooses which official-source artifact is approved. Project Brain has no bypass mechanism and remains useful as Core without it. |
| Apple M3 Pro / 36 GB measurements | DEFERRED TO TARGET MACHINE | Run the supplied local commands to record embedding p50/p95, batch throughput, 10/20/40/80 rerank latency, retrieval timing, and process/child peak memory. No unverified M3 numbers are claimed here. |
| Linux x86_64 measurements | DEFERRED TO TARGET MACHINE | Run the same local benchmark commands on the selected Linux host. |
| Time-split ticket replay and real enterprise Recall/MRR/nDCG | DEFERRED TO PRIVATE LOCAL DATA | Use the existing local golden/replay and historical Git evaluation infrastructure with private labels retained on the work machine. No private corpus is requested or committed. |
| Public GitHub release | IN PROGRESS | User authorization has been received. Only this clean commit may be tagged; its tag push triggers the release workflow after the verification steps recorded below pass. |

## Publication state audit

- At commit creation, the public latest release is `v0.6.0` (2026-08-17). It is
  evidence only for that older source/archive; `v0.6.1` is published solely from
  the clean, verified tag created from this commit.
- The local development host is Apple Silicon with 32 GB memory, not the stated
  M3 Pro / 36 GB target. Its measurements are development evidence only.
- No model download, model-pack installation, or external model inference is
  part of this release process.

## Product boundary reaffirmed

Project Brain remains a read-only local code-intelligence bridge for enterprise
chat products. Core requires no model, hosted index, API key, or cloud service.
Optional offline Semantic and Precision packs can discover or reorder a bounded
candidate set only; final source evidence is re-read and verified from the
investigation's pinned Git snapshot. Project Brain does not edit repositories,
run autonomous implementation loops, or act as a coding-agent framework.

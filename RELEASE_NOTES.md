# Project Brain v0.7.0

Project Brain v0.7.0 upgrades `brain ui` into the normal local operations
cockpit while keeping CLI commands available for automation and advanced
workflows.

- CLI and UI now share one full-refresh operation: repository discovery,
  allowed fetches, immutable snapshots, Core indexes, maps, relationships,
  experience/graph state, and Semantic indexing when Semantic or Precision is
  selected.
- The UI reports Core, Semantic, Precision, model-pack, freshness, and managed
  runtime state explicitly. A Semantic pack being installed does not imply it
  is indexed or active.
- A synchronized UI ticket start verifies that the requested Semantic or
  Precision edition is actually active before it pins the investigation. If
  snapshot alignment fails or Precision lacks a verified compatible reranker,
  the ticket does not start unless the user explicitly chooses the visible
  degraded path.
- Local refresh/model/edition work uses bounded status-tracked jobs. A small
  re-entrant workspace lock now extends the UI's in-process single-writer rule
  across CLI and UI processes for refresh, Semantic publication, edition,
  model, and GC mutations. A concurrent operation fails safely before it can
  publish state; errors expose no source contents, credentials, proxy data, or
  certificate material.
- Refresh jobs now retain structured, source-free progress from the real
  Semantic indexing loop: repository/card/cache/new-embedding/batch/shard
  counters, generation reuse or rebuild, and elapsed time. The CLI renders the
  same events as concise progress lines; no second refresh implementation was
  introduced.
- Retrieval results record requested/effective edition, local semantic/reranker
  participation, candidate/evidence counts, pinned generation, and safe timing
  metadata for UI inspection.

The v0.6.6/v0.6.7 TLS, proxy, checksum, loopback-runtime, atomic-Semantic, and
read-only product boundaries are unchanged. This release ships no model weights
and introduces no coding-agent, shell, source-editing, or cloud behavior.

# MPS retrieval and flow integration contract

This implemented contract covers MPS repository ingestion, source retrieval,
multi-root connections, forward jumps and reverse impact as one evidence-backed
ticket workflow. The user authorized continuing this concrete integration on
2026-10-04 after the approval boundary was explained. Implementation and its
acceptance checks cover the repository pipeline as well as attachment navigation.

## Immutable source projection

The existing lexical memberships are keyed by `(repo, Git SHA, path)`. Adding
MPS paths to an already indexed SHA would change the membership proof used by
older tickets. Preserve those memberships without alteration.

Add a generation-scoped `mps_models` component backed by an immutable local
artifact. Its identity includes:

```json
{
  "schema_version": "mps-models-v1",
  "parser_version": "mps-v9-v2",
  "snapshots": {"configured-repo": "original Git SHA or sealed export identity"},
  "source_manifest_hash": "hash of sorted repo/path/blob identities",
  "mapping_hash": "hash of project-declared flow mappings, or explicit absence",
  "content_hash": "hash of the bounded source payload, parser profile, discovery metadata and limitations"
}
```

The ordinary generation snapshots continue to hold real Git SHAs. Existing
lexical SQLite rows and old generation components remain unchanged. The new
component is optional on older generations; its absence is a declared
capability limitation, never permission to read a newer working tree.
Discovery profile v2 adds bounded property/compound candidates to the same
context retrieval entrance. Profile v1 components remain readable by pinned
tickets; a refresh publishes v2 for new tickets without changing old artifacts.

Ingestion reads MPS blobs from the pinned immutable Git manifest or verified
snapshot export. Non-Git sources must first have an immutable manifest/source
seal. Source bytes remain retrievable from this projection with their blob/hash
proofs, independent of the old lexical membership. Component publication and
retention must include the artifact so old ticket pins survive refresh and GC.

## Model navigation and flow semantics

- Decode nodes, registry identities, properties, containment, exact local and
  imported references, descriptors and source locations.
- Lift nested references to their enclosing roots while preserving the exact
  source node, containment path, role, target node and target root. A reference
  to an internal target is a precise structural jump into that node.
- Walk connected roots forward and backward with bounded depth, branches and
  source bytes. Retain all bounded distinct connections, revisit markers,
  unresolved leaves and explicit continuation anchors.
- A root/reference is structural evidence, not automatically an executable
  flow/call/transition. Execution semantics require project language evidence.

Project-declared semantic mappings select exact language/concept/reference-role
IDs (qualified names may be used only with verified unique identity). Mappings
must name source evidence in the language/aspect/generator or authoritative
project specification. Mapping identity is pinned with the projection. Multiple
matches and unavailable evidence remain ambiguous/candidate-only. Generic role
names such as `next`, `jump`, `flow` never establish execution semantics.

Use existing request kinds (`symbol`, `file_hint`), modes (`flow_trace`,
`impact_analysis`, `implementation_plan`) and exact file requests. No new
mandatory request field is needed. Node/root anchors and returned continuation
locations must be usable in the next request in the same ticket/generation.

## AI delivery

The handoff includes model/root/node identity, source ranges, connection roles,
forward and reverse routes, unresolved boundaries, exact source evidence and
next reads. Keep model structure separate from verified execution chronology.
Implementation plans identify authoring property/child/reference changes,
affected upstream/downstream roots, constraints/generator/test surfaces and
acceptance assertions. Generated-code changes require authoring/trace evidence.

## Completion checks

1. Automatic MPS ingestion works for already indexed Git SHAs as well as new
   SHAs, without changing old lexical membership or breaking old ticket pins.
2. Existing v5 requests find MPS names, concepts and nodes, hydrate exact source
   and follow continuation anchors across waves.
3. Nested A → B → C connections, branches, joins, loops, internal-node jumps and
   reverse impact retain accurate identity/provenance; duplicate/missing/dynamic
   targets cannot create verified edges.
4. Flow mappings are exact, pinned, evidence-backed and never guessed by names.
5. Refresh, parser/mapping upgrades, source poisoning and GC preserve or safely
   reject generation-specific evidence.
6. The final implementation plan includes concrete MPS editor operations and
   source/test impact, with no invented private IPF semantics.
7. Relevant narrow tests and the complete repository-required unit/compilation
   checks run against the final implementation.

Verification: 36 MPS tests (including 13 repository integration tests) and the
M365 kit/handoff check pass. Required full-suite integration discovery passed
891 tests with five skips; compilation and whitespace checks pass. Final
source-label/prompt adjustments were also checked by the narrow suites. These
checks verify supported formats and serving behavior, not private IPF delivery
accuracy or production latency.

## Approval boundary

The repository's AGENTS.md requires explicit approval for schema/data migration
and escalation for data-semantics/public-contract changes. This optional immutable
component is a logical serving-contract extension, even though it can avoid a
physical SQLite migration. The user's subsequent “继续啊” authorizes implementing
the described publication, source-serving and retention contract. Commit, push,
release, deployment and destructive operations remain outside this authorization.

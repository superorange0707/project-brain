# MPS and IPF evidence for ticket delivery

Project Brain reads standard MPS v9 XML into model-aware navigation. The reader
does not depend on a predefined IPF vocabulary: each supplied model's registry
provides the actual language, concept, property, child and reference names.
Model ingestion and parsing run locally. Existing delivery commands determine
which retrieved source and navigation are handed to the chosen AI.

## Repository models and pinned ticket retrieval

`brain refresh` / **Refresh Brain** captures supported model files and descriptors
from the configured immutable Git commit or a verified non-Git source seal. A
generation-scoped `mps_models` artifact stores original source bytes and their
path, snapshot, blob and SHA-256 proofs separately from lexical memberships.
Refresh can add this component at an already indexed Git SHA; it does not rewrite
the lexical membership used by older tickets. New tickets use the new generation.
Existing tickets retain their original models through later refreshes and GC.
Older generations without the component report unavailable MPS source; they
cannot substitute the current checkout. Model source reads use the indexed
component, including reads in current-generation mode.

The existing context retrieval pipeline discovers MPS nodes from bounded
objective/search terms, including node/concept names and decoded property values;
no model path or separate MPS entry mode is required first. CamelCase, separated
words and supported separators (space, `_`, `.`, `/`, `$`, `-`) share candidate matching. Short identifiers retain
their full value: `PE` is not an empty token query, and `FlowA` differs from `FlowB`.
Ordinary code anchors do not suppress matching MPS objective terms: the same
context can contain code and pinned model evidence. Exact model paths/node anchors
retain their focused scope. These are lexical candidates; glossary/source evidence
must establish aliases and business meanings that do not appear in the model.
Ordinary objective discovery retains two-character uppercase identifiers such as
unquoted `PE`; qualified code-anchor precision mode retains its stricter extraction.
Within the 16-query model budget, exact model anchors precede known
model candidates, which precede unrelated code queries; truncation stays explicit.
Candidate discovery bounds each value's tokenization to 256 characters. A long
value or the 4096-term limit makes the cheap gate conservative; broad or distributed
terms can trigger a bounded parse without a matching node. Per-node matching does
not combine words from unrelated property values. Exact anchors read omitted source.
New refreshes publish discovery profile v2; existing v1 ticket pins remain readable
with their original discovery metadata, without acquiring the v2 property index.
You can also use a known name in `resolve` or a `symbol` anchor. Then copy
the returned exact repository anchor into a focused request. The following
placeholders must be replaced with verified values from the handoff:

```json
{
  "INVESTIGATION_REQUEST": {
    "version": 5,
    "mode": "flow_trace",
    "objective": "Find the downstream authoring dependencies of this node.",
    "anchors": [{"kind": "file_hint", "value": "VERIFIED_REPO/VERIFIED_PATH.mps#id:VERIFIED_NODE_ID"}]
  }
}
```

Use `impact_analysis` for incoming references. Follow returned frontier or
`#lines:start-end` anchors in the same ticket; use an exact `files` request for
additional model, mapping or specification source. Delta contexts retain this
navigation. Exact resolved model anchors skip generic discovery; name searches
can also gather related code and tests. A complete candidate gate skips unrelated
ordinary symbols, and source/parse caches last only for
the current request. Backend operations, bytes, elapsed stages and omitted
routes are reported. Time limits are checked between bounded parser/graph units
and after enrichment; a single unit is not continuously preempted.

Navigation stays separate from execution/integration flow proof. Its source
windows are delivered as ordinary pinned E-ID evidence. Bare descriptor and
unsupported-file hints whose UTF-8 source was captured return `source_only`,
exact raw source and the unsupported reason, with no decoded model flow.
Excluded non-UTF-8/binary or oversized sources remain build limitations.
An unsupported/descriptor `#id:` remains
unmatched. A poisoned artifact is rejected; source reads never fall back to a
newer working tree. A failed optional refresh retains an intact component only
when its source snapshots still align.

## Optional project flow semantics

To label a role as a project-declared `flow_call`, `transition`, `branch`, `join`
or `return`, place `mps-flow-mappings.json` at the configured repository root.
Each entry must select the exact language UUID, the concept ID that declares
the reference role, and its role ID, plus source evidence in the same repository.
This illustrative format contains placeholders, not Icon/IPF language constants:

```json
{
  "version": 1,
  "mappings": [{
    "language_id": "COPY_VERIFIED_LANGUAGE_UUID",
    "concept_id": "COPY_VERIFIED_DECLARING_CONCEPT_ID",
    "role_id": "COPY_VERIFIED_REFERENCE_ROLE_ID",
    "kind": "flow_call",
    "evidence": {"path": "COPY_VERIFIED_SPECIFICATION_PATH", "line_start": 1, "line_end": 8}
  }]
}
```

IDs are strings. Evidence spans must be valid and at most 80 lines; an optional
`evidence.sha256` must match the entire pinned evidence file. Brain captures the
mapping and referenced evidence files with the generation. A single exact match
is `project_declared_with_source`; duplicate matches are `ambiguous`. Names such
as `next` or `jump` cannot qualify a role. Declarations do not prove observed
execution order or runtime behavior. Mapping/specification E-ID source is
included within a bounded allowance; `source_status` requests explicit file
reads if those sources did not fit. Mapping files are limited to 64 KB/64 entries.

## Attached models

Importing an attachment archives it locally. The same parser also supports:

```bash
brain evidence PAY-123 payment-project.zip --kind document --target m365
brain evidence PAY-123 relevant-flow.mps --kind document --target m365
brain evidence PAY-123 payment-language.mpl --kind document --target m365
```

The existing evidence command and ticket context pipeline carry the result.
Later `resolve` requests or `symbol`/`file_hint` anchors retrieve focused
connections and original source regions from the preserved, hash-checked
attachment. An exact `member.mps#id:node-id` anchor can select a nested node;
returned `source:N` keys select source locations only. `impact_analysis` returns
incoming static references within the supplied model project. Root names and concepts are discovery candidates and may match
multiple roots. Routes retain internal-node targets, revisits, unresolved leaves,
frontier anchors and explicit bounds; they do not prove execution chronology.
Source windows return `member.mps#lines:start-end` continuation anchors for
additional reads of the same hash-checked original (up to 80 lines per region).
Focused context also links used concept/property/child/reference IDs to supplied
language structure declarations. `definition_anchors` carries exact declaration
locations and source windows, or explicit missing/ambiguous/unrecognized states.
It uses complete language/concept/role identities; renamed labels cannot change
the match. Stable meta IDs take precedence over current declaration node IDs.
Language ownership requires an explicit language-ID property or a unique default
`${module}` model-root declaration in the supplied language descriptor. Generator
root overlap blocks descriptor fallback, even under a broad language source root;
a valid explicit per-concept language ID still takes precedence, as in MPS.
Guessed namespace prefixes cannot establish ownership. Foreign/custom
node-ID fallback, unsupported roots or numeric formats remain unresolved.
Built-in declaration identities and the implicit `LinkMetaclass` reference default
follow the pinned standard MPS structure profile recorded with the fixtures;
custom replacements of that built-in language require their own evidence.
This proves a structural identity link only. Constraints, behavior, typesystem,
generation and executable flow meaning still require their own source evidence.
The parser performs no model mutation, generation, dependency installation or
network request. Attached models use their managed originals; repository models
use the separately validated generation artifact described above.

## What the reader establishes

| Input | Decoded evidence |
| --- | --- |
| Standard single-file UTF-8 `.mps`, persistence v9 | Model identity, attributes, used languages/devkits, imports, registry, root/nested nodes, concept and role names, properties, explicit references, source lines |
| `.mpl`, `.msd`, `.mpr`, `.devkit` XML descriptors | Root attributes and nested sections, preserving model/source roots, module dependencies, generator boundaries and mapping declarations |
| ZIP containing those files | The same evidence across members; static references resolved only by model identity and persistent node ID |

Model-local registry indexes never serve as global names. A reference's
`resolve` string is retained as source data and is never used to guess a target.
The MPS reference grammar is parsed before separator escapes are decoded; raw
references and presentation names are retained. Known global model IDs ignore
presentation/module labels; module-private integer IDs retain module identity.
Custom factory identities remain unresolved. References report `resolved`, `missing_target`,
`ambiguous`, `dynamic`, `unknown_import`, `unsupported_identity` or `invalid_target`. Reference cycles
remain graph edges and do not expand the containment tree.
Local references resolve within their own supplied model file even when the
model uses a custom identity factory or the project contains duplicate copies.

Some valid nodes have no persistent ID. These retain `id: null` and an explicitly
local `source_location` key for containment; that key must never be used as an
MPS editor/node reference. Only persistent IDs participate in reference binding.
Absent/null property values remain distinct from explicit empty strings.

The reader preserves each attachment's original bytes and SHA-256. ZIP members
have their own path and SHA-256; nodes/references have source line locations.
The JSON navigation is derived from these supplied bytes and remains external
evidence. It does not inherit a repository snapshot's proof status.

## Turn a ticket into an implementation

1. Identify the authoring model, persistent node ID when present (otherwise
   source-location key and lines), qualified concept, and property,
   child or reference role that controls the requested change. Use decoded
   names for navigation and inspect the actual supplied source.
2. Trace the relevant references and affected usages. Resolve unavailable or
   ambiguous targets before treating them as a behavior dependency. Include
   the language definition/aspect models and generator templates in the configured
   repositories or local ZIP when they are relevant and available.
3. Read the relevant concept structure/inheritance, constraints, behavior,
   typesystem, generator and tests. Their models use the same MPS structure,
   so the reader decodes their nodes and references too. Semantics must be
   established from these definitions, never inferred from a concept name.
4. Supply a concrete proposal in a fenced `mps` editor-operation block:
   model/member path, model/node/concept identity,
   exact editor operation, before/after property or reference value, child
   insertion/removal location, impacted usages and generated/runtime boundary.
   Provide code/configuration snippets for actual textual authoring surfaces.
   MPS model changes should use the projectional editor or a verified Open API
   operation rather than speculative serialized XML patches.
5. Map each acceptance criterion to the edit and exact assertion. Generate
   and validate using project-evidenced commands. Return observed diff and test
   results through `brain feedback`; the AI reviews those results before
   claiming the ticket is delivered.

For cooperating flows, the AI must establish relevant callers/entries, branch
guards, input/output bindings, shared state and error/retry/timeout/transaction
boundaries. Explain a before/after business scenario and upstream/downstream
regressions from actual language/aspect/generator/test evidence. The reference
graph and optional flow labels do not automatically derive these semantics;
missing edit-changing semantics remain blockers. Brain supplies evidence and
proposals, while the developer applies model changes and supplies validation.

A generator descriptor declares configuration; it does not prove which
generated line implements a rule. AST child order establishes stored structure,
not runtime execution order. Generation traces, generated source and runtime
observations must be supplied separately when the ticket depends on them.

## IPF specialization and public evidence

Icon's [official IPF overview](https://iconsolutions.com/icon-payments-framework)
describes Studio orchestration and an SDK covering flows, business rules,
integration, data mapping and testing. Its [official IPF factsheet](https://iconsolutions.com/hubfs/UK%20IPF%20Factsheet.pdf)
identifies MPS as the basis of the Studio payment DSL. These sources establish
the product context; they do not publish a customer's language definitions,
concept IDs, proprietary rules or generator mappings.

The useful specialization is therefore evidence-driven: decode the actual
project language names and identities, inspect its authoring/aspect models,
then connect the ticket's payment terminology to those verified anchors.
Public product descriptions cannot supply private rule semantics on their own.

The serialization reader and tests use JetBrains' [official MPS sample models](../tests/fixtures/mps/README.md)
and the [v9 persistence writer](https://github.com/JetBrains/MPS/blob/master/core/persistence/source/jetbrains/mps/smodel/persistence/def/v9/ModelWriter9.java).
JetBrains documents the [AST/concept/language/generator relationship](https://www.jetbrains.com/help/mps/basic-notions.html)
and [Open API/persistence boundaries](https://www.jetbrains.com/help/mps/open-api-accessing-models-from-code.html).
Official samples verify serialization and reference handling; they do not
constitute IPF ticket-delivery accuracy measurements.

## Limits

The reader accepts UTF-8 XML, at most 128 MPS files, 3 MB per XML file, 16 MB total decoded
source, 50,000 XML elements per file and 128 nesting levels. ZIPs are read in
memory, never extracted; unsafe/duplicate member paths, symlink members,
encryption and oversize input are rejected. DTD/entity declarations are rejected.
Unrecognized or malformed models are reported without inferred structure.

The handoff navigation is at most 32 KB and reports omissions. Individual
sources at most 128 KB include their full source. ZIP summaries and larger
attachments preserve the originals; focused requests return bounded original
member regions without extracting ZIP paths. Focused navigation is at most
32 KB, with eight connection levels, sixteen edges per root and 128 roots.
A summary or source region is not an exhaustive project dump. Original source
hash mismatches reject focused reads rather than reading a replacement model.
Repository source capture shares the 128-file/16-MB total with optional mapping
and specification files. Its artifact is bounded to 64 MiB; file/byte exclusions
are explicit limitations. These bounds do not measure production retrieval
latency or private IPF ticket-delivery accuracy.

Binary/custom persistence, file-per-root model streams, non-file storage and
custom extensions require the appropriate MPS runtime/provider or a supported
export. Split `.model`/`.mpsr` members and `content="header"`/`content="root"`
markers are reported as unsupported, including in mixed ZIPs. Brain cannot claim universal support for every project solely from
public XML samples.

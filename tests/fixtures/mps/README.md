# Public MPS fixtures

These fixtures are copied verbatim from the public [JetBrains/MPS](https://github.com/JetBrains/MPS) repository at commit [`3e609c4accef48708d1892631dadcff5826881d6`](https://github.com/JetBrains/MPS/commit/3e609c4accef48708d1892631dadcff5826881d6), the `master` revision observed on 2026-10-04. They exercise the v9 model decoder and exact language-definition identities; they are not IPF models and do not define the private Icon Solutions payment language.

JetBrains MPS is distributed under the [Apache License 2.0](https://github.com/JetBrains/MPS/blob/3e609c4accef48708d1892631dadcff5826881d6/LICENSE.txt); the verbatim license text is included as [`LICENSE.txt`](./LICENSE.txt). The original source paths and pinned raw URLs are recorded below so each fixture can be refreshed or compared with upstream. The repository has no root or sample-directory `NOTICE` file at this revision; the unrelated `build/ivy/NOTICE` is not part of these sample sources.

| Fixture | Upstream sample and coverage |
| --- | --- |
| `mps_structure.mps` | [Official MPS structure language](https://github.com/JetBrains/MPS/blob/3e609c4accef48708d1892631dadcff5826881d6/languages/languageDesign/structure/languageModels/structure.mps). Concept/interface/property/link declarations and their explicit stable IDs, including IDs differing from current declaration node IDs. Git blob identity `3dd7909e0c64c5bb3c8d3a3cbfd682aaf451cbfd` is verified by the unit test. |
| `calculator_structure.mps` | [`samples/calculator-tutorial/languages/calculator/languageModels/structure.mps`](https://github.com/JetBrains/MPS/blob/3e609c4accef48708d1892631dadcff5826881d6/samples/calculator-tutorial/languages/calculator/languageModels/structure.mps). MPS persistence v9 header, used languages, imports, per-model registry, concept/property/reference/child role indexes, nested structure nodes, local references, and references to imported models. Raw: [`raw.githubusercontent.com`](https://raw.githubusercontent.com/JetBrains/MPS/3e609c4accef48708d1892631dadcff5826881d6/samples/calculator-tutorial/languages/calculator/languageModels/structure.mps). |
| `blreferences_model.mps` | [`samples/blReferences/sandbox/models/model.mps`](https://github.com/JetBrains/MPS/blob/3e609c4accef48708d1892631dadcff5826881d6/samples/blReferences/sandbox/models/model.mps). Nested BaseLanguage nodes with both local `node="..."` references and imported `to="index:node"` references. Raw: [`raw.githubusercontent.com`](https://raw.githubusercontent.com/JetBrains/MPS/3e609c4accef48708d1892631dadcff5826881d6/samples/blReferences/sandbox/models/model.mps). |
| `nodeuid_sandbox.mps` | [`samples/nodeuid/sandbox/models/jetbrains.mps.samples.nodeuid.sandbox.mps`](https://github.com/JetBrains/MPS/blob/3e609c4accef48708d1892631dadcff5826881d6/samples/nodeuid/sandbox/models/jetbrains.mps.samples.nodeuid.sandbox.mps). Small nested model with a custom sample concept and a model-level `doNotGenerate` attribute. Raw: [`raw.githubusercontent.com`](https://raw.githubusercontent.com/JetBrains/MPS/3e609c4accef48708d1892631dadcff5826881d6/samples/nodeuid/sandbox/models/jetbrains.mps.samples.nodeuid.sandbox.mps). |
| `calculator_language.mpl` | [`samples/calculator-tutorial/languages/calculator/jetbrains.mps.calculator.mpl`](https://github.com/JetBrains/MPS/blob/3e609c4accef48708d1892631dadcff5826881d6/samples/calculator-tutorial/languages/calculator/jetbrains.mps.calculator.mpl). Language/module descriptor with model roots/source roots, Java facet, generator model root, language/dependency versions, module dependencies, and extended languages. Raw: [`raw.githubusercontent.com`](https://raw.githubusercontent.com/JetBrains/MPS/3e609c4accef48708d1892631dadcff5826881d6/samples/calculator-tutorial/languages/calculator/jetbrains.mps.calculator.mpl). |

## What the fixtures establish

The v9 XML model shape represented here is:

```text
model ref + persistence version
  languages / devkits
  imports (local index -> model reference)
  registry (language -> concept -> property/reference/child role indexes)
  root nodes
    node concept/index/id/role
      property role/value
      ref role + local node or imported model target
      nested node children
```

The registry is local to each persisted model and contains the concepts and roles used by that model. A reader must decode the registry before interpreting `concept="..."` or `role="..."`; the short indexes are not globally meaningful by themselves. For an external reference, the v9 writer uses the model import index and node id (`import-index:node-id`). A local `node="..."` target is resolved within the current model.

The `.mpl` file is a language/module descriptor, not an MPS model. Its `<modelRoot>` and `<sourceRoot>` entries describe where models are found; its `<generators>` section describes generator models; its dependency and language-version sections describe module/language dependencies. It should be indexed separately from `.mps` model nodes.

## Official v9 stream and reference markers

The official [`ModelWriter9`](https://github.com/JetBrains/MPS/blob/3e609c4accef48708d1892631dadcff5826881d6/core/persistence/source/jetbrains/mps/smodel/persistence/def/v9/ModelWriter9.java) writes split persistence with a `<model content="header">` document for the model header and `<model content="root">` documents for root streams. [`ModelReader9Handler`](https://github.com/JetBrains/MPS/blob/3e609c4accef48708d1892631dadcff5826881d6/core/persistence/source_gen/jetbrains/mps/smodel/persistence/def/v9/ModelReader9Handler.java) accepts only `header` and `root` values. A v9 decoder that expects one complete XML model file should classify or reject either marker instead of treating a header-only or root-only stream as a complete model; the ordinary one-file form has no split-content marker.

The official [`SModelReference`](https://github.com/JetBrains/MPS/blob/3e609c4accef48708d1892631dadcff5826881d6/core/kernel/source/jetbrains/mps/smodel/SModelReference.java) grammar is `[moduleID/]modelID[([moduleName/]modelName)]`. The parenthesized part is a presentation name, so `r:UUID(name)` must not be treated as an arbitrary suffix. A reader should preserve the raw reference and parse the model ID and optional presentation separately; splitting at the first `(` can collide for names or forms outside the short fixture examples.

## Deliberate boundaries

Definition links follow the official [MetaIdByDeclaration](https://github.com/JetBrains/MPS/blob/3e609c4accef48708d1892631dadcff5826881d6/core/kernel/source/jetbrains/mps/smodel/adapter/ids/MetaIdByDeclaration.java)
conversion: explicit stable ID first, then supported regular node-ID fallback;
language UUID override or verified unique descriptor ownership; role owner is
the containing concept declaration. [IdEncoder](https://github.com/JetBrains/MPS/blob/3e609c4accef48708d1892631dadcff5826881d6/core/persistence/source/jetbrains/mps/smodel/persistence/def/v9/IdEncoder.java)
and [JavaFriendlyBase64](https://github.com/JetBrains/MPS/blob/3e609c4accef48708d1892631dadcff5826881d6/core/smodel/source/jetbrains/mps/smodel/JavaFriendlyBase64.java)
establish that meta IDs are decimal and regular node IDs use a different
encoding. Matching display names or registry indexes cannot establish a link.

Standard `LinkDeclaration.metaClass` distinguishes reference and containment by
its persisted enum literal ID, not its display suffix. The official structure
model declares `1084199179704` as the default reference member and
`1084199179705` as aggregation. The persisted form is a Java-friendly Base64 ID
followed by `/name`; absent property values store the default implicitly, as
defined by [SEnumerationAdapter](https://github.com/JetBrains/MPS/blob/3e609c4accef48708d1892631dadcff5826881d6/core/kernel/source/jetbrains/mps/smodel/adapter/structure/types/SEnumerationAdapter.java).
The reader applies this default only to the standard structure identity profile,
and retains separate reference/child binding keys.

These files cover the standard, one-file, XML persistence shape used by the public samples. They do not prove support for:

- binary model persistence;
- custom persistence factories or custom file extensions;
- non-file model roots or database-backed storage;
- file-per-root persistence where header and root content are stored in separate streams;
- generated source, BDD scenarios, documentation, or generation trace mappings;
- any private IPF concept, role, payment scheme, flow, or rule.

Those cases require the actual persistence provider, model files, MPS Open API/runtime, or project-specific generation metadata. Do not hand-edit these serialized models as a substitute for model-aware MPS editing.

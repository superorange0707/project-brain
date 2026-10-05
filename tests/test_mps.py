from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from brain import mps
from brain.core import add_external_evidence, create_context, load_settings, session_state, start_session, _external_evidence


FIXTURES = Path(__file__).parent / "fixtures" / "mps"


def model(reference: str, nodes: str, imports: str = "") -> bytes:
    return (f'<model ref="{reference}"><persistence version="9"/><imports>{imports}</imports>'
            '<registry><language id="language-id" name="example.language">'
            '<concept id="concept-id" index="c" name="example.language.Step">'
            '<property id="property-id" index="p" name="name"/>'
            '<child id="child-id" index="h" name="steps"/>'
            '<reference id="reference-id" index="r" name="next"/>'
            '</concept></language></registry>' + nodes + '</model>').encode()


def archive(documents: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as output:
        for name, source in documents.items():
            # Keep malformed member names intact even on Windows.
            info = zipfile.ZipInfo("placeholder.mps")
            info.filename = info.orig_filename = name
            info.compress_type = zipfile.ZIP_DEFLATED
            output.writestr(info, source)
    return stream.getvalue()


class MpsParserTests(unittest.TestCase):
    def test_stable_concept_and_reference_ids_find_actual_language_declarations(self) -> None:
        language = "26b3d6d5-b99a-4ed6-83be-d2ea6f3627a1"
        usage = model("f:usage", '<node concept="c" id="instance"><ref role="r" node="target"/></node>'
                      '<node concept="c" id="target"/>').replace(b'language-id', language.encode()).replace(
                          b'concept-id', b'1241363083334').replace(b'reference-id', b'1241363105304')
        documents = {
            "usage.mps": usage,
            "calculator/calculator.mpl": (FIXTURES / "calculator_language.mpl").read_bytes(),
            "calculator/languageModels/structure.mps": (FIXTURES / "calculator_structure.mps").read_bytes(),
        }
        project = mps.parse_project(documents)
        definitions = mps.definition_anchors(project, [("usage.mps", "id:instance")])["bindings"]
        concept = next(item for item in definitions if item["kind"] == "concept")
        link = next(item for item in definitions if item["kind"] == "reference")
        self.assertEqual("resolved", concept["status"])
        self.assertEqual("id:i470n16", concept["target"]["key"])
        self.assertEqual("resolved", link["status"])
        self.assertEqual("id:i470soo", link["target"]["key"])
        self.assertEqual("next", link["name"])  # A different display name cannot change the identity link.
        self.assertEqual("descriptor_source_root", concept["target"]["identity_proof"]["method"])
        focused = json.loads(mps.focused_navigation(documents, ["usage.mps#id:instance"]))
        self.assertTrue(any(item["path"] == "calculator/languageModels/structure.mps" for item in focused["source_regions"]))
        self.assertTrue(any(item["status"] == "resolved" for item in focused["definition_anchors"]))
        self.assertLessEqual(len(mps.focused_navigation(documents, ["usage.mps#id:instance"]).encode()), mps.MAX_SUMMARY_BYTES)
        child_usage = usage.replace(b'child-id', b'1241363105304').replace(
            b'<node concept="c" id="instance">', b'<node concept="c" id="instance"><node concept="c" id="child" role="h"/>')
        documents["usage.mps"] = child_usage
        typed = mps.definition_anchors(mps.parse_project(documents), [("usage.mps", "id:instance"), ("usage.mps", "id:child")])["bindings"]
        self.assertEqual("missing_declaration", next(item for item in typed if item["kind"] == "child")["status"])
        self.assertEqual("resolved", next(item for item in typed if item["kind"] == "reference")["status"])
        documents["usage.mps"] = usage
        documents["calculator/languageModels/copy.mps"] = documents["calculator/languageModels/structure.mps"]
        duplicated = mps.definition_anchors(mps.parse_project(documents), [("usage.mps", "id:instance")])["bindings"]
        self.assertTrue(all(item["status"] == "ambiguous_declaration" and item["target"] is None for item in duplicated))
        del documents["calculator/languageModels/copy.mps"]
        documents["usage.mps"] = usage.replace(language.encode(), b'26b3d6d5-b99a-4ed6-83be-d2ea6f3627a2')
        wrong_language = mps.definition_anchors(mps.parse_project(documents), [("usage.mps", "id:instance")])["bindings"]
        self.assertTrue(all(item["status"] == "missing_declaration" for item in wrong_language))

    def test_official_structure_property_declaration_and_regular_id_fallback(self) -> None:
        structure = (FIXTURES / "mps_structure.mps").read_bytes()
        self.assertEqual("3dd7909e0c64c5bb3c8d3a3cbfd682aaf451cbfd", hashlib.sha1(
            b"blob " + str(len(structure)).encode() + b"\0" + structure).hexdigest())
        documents = {
            "meta/languageModels/structure.mps": structure,
            "meta/language.mpl": b'<language uuid="c72da2b9-7cce-4447-8389-f407dc1158b7"><models>'
                                 b'<modelRoot type="default" contentPath="${module}"><sourceRoot location="languageModels"/></modelRoot>'
                                 b'</models></language>',
            "calculator.mps": (FIXTURES / "calculator_structure.mps").read_bytes(),
        }
        result = mps.definition_anchors(mps.parse_project(documents), [("calculator.mps", "id:i46Ymgg")])["bindings"]
        concept = next(item for item in result if item["kind"] == "concept")
        self.assertEqual("resolved", concept["status"])
        self.assertEqual("explicit_property", concept["target"]["identity_proof"]["id_method"])
        fallback = {"id": "1", "properties": []}
        self.assertEqual(("1", "regular_node_id_fallback"),
                         mps._declaration_id(fallback, mps.ABSTRACT_CONCEPT, mps.CONCEPT_ID_PROPERTY))
        fallback["id"] = "~foreign-declaration"
        self.assertIsNone(mps._declaration_id(fallback, mps.ABSTRACT_CONCEPT, mps.CONCEPT_ID_PROPERTY)[0])
        self.assertEqual("-1", mps._regular_node_id("fZZZZZZZZZZ"))
        for alias in ("00", "g0000000000", "000000000001", "~foreign"):
            self.assertIsNone(mps._regular_node_id(alias))
        self.assertEqual("1", mps._numeric_id("+" + "0" * 5000 + "1"))
        prop = next(item for item in result if item["kind"] == "property" and item["role_id"] == mps.CONCEPT_ID_PROPERTY)
        self.assertEqual("resolved", prop["status"])
        self.assertEqual("id:5OIo7_R7SN0", prop["target"]["key"])
        self.assertEqual("explicit_property", prop["target"]["identity_proof"]["id_method"])
        with mock.patch.object(mps, "MAX_DEFINITION_USES", 1):
            bounded = mps.definition_anchors(mps.parse_project(documents), [("calculator.mps", "id:i46Ymgg")])
        self.assertEqual(1, len(bounded["bindings"]))
        self.assertGreater(bounded["omitted_uses"], 0)

    def test_explicit_language_override_and_descriptor_boundaries_do_not_guess_ownership(self) -> None:
        language = "26b3d6d5-b99a-4ed6-83be-d2ea6f3627a1"
        override = "26b3d6d5-b99a-4ed6-83be-d2ea6f3627a2"
        source = (FIXTURES / "calculator_structure.mps").read_bytes()
        role = b'<property id="9005308665739310115" name="misleading-label" index="language-override"/>'
        with_role = source.replace(b'<property id="6714410169261853888"', role + b'<property id="6714410169261853888"')
        with_override = with_role.replace(b'<property role="EcuMT"',
            b'<property role="language-override" value="' + override.encode() + b'"/><property role="EcuMT"')
        usage = model("f:usage", '<node concept="c" id="instance"/>').replace(
            b'language-id', override.encode()).replace(b'concept-id', b'1241362555920')
        documents = {"usage.mps": usage, "calculator/languageModels/structure.mps": with_override}
        binding = mps.definition_anchors(mps.parse_project(documents), [("usage.mps", "id:instance")])["bindings"][0]
        self.assertEqual("resolved", binding["status"])
        self.assertEqual("explicit_language_property", binding["target"]["identity_proof"]["method"])
        documents["calculator/calculator.mpl"] = (FIXTURES / "calculator_language.mpl").read_bytes()
        documents["calculator/languageModels/structure.mps"] = with_override.replace(override.encode(), b'not-a-uuid')
        binding = mps.definition_anchors(mps.parse_project(documents), [("usage.mps", "id:instance")])["bindings"][0]
        self.assertEqual("missing_declaration", binding["status"])
        documents["usage.mps"] = usage.replace(override.encode(), language.encode())
        documents["calculator/languageModels/structure.mps"] = source
        documents["calculator/calculator.mpl"] = (
            b'<language uuid="' + language.encode() + b'"><generators><generator><models>'
            b'<modelRoot type="default" contentPath="${module}"><sourceRoot location="languageModels"/></modelRoot>'
            b'</models></generator></generators></language>')
        binding = mps.definition_anchors(mps.parse_project(documents), [("usage.mps", "id:instance")])["bindings"][0]
        self.assertEqual("missing_declaration", binding["status"])
        # A broad language root must not claim a nested generator's models.
        documents["calculator/calculator.mpl"] = (
            b'<language uuid="' + language.encode() + b'"><models>'
            b'<modelRoot type="default" contentPath="${module}"><sourceRoot location="."/></modelRoot>'
            b'</models><generators><generator><models>'
            b'<modelRoot type="default" contentPath="${module}"><sourceRoot location="languageModels"/></modelRoot>'
            b'</models></generator></generators></language>')
        binding = mps.definition_anchors(mps.parse_project(documents), [("usage.mps", "id:instance")])["bindings"][0]
        self.assertEqual("missing_declaration", binding["status"])
        documents["calculator/calculator.mpl"] = (FIXTURES / "calculator_language.mpl").read_bytes()
        documents["calculator/duplicate.mpl"] = documents["calculator/calculator.mpl"]
        binding = mps.definition_anchors(mps.parse_project(documents), [("usage.mps", "id:instance")])["bindings"][0]
        self.assertEqual("missing_declaration", binding["status"])

    def test_nested_multi_root_connections_forward_reverse_and_revisit_are_preserved(self) -> None:
        project = mps.parse_project({
            "a.mps": model("f:a", '<node concept="c" id="a"><property role="p" value="FlowA"/>'
                           '<node concept="c" id="call" role="h"><ref role="r" to="b:inside"/>'
                           '<ref role="r" to="d:d"/></node></node>',
                           '<import index="b" ref="f:b"/><import index="d" ref="f:d"/>'),
            "b.mps": model("f:b", '<node concept="c" id="b"><property role="p" value="FlowB"/>'
                           '<node concept="c" id="inside" role="h"><ref role="r" to="c:c"/></node></node>',
                           '<import index="c" ref="f:c"/>'),
            "c.mps": model("f:c", '<node concept="c" id="c"><property role="p" value="FlowC"/>'
                           '<ref role="r" to="a:a"/></node>', '<import index="a" ref="f:a"/>'),
            "d.mps": model("f:d", '<node concept="c" id="d"><property role="p" value="FlowD"/></node>'),
        })
        traced = mps.trace_model_connections(project, ["FlowA"])
        pairs = {(edge["source_root"]["label"], edge["target_root"]["label"]) for edge in traced["steps"]}
        self.assertEqual({("FlowA", "FlowB"), ("FlowA", "FlowD"), ("FlowB", "FlowC"), ("FlowC", "FlowA")}, pairs)
        jump = next(edge for edge in traced["steps"] if edge["target_root"]["label"] == "FlowB")
        self.assertEqual("call", jump["source_node"]["node_id"])
        self.assertEqual("inside", jump["target_node"]["node_id"])
        self.assertEqual("steps", jump["containment_path"][0]["role"]["name"])
        self.assertTrue(any(edge["revisited_root"] for edge in traced["steps"]))
        self.assertFalse(traced["truncated"])
        incoming = mps.trace_model_connections(project, ["FlowD"], direction="incoming")
        self.assertEqual(4, len(incoming["steps"]))
        partial = mps.trace_model_connections(project, ["FlowA"], depth=1)
        self.assertTrue(partial["truncated"])
        self.assertEqual(["FlowB"], [root["label"] for root in partial["frontier"]])
        nested = mps.trace_model_connections(project, ["b.mps#id:inside"])
        self.assertEqual(["FlowB"], [root["label"] for root in nested["seeds"]])
        self.assertEqual("inside", nested["matched_nodes"][0]["node_id"])
        reverse = mps.trace_model_connections(project, ["f:b#inside"], direction="incoming")
        self.assertTrue(any(edge["source_root"]["label"] == "FlowA" for edge in reverse["steps"]))
        navigation = json.loads(mps.render_navigation(project))
        jump = next(edge for edge in navigation["connections"] if edge["target_root_anchor"] == "b.mps#id:b")
        self.assertEqual("inside", jump["target_node"]["node_id"])
        self.assertEqual(0, navigation["omitted_connections"])

    def test_multi_root_trace_keeps_unresolved_leaves_and_never_guesses_display_targets(self) -> None:
        source = model("f:a", '<node concept="c" id="a"><property role="p" value="FlowA"/>'
                       '<ref role="r" to="b:b" resolve="FlowB"/><ref role="r" to="b:^" resolve="FlowB"/></node>',
                       '<import index="b" ref="f:b"/>')
        target = model("f:b", '<node concept="c" id="b"><property role="p" value="FlowB"/></node>')
        project = mps.parse_project({"a.mps": source, "b.mps": target, "duplicate.mps": target})
        traced = mps.trace_model_connections(project, ["FlowA"])
        self.assertEqual({"ambiguous", "dynamic"}, {edge["status"] for edge in traced["steps"]})
        self.assertTrue(all(edge["target_root"] is None for edge in traced["steps"]))
        self.assertEqual([], mps.trace_model_connections(project, ["FlowB"])["steps"])

    def test_trace_identity_is_case_sensitive_and_labels_report_ambiguity(self) -> None:
        project = mps.parse_project({"a.mps": model("f:a(display)",
            '<node concept="c" id="A"><property role="p" value="Duplicate"/></node>'
            '<node concept="c" id="a"><property role="p" value="Duplicate"/></node>')})
        exact = mps.trace_model_connections(project, ["f:a#A"])
        self.assertEqual(["A"], [root["node_id"] for root in exact["seeds"]])
        self.assertEqual([], exact["ambiguous_queries"])
        duplicate = mps.trace_model_connections(project, ["Duplicate"])
        self.assertEqual(2, len(duplicate["seeds"]))
        self.assertEqual(["Duplicate"], duplicate["ambiguous_queries"])

    def test_focused_source_windows_continue_from_exact_original_lines(self) -> None:
        source = model("f:a", '<node concept="c" id="a">\n' + '\n'.join(
            f'<property role="p" value="line-{number}"/>' for number in range(60)) + '\n</node>')
        documents = {"a.mps": source}
        first = json.loads(mps.focused_navigation(documents, ["a.mps#id:a"]))
        region = first["source_regions"][0]
        self.assertFalse(region["complete_file"])
        following = json.loads(mps.focused_navigation(documents, [region["continuation_anchor"]]))
        second = following["source_regions"][0]
        self.assertEqual(region["line_end"] + 1, second["line_start"])
        self.assertEqual(source.decode().splitlines()[second["line_start"] - 1:second["line_end"]], second["content"].splitlines())
        self.assertEqual(hashlib.sha256(source).hexdigest(), second["sha256"])
        self.assertIsNone(mps.focused_navigation(documents, ["../outside.mps#lines:1-2"]))

    def test_official_structure_decodes_concepts_roles_and_nested_nodes(self) -> None:
        source = (FIXTURES / "calculator_structure.mps").read_bytes()
        project = mps.parse_project({"languageModels/structure.mps": source})
        parsed = project["files"][0]
        self.assertEqual("model", parsed["kind"])
        self.assertEqual([], parsed["warnings"])
        nodes = {node["key"]: node for node in parsed["nodes"]}
        self.assertGreater(len(nodes), len(parsed["roots"]))
        self.assertTrue(any(node["concept"]["name"].endswith("ConceptDeclaration") for node in nodes.values()))
        properties = [prop for node in nodes.values() for prop in node["properties"]]
        self.assertTrue(any(prop["role"]["name"] == "name" and prop["value"] == "Calculator" for prop in properties))
        for node in nodes.values():
            if node["parent"]:
                self.assertIn(node["key"], nodes[node["parent"]]["children"])
                self.assertIsNotNone(node["role"]["name"])
            lines = source.decode().splitlines()
            self.assertIn(node["id"], lines[node["line_start"] - 1])
            self.assertLessEqual(node["line_start"], node["line_end"])
        self.assertGreater(project["reference_counts"].get("resolved", 0), 0)
        self.assertGreater(project["reference_counts"].get("missing_target", 0), 0)

    def test_official_baselanguage_local_refs_are_exact_and_imports_remain_missing(self) -> None:
        project = mps.parse_project({"model.mps": (FIXTURES / "blreferences_model.mps").read_bytes()})
        parsed = project["files"][0]
        self.assertEqual("model", parsed["kind"])
        self.assertEqual([], parsed["warnings"])
        references = [ref for node in parsed["nodes"] for ref in node["references"]]
        local = [ref for ref in references if ref.get("scope") == "local"]
        self.assertTrue(local)
        self.assertTrue(all(ref["status"] == "resolved" for ref in local))
        imported = [ref for ref in references if ref.get("scope") == "imported"]
        self.assertTrue(imported)
        self.assertTrue(all(ref["status"] in {"missing_target", "unsupported_identity"} for ref in imported))
        self.assertTrue(all(ref["role"]["name"] for ref in references))

    def test_official_custom_concept_and_generation_attribute_are_preserved(self) -> None:
        parsed = mps.parse_document((FIXTURES / "nodeuid_sandbox.mps").read_bytes())
        self.assertIn({"name": "doNotGenerate", "value": "true"},
                      [attribute["attributes"] for attribute in parsed["model_attributes"]])
        self.assertTrue(any("jetbrains.mps.samples.nodeuid" in node["concept"]["name"] for node in parsed["nodes"]))

    def test_descriptor_keeps_language_and_generator_sections_separate(self) -> None:
        parsed = mps.parse_document((FIXTURES / "calculator_language.mpl").read_bytes())
        self.assertEqual("language", parsed["descriptor_kind"])
        sections = {section["tag"]: section for section in parsed["sections"]}
        self.assertIn("models", sections)
        self.assertIn("dependencies", sections)
        generator = sections["generators"]["children"][0]
        self.assertEqual("generator", generator["tag"])
        self.assertIn("modelRoot", json.dumps(generator))
        self.assertIn("mapping-priorities", json.dumps(generator))
        self.assertNotIn("generator", json.dumps(sections["dependencies"]))

    def test_cross_model_targets_use_identity_and_never_display_name(self) -> None:
        source = model("r:a(source)", '<node concept="c" id="a"><ref role="r" to="i:b" resolve="wrong name"/>'
                       '<ref role="r" to="i:missing" resolve="display"/><ref role="r" to="i:^" resolve="display"/>'
                       '<ref role="r" to="unknown:b"/></node>', '<import index="i" ref="r:b(old-name)"/>')
        target = model("r:b(renamed)", '<node concept="c" id="b"><property role="p" value="display"/></node>')
        project = mps.parse_project({"source.mps": source, "elsewhere/target.mps": target})
        refs = next(entry for entry in project["files"] if entry["path"] == "source.mps")["nodes"][0]["references"]
        self.assertEqual(["resolved", "missing_target", "dynamic", "unknown_import"], [ref["status"] for ref in refs])
        self.assertEqual("elsewhere/target.mps", refs[0]["target_path"])
        duplicate = mps.parse_project({"source.mps": source, "target.mps": target, "copy.mps": target})
        self.assertEqual(1, duplicate["reference_counts"]["ambiguous"])

    def test_cycles_are_reference_edges_and_do_not_expand_containment(self) -> None:
        source = model("r:a", '<node concept="c" id="a"><ref role="r" node="b"/>'
                       '<node concept="c" id="b" role="h"><ref role="r" node="a"/></node></node>')
        project = mps.parse_project({"cycle.mps": source})
        self.assertEqual(2, project["reference_counts"]["resolved"])
        self.assertEqual(2, len(project["files"][0]["nodes"]))

    def test_local_scope_does_not_depend_on_globalness_or_duplicate_model_copies(self) -> None:
        source = model("custom:a", '<node concept="c" id="a"><ref role="r" node="a"/></node>')
        project = mps.parse_project({"one.mps": source, "copy.mps": source})
        self.assertEqual({"resolved": 2}, project["reference_counts"])
        for entry in project["files"]:
            self.assertEqual(entry["path"], entry["nodes"][0]["references"][0]["target_path"])
        regular = model("r:a", '<node concept="c" id="a"><ref role="r" node="a"/></node>')
        self.assertEqual({"resolved": 2}, mps.parse_project({"one.mps": regular, "copy.mps": regular})["reference_counts"])

    def test_model_reference_grammar_escapes_and_module_private_identities(self) -> None:
        reference = mps.parse_model_reference(" module/r:87765d2d-a756-4883-9acc-6a42e5bf6c23(module/name%28v2%29) ")
        self.assertEqual("name(v2)", reference["model_name"])
        self.assertEqual("r:87765d2d-a756-4883-9acc-6a42e5bf6c23", reference["identity"])
        foreign = mps.parse_model_reference("f:a%28b%29(display)")
        self.assertEqual("f:a(b)", foreign["model_id"])
        self.assertNotEqual(foreign["identity"], mps.parse_model_reference("f:a(other)")["identity"])
        self.assertEqual(mps.parse_model_reference("one/i:000a(old)")["identity"],
                         mps.parse_model_reference("one/i:A(new)")["identity"])
        self.assertNotEqual(mps.parse_model_reference("one/i:000a")["identity"],
                            mps.parse_model_reference("two/i:000a")["identity"])
        for value in ("r:a(trailing)junk", "r:a(", "f:bad%2", "f:bad%GG", "i:0001"):
            with self.subTest(value=value), self.assertRaises(mps.MpsError):
                mps.parse_model_reference(value)
        source = model("f:source", '<node concept="c" id="a"><ref role="r" to="i:b"/></node>',
                       '<import index="i" ref="custom:a(name)"/>')
        target = model("custom:a(renamed)", '<node concept="c" id="b"/>')
        project = mps.parse_project({"a.mps": source, "b.mps": target})
        self.assertEqual(1, project["reference_counts"]["unsupported_identity"])

    def test_idless_nodes_and_null_properties_are_preserved_without_reference_targets(self) -> None:
        source = model("r:a", '<node concept="c" id="a"><node concept="c" role="h">'
                       '<property role="p"/><property role="p" value=""/></node>'
                       '<ref role="r" node="source:1"/></node>')
        project = mps.parse_project({"a.mps": source})
        root, child = project["files"][0]["nodes"]
        self.assertIsNone(child["id"])
        self.assertEqual("source_location", child["identity_kind"])
        self.assertEqual(root["key"], child["parent"])
        self.assertEqual([child["key"]], root["children"])
        self.assertEqual([None, ""], [prop["value"] for prop in child["properties"]])
        self.assertEqual("missing_target", root["references"][0]["status"])

    def test_split_streams_are_explicit_even_in_mixed_zip_and_devkit_is_reachable(self) -> None:
        source = model("r:a", '<node concept="c" id="a"/>')
        split = source.replace(b'<model ref=', b'<model content="root" ref=')
        project = mps.project_from_attachment("project.zip", archive({
            "a.mps": source, "header.model": source, "root.mpsr": split, "renamed.mps": split,
            "language.devkit": b'<dev-kit name="example"><exported-languages/></dev-kit>',
        }))
        entries = {entry["path"]: entry for entry in project["files"]}
        self.assertEqual("descriptor", entries["language.devkit"]["kind"])
        for name in ("header.model", "root.mpsr", "renamed.mps"):
            self.assertEqual("unsupported", entries[name]["kind"])
            self.assertIn("file-per-root", entries[name]["reason"])

    def test_unsupported_and_invalid_models_are_explicit(self) -> None:
        examples = {
            "binary.mps": b'\x00\xffnot XML',
            "old.mps": model("r:a", '<node concept="c" id="a"/>').replace(b'version="9"', b'version="8"'),
            "duplicate.mps": model("r:a", '<node concept="c" id="a"/><node concept="c" id="a"/>'),
            "custom.mps": b'<custom-storage/>',
            "entity.mps": b'<!DOCTYPE model [<!ENTITY value "secret">]><model/>',
            "utf16.mps": '<model/>'.encode("utf-16"),
            "latin.mps": b'<?xml version="1.0" encoding="ISO-8859-1"?><model/>',
        }
        parsed = mps.parse_project(examples)
        self.assertTrue(all(entry["kind"] == "unsupported" for entry in parsed["files"]))
        self.assertEqual({}, parsed["reference_counts"])
        with self.assertRaisesRegex(mps.MpsError, "nesting"):
            mps.parse_document(b'<model>' * (mps.MAX_XML_DEPTH + 1) + b'</model>' * (mps.MAX_XML_DEPTH + 1))
        with mock.patch.object(mps, "MAX_XML_ELEMENTS", 2), self.assertRaisesRegex(mps.MpsError, "element"):
            mps.parse_document(b'<model><one/><two/></model>')
        with mock.patch.object(mps, "MAX_FILE_BYTES", 4), self.assertRaisesRegex(mps.MpsError, "file limit"):
            mps.parse_document(b'<model/>')

    def test_zip_resolves_members_and_enforces_paths_and_size_without_extraction(self) -> None:
        source = model("r:a", '<node concept="c" id="a"><ref role="r" to="i:b"/></node>', '<import index="i" ref="r:b"/>')
        target = model("r:b", '<node concept="c" id="b"/>')
        parsed = mps.project_from_attachment("project.zip", archive({"a.mps": source, "models/b.mps": target, "secret.txt": b'ignore'}))
        self.assertEqual(2, len(parsed["files"]))
        self.assertEqual(1, parsed["reference_counts"]["resolved"])
        self.assertIsNone(mps.project_from_attachment("other.zip", archive({"file.txt": b'hi'})))
        for name in ("../escape.mps", "/absolute.mps", "C:/model.mps", "dir\\model.mps", "model.mps\0hidden.mps"):
            with self.subTest(name=name), self.assertRaisesRegex(mps.MpsError, "unsafe"):
                mps.project_from_attachment("project.zip", archive({name: source}))
        with self.assertRaisesRegex(mps.MpsError, "duplicate"):
            mps.project_from_attachment("project.zip", archive({"A.mps": source, "a.mps": source}))
        with mock.patch.object(mps, "MAX_FILE_BYTES", 4), self.assertRaisesRegex(mps.MpsError, "oversized"):
            mps.project_from_attachment("project.zip", archive({"a.mps": source}))
        with mock.patch.object(mps, "MAX_PROJECT_FILES", 1), self.assertRaisesRegex(mps.MpsError, "128 file"):
            mps.project_from_attachment("project.zip", archive({"a.mps": source, "b.mps": target}))

    def test_navigation_is_bounded_valid_json_and_reports_omissions(self) -> None:
        source = model("r:a", ''.join(f'<node concept="c" id="n{i}"><property role="p" value="{i}"/></node>' for i in range(200)))
        rendered = mps.render_navigation(mps.parse_project({"large.mps": source}))
        self.assertLessEqual(len(rendered.encode()), mps.MAX_SUMMARY_BYTES)
        parsed = json.loads(rendered)
        entry = parsed["files"][0]
        self.assertGreater(entry["omitted_nodes"], 0)
        self.assertEqual(entry["node_count"], len(entry["nodes"]) + entry["omitted_nodes"])
        self.assertIn("runtime execution order", ' '.join(parsed["limitations"]))


class MpsEvidenceTests(unittest.TestCase):
    def test_context_hydrates_zip_member_routes_and_rejects_changed_original(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "repo").mkdir()
            config = root / "brain.toml"
            config.write_text("[project]\nname='mps-test'\n[[repositories]]\nname='repo'\npath='repo'\n", encoding="utf-8")
            settings = load_settings(config)
            start_session(settings, "MPS-ROUTE", "Trace FlowA and propose an exact model edit.")
            supplied = root / "models.zip"
            supplied.write_bytes(archive({
                "a.mps": model("f:a", '<node concept="c" id="a"><property role="p" value="FlowA"/>'
                               '<ref role="r" to="b:inside"/></node>', '<import index="b" ref="f:b"/>'),
                "b.mps": model("f:b", '<node concept="c" id="b"><property role="p" value="FlowB"/>'
                               '<node concept="c" id="inside" role="h"><property role="p" value="Decision"/></node></node>'),
                "usage.mps": model("f:usage", '<node concept="c" id="instance"><ref role="r" node="target"/></node>'
                                   '<node concept="c" id="target"/>').replace(b'language-id', b'26b3d6d5-b99a-4ed6-83be-d2ea6f3627a1').replace(
                                       b'concept-id', b'1241363083334').replace(b'reference-id', b'1241363105304'),
                "calculator/calculator.mpl": (FIXTURES / "calculator_language.mpl").read_bytes(),
                "calculator/languageModels/structure.mps": (FIXTURES / "calculator_structure.mps").read_bytes(),
            }))
            _, _, _, stored = add_external_evidence(settings, "MPS-ROUTE", supplied)
            request = {"version": 4, "objective": "Inspect FlowA authoring source", "resolve": ["FlowA"]}
            context, _, _ = create_context(settings, "MPS-ROUTE", json.dumps({"INVESTIGATION_REQUEST": request}))
            self.assertIn("Requested MPS connections and source", context)
            self.assertIn('"target_root"', context)
            self.assertIn('"node_id": "inside"', context)
            self.assertIn('value=\\"Decision\\"', context)
            self.assertIn("not pinned repository proof", context)
            definition_request = {"version": 4, "objective": "Read the exact language declaration", "resolve": ["usage.mps#id:instance"]}
            defined, _, _ = create_context(settings, "MPS-ROUTE", json.dumps({"INVESTIGATION_REQUEST": definition_request}))
            self.assertIn('"definition_anchors"', defined)
            self.assertIn('"key": "id:i470soo"', defined)
            self.assertIn('"path": "calculator/languageModels/structure.mps"', defined)
            reverse_request = {"mode": "impact_analysis", "anchors": [{"kind": "symbol", "value": "b.mps#id:inside"}]}
            reverse = _external_evidence(settings, "MPS-ROUTE", request=reverse_request)[0].content
            self.assertIn('"label": "FlowA"', reverse)
            stored.write_bytes(archive({"poison.mps": model("f:poison", '<node concept="c" id="poison"/>')}))
            rejected = _external_evidence(settings, "MPS-ROUTE", request=request)[0].content
            self.assertIn("original attachment SHA-256 does not match", rejected)
            self.assertNotIn("## Requested MPS connections and source", rejected)

    def test_original_model_is_preserved_and_decoded_navigation_reaches_later_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "brain.toml"
            (root / "repo").mkdir()
            config.write_text("[project]\nname='mps-test'\n[[repositories]]\nname='repo'\npath='repo'\n", encoding="utf-8")
            settings = load_settings(config)
            start_session(settings, "MPS-1", "Inspect MPS nesting.")
            source = (FIXTURES / "blreferences_model.mps").read_bytes()
            supplied = root / "model.mps"
            supplied.write_bytes(source)
            content, artifact, number, stored = add_external_evidence(settings, "MPS-1", supplied)
            self.assertEqual(source, stored.read_bytes())
            self.assertEqual(1, number)
            self.assertIn(hashlib.sha256(source).hexdigest(), content)
            self.assertIn("MPS structural navigation", content)
            self.assertIn('"resolved"', content)
            self.assertIn("not repository proof", content)
            self.assertEqual(content, artifact.read_text(encoding="utf-8"))
            self.assertEqual("waiting_for_ai", session_state(settings, "MPS-1")["status"])
            self.assertIn("MPS structural navigation", _external_evidence(settings, "MPS-1")[0].content)

    def test_zip_failure_preserves_bytes_and_fence_cannot_be_closed_by_model_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "brain.toml"
            (root / "repo").mkdir()
            config.write_text("[project]\nname='mps-test'\n[[repositories]]\nname='repo'\npath='repo'\n", encoding="utf-8")
            settings = load_settings(config)
            start_session(settings, "MPS-2", "Inspect untrusted MPS evidence.")
            supplied = root / "project.zip"
            source = archive({"../escape.mps": model("r:a", '<node concept="c" id="a"/>')})
            supplied.write_bytes(source)
            content, _, _, stored = add_external_evidence(settings, "MPS-2", supplied)
            self.assertEqual(source, stored.read_bytes())
            self.assertIn("MPS decoding unavailable", content)
            self.assertFalse((root / "escape.mps").exists())
            supplied = root / "model.mps"
            supplied.write_bytes(b'<broken')
            content, _, _, stored = add_external_evidence(settings, "MPS-2", supplied)
            self.assertEqual(b'<broken', stored.read_bytes())
            self.assertIn("MPS decoding unavailable", content)
            self.assertNotIn("## MPS structural navigation", content)
            supplied.write_bytes(model("r:a", '<node concept="c" id="a"><property role="p" value="```"/></node>'))
            content, _, _, _ = add_external_evidence(settings, "MPS-2", supplied)
            self.assertIn('value="```"', content)
            self.assertIn("~~~xml", content)


if __name__ == "__main__":
    unittest.main()

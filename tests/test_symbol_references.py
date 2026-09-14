from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from brain import core, index
from brain.catalog import current_generation_ref
from brain.retrieval.models import RetrievalTrace


class SymbolReferenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("service", "caller"):
            (self.root / name).mkdir()
        (self.root / "service/Payments.java").write_text(
            "package com.example;\npublic class Payments {\n public void handlePayment() {}\n}\n", encoding="utf-8",
        )
        self.caller = self.root / "caller/Controller.java"
        self.caller.write_text(
            "package com.client;\nimport com.example.Payments;\nclass Controller {\n"
            " void submit(Payments payments) { payments.handlePayment(); }\n}\n", encoding="utf-8",
        )
        self.config = self.root / "brain.toml"
        self.config.write_text(
            "[project]\nname='symbol-references'\n[graph]\nenabled=false\n[experience]\nenabled=false\n"
            "[[repositories]]\nname='service'\npath='service'\n[[repositories]]\nname='caller'\npath='caller'\n",
            encoding="utf-8",
        )
        self.settings = core.load_settings(self.config)

    def request(self, query="com.example.Payments.handlePayment"):
        return {"INVESTIGATION_REQUEST": {
            "version": 5, "mode": "flow_trace", "objective": query,
            "anchors": [{"kind": "symbol", "value": query}], "required": ["callers"],
        }}

    def pinned(self, generation=None):
        generation = generation or current_generation_ref(self.settings)
        return replace(self.settings, atlas_generation=generation, atlas_generation_mode="pinned",
                       repositories=[replace(repo, source_sha=generation.snapshots[repo.name])
                                     for repo in self.settings.repositories])

    def test_java_reference_matching_ignores_noise_without_losing_real_call_sites(self):
        content = (
            '// handlePayment();\r\nString example = "handlePayment()";\r\n'
            '/* handlePayment(); */\r\nvoid handlePayment() {}\r\n'
            'void handlePayment() { other.handlePayment(); }\r\n'
            'var reference = payments::handlePayment;\r\n'
            'payments.\r\nhandlePayment\r\n();\r\n'
            'return handlePayment();\r\n'
            'otherhandlePayment(); handlePaymentExtra();\r\n'
            'String block = """\r\nhandlePayment();\r\n""";\r\n'
            '@handlePayment()\r\n'
        )
        matches = list(index._java_call_reference_lines(content, "handlePayment"))
        self.assertEqual([5, 6, 8, 10], [number for number, _ in matches])
        for number, text in matches:
            self.assertEqual(content.splitlines()[number - 1], text)
        self.assertEqual([(1, 'var ref = payments::handle$;')],
                         list(index._java_call_reference_lines('var ref = payments::handle$;', 'handle$')))

    def test_generic_java_method_references_are_bounded_and_source_masked(self):
        for prefix in ('Payments::', 'Payments::<String>',
                       'Payments:: <java.util.Map<String, java.util.List<Integer>>> ',
                       'Payments::\n <java.util.Map<String, java.util.List<Integer>>>\n ',
                       'Payments:: /* comment */ <String[]> '):
            content = 'var reference = ' + prefix + 'handlePayment;'
            with self.subTest(prefix=prefix):
                hits = list(index._java_call_reference_lines(content, 'handlePayment'))
                self.assertEqual([(len(content.splitlines()), content.splitlines()[-1])], hits)
        for content in ('boolean value = a < b > handlePayment;',
                        'var reference = Payments::<String>handlePaymentExtra;',
                        'var reference = Payments::<List<String>handlePayment;',
                        'var reference = Payments::<>handlePayment;',
                        'var reference = Payments::<"String">handlePayment;',
                        'var reference = Payments::<String + Other>handlePayment;',
                        'var reference = Payments::<' + 'T' * 600 + '>handlePayment;',
                        '// Payments::<String>handlePayment;',
                        'String sample = "Payments::<String>handlePayment";',
                        '/*\nPayments::<String>handlePayment;\n*/',
                        'String sample = """\nPayments::<String>handlePayment;\n""";'):
            with self.subTest(content=content[:60]):
                self.assertEqual([], list(index._java_call_reference_lines(content, 'handlePayment')))
        self.assertEqual([], list(index._java_call_reference_lines('Payments::<String>handlePayment', 'handlePayment', deadline=0)))

    def test_generic_method_reference_source_is_delivered_on_its_ticket_pin(self):
        payment = self.root / 'service/Payments.java'
        payment.write_text('package com.example;\npublic class Payments {\n'
                           ' public static <T> T handlePayment(T value) { return value; }\n}\n', encoding='utf-8')
        caller = ('package com.client;\nimport com.example.Payments;\n'
                  'import java.util.function.Function;\nclass Controller {\n'
                  ' Function<String,String> submit() { return Payments::<String>handlePayment; }\n}\n')
        self.caller.write_text(caller, encoding='utf-8')
        core.snapshot_indexes(self.settings)
        core.start_session(self.settings, 'GENERIC-OLD', 'Read the method reference')
        self.caller.write_text(caller.replace('String', 'Integer'), encoding='utf-8')
        core.snapshot_indexes(self.settings)
        core.start_session(self.settings, 'GENERIC-NEW', 'Read the method reference')
        for ticket, expected, forbidden in (('GENERIC-OLD', 'String', 'Integer'), ('GENERIC-NEW', 'Integer', 'String')):
            with self.subTest(ticket=ticket):
                content, _, _ = core.create_context(self.settings, ticket, json.dumps(self.request()))
                self.assertIn(f'Payments::<{expected}>handlePayment', content)
                self.assertNotIn(f'Payments::<{forbidden}>handlePayment', content)
                runtime = core.session_state(self.settings, ticket)['investigation_runtime']
                self.assertFalse(any(step['repo'] == 'caller' and step['state'] == 'verified'
                                     for step in runtime['execution_flow']['steps']))
                self.assertIn('dispatch', content.casefold())

    def test_java_declarations_mask_full_source_and_preserve_offsets(self):
        content = (
            '/*\r\npublic void handlePayment() {}\r\n*/\r\n'
            'String note = "public void handlePayment() {}";\r\n'
            'String block = """\r\npublic void handlePayment() {}\r\n""";\r\n'
            'void handlePayment() { other.handlePayment(); }\r\n'
            'public void\r\nhandlePayment() {}\r\n'
            'return handlePayment();\r\nother.handlePayment();\r\n'
        )
        matches = list(index._java_declaration_lines(content, "handlePayment"))
        self.assertEqual([8, 10], [number for number, _ in matches])
        self.assertTrue(all(content.splitlines()[number - 1] == text for number, text in matches))
        self.assertEqual([(1, 'class Pay$ {}')], list(index._java_declaration_lines('class Pay$ {}', 'Pay$')))
        self.assertEqual([], list(index._java_declaration_lines(content, 'handlePayment', deadline=0)))
        annotated = '@handlePayment\nvoid handlePayment() {}'
        self.assertEqual([(2, 'void handlePayment() {}')], list(index._java_declaration_lines(annotated, 'handlePayment')))
        for expression in ('return ready ? handlePayment() : fallback();',
                           'if (ready) other(); else handlePayment();',
                           'Object answer = flag ? handlePayment() : other();',
                           'assert handlePayment();', 'yield handlePayment();',
                           'boolean valid = a < b ? enabled : d > handlePayment();',
                           'return foo < bar > handlePayment();', 'if (foo < bar > handlePayment()) {}',
                           'boolean x = foo < bar > handlePayment();', 'return Foo<Bar> handlePayment();'):
            with self.subTest(expression=expression):
                self.assertEqual([], list(index._java_declaration_lines(expression, 'handlePayment')))
        for declaration in ('public List<? extends Rule> handlePayment() {}',
                            'Map<String, List<Rule>> handlePayment() {}',
                            'public java.util.Map<String, List<Rule>> [] handlePayment() {}',
                            'abstract void handlePayment();', '@Override public void handlePayment() {}',
                            'public <T extends Rule & Comparable<T>> List<T> handlePayment() {}'):
            with self.subTest(declaration=declaration):
                self.assertEqual([(1, declaration)], list(index._java_declaration_lines(declaration, 'handlePayment')))

    def test_java_declarations_filter_before_budgets_and_stay_scoped(self):
        source = '// public void handlePayment() {}\n' * 200 + 'class Controller { void handlePayment() {} }\n'
        self.caller.write_text(source, encoding='utf-8')
        (self.root / 'caller/A-guide.md').write_text('public void handlePayment() {}\n', encoding='utf-8')
        core.snapshot_indexes(self.settings)
        pinned = self.pinned()
        stats = {}
        arguments = dict(max_results=1, max_candidate_files=1, max_hits=1, max_bytes=30_000,
                         max_seconds=5, java_declarations_only=True, stats=stats)
        hits = index.query_generation_indexes(pinned, pinned.atlas_generation, [pinned.repo('caller')],
                                             'handlePayment', **arguments)
        self.assertEqual({'caller': [('Controller.java', 201, source.splitlines()[-1])]}, hits)
        self.assertEqual(1, stats['candidate_files'])
        self.assertEqual('hits', stats['reason'])
        self.assertIsNone(index.query_generation_indexes(pinned, pinned.atlas_generation, pinned.repositories,
                                                         'handlePayment', java_calls_only=True, **arguments))

    def test_java_declaration_cache_preserves_generation_scope_and_reports_incomplete(self):
        core.snapshot_indexes(self.settings)
        old = self.pinned()
        self.caller.write_text('class Controller { void handlePayment() { NEW(); } }\n', encoding='utf-8')
        core.snapshot_indexes(self.settings)
        new = self.pinned()
        cache = core._ACTIVE_RETRIEVAL_CACHE.set({})
        self.addCleanup(core._ACTIVE_RETRIEVAL_CACHE.reset, cache)
        trace = RetrievalTrace()
        token = core._ACTIVE_RETRIEVAL_TRACE.set(trace)
        self.addCleanup(core._ACTIVE_RETRIEVAL_TRACE.reset, token)
        with mock.patch.object(index, 'query_generation_indexes', wraps=index.query_generation_indexes) as lookup:
            for serving, expected in ((old, []), (new, ['caller']), (old, [])):
                hits = core._symbol_definition_hits(serving, 'handlePayment', ['caller'], [])
                self.assertEqual(expected, [hit.repo for hit in hits])
            self.assertEqual(2, lookup.call_count)
        core._ACTIVE_RETRIEVAL_CACHE.get().clear()
        for serving in (replace(new, atlas_generation=replace(new.atlas_generation, components={})),
                        new):
            with self.subTest(missing=not serving.atlas_generation.components), \
                    mock.patch.object(core, 'MAX_PINNED_QUERY_BYTES', 1):
                self.assertEqual([], core._symbol_definition_hits(serving, 'handlePayment', ['caller'], []))
                self.assertEqual({}, core._ACTIVE_RETRIEVAL_CACHE.get())
        self.assertTrue(any('unavailable' in item for item in trace.fallback_reasons))
        self.assertTrue(any('bytes' in item for item in trace.fallback_reasons))
        self.assertTrue(all('source availability is unknown' in item for item in trace.fallback_reasons))
        connection = index._connect(new)
        try:
            connection.execute("UPDATE blobs SET content='corrupt' WHERE blob IN "
                               "(SELECT blob FROM file_membership WHERE repo='caller' AND path='Controller.java')")
            connection.commit()
        finally:
            connection.close()
        self.assertEqual([], core._symbol_definition_hits(new, 'handlePayment', ['caller'], []))

    def test_lexical_scan_shares_validated_declarations_but_not_partial_results(self):
        core.snapshot_indexes(self.settings)
        pinned = self.pinned()
        cache = {}
        token = core._ACTIVE_RETRIEVAL_CACHE.set(cache)
        self.addCleanup(core._ACTIVE_RETRIEVAL_CACHE.reset, token)
        with mock.patch.object(index, 'query_generation_indexes', wraps=index.query_generation_indexes) as lookup:
            lexical = core.search(pinned, 'handlePayment', ['service'], fixed=True)
            definitions = core._symbol_definition_hits(pinned, 'handlePayment', ['service'], lexical)
            self.assertEqual([3], [hit.line for hit in definitions])
            self.assertEqual(1, lookup.call_count)
            self.assertTrue(lookup.call_args.kwargs['collect_java_declarations'])
        cache.clear()
        limited = replace(pinned, max_results=1)
        with mock.patch.object(index, 'query_generation_indexes', wraps=index.query_generation_indexes) as lookup:
            lexical = core.search(limited, 'handlePayment', ['service'], fixed=True)
            self.assertFalse(any(key[0] == 'java-declarations' for key in cache))
            definitions = core._symbol_definition_hits(limited, 'handlePayment', ['service'], lexical)
            self.assertEqual([3], [hit.line for hit in definitions])
            self.assertEqual(2, lookup.call_count)
            self.assertFalse(any(key[0] == 'java-declarations' for key in cache))

    def test_legacy_multiline_references_share_one_pinned_lexical_pass(self):
        self.caller.write_text('class Controller { Object callback = Payments::\n <String>\n handlePayment; }\n', encoding='utf-8')
        core.snapshot_indexes(self.settings)
        pinned = self.pinned()
        requests = [
            {'CONTEXT_REQUEST': {'version': 2, 'objective': 'Find callers of handlePayment',
                                'symbols': [{'name': 'handlePayment', 'repos': ['caller', 'service'], 'include': ['definition', 'callers']}]}},
            {'CONTEXT_REQUEST': {'version': 3, 'objective': 'Find callers of handlePayment',
                                'hints': {'symbols': ['handlePayment'], 'repos': ['caller', 'service']}}},
        ]
        for request in requests:
            with self.subTest(version=request['CONTEXT_REQUEST']['version']), \
                    mock.patch.object(index, 'query_generation_indexes', wraps=index.query_generation_indexes) as queries:
                bundle = core.retrieve_context(pinned, core.parse_context_request(json.dumps(request)))
                evidence = [item for item in bundle.evidence if item.path == 'Controller.java' and 'reference candidate' in item.kind]
                self.assertTrue(evidence, [(item.path, item.kind) for item in bundle.evidence])
                self.assertIn('<String>\n handlePayment', evidence[0].content)
                self.assertFalse(any('CALLS' in item for item in bundle.relationships))
                lexical = [call for call in queries.call_args_list if not call.kwargs.get('test_only')]
                self.assertEqual(1, len(lexical))
                self.assertTrue(lexical[0].kwargs['collect_java_call_references'])
                self.assertFalse(lexical[0].kwargs.get('java_calls_only'))

    def test_legacy_reference_cache_keeps_generation_scope_and_owned_hits(self):
        self.caller.write_text('class Controller { Object callback = Payments::\n <String>\n handlePayment; }\n', encoding='utf-8')
        core.snapshot_indexes(self.settings)
        old = self.pinned()
        self.caller.write_text('class Controller { Object callback = Payments::\n <Integer>\n handlePayment; void run() { handlePayment(NEW); } }\n', encoding='utf-8')
        core.snapshot_indexes(self.settings)
        new = self.pinned()
        cache = {}
        token = core._ACTIVE_RETRIEVAL_CACHE.set(cache)
        self.addCleanup(core._ACTIVE_RETRIEVAL_CACHE.reset, token)
        with mock.patch.object(index, 'query_generation_indexes', wraps=index.query_generation_indexes) as queries:
            for serving, expected in ((old, 'handlePayment; }'), (new, 'handlePayment; void run()'), (old, 'handlePayment; }')):
                hits, _ = core.trace_symbol(serving, 'handlePayment', ['caller', 'service'])
                refs = [hit for hit in hits if hit.repo == 'caller' and 'reference candidate' in hit.kind]
                self.assertTrue(any(expected in hit.text for hit in refs), [(hit.text, hit.kind) for hit in hits])
                if serving is old:
                    self.assertFalse(any('NEW' in hit.text for hit in hits))
                refs[0].text = 'poisoned return value'
                refs[0].found_by.append('poisoned caller list')
            self.assertEqual(2, queries.call_count)
            self.assertTrue(all(call.kwargs.get('collect_java_call_references') for call in queries.call_args_list))
            for hit in core.trace_symbol(old, 'handlePayment', ['caller', 'service'])[0]:
                self.assertNotIn('poisoned', hit.text)
                self.assertNotIn('poisoned caller list', hit.found_by)
            self.assertEqual(2, queries.call_count)

    def test_ordinary_search_cache_is_generation_scope_and_limit_aware(self):
        self.caller.write_text('handlePayment(OLD);\nhandlePayment(SECOND);\n', encoding='utf-8')
        core.snapshot_indexes(self.settings)
        old = self.pinned()
        self.caller.write_text('handlePayment(NEW);\nhandlePayment(SECOND);\n', encoding='utf-8')
        core.snapshot_indexes(self.settings)
        new = self.pinned()
        token = core._ACTIVE_RETRIEVAL_CACHE.set({})
        self.addCleanup(core._ACTIVE_RETRIEVAL_CACHE.reset, token)
        with mock.patch.object(index, 'query_generation_indexes', wraps=index.query_generation_indexes) as queries, \
                mock.patch.object(index, '_java_call_reference_lines', side_effect=AssertionError('ordinary query caller scan')):
            for serving, marker in ((old, 'OLD'), (new, 'NEW'), (old, 'OLD')):
                hits = core.search(serving, 'handlePayment', ['caller'], fixed=True)
                self.assertEqual(2, len(hits))
                self.assertIn(marker, hits[0].text)
            self.assertEqual(2, queries.call_count)
            self.assertEqual({'service'}, {hit.repo for hit in core.search(old, 'handlePayment', ['service'])})
            self.assertEqual(1, len(core.search(replace(old, max_results=1), 'handlePayment', ['caller'])))
            self.assertEqual(4, queries.call_count)
            self.assertEqual(2, len(core.search(old, 'handlePayment', ['caller'])))
            self.assertEqual(4, queries.call_count)
            core.search(replace(old, candidate_limit=20), 'handlePayment', ['caller'])
            self.assertEqual(5, queries.call_count)
            elsewhere = replace(old, state_dir=self.root / 'unavailable-state')
            self.assertEqual([], core.search(elsewhere, 'handlePayment', ['caller']))
            self.assertEqual(6, queries.call_count)

    def test_warm_projection_cannot_hide_a_missing_component_registration(self):
        core.snapshot_indexes(self.settings)
        pinned = self.pinned()
        cache = {('java-call-queries',): {'handlePayment'}}
        token = core._ACTIVE_RETRIEVAL_CACHE.set(cache)
        self.addCleanup(core._ACTIVE_RETRIEVAL_CACHE.reset, token)
        core.search(pinned, 'handlePayment', ['caller'], fixed=True)
        self.assertTrue(core._java_caller_reference_hits(pinned, 'handlePayment', ['caller'])[0])
        missing = replace(pinned, atlas_generation=replace(pinned.atlas_generation, components={}))
        hits, reason = core._java_caller_reference_hits(missing, 'handlePayment', ['caller'])
        self.assertEqual([], hits)
        self.assertIn('unavailable', reason)

    def test_path_cache_keeps_generation_and_path_limits(self):
        source = self.root / 'caller/PaymentOldTest.java'
        source.write_text('class PaymentOldTest {}\n', encoding='utf-8')
        core.snapshot_indexes(self.settings)
        old = self.pinned()
        source.rename(self.root / 'caller/PaymentNewTest.java')
        (self.root / 'caller/PaymentOtherTest.java').write_text('class PaymentOtherTest {}\n', encoding='utf-8')
        core.snapshot_indexes(self.settings)
        new = self.pinned()
        token = core._ACTIVE_RETRIEVAL_CACHE.set({})
        self.addCleanup(core._ACTIVE_RETRIEVAL_CACHE.reset, token)
        with mock.patch.object(index, 'query_generation_paths', wraps=index.query_generation_paths) as queries:
            for serving, expected in ((old, {'PaymentOldTest.java'}),
                                      (new, {'PaymentNewTest.java', 'PaymentOtherTest.java'}),
                                      (old, {'PaymentOldTest.java'})):
                self.assertEqual(expected, {hit.path for hit in core.path_hits(serving, 'Payment', ['caller'])})
            self.assertEqual(2, queries.call_count)
            self.assertEqual(1, len(core.path_hits(replace(new, path_result_limit=1), 'Payment', ['caller'])))
            self.assertEqual(3, queries.call_count)

    def test_test_source_cache_keeps_limits_and_workspace(self):
        (self.root / 'caller/PaymentTest.java').write_text('handlePayment(ONE);\nhandlePayment(TWO);\n', encoding='utf-8')
        core.snapshot_indexes(self.settings)
        pinned = self.pinned()
        token = core._ACTIVE_RETRIEVAL_CACHE.set({})
        self.addCleanup(core._ACTIVE_RETRIEVAL_CACHE.reset, token)
        self.assertEqual(2, len(core.test_hits(pinned, 'handlePayment', ['caller'])))
        self.assertEqual(1, len(core.test_hits(replace(pinned, max_results=1), 'handlePayment', ['caller'])))
        self.assertEqual([], core.test_hits(replace(pinned, state_dir=self.root / 'unavailable-state'),
                                            'handlePayment', ['caller']))

    def test_partial_reference_projection_does_not_change_lexical_results_or_seed_negative_cache(self):
        (self.root / 'service/Payments.java').write_text('class Payments {}\n', encoding='utf-8')
        self.caller.write_text('first.handlePayment();\nsecond.handlePayment();\n', encoding='utf-8')
        core.snapshot_indexes(self.settings)
        pinned = replace(self.pinned(), max_results=1)
        stats = {}
        arguments = dict(max_results=1, max_candidate_files=10, max_hits=2, max_bytes=10000, max_seconds=5)
        raw = index.query_generation_indexes(pinned, pinned.atlas_generation, pinned.repositories,
                                             'handlePayment', **arguments)
        combined = index.query_generation_indexes(pinned, pinned.atlas_generation, pinned.repositories,
                                                  'handlePayment', stats=stats, collect_java_call_references=True, **arguments)
        self.assertEqual(raw, combined)
        self.assertFalse(stats['budget_exhausted'])
        self.assertFalse(stats['java_call_references_complete'])
        cache = {('java-call-queries',): {'handlePayment'}}
        token = core._ACTIVE_RETRIEVAL_CACHE.set(cache)
        self.addCleanup(core._ACTIVE_RETRIEVAL_CACHE.reset, token)
        with mock.patch.object(index, 'query_generation_indexes', wraps=index.query_generation_indexes) as queries:
            self.assertTrue(core.search(pinned, 'handlePayment', fixed=True))
            self.assertFalse(any(key[0] == 'java-caller-references' for key in cache))
            hits, reason = core._java_caller_reference_hits(pinned, 'handlePayment')
            self.assertTrue(hits)
            self.assertIn('budget', reason)
            self.assertEqual(2, queries.call_count)
            self.assertFalse(any(key[0] == 'java-caller-references' for key in cache))

    def test_noise_or_source_byte_limit_cannot_cache_missing_references(self):
        (self.root / 'caller/A-guide.md').write_text('handlePayment();\n', encoding='utf-8')
        core.snapshot_indexes(self.settings)
        pinned = self.pinned()
        cache = {('java-call-queries',): {'handlePayment'}}
        token = core._ACTIVE_RETRIEVAL_CACHE.set(cache)
        self.addCleanup(core._ACTIVE_RETRIEVAL_CACHE.reset, token)
        for limit, value in (('MAX_PINNED_QUERY_CANDIDATE_FILES', 1), ('MAX_PINNED_QUERY_BYTES', 1)):
            with self.subTest(limit=limit), mock.patch.object(core, limit, value):
                core.search(pinned, 'handlePayment', ['caller'], fixed=True)
                self.assertFalse(any(key[0] == 'java-caller-references' for key in cache))
        with mock.patch.object(index, 'query_generation_indexes', wraps=index.query_generation_indexes) as queries:
            hits, reason = core._java_caller_reference_hits(pinned, 'handlePayment', ['caller'])
            self.assertIsNone(reason)
            self.assertEqual(['Controller.java'], [hit.path for hit in hits])
            self.assertEqual(1, queries.call_count)

    def test_standalone_trace_cache_is_removed_even_after_failure(self):
        self.assertIsNone(core._ACTIVE_RETRIEVAL_CACHE.get())
        with mock.patch.object(core, '_trace_symbol', side_effect=RuntimeError('trace failed')):
            with self.assertRaisesRegex(RuntimeError, 'trace failed'):
                core.trace_symbol(self.settings, 'handlePayment')
        self.assertIsNone(core._ACTIVE_RETRIEVAL_CACHE.get())

    def test_legacy_request_reports_incomplete_caller_verification(self):
        core.snapshot_indexes(self.settings)
        request = core.parse_context_request(json.dumps({'CONTEXT_REQUEST': {
            'version': 2, 'objective': 'Find callers of handlePayment',
            'symbols': [{'name': 'handlePayment', 'repos': ['caller', 'service'], 'include': ['callers']}],
        }}))
        with mock.patch.object(core, '_java_caller_reference_hits', return_value=([], 'physical operation budget')):
            bundle = core.retrieve_context(self.pinned(), request)
        self.assertTrue(any('Java caller verification is incomplete: physical operation budget' in item
                            for item in bundle.unresolved), bundle.unresolved)

    def test_late_caller_intent_reports_budget_then_reuses_completed_projection(self):
        self.caller.write_text('var reference = Payments::\n <String>\n handlePayment;\n', encoding='utf-8')
        core.snapshot_indexes(self.settings)
        pinned = self.pinned()
        cache_token = core._ACTIVE_RETRIEVAL_CACHE.set({})
        self.addCleanup(core._ACTIVE_RETRIEVAL_CACHE.reset, cache_token)
        trace = RetrievalTrace(max_physical_backend_operations=1)
        trace_token = core._ACTIVE_RETRIEVAL_TRACE.set(trace)
        self.addCleanup(core._ACTIVE_RETRIEVAL_TRACE.reset, trace_token)
        with mock.patch.object(index, 'query_generation_indexes', wraps=index.query_generation_indexes) as queries:
            core.search(pinned, 'handlePayment', ['caller'], fixed=True)
            hits, _ = core.trace_symbol(pinned, 'handlePayment', ['caller'])
            self.assertFalse(any('reference candidate' in hit.kind for hit in hits))
            self.assertTrue(any('Java caller verification is incomplete: physical operation budget' in item
                                for item in trace.fallback_reasons))
            self.assertEqual(1, queries.call_count)
            core._ACTIVE_RETRIEVAL_TRACE.set(RetrievalTrace(max_physical_backend_operations=1))
            for _ in range(2):
                hits, _ = core.trace_symbol(pinned, 'handlePayment', ['caller'])
                self.assertTrue(any('reference candidate' in hit.kind for hit in hits))
            self.assertEqual(2, queries.call_count)

    def test_direct_trace_shares_one_source_mask_for_both_projections(self):
        from brain import investigation

        self.caller.write_text('// example handlePayment();\nvar ref = Payments::\n handlePayment;\n', encoding='utf-8')
        core.snapshot_indexes(self.settings)
        self.assertIsNone(investigation._SOURCE_VERIFICATION_CACHE.get())
        with mock.patch.object(investigation, '_mask_java_comments_uncached', wraps=investigation._mask_java_comments_uncached) as mask:
            hits, _ = core.trace_symbol(self.pinned(), 'handlePayment', ['caller'])
            self.assertTrue(any('reference candidate' in hit.kind for hit in hits))
            self.assertEqual(1, mask.call_count)
        self.assertIsNone(investigation._SOURCE_VERIFICATION_CACHE.get())

    def test_noise_is_filtered_before_hit_budget_and_non_java_before_file_budget(self):
        self.caller.write_text('// handlePayment();\n' * 200 +
                               'class Controller { void run() { payments.handlePayment(); } }\n', encoding="utf-8")
        (self.root / 'caller/A-guide.md').write_text('handlePayment();\n' * 100, encoding='utf-8')
        core.snapshot_indexes(self.settings)
        pinned = self.pinned()
        stats = {}
        hits = index.query_generation_indexes(
            pinned, pinned.atlas_generation, [pinned.repo('caller')], 'handlePayment',
            max_results=1, max_candidate_files=1, max_hits=1, max_bytes=10_000, max_seconds=5,
            stats=stats, java_calls_only=True,
        )
        self.assertEqual([('Controller.java', 201, 'class Controller { void run() { payments.handlePayment(); } }')], hits['caller'])
        self.assertEqual(1, stats['candidate_files'])
        self.assertEqual(1, stats['hits'])
        index.query_generation_indexes(
            pinned, pinned.atlas_generation, [pinned.repo('caller')], 'handlePayment',
            max_results=1, max_candidate_files=1, max_hits=10, max_bytes=10_000, max_seconds=5,
            stats=stats, java_calls_only=True,
        )
        self.assertTrue(stats['budget_exhausted'])
        self.assertEqual('repository_hits', stats['reason'])

    def test_reference_cache_and_partial_results_do_not_cross_generation_or_hide_failure(self):
        self.caller.write_text("class Controller { void run() { payments.handlePayment(OLD); } }\n", encoding='utf-8')
        core.snapshot_indexes(self.settings)
        old = current_generation_ref(self.settings)
        self.caller.write_text("class Controller { void run() { payments.handlePayment(NEW); } }\n", encoding='utf-8')
        core.snapshot_indexes(self.settings)
        new = current_generation_ref(self.settings)
        cache = core._ACTIVE_RETRIEVAL_CACHE.set({})
        self.addCleanup(core._ACTIVE_RETRIEVAL_CACHE.reset, cache)
        trace = RetrievalTrace()
        token = core._ACTIVE_RETRIEVAL_TRACE.set(trace)
        self.addCleanup(core._ACTIVE_RETRIEVAL_TRACE.reset, token)
        with mock.patch.object(index, 'query_generation_indexes', wraps=index.query_generation_indexes) as query:
            for generation, marker in ((old, 'OLD'), (new, 'NEW'), (old, 'OLD')):
                hits, reason = core._java_caller_reference_hits(self.pinned(generation), 'Payments.handlePayment')
                self.assertIsNone(reason)
                self.assertEqual(1, len(hits))
                self.assertIn(marker, hits[0].text)
            self.assertEqual(2, query.call_count)
        self.assertEqual(2, trace.physical_backend_operations)
        core._ACTIVE_RETRIEVAL_CACHE.get().clear()
        with mock.patch.object(core, 'MAX_PINNED_QUERY_BYTES', 1):
            hits, reason = core._java_caller_reference_hits(self.pinned(old), 'Payments.handlePayment')
        self.assertEqual([], hits)
        self.assertIn('bytes', reason)
        self.assertEqual({}, core._ACTIVE_RETRIEVAL_CACHE.get())
        self.assertEqual(1, len(core._java_caller_reference_hits(self.pinned(old), 'Payments.handlePayment')[0]))

    def test_missing_corrupt_component_and_physical_budget_fail_closed(self):
        core.snapshot_indexes(self.settings)
        pinned = self.pinned()
        trace = RetrievalTrace(max_physical_backend_operations=0)
        token = core._ACTIVE_RETRIEVAL_TRACE.set(trace)
        try:
            with mock.patch.object(index, 'query_generation_indexes', side_effect=AssertionError('budget bypass')):
                self.assertEqual(([], 'physical operation budget'),
                                 core._java_caller_reference_hits(pinned, 'Payments.handlePayment'))
        finally:
            core._ACTIVE_RETRIEVAL_TRACE.reset(token)
        missing = replace(pinned.atlas_generation, components={})
        with mock.patch.object(core, 'search', side_effect=AssertionError('no newer-generation fallback')):
            hits, reason = core._java_caller_reference_hits(replace(pinned, atlas_generation=missing), 'Payments.handlePayment')
            self.assertEqual([], hits)
            self.assertIn('unavailable', reason)
            connection = index._connect(pinned)
            try:
                connection.execute("UPDATE blobs SET content='corrupt' WHERE blob IN "
                                   "(SELECT blob FROM file_membership WHERE repo='caller' AND path='Controller.java')")
                connection.commit()
            finally:
                connection.close()
            hits, reason = core._java_caller_reference_hits(pinned, 'Payments.handlePayment')
            self.assertEqual([], hits)
            self.assertIn('unavailable', reason)

    def test_unavailable_fallbacks_retry_without_caching_false_absence(self):
        (self.root / 'caller/PaymentTest.java').write_text('payments.handlePayment();\n', encoding='utf-8')
        core.snapshot_indexes(self.settings)
        pinned = self.pinned()
        pinned = replace(pinned, atlas_generation=replace(pinned.atlas_generation, components={
            **pinned.atlas_generation.components, 'zoekt': {'status': 'unavailable'},
        }))
        connection = index._connect(pinned)
        self.addCleanup(connection.close)
        rows = connection.execute('SELECT repo,snapshot_sha,indexed_at,file_count,membership_hash FROM indexed_snapshots').fetchall()
        lookups = (
            ('search', lambda value: core.search(pinned, value, ['caller'], fixed=True), 'handlePayment'),
            ('path', lambda value: core.path_hits(pinned, value, ['caller']), 'PaymentTest.java'),
            ('test-search', lambda value: core.test_hits(pinned, value, ['caller']), 'handlePayment'),
        )
        for kind, lookup, value in lookups:
            with self.subTest(kind=kind):
                connection.execute('DELETE FROM indexed_snapshots')
                connection.commit()
                cache = {}
                token = core._ACTIVE_RETRIEVAL_CACHE.set(cache)
                trace_token = core._ACTIVE_RETRIEVAL_TRACE.set(RetrievalTrace())
                try:
                    self.assertEqual([], lookup(value))
                    self.assertFalse(any(key[0] in {'search', 'path', 'test-search'} for key in cache))
                    self.assertIn('lookup_unavailable', ' '.join(core._ACTIVE_RETRIEVAL_TRACE.get().fallback_reasons))
                    connection.executemany('INSERT INTO indexed_snapshots VALUES (?,?,?,?,?)', rows)
                    connection.commit()
                    self.assertTrue(lookup(value))
                    self.assertEqual([], lookup('absent_lookup_value'))
                    with mock.patch.object(index, 'query_generation_indexes', side_effect=AssertionError('uncached completed search')), \
                            mock.patch.object(index, 'query_generation_paths', side_effect=AssertionError('uncached completed path')):
                        self.assertTrue(lookup(value))
                        self.assertEqual([], lookup('absent_lookup_value'))
                finally:
                    core._ACTIVE_RETRIEVAL_TRACE.reset(trace_token)
                    core._ACTIVE_RETRIEVAL_CACHE.reset(token)

    def test_malformed_lexical_details_fail_closed_without_caching_absence(self):
        core.snapshot_indexes(self.settings)
        pinned = self.pinned()
        generation = pinned.atlas_generation
        invalid_details = [1], 'invalid', 1, {'snapshots': [1]}, {
            **generation.component('lexical')['details'], 'repository_files': {'caller': None},
        }
        for details in invalid_details:
            with self.subTest(details=details):
                broken = replace(pinned, atlas_generation=replace(generation, components={
                    **generation.components,
                    'lexical': {**generation.component('lexical'), 'details': details},
                    'zoekt': {'status': 'unavailable'},
                }))
                cache = {}
                trace = RetrievalTrace()
                cache_token = core._ACTIVE_RETRIEVAL_CACHE.set(cache)
                trace_token = core._ACTIVE_RETRIEVAL_TRACE.set(trace)
                try:
                    self.assertEqual([], core.search(broken, 'handlePayment', ['caller'], fixed=True))
                    self.assertIn('lexical_lookup_unavailable', trace.fallback_reasons)
                    self.assertFalse(any(key[0] in {'search', 'component-validation'} for key in cache))
                finally:
                    core._ACTIVE_RETRIEVAL_TRACE.reset(trace_token)
                    core._ACTIVE_RETRIEVAL_CACHE.reset(cache_token)

    def test_partial_repository_fallback_does_not_hide_later_recovered_source(self):
        core.snapshot_indexes(self.settings)
        pinned = self.pinned()
        pinned = replace(pinned, atlas_generation=replace(pinned.atlas_generation, components={
            **pinned.atlas_generation.components, 'zoekt': {'status': 'unavailable'},
        }))
        connection = index._connect(pinned)
        self.addCleanup(connection.close)
        row = connection.execute('SELECT repo,snapshot_sha,indexed_at,file_count,membership_hash FROM indexed_snapshots WHERE repo=?',
                                 ('caller',)).fetchone()
        connection.execute('DELETE FROM indexed_snapshots WHERE repo=?', ('caller',))
        connection.commit()
        cache = {}
        token = core._ACTIVE_RETRIEVAL_CACHE.set(cache)
        self.addCleanup(core._ACTIVE_RETRIEVAL_CACHE.reset, token)
        self.assertEqual({'service'}, {hit.repo for hit in core.search(pinned, 'handlePayment', fixed=True)})
        self.assertFalse(any(key[0] == 'search' for key in cache))
        connection.execute('INSERT INTO indexed_snapshots VALUES (?,?,?,?,?)', row)
        connection.commit()
        self.assertEqual({'service', 'caller'}, {hit.repo for hit in core.search(pinned, 'handlePayment', fixed=True)})

    def test_current_configuration_drift_cannot_invalidate_a_pinned_reference_lookup(self):
        core.snapshot_indexes(self.settings)
        pinned = self.pinned()
        extra = replace(pinned.repo('caller'), name='new', path=self.root / 'new')
        changed = replace(pinned, repositories=[*pinned.repositories, extra])
        hits, reason = core._java_caller_reference_hits(changed, 'Payments.handlePayment')
        self.assertIsNone(reason)
        self.assertEqual({'caller'}, {hit.repo for hit in hits})
        self.assertEqual(([], None), core._java_caller_reference_hits(changed, 'Payments.handlePayment', ['service']))
        self.assertEqual(([], 'requested repository is outside the pinned generation'),
                         core._java_caller_reference_hits(changed, 'Payments.handlePayment', ['new']))

    def test_many_possible_callers_cannot_evict_the_exact_definition(self):
        for number in range(25):
            (self.root / f'caller/Caller{number:02}.java').write_text(
                f'class Caller{number} {{ void run() {{ payments.handlePayment(); }} }}\n', encoding='utf-8')
        core.snapshot_indexes(self.settings)
        pinned = replace(self.pinned(), hydrate_limit=1)
        bundle = core.retrieve_context(pinned, core.parse_context_request(json.dumps(self.request())))
        self.assertEqual([('service', 'Payments.java')], [(item.repo, item.path) for item in bundle.evidence])

    def test_many_resolved_callers_retain_definition_and_requested_relation_source(self):
        source = 'package com.example;\nclass Payments {\n void handlePayment() {}\n'
        for number in range(25):
            source += '\n' * 100 + f' void caller{number}() {{ handlePayment(); }}\n'
        source += '}\n'
        (self.root / 'service/Payments.java').write_text(source, encoding='utf-8')
        core.snapshot_indexes(self.settings)
        pinned = replace(self.pinned(), hydrate_limit=2, source_window_lines=20)
        bundle = core.retrieve_context(pinned, core.parse_context_request(json.dumps(self.request())))
        self.assertTrue(any('void handlePayment() {}' in item.content for item in bundle.evidence),
                        ([(item.path, item.line_start, item.kind) for item in bundle.evidence],
                         [(item.path, item.line, item.kind, item.score) for item in bundle.additional_candidates
                          if item.line < 10 or 'definition' in item.kind]))
        self.assertTrue(any('requested symbol relationship' in item.kind for item in bundle.evidence))

    def test_cross_repo_receiver_call_source_is_delivered_without_claiming_resolved_dispatch(self):
        core.snapshot_indexes(self.settings)
        core.start_session(self.settings, "REFERENCES", "Find callers of the known payment method")
        content, _, _ = core.create_context(self.settings, "REFERENCES", json.dumps(self.request()))
        self.assertIn("payments.handlePayment();", content)
        runtime = core.session_state(self.settings, "REFERENCES")["investigation_runtime"]
        self.assertFalse(any(step["repo"] == "caller" and step["state"] == "verified"
                             for step in runtime["execution_flow"]["steps"]),
                         "a visible call reference does not prove receiver dispatch")
        self.assertIn("dispatch", content.casefold())

    def test_ticket_reference_source_stays_pinned_after_refresh(self):
        self.caller.write_text("class Controller { void run() { payments.handlePayment(OLD); } }\n", encoding='utf-8')
        core.snapshot_indexes(self.settings)
        core.start_session(self.settings, 'CALLER-OLD', 'Find the exact caller source')
        self.caller.write_text("class Controller { void run() { payments.handlePayment(NEW); } }\n", encoding='utf-8')
        core.snapshot_indexes(self.settings)
        core.start_session(self.settings, 'CALLER-NEW', 'Find the exact caller source')
        self.caller.write_text("class Controller { void run() { payments.handlePayment(UNREFRESHED); } }\n", encoding='utf-8')
        for ticket, expected, forbidden in (('CALLER-OLD', 'OLD', 'NEW'), ('CALLER-NEW', 'NEW', 'OLD')):
            with self.subTest(ticket=ticket):
                content, _, _ = core.create_context(self.settings, ticket, json.dumps(self.request()))
                self.assertIn(f'handlePayment({expected})', content)
                self.assertNotIn(f'handlePayment({forbidden})', content)
                self.assertNotIn('UNREFRESHED', content)

    def test_reference_lookup_10_50_100_repositories_shares_connection_and_filters_noise(self):
        config = "[project]\nname='reference-scale'\n[graph]\nenabled=false\n[experience]\nenabled=false\n"
        for number in range(100):
            name = f'scale-{number:03}'
            repo = self.root / name
            repo.mkdir()
            source = '// handlePayment();\n' * 40
            if number in (9, 49, 99):
                source += f'class Client{number} {{ void run() {{ payments.handlePayment(); }} }}\n'
            (repo / 'Client.java').write_text(source, encoding='utf-8')
            (repo / 'A-guide.md').write_text('handlePayment();\n' * 40, encoding='utf-8')
            config += f"[[repositories]]\nname='{name}'\npath='{name}'\n"
        self.config.write_text(config, encoding='utf-8')
        # With 100 repositories a fixed fair share is only five files. The
        # later real call must still use capacity left unused by other repos.
        crowded = self.root / 'scale-009'
        (crowded / 'Client.java').rename(crowded / 'ZCaller.java')
        for number in range(8):
            (crowded / f'ADeclaration{number}.java').write_text(
                f'class Declaration{number} {{ void handlePayment() {{}} }}\n', encoding='utf-8')
        self.settings = core.load_settings(self.config)
        core.snapshot_indexes(self.settings)
        for count in (10, 50, 100):
            with self.subTest(repositories=count):
                pinned = self.pinned()
                pinned = replace(pinned, repositories=pinned.repositories[:count])
                trace = RetrievalTrace()
                token = core._ACTIVE_RETRIEVAL_TRACE.set(trace)
                try:
                    with mock.patch.object(index, '_connect', wraps=index._connect) as connections, \
                            mock.patch.object(core, 'search', side_effect=AssertionError('per-repository fallback')):
                        hits, reason = core._java_caller_reference_hits(pinned, 'Payments.handlePayment')
                    self.assertIsNone(reason)
                    self.assertEqual({f'scale-{number:03}' for number in (9, 49, 99) if number < count}, {hit.repo for hit in hits})
                    self.assertEqual(1, connections.call_count)
                    self.assertEqual(1, trace.physical_backend_operations)
                finally:
                    core._ACTIVE_RETRIEVAL_TRACE.reset(token)
        # A genuinely full file budget is explicit partial coverage, not a
        # false claim that the known call site does not exist.
        pinned = self.pinned()
        stats = {}
        with mock.patch.object(index, '_connect', wraps=index._connect) as connections:
            hits = index.query_generation_indexes(
                pinned, pinned.atlas_generation, pinned.repositories, 'handlePayment',
                max_results=10, max_candidate_files=100, max_hits=100, max_bytes=1_000_000,
                max_seconds=5, java_calls_only=True, stats=stats,
            )
        self.assertEqual([], hits['scale-009'])
        self.assertTrue(stats['budget_exhausted'])
        self.assertEqual('candidate_files', stats['reason'])
        self.assertLessEqual(stats['candidate_files'], 100)
        self.assertEqual(1, connections.call_count)


if __name__ == "__main__":
    unittest.main()

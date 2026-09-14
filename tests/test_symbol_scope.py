from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from brain import core, index, investigation
from brain.catalog import connect, current_generation_ref
from brain.investigation import resolve_runtime_anchors


class SymbolScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'service').mkdir()
        self.source = self.root / 'service/Authorization.java'
        self.source.write_text(
            'package billing;\nclass Authorization {\n' +
            ''.join(f' int field{number};\n' for number in range(200)) +
            ' boolean authorize(int amount) {\n  int total = 0;\n' +
            ''.join(f'  total += {number};\n' for number in range(200)) +
            '  if (amount < 0) return false;\n  return total >= amount;\n }\n}\n', encoding='utf-8')
        config = self.root / 'brain.toml'
        config.write_text("[project]\nname='symbol-scope'\n[graph]\nenabled=false\n[experience]\nenabled=false\n"
                          "[[repositories]]\nname='service'\npath='service'\n", encoding='utf-8')
        self.settings = core.load_settings(config)

    def request(self, symbol='billing.Authorization.authorize'):
        return {'INVESTIGATION_REQUEST': {
            'version': 5, 'mode': 'root_cause', 'objective': f'Read the complete implementation of {symbol}',
            'anchors': [{'kind': 'symbol', 'value': symbol}],
        }}

    def publish(self):
        core.snapshot_indexes(self.settings)
        generation = current_generation_ref(self.settings)
        return replace(self.settings, atlas_generation=generation, atlas_generation_mode='pinned',
                       repositories=[replace(repo, source_sha=generation.snapshots[repo.name])
                                     for repo in self.settings.repositories])

    def retrieve(self, pinned, request=None):
        return core.retrieve_context(pinned, core.parse_context_request(json.dumps(request or self.request())))

    def test_qualified_method_delivers_decisive_tail_not_just_matching_header(self):
        pinned = self.publish()
        with mock.patch.object(index, 'read_generation_files', wraps=index.read_generation_files) as read, \
                mock.patch.object(index, 'read_indexed_file', side_effect=AssertionError('duplicate source IO')):
            bundle = self.retrieve(pinned)
        self.assertEqual(1, read.call_count)
        content = core.pack_context(pinned, 'METHOD', 1, bundle)
        self.assertIn('boolean authorize(int amount)', content)
        self.assertIn('if (amount < 0) return false;', content)
        self.assertIn('return total >= amount;', content)
        self.assertIn('int field199;', content)
        self.assertEqual('complete_range', bundle.trace['symbol_reads'][0]['status'])
        self.assertEqual('128-407', bundle.trace['symbol_reads'][0]['returned_lines'])
        self.assertEqual(3, bundle.trace['physical_backend_operations'])  # anchor, source, exact range

    def test_route_definition_and_relations_share_request_local_anchor_resolution(self):
        self.source.write_text(
            'package billing;\nclass Authorization {\n boolean authorize() { return helper(); }\n'
            ' boolean helper() { return true; }\n}\n', encoding='utf-8')
        (self.root / 'service/Settlement.java').write_text(
            'package billing;\nclass Settlement {\n int settle() { return calculate(); }\n'
            ' int calculate() { return 42; }\n}\n', encoding='utf-8')
        pinned = self.publish()
        resolver = investigation.resolve_runtime_anchors
        for symbols in (['billing.Authorization.authorize'],
                        ['billing.Authorization.authorize', 'billing.Settlement.settle']):
            request = self.request()
            request['INVESTIGATION_REQUEST'].update(
                anchors=[{'kind': 'symbol', 'value': value} for value in symbols], required=['callees'])
            results = []

            def measured(*args, **kwargs):
                with mock.patch.object(investigation, 'connect', wraps=investigation.connect) as opened:
                    result = resolver(*args, **kwargs)
                results.append((result, opened.call_count))
                return result

            with self.subTest(symbols=symbols), mock.patch.object(
                    investigation, 'resolve_runtime_anchors', side_effect=measured):
                bundle = self.retrieve(pinned, request)
            self.assertEqual(1, sum(count for _, count in results), results)
            batch = results[0][0]
            self.assertEqual(len(symbols), len(batch['candidates']))
            for result, count in results[1:]:
                expected = 'Authorization.java' if 'Authorization' in result['inputs'][0] else 'Settlement.java'
                self.assertEqual([expected], [item['path'] for item in result['candidates']])
                self.assertEqual(0, count)
                self.assertEqual(0, result['database_operations'])
                self.assertTrue(result['request_cache_hit'])
                self.assertEqual(pinned.atlas_generation.generation, result['generation'])
            self.assertTrue(any('CALLS' in item for item in bundle.relationships))
            self.assertTrue(all(item.repo == 'service' for item in bundle.evidence))
            content = core.pack_context(pinned, 'MEMO', 1, bundle)
            self.assertIn('return helper()', content)
            self.assertIn('return true;', content)
            if len(symbols) > 1:
                self.assertIn('return 42;', content)
            self.assertFalse(bundle.unresolved)
            self.assertIsNone(core._ACTIVE_RETRIEVAL_CACHE.get())

    def test_checkpoint_and_runtime_share_only_this_contexts_ordered_anchor_resolution(self):
        self.source.write_text(
            'package billing;\nclass Authorization {\n @GetMapping("/authorize")\n'
            ' boolean authorize() { return helper(); }\n boolean helper() { return true; }\n}\n', encoding='utf-8')
        pinned = self.publish()
        core.start_session(self.settings, 'CONTEXT-ANCHORS', 'Trace the authorization entry point')
        request = self.request()
        request['INVESTIGATION_REQUEST']['required'] = ['callees']
        observed = []
        stage = 'retrieval'
        resolver = investigation.resolve_runtime_anchors
        checkpoint = core._publish_first_useful_checkpoint
        runtime = investigation.build_ticket_runtime

        def measured(*args, **kwargs):
            with mock.patch.object(investigation, 'connect', wraps=investigation.connect) as opened:
                result = resolver(*args, **kwargs)
            observed.append((stage, opened.call_count, json.loads(json.dumps(result))))
            return result

        def during(phase, function):
            def run(*args, **kwargs):
                nonlocal stage
                previous, stage = stage, phase
                try:
                    return function(*args, **kwargs)
                finally:
                    stage = previous
            return run

        with mock.patch.object(investigation, 'resolve_runtime_anchors', side_effect=measured), \
                mock.patch.object(core, '_publish_first_useful_checkpoint', side_effect=during('checkpoint', checkpoint)), \
                mock.patch.object(investigation, 'build_ticket_runtime', side_effect=during('runtime', runtime)):
            content, _, _ = core.create_context(self.settings, 'CONTEXT-ANCHORS', json.dumps(request))
        checkpoint_rows = [row for row in observed if row[0] == 'checkpoint']
        runtime_rows = [row for row in observed if row[0] == 'runtime']
        self.assertEqual(1, len(checkpoint_rows))
        self.assertEqual(1, len(runtime_rows))
        self.assertEqual(1, checkpoint_rows[0][1])
        self.assertEqual(0, runtime_rows[0][1])
        self.assertEqual(checkpoint_rows[0][2]['inputs'], runtime_rows[0][2]['inputs'])
        self.assertEqual('billing.Authorization.authorize', checkpoint_rows[0][2]['inputs'][0])
        self.assertEqual(checkpoint_rows[0][2]['candidates'], runtime_rows[0][2]['candidates'])
        self.assertTrue(runtime_rows[0][2]['request_cache_hit'])
        self.assertEqual(0, runtime_rows[0][2]['database_operations'])
        state = core.session_state(self.settings, 'CONTEXT-ANCHORS')
        self.assertEqual('published', state['progressive_checkpoint']['continuation_status'])
        self.assertEqual(pinned.atlas_generation.generation, state['investigation_runtime']['generation'])
        self.assertTrue(state['investigation_runtime']['execution_flow']['steps'])
        self.assertFalse(state['investigation_runtime']['execution_flow']['cache_reused'])
        self.assertIn('return helper()', content)
        self.assertIn('return true;', content)
        self.assertIsNone(core._ACTIVE_RETRIEVAL_CACHE.get())

    def test_context_anchor_input_contract_preserves_explicit_priority_and_byte_limits(self):
        anchors = [{'kind': 'symbol', 'value': f'billing.Owner{number}.method'} for number in range(50)]
        for request in (
            {'anchors': anchors, 'resolve': ['other'], 'runtime_facts': ['fact'], 'objective': 'Inspect the failure'},
            {'anchors': [anchors[-1], *anchors, anchors[-1]], 'objective': 'Inspect the failure'},
            {'anchors': [{'kind': 'exception', 'value': '界' * 333 + str(number)} for number in range(40)],
             'objective': 'Find the bounded text'},
        ):
            expected = investigation._bounded_anchor_queries([
                *request.get('anchors', []), *request.get('resolve', []),
                *request.get('runtime_facts', []), request.get('objective'),
            ])
            actual = investigation._runtime_anchor_inputs(request)
            self.assertEqual(expected, [(item['kind'], item['value']) for item in actual])
            self.assertLessEqual(len(actual), investigation.MAX_ANCHOR_INPUTS)
            self.assertLessEqual(sum(len(item['value'].encode('utf-8')) for item in actual),
                                 investigation.MAX_ANCHOR_INPUT_BYTES)
        actual = investigation._runtime_anchor_inputs({'anchors': anchors, 'objective': 'noise'})
        self.assertEqual(anchors, actual)

    def test_nested_context_anchor_scopes_keep_old_and_new_tickets_and_parent_trace_isolated(self):
        from brain.retrieval.models import RetrievalTrace

        source = ('package billing;\nclass Authorization {\n @GetMapping("/authorize")\n'
                  ' boolean authorize() { return helper(); }\n boolean helper() { return true; }\n}\n')
        self.source.write_text(source, encoding='utf-8')
        old = self.publish()
        core.start_session(self.settings, 'ANCHOR-OLD', 'Trace the old authorization')
        self.source.write_text(source.replace('return true;', 'return false;'), encoding='utf-8')
        new = self.publish()
        core.start_session(self.settings, 'ANCHOR-NEW', 'Trace the new authorization')
        request = self.request()
        request['INVESTIGATION_REQUEST']['required'] = ['callees']
        text = json.dumps(request)
        nested = []
        outer_cache = {('test-parent',): 'unchanged'}
        outer_trace = RetrievalTrace()
        cache_token = core._ACTIVE_RETRIEVAL_CACHE.set(outer_cache)
        trace_token = core._ACTIVE_RETRIEVAL_TRACE.set(outer_trace)

        def progress(event):
            if event.get('phase') == 'first_useful_checkpoint':
                scoped = core._ACTIVE_RETRIEVAL_CACHE.get()
                self.assertIsNot(outer_cache, scoped)
                self.assertIsNone(core._ACTIVE_RETRIEVAL_TRACE.get())
                nested.append(core.create_context(self.settings, 'ANCHOR-NEW', text)[0])
                self.assertIs(scoped, core._ACTIVE_RETRIEVAL_CACHE.get())
                self.assertIsNone(core._ACTIVE_RETRIEVAL_TRACE.get())

        try:
            content, _, _ = core.create_context(self.settings, 'ANCHOR-OLD', text, progress=progress)
            self.assertIs(outer_cache, core._ACTIVE_RETRIEVAL_CACHE.get())
            self.assertEqual({('test-parent',): 'unchanged'}, outer_cache)
            self.assertIs(outer_trace, core._ACTIVE_RETRIEVAL_TRACE.get())
            self.assertEqual(0, outer_trace.physical_backend_operations)
            self.assertEqual(0, outer_trace.cache_hits)
        finally:
            core._ACTIVE_RETRIEVAL_TRACE.reset(trace_token)
            core._ACTIVE_RETRIEVAL_CACHE.reset(cache_token)
        self.assertEqual(1, len(nested))
        self.assertIn('return true;', content)
        self.assertNotIn('return false;', content)
        self.assertIn('return false;', nested[0])
        self.assertNotIn('return true;', nested[0])
        for ticket, generation in (('ANCHOR-OLD', old.atlas_generation), ('ANCHOR-NEW', new.atlas_generation)):
            state = core.session_state(self.settings, ticket)
            self.assertEqual(generation.generation, state['investigation_runtime']['generation'])
            self.assertEqual(generation.generation, state['progressive_checkpoint']['generation'])
            self.assertEqual('published', state['progressive_checkpoint']['continuation_status'])

    def test_context_anchor_scope_is_discarded_on_checkpoint_or_runtime_failure_and_retry(self):
        from brain.retrieval.models import RetrievalTrace

        self.source.write_text(
            'package billing;\nclass Authorization {\n @GetMapping("/authorize")\n'
            ' boolean authorize() { return true; }\n}\n', encoding='utf-8')
        self.publish()
        text = json.dumps(self.request())
        for phase in ('checkpoint', 'runtime'):
            ticket = f'ANCHOR-FAIL-{phase}'
            core.start_session(self.settings, ticket, 'Trace the entry point')
            parent = {('test-parent',): phase}
            trace = RetrievalTrace()
            cache_token = core._ACTIVE_RETRIEVAL_CACHE.set(parent)
            trace_token = core._ACTIVE_RETRIEVAL_TRACE.set(trace)
            runtime = investigation.build_ticket_runtime

            def fail_runtime(*args, **kwargs):
                raise RuntimeError('runtime failed')

            def progress(event):
                if phase == 'checkpoint' and event.get('phase') == 'first_useful_checkpoint':
                    raise RuntimeError('checkpoint notification failed')

            try:
                with mock.patch.object(investigation, 'build_ticket_runtime',
                                       side_effect=fail_runtime if phase == 'runtime' else runtime), \
                        self.assertRaises(RuntimeError):
                    core.create_context(self.settings, ticket, text, progress=progress)
                self.assertIs(parent, core._ACTIVE_RETRIEVAL_CACHE.get())
                self.assertEqual({('test-parent',): phase}, parent)
                self.assertIs(trace, core._ACTIVE_RETRIEVAL_TRACE.get())
                self.assertEqual(0, trace.cache_hits)
                failed = core.session_state(self.settings, ticket)['progressive_checkpoint']
                self.assertEqual('failed', failed['continuation_status'])
                content, _, _ = core.create_context(self.settings, ticket, text)
                self.assertIn('return true;', content)
                state = core.session_state(self.settings, ticket)
                self.assertEqual(failed['context_id'], state['last_context_id'])
                self.assertEqual('published', state['progressive_checkpoint']['continuation_status'])
                self.assertIs(parent, core._ACTIVE_RETRIEVAL_CACHE.get())
                self.assertEqual({('test-parent',): phase}, parent)
            finally:
                core._ACTIVE_RETRIEVAL_TRACE.reset(trace_token)
                core._ACTIVE_RETRIEVAL_CACHE.reset(cache_token)

    def test_checkpoint_obeys_anchor_and_persistent_cache_evaluation_ablations(self):
        self.source.write_text(
            'package billing;\nclass Authorization {\n @GetMapping("/authorize")\n'
            ' boolean authorize() { return true; }\n}\n', encoding='utf-8')
        pinned = self.publish()
        request = core.parse_context_request(json.dumps(self.request()))
        core.start_session(self.settings, 'ABLATE-ANCHORS', 'Inspect exact entry source')
        bundle = self.retrieve(pinned)
        state = core.session_state(self.settings, 'ABLATE-ANCHORS')
        with mock.patch.object(investigation, 'resolve_runtime_anchors', side_effect=AssertionError('anchors disabled')), \
                mock.patch.object(investigation, '_java_file_intelligence', side_effect=AssertionError('anchors disabled')):
            self.assertIsNone(core._publish_first_useful_checkpoint(
                self.settings, 'ABLATE-ANCHORS', 1, 'CTX-001', None, bundle,
                {**request, '_evaluation_ablation': ['anchors']}, 'test-signature', state,
                core.session_dir(self.settings, 'ABLATE-ANCHORS'), None,
            ))
        settings = replace(self.settings, evaluation_ablations={'generation_cache'})
        core.start_session(settings, 'ABLATE-CACHE', 'Inspect exact entry source')
        resolver = investigation.resolve_runtime_anchors
        results = []

        def measured(*args, **kwargs):
            self.assertFalse(kwargs.get('use_cache', True))
            result = resolver(*args, **kwargs)
            results.append(json.loads(json.dumps(result)))
            return result

        with mock.patch.object(investigation, 'resolve_runtime_anchors', side_effect=measured):
            content, _, _ = core.create_context(settings, 'ABLATE-CACHE', json.dumps(self.request()))
        self.assertIn('return true;', content)
        self.assertTrue(results[-1]['request_cache_hit'])
        self.assertTrue(all(not result['cache_hit'] for result in results))

    def test_anchor_request_aliases_preserve_ambiguity_warm_cache_and_owned_results(self):
        (self.root / 'service/Duplicate.java').write_text(
            'package billing;\nclass Authorization {\n boolean authorize() { return false; }\n}\n', encoding='utf-8')
        (self.root / 'service/Other.java').write_text(
            'package billing;\nclass Other {\n boolean authorize() { return true; }\n}\n', encoding='utf-8')
        pinned = self.publish()
        queries = [{'kind': 'symbol', 'value': name} for name in
                   ('billing.Authorization.authorize', 'billing.Other.authorize', 'billing.Missing.authorize')]
        expected = [resolve_runtime_anchors(pinned, pinned.atlas_generation, [query]) for query in queries]
        for warm in (False, True):
            token = core._ACTIVE_RETRIEVAL_CACHE.set({})
            try:
                batch = resolve_runtime_anchors(pinned, pinned.atlas_generation, queries)
                self.assertEqual(warm, batch['cache_hit'])
                for query, original in zip(queries, expected):
                    with mock.patch.object(investigation, 'connect', side_effect=AssertionError('repeated anchor IO')):
                        result = resolve_runtime_anchors(pinned, pinned.atlas_generation, [query, query])
                        self.assertEqual(original['candidates'], result['candidates'])
                        self.assertEqual(original['ambiguous'], result['ambiguous'])
                        self.assertEqual([query['value']], result['inputs'])
                        self.assertTrue(result['request_cache_hit'])
                        result['candidates'].clear()
                        self.assertEqual(original['candidates'], resolve_runtime_anchors(
                            pinned, pinned.atlas_generation, [query])['candidates'])
            finally:
                core._ACTIVE_RETRIEVAL_CACHE.reset(token)
        self.assertTrue(expected[0]['ambiguous'])
        self.assertEqual(2, len(expected[0]['candidates']))

    def test_large_anchor_request_reuses_only_its_complete_resolver_prefix(self):
        self.source.write_text(
            'package billing;\nclass Authorization {\n' + ''.join(
                f' boolean method{number}() {{ return true; }}\n' for number in range(50)
            ) + '}\n', encoding='utf-8')
        pinned = self.publish()
        queries = [{'kind': 'symbol', 'value': f'billing.Authorization.method{number}'} for number in range(50)]
        expected = {number: resolve_runtime_anchors(pinned, pinned.atlas_generation, [queries[number]])
                    for number in (0, 14, 15, 16, 49)}
        for warm in (False, True):
            token = core._ACTIVE_RETRIEVAL_CACHE.set({})
            try:
                batch = resolve_runtime_anchors(pinned, pinned.atlas_generation, queries)
                self.assertEqual(warm, batch['cache_hit'])
                self.assertEqual(16, len(batch['candidates']))
                for number in (0, 14, 15, 16, 49):
                    with mock.patch.object(investigation, 'connect', wraps=investigation.connect) as opened:
                        result = resolve_runtime_anchors(pinned, pinned.atlas_generation, [queries[number]])
                    self.assertEqual(0 if number < 16 else 1, opened.call_count, number)
                    self.assertEqual(expected[number]['candidates'], result['candidates'])
                    self.assertEqual(expected[number]['ambiguous'], result['ambiguous'])
                    self.assertEqual([queries[number]['value']], result['inputs'])
                    self.assertEqual(number < 16, result.get('request_cache_hit', False))
                    if number < 16:
                        self.assertEqual(0, result['database_operations'])
                self.assertEqual(16, len(batch['candidates']), 'singleton results must not mutate the batch')
            finally:
                core._ACTIVE_RETRIEVAL_CACHE.reset(token)

    def test_qualified_batch_reuses_python_preflight_without_entity_scans(self):
        self.source.write_text(
            'package billing;\nclass Authorization {\n' + ''.join(
                f' boolean method{number}() {{ return true; }}\n' for number in range(16)
            ) + '}\n', encoding='utf-8')
        queries = [{'kind': 'symbol', 'value': f'billing.Authorization.method{number}'} for number in range(16)]
        config = self.settings.config_path.read_text(encoding='utf-8')
        added = 1
        lookup = investigation._entity_name_anchor_rows
        observations = []
        for count in (10, 50, 100):
            for number in range(added, count):
                repo = self.root / f'noise{number}'
                repo.mkdir()
                (repo / 'Noise.java').write_text(f'package noise{number};\nclass Authorization {{\n' + ''.join(
                    f' boolean method{item}() {{ return false; }}\n' for item in range(16)
                ) + '}\n', encoding='utf-8')
                config += f"[[repositories]]\nname='noise{number}'\npath='noise{number}'\n"
            added = count
            self.settings.config_path.write_text(config, encoding='utf-8')
            self.settings = core.load_settings(self.settings.config_path)
            pinned = self.publish()
            statements, ticks = [], []

            def measured(connection, generation, names, **kwargs):
                connection.set_trace_callback(statements.append)
                connection.set_progress_handler(lambda: ticks.append(1) or 0, 1)
                try:
                    return lookup(connection, generation, names, **kwargs)
                finally:
                    connection.set_trace_callback(None)
                    connection.set_progress_handler(None, 0)

            with self.subTest(repositories=count), mock.patch.object(
                investigation, '_entity_name_anchor_rows', side_effect=measured,
            ), mock.patch.object(investigation, '_java_file_intelligence', side_effect=AssertionError('query-time parsing')):
                batch = resolve_runtime_anchors(pinned, pinned.atlas_generation, queries, use_cache=False)
                self.assertEqual('ready', batch['status'])
                self.assertEqual(16, len(batch['candidates']))
                self.assertEqual({'service'}, {item['repo'] for item in batch['candidates']})
                self.assertLessEqual(batch['database_operations'], 40)
                self.assertLessEqual(len(statements), 37, 'actual SQL statements, not just a coarser backend counter')
                self.assertEqual(1, sum('WITH requested(name)' in sql for sql in statements))
                self.assertGreater(len(ticks), 0)
                self.assertLessEqual(len(ticks), 60_000, 'batching must preserve indexed work, not hide workspace scans')
                observations.append((len(statements), len(ticks)))
        self.assertEqual(1, len({statements for statements, _ in observations}))

    def test_large_anchor_prefix_preserves_ambiguity_python_and_negative_results(self):
        (self.root / 'service/Duplicate.java').write_text(
            'package billing;\nclass Authorization {\n boolean authorize() { return false; }\n}\n', encoding='utf-8')
        (self.root / 'service/worker.py').write_text(
            'class Worker:\n    def run(self):\n        return 42\n', encoding='utf-8')
        pinned = self.publish()
        queries = [{'kind': 'symbol', 'value': value} for value in (
            'billing.Authorization.authorize', 'billing.Missing.run', 'worker.Worker.run',
            *(f'billing.Other{number}.run' for number in range(47)),
        )]
        expected = [resolve_runtime_anchors(pinned, pinned.atlas_generation, [query]) for query in queries[:3]]
        self.assertTrue(expected[0]['ambiguous'])
        self.assertFalse(expected[1]['candidates'])
        self.assertTrue(expected[2]['candidates'])
        for use_cache in (False, True):
            token = core._ACTIVE_RETRIEVAL_CACHE.set({})
            try:
                resolve_runtime_anchors(pinned, pinned.atlas_generation, queries, use_cache=use_cache)
                for query, original in zip(queries[:3], expected):
                    with mock.patch.object(investigation, 'connect', side_effect=AssertionError('repeated prefix lookup')):
                        result = resolve_runtime_anchors(pinned, pinned.atlas_generation, [query], use_cache=use_cache)
                    self.assertEqual(original['candidates'], result['candidates'])
                    self.assertEqual(original['ambiguous'], result['ambiguous'])
                    self.assertTrue(result['request_cache_hit'])
                with mock.patch.object(investigation, 'connect', wraps=investigation.connect) as opened:
                    result = resolve_runtime_anchors(pinned, pinned.atlas_generation, [queries[-1]], use_cache=use_cache)
                self.assertEqual(1, opened.call_count, 'an unqueried negative is not a proven absence')
                self.assertFalse(result.get('request_cache_hit', False))
            finally:
                core._ACTIVE_RETRIEVAL_CACHE.reset(token)

    def test_positive_java_and_python_tails_are_never_cached_as_unqueried_negatives(self):
        (self.root / 'service/worker.py').write_text(
            'class Worker:\n    def run(self):\n        return 42\n', encoding='utf-8')
        pinned = self.publish()
        for name in ('billing.Authorization.authorize', 'worker.Worker.run'):
            target = {'kind': 'symbol', 'value': name}
            expected = resolve_runtime_anchors(pinned, pinned.atlas_generation, [target])
            self.assertTrue(expected['candidates'])
            for position in (16, 49):
                queries = [{'kind': 'symbol', 'value': f'billing.Missing{number}.run'} for number in range(position)] + [target]
                for warm in (False, True):
                    token = core._ACTIVE_RETRIEVAL_CACHE.set({})
                    try:
                        with self.subTest(name=name, position=position, warm=warm):
                            batch = resolve_runtime_anchors(pinned, pinned.atlas_generation, queries)
                            self.assertFalse(batch['candidates'])
                            self.assertEqual([item['value'] for item in queries], batch['inputs'])
                            self.assertEqual(0, batch['bounds']['compound_terms'])
                            with mock.patch.object(investigation, 'connect', wraps=investigation.connect) as opened:
                                result = resolve_runtime_anchors(pinned, pinned.atlas_generation, [target])
                            self.assertEqual(1, opened.call_count)
                            self.assertFalse(result.get('request_cache_hit', False))
                            self.assertEqual(expected['candidates'], result['candidates'])
                    finally:
                        core._ACTIVE_RETRIEVAL_CACHE.reset(token)

    def test_batched_global_candidate_cap_never_seeds_incomplete_singletons(self):
        self.source.unlink()
        for owner in range(4):
            (self.root / f'service/Owner{owner}.java').write_text(
                'package billing;\nclass Authorization {\n' + ''.join(
                    f' boolean method{number}() {{ return true; }}\n' for number in range(13)
                ) + '}\n', encoding='utf-8')
        pinned = self.publish()
        queries = [{'kind': 'symbol', 'value': f'billing.Authorization.method{number}'} for number in range(13)]
        expected = resolve_runtime_anchors(pinned, pinned.atlas_generation, [queries[-1]])
        self.assertEqual(4, len(expected['candidates']))
        for warm in (False, True):
            token = core._ACTIVE_RETRIEVAL_CACHE.set({})
            try:
                with self.subTest(warm=warm):
                    batch = resolve_runtime_anchors(pinned, pinned.atlas_generation, queries)
                    self.assertEqual(50, len(batch['candidates']))
                    self.assertTrue(batch['ambiguous'])
                    with mock.patch.object(investigation, 'connect', wraps=investigation.connect) as opened:
                        result = resolve_runtime_anchors(pinned, pinned.atlas_generation, [queries[-1]])
                    self.assertEqual(1, opened.call_count)
                    self.assertFalse(result.get('request_cache_hit', False))
                    self.assertTrue(result['ambiguous'])
                    self.assertEqual(expected['candidates'], result['candidates'])
            finally:
                core._ACTIVE_RETRIEVAL_CACHE.reset(token)

    def test_batched_python_preflight_preserves_same_name_java_and_python_owners(self):
        self.source.write_text('package billing;\nclass Authorization {\n' + ''.join(
            f' int method{number}() {{ return {number}; }}\n' for number in range(16)
        ) + '}\n', encoding='utf-8')
        (self.root / 'service/billing.py').write_text('class Authorization:\n' + ''.join(
            f'    def method{number}(self):\n        return {number}\n' for number in range(16)
        ), encoding='utf-8')
        pinned = self.publish()
        queries = [{'kind': 'symbol', 'value': f'billing.Authorization.method{number}'} for number in range(16)]
        expected = [resolve_runtime_anchors(pinned, pinned.atlas_generation, [query]) for query in queries]
        self.assertTrue(all(len(result['candidates']) == 2 for result in expected))
        (self.root / 'service/billing.py').unlink()
        newer = self.publish()
        for warm in (False, True):
            token = core._ACTIVE_RETRIEVAL_CACHE.set({})
            try:
                batch = resolve_runtime_anchors(pinned, pinned.atlas_generation, queries)
                self.assertEqual(32, len(batch['candidates']))
                for query, original in zip(queries, expected):
                    with mock.patch.object(investigation, 'connect', side_effect=AssertionError('repeated owner lookup')):
                        result = resolve_runtime_anchors(pinned, pinned.atlas_generation, [query])
                    self.assertEqual(original['candidates'], result['candidates'])
                    self.assertTrue(result['ambiguous'])
                    self.assertTrue(result['request_cache_hit'])
                new_batch = resolve_runtime_anchors(newer, newer.atlas_generation, queries)
                self.assertEqual(16, len(new_batch['candidates']))
                self.assertEqual({'Authorization.java'}, {item['path'] for item in new_batch['candidates']})
                restored = resolve_runtime_anchors(pinned, pinned.atlas_generation, queries)
                self.assertEqual(batch['candidates'], restored['candidates'])
                self.assertTrue(restored['request_cache_hit'])
            finally:
                core._ACTIVE_RETRIEVAL_CACHE.reset(token)

    def test_large_relation_request_spends_its_budget_on_graph_and_source_not_prefix_rediscovery(self):
        self.source.write_text(
            'package billing;\nclass Authorization {\n' + ''.join(
                f' boolean method{number}() {{ return helper{number}(); }}\n'
                f' boolean helper{number}() {{ return true; }}\n' for number in range(50)
            ) + '}\n', encoding='utf-8')
        pinned = replace(self.publish(), max_backend_operations=18)
        request = self.request()
        request['INVESTIGATION_REQUEST'].update(
            anchors=[{'kind': 'symbol', 'value': f'billing.Authorization.method{number}'} for number in range(50)],
            required=['callees'],
        )
        resolver = investigation.resolve_runtime_anchors
        opened_per_call = []

        def measured(*args, **kwargs):
            with mock.patch.object(investigation, 'connect', wraps=investigation.connect) as opened:
                result = resolver(*args, **kwargs)
            opened_per_call.append(opened.call_count)
            return result

        with mock.patch.object(investigation, 'resolve_runtime_anchors', side_effect=measured), \
                mock.patch.object(index, 'read_generation_files', wraps=index.read_generation_files) as sources:
            bundle = self.retrieve(pinned, request)
        self.assertEqual(1, sum(opened_per_call))
        self.assertEqual(18, bundle.trace['physical_backend_operations'])
        self.assertEqual(15, bundle.trace['effective_operations'])
        self.assertEqual('operation_budget', bundle.trace['stop_reason'])
        self.assertEqual(15, len(bundle._resolved_relation_seeds))
        self.assertEqual(1, sources.call_count)
        content = core.pack_context(pinned, 'RELATION-BATCH', 1, bundle)
        for number in range(15):
            self.assertIn(f'return helper{number}();', content)
            self.assertTrue(any(f'CALLS  helper{number} ' in item for item in bundle.relationships))
        for number in range(15, 50):
            self.assertTrue(any(f'`billing.Authorization.method{number}`' in item and 'No dedicated' in item
                                for item in bundle.unresolved))
        self.assertFalse(any('physical operation budget' in item for item in bundle.unresolved))

    def test_large_request_keeps_source_and_method_range_capacity_on_its_pin(self):
        source = ('package billing;\nclass Authorization {\n boolean method0() {\n int x = 0;\n'
                  + ' x++;\n' * 100 + ' return OLD_DECISION;\n }\n' + ''.join(
                      f' boolean method{number}() {{ return helper{number}(); }}\n'
                      f' boolean helper{number}() {{ return true; }}\n' for number in range(1, 50)
                  ) + '}\n')
        self.source.write_text(source, encoding='utf-8')
        old = replace(self.publish(), max_effective_operations=50, full_file_lines=50, source_window_lines=20)
        self.source.write_text(source.replace('OLD_DECISION', 'NEW_DECISION'), encoding='utf-8')
        newer = self.publish()
        request = self.request()
        request['INVESTIGATION_REQUEST'].update(
            anchors=[{'kind': 'symbol', 'value': f'billing.Authorization.method{number}'} for number in range(50)],
            required=['callees'],
        )
        for budget in (18, 40, 160):
            with self.subTest(budget=budget), \
                    mock.patch.object(index, 'read_generation_files', wraps=index.read_generation_files) as reads:
                bundle = self.retrieve(replace(old, max_backend_operations=budget), request)
            content = core.pack_context(old, 'SOURCE-HEADROOM', 1, bundle)
            self.assertEqual(1, reads.call_count)
            self.assertLessEqual(bundle.trace['physical_backend_operations'], budget)
            self.assertIn('source-hydration', bundle.trace['backend_ms'])
            self.assertIn('symbol_ranges', bundle.trace['backend_ms'])
            self.assertIn('return OLD_DECISION;', content)
            self.assertNotIn('NEW_DECISION', content)
            self.assertEqual(old.atlas_generation, bundle.atlas_generation)
            self.assertNotEqual(newer.atlas_generation, bundle.atlas_generation)
            self.assertFalse(any('returned no code matches' in item for item in bundle.unresolved))
            if budget < 160:
                self.assertTrue(bundle.unresolved)
                self.assertNotEqual('coverage_satisfied', bundle.trace['stop_reason'])

    def test_buffered_direct_source_does_not_reserve_a_duplicate_read_before_relations(self):
        self.source.write_text(
            'package billing;\nclass Authorization {\n boolean authorize() { return helper(); }\n'
            ' boolean helper() { return true; }\n}\n', encoding='utf-8')
        pinned = replace(self.publish(), max_backend_operations=4)
        request = self.request()
        request['INVESTIGATION_REQUEST'].update(
            files=[{'repo': 'service', 'path': 'Authorization.java', 'lines': '1-2'}], required=['callees'],
        )
        with mock.patch.object(index, 'read_generation_files', wraps=index.read_generation_files) as reads:
            bundle = self.retrieve(pinned, request)
        self.assertEqual(0, reads.call_count)
        self.assertEqual(4, bundle.trace['physical_backend_operations'])
        self.assertTrue(any('CALLS  helper ' in value for value in bundle.relationships))
        self.assertFalse(bundle.unresolved)
        self.assertIn('return true;', core.pack_context(pinned, 'BUFFERED', 1, bundle))

    def test_deferred_requested_relations_never_claim_coverage_satisfied(self):
        self.source.write_text(
            'package billing;\nclass Authorization {\n' + ''.join(
                f' boolean method{number}() {{ return helper{number}(); }}\n'
                f' boolean helper{number}() {{ return true; }}\n' for number in range(50)
            ) + '}\n', encoding='utf-8')
        pinned = replace(self.publish(), max_backend_operations=40, max_effective_operations=50)
        request = self.request()
        request['INVESTIGATION_REQUEST'].update(
            anchors=[{'kind': 'symbol', 'value': f'billing.Authorization.method{number}'} for number in range(50)],
            required=['callees'],
        )
        bundle = self.retrieve(pinned, request)
        self.assertTrue(bundle.evidence)
        self.assertTrue(any('Symbol relationships' in item and 'deferred' in item for item in bundle.unresolved))
        self.assertEqual('physical_budget', bundle.trace['stop_reason'])
        self.assertEqual('physical_budget', bundle.trace['planner']['stop_reason'])
        self.assertLessEqual(bundle.trace['physical_backend_operations'], 40)

    def test_mixed_incomplete_file_reads_never_claim_coverage_satisfied(self):
        pinned = self.publish()
        self.source.write_text(self.source.read_text(encoding='utf-8').replace('return total >= amount;', 'return NEW_DECISION;'),
                               encoding='utf-8')
        self.publish()
        for target, lines, expected in (
            ('Missing.java', None, 'unavailable'),
            ('Authorization.java', '9999-10000', 'out_of_range'),
        ):
            for budget in (1, 4, 5, 6):
                with self.subTest(target=target, budget=budget):
                    request = self.request()
                    request['INVESTIGATION_REQUEST'].update(files=[
                        {'repo': 'service', 'path': 'Authorization.java', 'lines': '1-2'},
                        {'repo': 'service', 'path': target, **({'lines': lines} if lines else {})},
                    ])
                    bundle = self.retrieve(replace(pinned, max_backend_operations=budget), request)
                    self.assertTrue(bundle.evidence)
                    self.assertTrue(bundle.unresolved)
                    self.assertNotEqual('coverage_satisfied', bundle.trace['stop_reason'])
                    self.assertEqual(bundle.trace['stop_reason'], bundle.trace['planner']['stop_reason'])
                    self.assertLessEqual(bundle.trace['physical_backend_operations'], budget)
                    if budget > 1:
                        self.assertEqual(expected, bundle.trace['file_reads'][1]['status'])
                    self.assertEqual(pinned.atlas_generation, bundle.atlas_generation)
                    self.assertNotIn('NEW_DECISION', core.pack_context(pinned, 'MISSING-FILE', 1, bundle))

    def test_partial_explicit_file_page_keeps_incomplete_status_with_or_without_symbols(self):
        pinned = replace(self.publish(), hard_context_chars=300)
        for with_symbol in (False, True):
            with self.subTest(with_symbol=with_symbol):
                request = self.request()
                request['INVESTIGATION_REQUEST'].update(
                    files=[{'repo': 'service', 'path': 'Authorization.java'}],
                    anchors=request['INVESTIGATION_REQUEST']['anchors'] if with_symbol else [],
                    objective='Read this pinned file',
                )
                bundle = self.retrieve(pinned, request)
                report = bundle.trace['file_reads'][0]
                self.assertEqual('partial', report['status'])
                self.assertTrue(report['next_lines'])
                self.assertEqual('requested_files_incomplete', bundle.trace['stop_reason'])
                if with_symbol:
                    self.assertEqual('requested_files_incomplete', bundle.trace['planner']['stop_reason'])
                self.assertTrue(bundle.evidence)

    def test_direct_file_status_preserves_operation_and_time_budget_causes(self):
        from brain.retrieval.planner import compile_request

        pinned = self.publish()
        request = {'INVESTIGATION_REQUEST': {
            'version': 5, 'mode': 'root_cause', 'objective': 'Read these pinned files',
            'files': [{'repo': 'service', 'path': 'Authorization.java', 'lines': f'{line}-{line}'}
                      for line in range(1, 4)],
        }}
        bundle = self.retrieve(replace(pinned, max_effective_operations=1), request)
        self.assertEqual('operation_budget', bundle.trace['stop_reason'])
        self.assertTrue(bundle.evidence)
        self.assertTrue(bundle.unresolved)
        parsed = core.parse_context_request(json.dumps(request))
        plan = replace(compile_request(parsed), timeout_ms=0)
        with mock.patch('brain.retrieval.compile_request', return_value=plan):
            bundle = core.retrieve_context(pinned, parsed)
        self.assertEqual('time_budget', bundle.trace['stop_reason'])
        self.assertFalse(bundle.evidence)
        self.assertTrue(bundle.unresolved)

    def test_source_headroom_does_not_count_work_or_allow_parallel_overrun(self):
        from concurrent.futures import ThreadPoolExecutor
        from brain.retrieval.models import RetrievalTrace

        trace = RetrievalTrace(max_physical_backend_operations=8)
        trace._set_backend_headroom(2)
        self.assertEqual(0, trace.physical_backend_operations)
        self.assertEqual(6, trace.physical_budget_remaining)
        self.assertEqual(8, trace.max_physical_backend_operations)
        with ThreadPoolExecutor(max_workers=8) as workers:
            accepted = list(workers.map(lambda _: trace.try_reserve_backend(), range(50)))
        self.assertEqual(6, sum(accepted))
        self.assertEqual(6, trace.operation_count)
        self.assertEqual(0, trace.physical_budget_remaining)
        trace._set_backend_headroom(0)
        self.assertEqual(6, trace.physical_backend_operations)
        self.assertEqual(2, trace.physical_budget_remaining)
        self.assertTrue(trace.try_reserve_backend())
        self.assertTrue(trace.try_reserve_backend())
        self.assertFalse(trace.try_reserve_backend())
        trace._set_backend_headroom(100)
        self.assertEqual(0, trace._backend_headroom)
        self.assertEqual(8, trace.as_dict()['physical_backend_operations'])
        self.assertNotIn('_backend_headroom', trace.as_dict())

    def test_source_headroom_is_scoped_across_nested_retrieval_and_failure(self):
        from brain.retrieval.models import RetrievalTrace

        pinned = self.publish()
        parent = RetrievalTrace()
        parent._set_backend_headroom(3)
        token = core._ACTIVE_RETRIEVAL_TRACE.set(parent)
        observed = []

        def progress(event):
            if event.get('phase') != 'global_discovery':
                return
            outer = core._ACTIVE_RETRIEVAL_TRACE.get()
            observed.append(outer)
            self.assertEqual(2, outer._backend_headroom)
            self.assertIsNot(parent, outer)
            nested = self.retrieve(pinned)
            self.assertTrue(nested.evidence)
            self.assertIs(outer, core._ACTIVE_RETRIEVAL_TRACE.get())
            self.assertEqual(2, outer._backend_headroom)
            raise RuntimeError('discovery callback failed')

        try:
            with self.assertRaisesRegex(RuntimeError, 'callback failed'):
                core.retrieve_context(pinned, core.parse_context_request(json.dumps(self.request())), progress=progress)
            self.assertEqual(1, len(observed))
            self.assertEqual(0, observed[0]._backend_headroom)
            self.assertIs(parent, core._ACTIVE_RETRIEVAL_TRACE.get())
            self.assertEqual(3, parent._backend_headroom)
            self.assertEqual(0, parent.physical_backend_operations)
            self.assertTrue(self.retrieve(pinned).evidence)
            self.assertIs(parent, core._ACTIVE_RETRIEVAL_TRACE.get())
            self.assertEqual(3, parent._backend_headroom)
        finally:
            core._ACTIVE_RETRIEVAL_TRACE.reset(token)

    def test_anchor_request_cache_keeps_generation_component_options_and_failure_isolation(self):
        pinned = self.publish()
        self.source.write_text(self.source.read_text(encoding='utf-8').replace('return total >= amount;', 'return false;'),
                               encoding='utf-8')
        newer = self.publish()
        query = self.request()['INVESTIGATION_REQUEST']['anchors']
        token = core._ACTIVE_RETRIEVAL_CACHE.set({})
        try:
            old = resolve_runtime_anchors(pinned, pinned.atlas_generation, query)
            with mock.patch.object(investigation, 'connect', wraps=investigation.connect) as opened:
                new = resolve_runtime_anchors(newer, newer.atlas_generation, query)
            self.assertEqual(1, opened.call_count)
            self.assertNotEqual(old['candidates'][0]['entity_id'], new['candidates'][0]['entity_id'])
            with mock.patch.object(investigation, 'connect', side_effect=AssertionError('old pin was replaced')):
                self.assertEqual(old['candidates'], resolve_runtime_anchors(pinned, pinned.atlas_generation, query)['candidates'])
            for options in ({'limit': 1}, {'use_cache': False}):
                with mock.patch.object(investigation, 'connect', wraps=investigation.connect) as opened:
                    result = resolve_runtime_anchors(pinned, pinned.atlas_generation, query, **options)
                self.assertEqual(1, opened.call_count)
                self.assertEqual(old['candidates'], result['candidates'])
                self.assertFalse(result.get('request_cache_hit', False))
            for name in ('runtime_anchors', 'hierarchy'):
                broken = replace(pinned.atlas_generation, components={**pinned.atlas_generation.components,
                    name: {**pinned.atlas_generation.component(name), 'status': 'unavailable'}})
                for _ in range(2):
                    result = resolve_runtime_anchors(pinned, broken, query)
                    self.assertEqual('degraded', result['status'])
                    self.assertFalse(result['candidates'])
                    self.assertFalse(result.get('request_cache_hit', False))
            other_workspace = replace(pinned, state_dir=self.root / 'separate-state')
            self.assertEqual('degraded', resolve_runtime_anchors(other_workspace, pinned.atlas_generation, query)['status'])
        finally:
            core._ACTIVE_RETRIEVAL_CACHE.reset(token)
        connection = connect(pinned)
        try:
            connection.execute('DELETE FROM generation_entities WHERE generation=? AND entity_id=?',
                               (pinned.atlas_generation.generation, old['candidates'][0]['entity_id']))
            connection.commit()
        finally:
            connection.close()
        token = core._ACTIVE_RETRIEVAL_CACHE.set({})
        try:
            self.assertEqual('degraded', resolve_runtime_anchors(pinned, pinned.atlas_generation, query)['status'])
        finally:
            core._ACTIVE_RETRIEVAL_CACHE.reset(token)

    def test_truncated_anchor_batches_and_full_request_cache_fall_back_to_authoritative_lookup(self):
        pinned = self.publish()
        target = self.request()['INVESTIGATION_REQUEST']['anchors']
        token = core._ACTIVE_RETRIEVAL_CACHE.set({})
        try:
            missing = [{'kind': 'symbol', 'value': f'billing.Missing{number}.run'} for number in range(16)]
            self.assertFalse(resolve_runtime_anchors(pinned, pinned.atlas_generation, missing + target)['candidates'])
            with mock.patch.object(investigation, 'connect', wraps=investigation.connect) as opened:
                found = resolve_runtime_anchors(pinned, pinned.atlas_generation, target)
            self.assertEqual(1, opened.call_count)
            self.assertTrue(found['candidates'])
            entries, _ = core._ACTIVE_RETRIEVAL_CACHE.get()[('runtime-anchor-results',)]
            for number in range(60):
                resolve_runtime_anchors(pinned, pinned.atlas_generation,
                                        [{'kind': 'symbol', 'value': f'billing.Absent{number}.run'}])
            self.assertEqual(50, len(entries))
            self.assertLessEqual(sum(map(len, entries.values())), 2 * 1024 * 1024)
            with mock.patch.object(investigation, 'connect', wraps=investigation.connect) as opened:
                result = resolve_runtime_anchors(pinned, pinned.atlas_generation, target, limit=1)
            self.assertEqual(1, opened.call_count)
            self.assertEqual(found['candidates'], result['candidates'])
            self.assertEqual(50, len(entries))
        finally:
            core._ACTIVE_RETRIEVAL_CACHE.reset(token)
        for byte_cap in (1, investigation.MAX_ANCHOR_REQUEST_CACHE_BYTES):
            token = core._ACTIVE_RETRIEVAL_CACHE.set({})
            try:
                with mock.patch.object(investigation, 'MAX_ANCHOR_REQUEST_CACHE_BYTES', byte_cap):
                    resolve_runtime_anchors(pinned, pinned.atlas_generation,
                                            target + [{'kind': 'symbol', 'value': 'billing.Authorization'}], limit=1)
                    with mock.patch.object(investigation, 'connect', wraps=investigation.connect) as opened:
                        result = resolve_runtime_anchors(pinned, pinned.atlas_generation, target, limit=1)
                    self.assertEqual(1, opened.call_count, 'candidate-truncated batches cannot seed singleton aliases')
                    self.assertEqual(found['candidates'], result['candidates'])
                    entries, _ = core._ACTIVE_RETRIEVAL_CACHE.get()[('runtime-anchor-results',)]
                    self.assertLessEqual(sum(map(len, entries.values())), byte_cap)
            finally:
                core._ACTIVE_RETRIEVAL_CACHE.reset(token)

    def test_qualified_batch_reuse_leaves_the_physical_budget_for_source_evidence(self):
        (self.root / 'service/Other.java').write_text(
            'package billing;\nclass Other {\n boolean authorize() { return true; }\n}\n', encoding='utf-8')
        pinned = replace(self.publish(), max_backend_operations=3)
        request = self.request()
        request['INVESTIGATION_REQUEST']['anchors'].append({'kind': 'symbol', 'value': 'billing.Other.authorize'})
        bundle = self.retrieve(pinned, request)
        self.assertFalse(bundle.unresolved)
        self.assertEqual(3, bundle.trace['physical_backend_operations'])
        self.assertIn('runtime_anchor', bundle.trace['backend_ms'])
        self.assertEqual(2, len(bundle.trace['symbol_reads']))
        content = core.pack_context(pinned, 'BUDGET', 1, bundle)
        self.assertIn('return total >= amount;', content)
        self.assertIn('return true;', content)

    def test_branch_bounded_anchor_batch_does_not_claim_complete_singleton_membership(self):
        for number in range(8):
            (self.root / f'service/Owner{number}.java').write_text(
                'package billing;\nclass Authorization {\n boolean authorize() { return false; }\n}\n', encoding='utf-8')
        pinned = self.publish()
        query = self.request()['INVESTIGATION_REQUEST']['anchors']
        expected = resolve_runtime_anchors(pinned, pinned.atlas_generation, query)
        self.assertEqual(8, len(expected['candidates']))
        for warm in (False, True):
            token = core._ACTIVE_RETRIEVAL_CACHE.set({})
            try:
                batch = resolve_runtime_anchors(pinned, pinned.atlas_generation,
                                               query + [{'kind': 'symbol', 'value': 'billing.Missing.authorize'}])
                self.assertEqual(warm, batch['cache_hit'])
                with mock.patch.object(investigation, 'connect', wraps=investigation.connect) as opened:
                    result = resolve_runtime_anchors(pinned, pinned.atlas_generation, query)
                self.assertEqual(1, opened.call_count)
                self.assertFalse(result.get('request_cache_hit', False))
                self.assertTrue(result['ambiguous'])
                self.assertEqual(expected['candidates'], result['candidates'])
            finally:
                core._ACTIVE_RETRIEVAL_CACHE.reset(token)

    def test_python_ast_range_delivers_the_return_after_the_default_window(self):
        self.source.unlink()
        source = self.root / 'service/authorize.py'
        source.write_text(''.join(f'FIELD_{n} = {n}\n' for n in range(200)) +
                          'def authorize(amount):\n    total = 0\n' +
                          ''.join(f'    total += {n}\n' for n in range(200)) +
                          '    return total >= amount\n', encoding='utf-8')
        pinned = self.publish()
        for request in (self.request('authorize.authorize'), {'CONTEXT_REQUEST': {
            'version': 2, 'objective': 'Read authorize',
            'symbols': [{'name': 'authorize', 'repos': ['service'], 'include': ['definition']}],
        }}, {'CONTEXT_REQUEST': {
            'version': 3, 'objective': 'Read authorize', 'hints': {'symbols': ['authorize'], 'repos': ['service']},
            'coverage': {'production': 'required', 'tests': 'omit', 'relationships': 'omit'},
        }}):
            with self.subTest(request=request):
                bundle = self.retrieve(pinned, request)
                content = core.pack_context(pinned, 'PYTHON', 1, bundle)
                self.assertIn('return total >= amount', content)
                self.assertIn('FIELD_199 =', content)
                self.assertEqual('complete_range', bundle.trace['symbol_reads'][0]['status'])

    def test_same_name_owners_deliver_both_bodies_without_claiming_unique_dispatch(self):
        self.source.write_text(''.join(
            f'class {owner} {{\n boolean authorize(int amount) {{\n' +
            ''.join(f'  amount += {number};\n' for number in range(200)) +
            f'  return {owner.upper()}_DECISION;\n }}\n}}\n'
            for owner in ('First', 'Second')), encoding='utf-8')
        pinned = replace(self.publish(), hydrate_limit=2, max_regions_per_file=2)
        for request in ({'CONTEXT_REQUEST': {
            'version': 2, 'objective': 'Inspect the implementations',
            'symbols': [{'name': 'authorize', 'repos': ['service'], 'include': ['definition']}],
        }}, self.request('authorize')):
            with self.subTest(request=request), \
                    mock.patch.object(core, '_requested_symbol_ranges', wraps=core._requested_symbol_ranges) as ranges, \
                    mock.patch.object(index, 'read_generation_files', wraps=index.read_generation_files) as sources:
                bundle = self.retrieve(pinned, request)
            self.assertEqual(1, ranges.call_count, 'all selected locations share one validation batch')
            self.assertEqual(1, sources.call_count)
            self.assertEqual(2, len(bundle.trace['symbol_reads']))
            content = core.pack_context(pinned, 'OWNERS', 1, bundle)
            self.assertIn('FIRST_DECISION', content)
            self.assertIn('SECOND_DECISION', content)
            self.assertFalse(any('generation-validated qualified symbol' in item.found_by for item in bundle.evidence))

    def test_ambiguous_location_does_not_hide_its_source_window_or_starve_another_body(self):
        self.source.write_text(self.source.read_text(encoding='utf-8') + '\n' * 250 +
                               'class Recovery {\n boolean recover() {\n' +
                               ''.join(f'  int step{n} = {n};\n' for n in range(200)) +
                               '  return RECOVERY_DECISION;\n }\n}\n', encoding='utf-8')
        pinned = replace(self.publish(), hydrate_limit=2, max_regions_per_file=2)
        connection = connect(pinned)
        try:
            columns = [row[1] for row in connection.execute('PRAGMA table_info(atlas_entities)')]
            row = connection.execute("SELECT * FROM atlas_entities WHERE simple_name='authorize'").fetchone()
            for number in range(64):
                identifier = f'ambiguous-{number}'
                connection.execute(f"INSERT INTO atlas_entities ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                                   (identifier, *row[1:]))
                connection.execute('INSERT INTO generation_entities VALUES (?,?,?)',
                                   (pinned.atlas_generation.generation, identifier, pinned.atlas_generation.snapshots['service']))
            connection.commit()
            locations = {(repo, path, line): {name} for repo, path, line, name in connection.execute(
                "SELECT repo,path,line_start,simple_name FROM atlas_entities WHERE simple_name IN ('authorize','recover')")}
        finally:
            connection.close()
        from brain import atlas
        with mock.patch.object(atlas, '_valid_generation_entities', wraps=atlas._valid_generation_entities) as validate:
            ranges, reason = core._requested_symbol_ranges(pinned, {}, definitions=locations)
        self.assertEqual(2, validate.call_count)  # One entity batch and one owner batch, not per ambiguous row.
        self.assertEqual(['recover'], [row['simple_name'] for row in ranges])
        self.assertIn('incomplete', reason)
        bundle = self.retrieve(pinned, {'CONTEXT_REQUEST': {
            'version': 2, 'objective': 'Inspect both implementations',
            'symbols': [{'name': name, 'repos': ['service'], 'include': ['definition']}
                        for name in ('authorize', 'recover')],
        }})
        content = core.pack_context(pinned, 'AMBIGUOUS', 1, bundle)
        self.assertIn('boolean authorize(int amount)', content, 'an unrelated good range cannot consume this fallback window')
        self.assertIn('RECOVERY_DECISION', content)
        self.assertNotIn('return total >= amount;', content)
        self.assertEqual(1, len(bundle.trace['symbol_reads']))
        self.assertTrue(any('Full requested symbol ranges unavailable' in item for item in bundle.unresolved))
        self.assertNotEqual('coverage_satisfied', bundle.trace['stop_reason'])

    def test_definition_range_requires_exact_locus_name_and_valid_generation_membership(self):
        pinned = self.publish()
        locus = ('service', 'Authorization.java', 203)
        valid = {locus: {'authorize'}}
        self.assertEqual(1, len(core._requested_symbol_ranges(pinned, {}, definitions=valid)[0]))
        for location, name in ((('other', locus[1], 203), 'authorize'),
                               ((locus[0], 'Other.java', 203), 'authorize'),
                               ((locus[0], locus[1], 204), 'authorize'), (locus, 'Authorization')):
            with self.subTest(location=location, name=name):
                ranges, reason = core._requested_symbol_ranges(pinned, {}, definitions={location: {name}})
                self.assertFalse(ranges)
                self.assertIn('incomplete', reason)
        connection = connect(pinned)
        try:
            identifier, line_end = connection.execute("SELECT entity_id,line_end FROM atlas_entities WHERE simple_name='authorize'").fetchone()
            for statement, parameters in (
                ('UPDATE atlas_entities SET line_end=line_end+1 WHERE entity_id=?', (identifier,)),
                ('UPDATE generation_entities SET snapshot_sha=? WHERE generation=? AND entity_id=?',
                 ('wrong-snapshot', pinned.atlas_generation.generation, identifier)),
                ('DELETE FROM generation_entities WHERE generation=? AND entity_id=?',
                 (pinned.atlas_generation.generation, identifier)),
            ):
                with self.subTest(statement=statement):
                    connection.execute(statement, parameters)
                    connection.commit()
                    ranges, reason = core._requested_symbol_ranges(pinned, {}, definitions=valid)
                    self.assertFalse(ranges)
                    self.assertTrue(reason)
                    connection.execute('UPDATE atlas_entities SET line_end=? WHERE entity_id=?', (line_end, identifier))
                    connection.execute('INSERT OR REPLACE INTO generation_entities VALUES (?,?,?)',
                                       (pinned.atlas_generation.generation, identifier, pinned.atlas_generation.snapshots['service']))
                    connection.commit()
                    self.assertEqual(1, len(core._requested_symbol_ranges(pinned, {}, definitions=valid)[0]))
        finally:
            connection.close()

    def test_unqualified_body_budget_or_hierarchy_failure_returns_explicit_partial_source(self):
        pinned = self.publish()
        request = {'CONTEXT_REQUEST': {
            'version': 2, 'objective': 'Inspect the implementation',
            'symbols': [{'name': 'authorize', 'repos': ['service'], 'include': ['definition']}],
        }}
        complete = self.retrieve(pinned, request)
        generation = replace(pinned.atlas_generation, components={
            **pinned.atlas_generation.components, 'hierarchy': {'status': 'unavailable'},
        })
        for settings in (replace(pinned, max_backend_operations=complete.trace['physical_backend_operations'] - 1),
                         replace(pinned, atlas_generation=generation)):
            with self.subTest(components=settings.atlas_generation.components, budget=settings.max_backend_operations):
                bundle = self.retrieve(settings, request)
                content = core.pack_context(settings, 'PARTIAL', 1, bundle)
                self.assertIn('boolean authorize(int amount)', content)
                self.assertNotIn('return total >= amount;', content)
                self.assertNotIn('symbol_reads', bundle.trace)
                self.assertTrue(any('Full requested symbol ranges unavailable' in item for item in bundle.unresolved))
                self.assertNotEqual('coverage_satisfied', bundle.trace['stop_reason'])

    def test_unqualified_small_file_needs_no_range_query_and_large_bytes_still_use_pages(self):
        for large in (False, True):
            source = ('class Authorization {\n' + (' // ' + '界' * 1000 + '\n') * (100 if large else 0) +
                      ' boolean authorize(int amount) { return COMPLETE_DECISION; }\n}\n')
            self.source.write_text(source, encoding='utf-8')
            pinned = self.publish()
            for request in (self.request('authorize'), {'CONTEXT_REQUEST': {
                'version': 2, 'objective': 'Inspect the implementation',
                'symbols': [{'name': 'authorize', 'repos': ['service'], 'include': ['definition']}],
            }}):
                with self.subTest(large=large, request=request), \
                        mock.patch.object(core, '_requested_symbol_ranges', wraps=core._requested_symbol_ranges) as ranges:
                    bundle = self.retrieve(pinned, request)
                self.assertEqual(int(large), ranges.call_count)
                self.assertIn('return COMPLETE_DECISION;', core.pack_context(pinned, 'SMALL', 1, bundle))
                if large:
                    self.assertEqual('complete_range', bundle.trace['symbol_reads'][0]['status'])
                    self.assertLessEqual(len(bundle.evidence[0].content.encode('utf-8')), 64_000)
                else:
                    self.assertNotIn('symbol_reads', bundle.trace)
                    self.assertFalse(any('Full requested symbol ranges unavailable' in item for item in bundle.unresolved))

    def test_legacy_long_method_requests_deliver_the_decisive_tail_from_each_ticket_pin(self):
        old = self.publish()
        core.start_session(self.settings, 'OLD-BODY', 'Inspect the original authorization implementation')
        self.source.write_text(self.source.read_text(encoding='utf-8').replace(
            'return total >= amount;', 'return NEW_GENERATION;'), encoding='utf-8')
        new = self.publish()
        core.start_session(self.settings, 'NEW-BODY', 'Inspect the changed authorization implementation')
        for version in (2, 3):
            request = {'version': version, 'objective': 'Read the authorization implementation'}
            if version == 2:
                request['symbols'] = [{'name': 'authorize', 'repos': ['service'], 'include': ['definition']}]
            else:
                request['hints'] = {'symbols': ['authorize'], 'repos': ['service']}
                request['coverage'] = {'production': 'required', 'tests': 'omit', 'relationships': 'omit'}
            encoded = json.dumps({'CONTEXT_REQUEST': request})
            for ticket, pinned, present, absent in (
                ('OLD-BODY', old, 'return total >= amount;', 'return NEW_GENERATION;'),
                ('NEW-BODY', new, 'return NEW_GENERATION;', 'return total >= amount;'),
            ):
                with self.subTest(version=version, ticket=ticket):
                    bundle = core.retrieve_context(pinned, core.parse_context_request(encoded))
                    self.assertIn(present, core.pack_context(pinned, ticket, 1, bundle))
                    self.assertNotIn(absent, core.pack_context(pinned, ticket, 1, bundle))
                    self.assertEqual('complete_range', bundle.trace['symbol_reads'][0]['status'])
                    content, _, _ = core.create_context(self.settings, ticket, encoded)
                    self.assertIn(present, content)
                    self.assertNotIn(absent, content)
                    self.assertEqual(pinned.atlas_generation.identity,
                                     core.session_state(self.settings, ticket)['atlas_generation_id'])

    def test_mixed_file_and_symbol_request_reuses_only_its_verified_pinned_source(self):
        old = self.publish()
        self.source.write_text(self.source.read_text(encoding='utf-8').replace(
            'return total >= amount;', 'return NEW_GENERATION;'), encoding='utf-8')
        new = self.publish()
        request = self.request()
        request['INVESTIGATION_REQUEST']['files'] = [
            {'repo': 'service', 'path': 'Authorization.java', 'lines': '1-2'},
        ]
        for pinned, present, absent in ((old, 'return total >= amount;', 'return NEW_GENERATION;'),
                                        (new, 'return NEW_GENERATION;', 'return total >= amount;'),
                                        (old, 'return total >= amount;', 'return NEW_GENERATION;')):
            with self.subTest(generation=pinned.atlas_generation.identity), \
                    mock.patch.object(index, 'read_indexed_file', wraps=index.read_indexed_file) as direct, \
                    mock.patch.object(index, 'read_generation_files', wraps=index.read_generation_files) as batch:
                bundle = self.retrieve(replace(pinned, max_backend_operations=3), request)
            self.assertEqual(1, direct.call_count)
            self.assertEqual(0, batch.call_count, 'a full pinned file already read for a page needs no second read')
            self.assertEqual(3, bundle.trace['physical_backend_operations'])
            self.assertEqual('complete_range', bundle.trace['symbol_reads'][0]['status'])
            content = core.pack_context(pinned, 'MIXED', 1, bundle)
            self.assertIn(present, content)
            self.assertNotIn(absent, content)
        with mock.patch.object(index, 'read_indexed_file', return_value=None), \
                mock.patch.object(index, 'read_generation_files', return_value={}):
            unavailable = self.retrieve(old, request)
        self.assertFalse(unavailable.evidence, 'no successful source cache may survive a request')
        self.assertTrue(unavailable.unresolved)

    def test_failed_direct_read_does_not_poison_later_pinned_batch_hydration(self):
        pinned = self.publish()
        request = self.request()
        request['INVESTIGATION_REQUEST']['files'] = [
            {'repo': 'service', 'path': 'Authorization.java', 'lines': '1-2'},
        ]
        with mock.patch.object(index, 'read_indexed_file', return_value=None), \
                mock.patch.object(index, 'read_generation_files', wraps=index.read_generation_files) as batch:
            bundle = self.retrieve(pinned, request)
        self.assertEqual(1, batch.call_count)
        self.assertIn('return total >= amount;', core.pack_context(pinned, 'RETRY', 1, bundle))
        self.assertEqual('unavailable', bundle.trace['file_reads'][0]['status'])
        self.assertEqual('complete_range', bundle.trace['symbol_reads'][0]['status'])

    def test_source_hydration_is_counted_and_precedes_optional_range_metadata(self):
        pinned = self.publish()
        for budget in (0, 1, 2, 3):
            with self.subTest(budget=budget), \
                    mock.patch.object(index, 'read_generation_files', wraps=index.read_generation_files) as batch, \
                    mock.patch.object(core, '_requested_symbol_ranges', wraps=core._requested_symbol_ranges) as ranges:
                bundle = self.retrieve(replace(pinned, max_backend_operations=budget))
            self.assertEqual(budget, bundle.trace['physical_backend_operations'])
            self.assertEqual(int(budget >= 2), batch.call_count)
            self.assertEqual(int(budget >= 3), ranges.call_count)
            if budget <= 1:
                self.assertFalse(bundle.evidence)
                self.assertIn('physical-operation budget', '\n'.join(bundle.unresolved))
            else:
                content = core.pack_context(pinned, 'BUDGET', 1, bundle)
                self.assertIn('boolean authorize(int amount)', content)
                self.assertIn('source-hydration', bundle.trace['backend_ms'])
                if budget == 3:
                    self.assertIn('return total >= amount;', content)
                    self.assertEqual('complete_range', bundle.trace['symbol_reads'][0]['status'])
            if budget < 3:
                self.assertNotIn('symbol_reads', bundle.trace)
                self.assertNotEqual('coverage_satisfied', bundle.trace['stop_reason'])

    def test_small_file_keeps_its_imports_class_fields_and_other_context(self):
        source = ('package billing;\nimport example.Policy;\nclass Authorization {\n Policy policy;\n'
                  ' boolean authorize(int amount) { return policy.permits(amount); }\n}\n')
        self.source.write_text(source, encoding='utf-8')
        pinned = self.publish()
        bundle = self.retrieve(pinned)
        self.assertIn(source.rstrip(), core.pack_context(pinned, 'SMALL', 1, bundle))
        self.assertEqual('complete_file', bundle.trace['symbol_reads'][0]['status'])

    def test_long_method_keeps_annotations_previously_visible_in_its_source_window(self):
        self.source.write_text(self.source.read_text(encoding='utf-8').replace(
            ' boolean authorize(int amount)', ' @Transactional(readOnly = true)\n boolean authorize(int amount)'), encoding='utf-8')
        pinned = self.publish()
        bundle = self.retrieve(pinned)
        content = core.pack_context(pinned, 'ANNOTATED', 1, bundle)
        self.assertIn('@Transactional(readOnly = true)', content)
        self.assertIn('return total >= amount;', content)

    def test_fused_caller_window_survives_method_expansion_with_a_distant_callee(self):
        self.source.write_text(
            'package billing;\nclass Authorization {\n' + ''.join(f' int field{n};\n' for n in range(200)) +
            ' boolean authorize(int amount) {\n  return helper(amount);\n }\n' + '\n' * 44 +
            ' void invoke() {\n  authorize(1);\n }\n' + '\n' * 145 +
            ' boolean helper(int amount) {\n  return amount > 0;\n }\n}\n', encoding='utf-8')
        pinned = self.publish()
        request = self.request()
        request['INVESTIGATION_REQUEST']['required'] = ['callers', 'callees']
        bundle = self.retrieve(pinned, request)
        content = core.pack_context(pinned, 'RELATIONS', 1, bundle)
        self.assertIn('return helper(amount);', content)
        self.assertIn('authorize(1);', content)
        self.assertIn('return amount > 0;', content)

    def test_small_file_boundary_with_final_newline_is_not_counted_as_an_extra_line(self):
        self.source.write_text(
            'package billing;\nclass Authorization {\n' + ' int field;\n' * 345 +
            ' boolean authorize(int amount) { return amount > 0; }\n}\n// FINAL_LINE\n', encoding='utf-8')
        self.assertEqual(350, len(self.source.read_text(encoding='utf-8').splitlines()))
        pinned = self.publish()
        bundle = self.retrieve(pinned)
        self.assertEqual('complete_file', bundle.trace['symbol_reads'][0]['status'])
        self.assertEqual(350, bundle.trace['symbol_reads'][0]['total_lines'])
        self.assertIn('// FINAL_LINE', core.pack_context(pinned, 'BOUNDARY', 1, bundle))

    def test_explicit_symbols_lost_to_operation_or_selection_budgets_are_reported(self):
        for number in range(20):
            (self.root / f'service/A{number}.java').write_text(
                f'package billing;\nclass A{number} {{\n boolean run() {{ return true; }}\n}}\n', encoding='utf-8')
        pinned = replace(self.publish(), hydrate_limit=8)
        request = self.request()
        request['INVESTIGATION_REQUEST']['anchors'] = [
            {'kind': 'symbol', 'value': f'billing.A{number}.run'} for number in range(20)]
        bundle = self.retrieve(pinned, request)
        self.assertTrue(any('No dedicated symbol lookup was scheduled' in item for item in bundle.unresolved))
        self.assertTrue(any('source deferred by candidate selection' in item for item in bundle.unresolved))
        self.assertNotEqual('coverage_satisfied', bundle.trace['stop_reason'])

    def test_explicit_methods_share_one_repository_without_discovery_diversity_loss(self):
        for number in range(12):
            (self.root / f'service/A{number}.java').write_text(
                f'package billing;\nclass A{number} {{\n int run() {{ return {1000 + number}; }}\n}}\n', encoding='utf-8')
        pinned = replace(self.publish(), max_regions_per_repo=1)
        request = self.request()
        request['INVESTIGATION_REQUEST']['anchors'] = [
            {'kind': 'symbol', 'value': f'billing.A{number}.run'} for number in range(12)]
        bundle = self.retrieve(pinned, request)
        content = core.pack_context(pinned, 'BATCH', 1, bundle)
        for number in range(12):
            self.assertIn(f'return {1000 + number};', content)
        self.assertEqual(12, len(bundle.trace['symbol_reads']))
        self.assertFalse(any('source deferred by candidate selection' in item for item in bundle.unresolved))

    def test_explicit_anchor_batch_reaches_the_configured_request_capacity(self):
        for number in range(20):
            (self.root / f'service/B{number}.java').write_text(
                f'package billing;\nclass B{number} {{\n int run() {{ return {2000 + number}; }}\n}}\n', encoding='utf-8')
        pinned = self.publish()
        for count in (15, 20):
            with self.subTest(count=count):
                request = self.request()
                request['INVESTIGATION_REQUEST'].update(
                    objective='Compare the explicitly requested implementations',
                    anchors=[{'kind': 'symbol', 'value': f'billing.B{number}.run'} for number in range(count)],
                )
                bounded = replace(pinned, max_effective_operations=count, hydrate_limit=count)
                parsed = core.parse_context_request(json.dumps(request))
                self.assertGreaterEqual(len(parsed['searches']), count)
                bundle = core.retrieve_context(bounded, parsed)
                content = core.pack_context(bounded, 'CAPACITY', 1, bundle)
                for number in range(count):
                    self.assertIn(f'return {2000 + number};', content)
                self.assertEqual(count, len(bundle.trace['symbol_reads']))
                self.assertFalse(any('No dedicated symbol lookup' in item for item in bundle.unresolved))

    def test_requested_relations_share_the_anchor_operation_without_losing_definitions(self):
        from brain.retrieval.planner import compile_request

        for number in range(15):
            (self.root / f'service/C{number}.java').write_text(
                f'package billing;\nclass C{number} {{\n int run() {{ return {3000 + number}; }}\n}}\n', encoding='utf-8')
        pinned = self.publish()
        for required in ('callers', 'tests'):
            with self.subTest(required=required):
                request = self.request()
                request['INVESTIGATION_REQUEST'].update(
                    objective='Compare the explicitly requested implementations', required=[required],
                    anchors=[{'kind': 'symbol', 'value': f'billing.C{number}.run'} for number in range(15)],
                )
                parsed = core.parse_context_request(json.dumps(request))
                plan = compile_request(parsed)
                self.assertEqual(15, len(plan.operations))
                self.assertEqual(0, plan.deferred_operations)
                self.assertTrue(all(item.kind == 'symbol' and set(item.includes) == {'definition', required}
                                    for item in plan.operations))
                bundle = core.retrieve_context(pinned, parsed)
                content = core.pack_context(pinned, 'FUSED', 1, bundle)
                for number in range(15):
                    self.assertIn(f'return {3000 + number};', content)
                self.assertEqual(15, len(bundle.trace['symbol_reads']))
                self.assertFalse(any('No dedicated symbol lookup' in item for item in bundle.unresolved))
                self.assertTrue(any(('not established' if required == 'callers' else 'No tests referencing') in item
                                    for item in bundle.unresolved))

    def test_runtime_retains_each_exact_relation_seed_and_reports_graph_batch_truncation(self):
        from brain.investigation import MAX_FLOW_SEEDS, build_ticket_runtime, render_protocol_v5

        for number in range(30):
            (self.root / f'service/D{number}.java').write_text(
                f'package billing;\nclass D{number} {{\n int run() {{ return helper(); }}\n'
                f' int helper() {{ return {4000 + number}; }}\n}}\n', encoding='utf-8')
        pinned = self.publish()
        for count in (20, 30):
            with self.subTest(count=count):
                request = self.request()
                request['INVESTIGATION_REQUEST'].update(
                    objective='Compare the requested implementations', required=['callees'],
                    anchors=[{'kind': 'symbol', 'value': f'billing.D{number}.run'} for number in range(count)],
                )
                parsed = core.parse_context_request(json.dumps(request))
                bounded = replace(pinned, max_effective_operations=count, hydrate_limit=50)
                bundle = core.retrieve_context(bounded, parsed)
                self.assertEqual(count, len(bundle._resolved_relation_seeds))
                runtime = build_ticket_runtime(
                    bounded, pinned.atlas_generation, parsed, bundle,
                    {'coverage_map': {}, 'stable_identities': {}}, context_id=f'CTX-BATCH-{count}',
                )
                flow = runtime['execution_flow']
                self.assertEqual(min(count, MAX_FLOW_SEEDS), len({
                    item['source_id'] for item in flow['steps'] if item['edge_type'] == 'CALLS'}))
                self.assertEqual(count > MAX_FLOW_SEEDS, flow['truncated'])
                if count > MAX_FLOW_SEEDS:
                    self.assertIn('Partial bounded graph', render_protocol_v5(runtime))

    def test_parser_preserves_all_explicit_inputs_and_the_public_anchor_byte_contract(self):
        from brain.investigation import _qualified_symbol_queries
        from brain.retrieval.planner import compile_request

        request = self.request()
        anchors = [{'kind': 'symbol', 'value': f'billing.{"A" * 550}{number}.run'} for number in range(50)]
        resolves = [f'literal-{number}' for number in range(50)]
        request['INVESTIGATION_REQUEST'].update(anchors=anchors, resolve=resolves, objective='Inspect "AUTH_TIMEOUT"')
        parsed = core.parse_context_request(json.dumps(request))
        explicit = [item['value'] for item in anchors] + resolves
        self.assertEqual(explicit, [item['query'] for item in parsed['searches'][:100]])
        self.assertIn('AUTH_TIMEOUT', [item['query'] for item in parsed['searches']])
        self.assertEqual(50, len(_qualified_symbol_queries(parsed['anchors'])))
        plan = compile_request(parsed)
        self.assertEqual(15, len(plan.operations))
        self.assertEqual(len(parsed['searches']) - 15, plan.deferred_operations)
        for value in ('a' * 1_001, '界' * 334):
            with self.subTest(invalid=value[:4]):
                request['INVESTIGATION_REQUEST']['anchors'] = [{'kind': 'symbol', 'value': value}]
                with self.assertRaisesRegex(core.BrainError, 'invalid kind or value'):
                    core.parse_context_request(json.dumps(request))

    def test_runtime_relation_seeds_cannot_substitute_a_newer_generation(self):
        from brain.investigation import build_ticket_runtime

        self.source.write_text(
            'package billing;\nclass Authorization {\n boolean authorize() { return helper(); }\n'
            ' boolean helper() { return true; }\n}\n', encoding='utf-8')
        pinned = self.publish()
        request = self.request()
        request['INVESTIGATION_REQUEST']['required'] = ['callees']
        parsed = core.parse_context_request(json.dumps(request))
        bundle = core.retrieve_context(pinned, parsed)
        self.source.write_text(self.source.read_text(encoding='utf-8').replace('return true;', 'return false;'), encoding='utf-8')
        newer = self.publish()
        newer_anchor = resolve_runtime_anchors(newer, newer.atlas_generation, parsed['anchors'])['candidates'][0]
        runtime = build_ticket_runtime(
            pinned, pinned.atlas_generation, parsed, bundle,
            {'coverage_map': {}, 'stable_identities': {}}, context_id='CTX-OLD-SEEDS',
        )
        self.assertEqual(pinned.atlas_generation.generation, runtime['generation'])
        self.assertTrue(runtime['execution_flow']['steps'])
        # A damaged handoff must not authorize an otherwise valid G2 entity.
        bundle._resolved_relation_seeds['billing.Authorization.authorize'] = newer_anchor['entity_id']
        rejected = build_ticket_runtime(
            pinned, pinned.atlas_generation, parsed, bundle,
            {'coverage_map': {}, 'stable_identities': {}}, context_id='CTX-INVALID-SEEDS',
        )
        self.assertEqual('degraded', rejected['execution_flow']['status'])
        self.assertFalse(rejected['execution_flow']['steps'])

    def test_resolver_tail_inputs_do_not_reuse_stale_metadata_or_broaden_qualified_names(self):
        pinned = self.publish()
        anchors = [{'kind': 'symbol', 'value': f'billing.Missing{number}.run'} for number in range(49)]
        anchors.append({'kind': 'symbol', 'value': 'billing.Authorization.authorize'})
        first = resolve_runtime_anchors(pinned, pinned.atlas_generation, anchors)
        self.assertFalse(first['cache_hit'])
        self.assertFalse(first['candidates'])  # The batch budget cannot turn a tail symbol into fuzzy discovery.
        self.assertEqual(0, first['bounds']['compound_terms'])
        prefix = resolve_runtime_anchors(pinned, pinned.atlas_generation, anchors[:16])
        self.assertEqual(prefix['database_operations'], first['database_operations'])
        anchors[-1] = {'kind': 'symbol', 'value': 'billing.Other.authorize'}
        second = resolve_runtime_anchors(pinned, pinned.atlas_generation, anchors)
        self.assertFalse(second['cache_hit'])
        self.assertEqual([item['value'] for item in anchors], second['inputs'])
        self.assertFalse(second['candidates'])
        cached = resolve_runtime_anchors(pinned, pinned.atlas_generation, anchors)
        self.assertTrue(cached['cache_hit'])
        self.assertEqual(second['inputs'], cached['inputs'])

    def test_qualified_follow_up_does_not_turn_prose_into_searches_or_blockers(self):
        pinned = self.publish()
        request = self.request()
        request['INVESTIGATION_REQUEST']['objective'] = 'Read the authorization body again to assess its decisive return'
        parsed = core.parse_context_request(json.dumps(request))
        self.assertEqual(['billing.Authorization.authorize'], [item['query'] for item in parsed['searches']])
        bundle = core.retrieve_context(pinned, parsed)
        self.assertIn('return total >= amount;', core.pack_context(pinned, 'FOLLOW-UP', 1, bundle))
        self.assertEqual(3, bundle.trace['physical_backend_operations'])
        self.assertFalse(bundle.unresolved)
        core.start_session(self.settings, 'FOLLOW-UP', 'Verify the authorization decision')
        content, _, _ = core.create_context(self.settings, 'FOLLOW-UP', json.dumps(request))
        self.assertIn('return total >= amount;', content)
        state = core.session_state(self.settings, 'FOLLOW-UP')
        unknowns = state['investigation_memory']['blocking_unknowns']
        frontier = state['investigation_runtime']['evidence_frontier']['items']
        self.assertFalse(any('returned no code matches' in item for item in unknowns))
        self.assertFalse(any('returned no code matches' in item['statement'] for item in frontier))
        self.assertTrue(any(item['statement'] == 'Establish production entry point' for item in frontier))

    def test_qualified_follow_up_keeps_structured_clues_and_explicit_evidence(self):
        (self.root / 'service/Recovery.java').write_text(
            'class Recovery {\n String reason() { return "AUTH_TIMEOUT"; }\n}\n', encoding='utf-8')
        (self.root / 'service/resolve.py').write_text('explicit_literal = 1\n', encoding='utf-8')
        (self.root / 'service/manual.py').write_text('FILE_EVIDENCE = 2\n', encoding='utf-8')
        pinned = self.publish()
        request = self.request()
        request['INVESTIGATION_REQUEST'].update(
            objective='Read the authorization body and trace "AUTH_TIMEOUT"',
            resolve=['explicit_literal'],
            files=[{'repo': 'service', 'path': 'manual.py'}],
        )
        parsed = core.parse_context_request(json.dumps(request))
        queries = [item['query'] for item in parsed['searches']]
        self.assertIn('AUTH_TIMEOUT', queries)
        self.assertIn('explicit_literal', queries)
        bundle = core.retrieve_context(pinned, parsed)
        content = core.pack_context(pinned, 'CLUES', 1, bundle)
        self.assertIn('return total >= amount;', content)
        self.assertIn('return "AUTH_TIMEOUT";', content)
        self.assertIn('explicit_literal', content)
        self.assertIn('FILE_EVIDENCE', content)

    def test_qualified_follow_up_still_routes_meaningful_natural_language_clues(self):
        from brain import atlas

        (self.root / 'service/TimeoutPolicy.java').write_text(
            'package billing;\nclass TimeoutPolicy {\n boolean rejectAfterTimeout() { return true; }\n}\n', encoding='utf-8')
        pinned = self.publish()
        for objective in (
            'Why is the authorization body rejected when timeout occurs?',
            'Why does billing.Authorization.authorize reject timeout',
            'Trace billing.Authorization.authorize during timeout',
        ):
            with self.subTest(objective=objective):
                request = self.request()
                request['INVESTIGATION_REQUEST']['objective'] = objective
                parsed = core.parse_context_request(json.dumps(request))
                self.assertEqual(['billing.Authorization.authorize'], [item['query'] for item in parsed['searches']])
                routed = atlas.route(pinned, parsed['objective'], parsed, pinned.atlas_generation)
                self.assertFalse(routed['qualified_symbols_only'])
                bundle = core.retrieve_context(pinned, parsed)
                content = core.pack_context(pinned, 'TIMEOUT', 1, bundle)
                self.assertIn('return total >= amount;', content)
                self.assertIn('rejectAfterTimeout() { return true; }', content)
                self.assertFalse(any('returned no code matches' in item for item in bundle.unresolved))
                self.assertLess(bundle.trace['physical_backend_operations'], 7)

    def test_exact_source_framing_keeps_the_qualified_fast_path(self):
        from brain import atlas

        pinned = self.publish()
        for objective in (
            'billing.Authorization.authorize',
            'Read the complete implementation of billing.Authorization.authorize',
            'Read billing.Authorization.authorize again',
            'Read `billing.Authorization.authorize()` again.',
            'Trace billing.Authorization.authorize',
        ):
            with self.subTest(objective=objective):
                request = self.request()
                request['INVESTIGATION_REQUEST']['objective'] = objective
                parsed = core.parse_context_request(json.dumps(request))
                self.assertTrue(atlas.route(pinned, objective, parsed, pinned.atlas_generation)['qualified_symbols_only'])
                with mock.patch('brain.editions.current_edition', return_value='precision'), \
                        mock.patch('brain.semantic.search_semantic', side_effect=AssertionError('unnecessary model search')), \
                        mock.patch('brain.models.rerank_candidates', side_effect=AssertionError('unnecessary rerank')):
                    bundle = core.retrieve_context(pinned, parsed)
                self.assertIn('return total >= amount;', core.pack_context(pinned, 'EXACT', 1, bundle))
                self.assertEqual(3, bundle.trace['physical_backend_operations'])

    def test_short_numeric_and_unicode_clues_do_not_disable_precision_discovery(self):
        (self.root / 'service/RegionalPolicy.java').write_text(
            'class RegionalPolicy {\n boolean permits() { return REGION_DENIED; }\n}\n', encoding='utf-8')
        pinned = self.publish()
        for clue in ('EU', '42', 'v2', '東京', '🔒', '+'):
            with self.subTest(clue=clue):
                request = self.request()
                request['INVESTIGATION_REQUEST']['objective'] = f'Trace billing.Authorization.authorize for {clue}'
                parsed = core.parse_context_request(json.dumps(request))
                self.assertEqual(['billing.Authorization.authorize'], [item['query'] for item in parsed['searches']])

                def semantic(settings, objective, **kwargs):
                    self.assertEqual(parsed['objective'], objective)
                    self.assertEqual(pinned.atlas_generation, kwargs['generation'])
                    kwargs['serving_status']['status'] = 'ready'
                    return [{'repo': 'service', 'path': 'RegionalPolicy.java', 'line': 2, 'symbol': 'permits', 'score': .9}]

                with mock.patch('brain.editions.current_edition', return_value='precision'), \
                        mock.patch('brain.semantic.search_semantic', side_effect=semantic) as model, \
                        mock.patch('brain.models.rerank_candidates', side_effect=lambda settings, query, hits, **kwargs: hits) as rerank:
                    bundle = core.retrieve_context(pinned, parsed)
                model.assert_called_once()
                rerank.assert_called_once()
                self.assertEqual('ready', bundle.trace['semantic_status'])
                self.assertFalse(bundle.trace.get('qualified_symbols_only'))
                content = core.pack_context(pinned, 'SHORT-CLUE', 1, bundle)
                self.assertIn('return total >= amount;', content)
                self.assertIn('return REGION_DENIED;', content)
                self.assertFalse(any('returned no code matches' in item for item in bundle.unresolved))

    def test_canonical_id_source_framing_reuses_the_validated_leaf_identity(self):
        from brain import atlas

        self.source.write_text(self.source.read_text(encoding='utf-8').replace(
            'authorize(', 'authorize_payment('), encoding='utf-8')
        (self.root / 'service/Noise.java').write_text(
            'class Noise {\n void payment() {}\n void authorize() {}\n}\n', encoding='utf-8')
        pinned = self.publish()
        resolved = resolve_runtime_anchors(pinned, pinned.atlas_generation,
            [{'kind': 'symbol', 'value': 'billing.Authorization.authorize_payment'}])
        target = resolved['candidates'][0]
        for objective in ('Read authorize_payment again',
                          f"Read `{target['value']}` again"):
            with self.subTest(objective=objective):
                request = self.request(target['entity_id'])
                request['INVESTIGATION_REQUEST']['objective'] = objective
                parsed = core.parse_context_request(json.dumps(request))
                for cached in (False, True):
                    routed = atlas.route(pinned, objective, parsed, pinned.atlas_generation)
                    self.assertEqual(cached, routed['cache_hit'])
                    self.assertTrue(routed['qualified_symbols_only'])
                    self.assertEqual(0, routed['cards_considered'])
                with mock.patch('brain.editions.current_edition', return_value='precision'), \
                        mock.patch('brain.semantic.search_semantic', side_effect=AssertionError('unnecessary model search')), \
                        mock.patch('brain.models.rerank_candidates', side_effect=AssertionError('unnecessary rerank')), \
                        mock.patch.object(core, 'search', side_effect=AssertionError('duplicate identity search')):
                    bundle = core.retrieve_context(pinned, parsed)
                self.assertEqual({'Authorization.java'}, {item.path for item in bundle.evidence})
                self.assertIn('return total >= amount;', core.pack_context(pinned, 'ID-ALIAS', 1, bundle))
                self.assertEqual(3, bundle.trace['physical_backend_operations'])

    def test_canonical_id_alias_does_not_swallow_business_clues_or_unavailable_identity(self):
        from brain import atlas

        pinned = self.publish()
        resolved = resolve_runtime_anchors(pinned, pinned.atlas_generation,
            [{'kind': 'symbol', 'value': 'billing.Authorization.authorize'}])
        identifier = resolved['candidates'][0]['entity_id']
        for clue in ('timeout', 'EU', '42', 'v2', '東京', '🔒', '+', 'authorization'):
            with self.subTest(clue=clue):
                request = self.request(identifier)
                request['INVESTIGATION_REQUEST']['objective'] = f'Read authorize for {clue}'
                parsed = core.parse_context_request(json.dumps(request))
                self.assertFalse(atlas.route(pinned, parsed['objective'], parsed,
                                            pinned.atlas_generation)['qualified_symbols_only'])
        request = self.request(identifier)
        request['INVESTIGATION_REQUEST']['objective'] = 'Read authorize again'
        parsed = core.parse_context_request(json.dumps(request))
        unavailable = replace(pinned.atlas_generation, components={**pinned.atlas_generation.components,
            'runtime_anchors': {'status': 'unavailable'}})
        self.assertFalse(atlas.route(pinned, parsed['objective'], parsed, unavailable)['qualified_symbols_only'])

    def test_legacy_v4_keeps_all_explicit_resolves_for_execution_and_deferral(self):
        queries = [f'RESOLVE_{number:02}' for number in range(20)]
        (self.root / 'service/resolves.py').write_text(
            ''.join(f'{query} = {number}\n' for number, query in enumerate(queries)), encoding='utf-8')
        pinned = self.publish()
        request = {'INVESTIGATION_REQUEST': {
            'version': 4, 'objective': 'Inspect all explicit resolution values', 'resolve': queries,
        }}
        parsed = core.parse_context_request(json.dumps(request))
        self.assertEqual(queries, [item['query'] for item in parsed['searches']])
        for budget in (15, 20):
            with self.subTest(budget=budget), mock.patch.object(core, 'search', wraps=core.search) as search:
                bundle = core.retrieve_context(replace(pinned, max_effective_operations=budget), parsed)
                self.assertEqual(queries[:budget], [call.args[1] for call in search.call_args_list])
                self.assertEqual(20, bundle.trace['requested_operations'])
                self.assertEqual(budget, bundle.trace['effective_operations'])
                self.assertEqual(20 - budget, bundle.trace['planner']['deferred_operations'])
                self.assertEqual(20 - budget, sum(item.startswith('Explicit resolve') for item in bundle.unresolved))
                if budget < 20:
                    self.assertEqual('operation_budget', bundle.trace['stop_reason'])
                    self.assertTrue(any('deferred' in item and 'unknown' in item for item in bundle.unresolved))

        settings = replace(self.settings, max_effective_operations=15)
        core.start_session(settings, 'LEGACY-RESOLVE', 'Inspect all explicit resolution values')
        core.create_context(settings, 'LEGACY-RESOLVE', json.dumps(request))
        state = core.session_state(settings, 'LEGACY-RESOLVE')
        (self.root / 'service/resolves.py').write_text(
            ''.join(f'{query} = NEW_G2_VALUE\n' for query in queries), encoding='utf-8')
        self.publish()
        follow_up = {'INVESTIGATION_REQUEST': {
            'version': 4, 'objective': 'Inspect remaining explicit values', 'resolve': queries[15:],
            'base_context_id': state['last_context_id'], 'checkpoint': True,
        }}
        content, _, _ = core.create_context(settings, 'LEGACY-RESOLVE', json.dumps(follow_up))
        self.assertIn('RESOLVE_19 = 19', content)
        self.assertNotIn('NEW_G2_VALUE', content)
        continued = core.session_state(settings, 'LEGACY-RESOLVE')
        self.assertEqual(pinned.atlas_generation.identity, continued['atlas_generation_id'])
        self.assertEqual(2, len(continued['request_history']))

    def test_legacy_v4_parser_retains_its_small_request_discovery_budget(self):
        for count in (0, 1, 12, 20, 50):
            with self.subTest(count=count):
                queries = [f'resolve-{number}' for number in range(count)]
                parsed = core.parse_context_request(json.dumps({'INVESTIGATION_REQUEST': {
                    'version': 4, 'objective': 'invoice cancellation rejection routing authorization handling',
                    'resolve': queries,
                }}))
                self.assertEqual(queries, [item['query'] for item in parsed['searches'][:count]])
                self.assertLessEqual(len(parsed['searches']), max(12, count))

    def test_natural_language_discovery_remains_for_requests_without_qualified_symbols(self):
        for anchors in ([], [{'kind': 'topic', 'value': 'orders'}], [{'kind': 'symbol', 'value': 'authorize'}]):
            with self.subTest(anchors=anchors):
                request = self.request()
                request['INVESTIGATION_REQUEST'].update(objective='invoice cancellation', anchors=anchors)
                parsed = core.parse_context_request(json.dumps(request))
                self.assertTrue({'invoice', 'cancellation'} <= {item['query'] for item in parsed['searches']})

    def test_qualified_class_uses_structural_range_without_hiding_its_last_method(self):
        pinned = self.publish()
        bundle = self.retrieve(pinned, self.request('billing.Authorization'))
        content = core.pack_context(pinned, 'CLASS', 1, bundle)
        self.assertIn('return total >= amount;', content)
        self.assertIn('class Authorization', content)
        self.assertEqual('complete_file', bundle.trace['symbol_reads'][0]['status'])

    def test_public_protocol_delivers_method_tail_and_keeps_its_ticket_generation(self):
        old = self.publish()
        core.start_session(self.settings, 'METHOD', 'Investigate the full authorization decision')
        content, _, _ = core.create_context(self.settings, 'METHOD', json.dumps(self.request()))
        self.assertIn('if (amount < 0) return false;', content)
        self.assertIn('return total >= amount;', content)
        self.source.write_text(self.source.read_text(encoding='utf-8').replace(
            'return total >= amount;', 'return NEW_GENERATION;'), encoding='utf-8')
        self.publish()
        request = {'INVESTIGATION_REQUEST': {
            'version': 5, 'mode': 'root_cause', 'objective': 'Verify the decisive return in the same investigation',
            'files': [{'repo': 'service', 'path': 'Authorization.java', 'lines': '405-407'}],
        }}
        content, _, _ = core.create_context(self.settings, 'METHOD', json.dumps(request))
        self.assertIn('return total >= amount;', content)
        self.assertNotIn('return NEW_GENERATION;', content)
        self.assertEqual(old.atlas_generation.identity, core.session_state(self.settings, 'METHOD')['atlas_generation_id'])

    def test_explicit_symbol_is_reemitted_in_a_known_evidence_delta(self):
        old = self.publish()
        settings = replace(self.settings, hydrate_limit=1, max_regions_per_repo=1, max_regions_per_file=1)
        tickets = (('REPEAT', 'billing.Authorization.authorize'), ('UNQUALIFIED', 'authorize'))
        states = {}
        for ticket, symbol in tickets:
            core.start_session(settings, ticket, 'Inspect the authorization implementation')
            first, _, _ = core.create_context(settings, ticket, json.dumps(self.request(symbol)))
            self.assertIn('return total >= amount;', first)
            states[ticket] = core.session_state(settings, ticket)
        self.source.write_text(self.source.read_text(encoding='utf-8').replace(
            'return total >= amount;', 'return NEW_GENERATION;'), encoding='utf-8')
        self.publish()
        for ticket, symbol in tickets:
            with self.subTest(symbol=symbol):
                request = self.request(symbol)
                request['INVESTIGATION_REQUEST'].update(
                    objective='Read the authorization body again to assess its decisive return',
                    base_context_id=states[ticket]['last_context_id'],
                )
                second, _, _ = core.create_context(settings, ticket, json.dumps(request))
                self.assertTrue(second.startswith('# PROJECT BRAIN CONTEXT DELTA'))
                self.assertIn('New or not-yet-emitted evidence: `0`', second)
                self.assertIn('return total >= amount;', second)
                self.assertNotIn('return NEW_GENERATION;', second)
                self.assertEqual(old.atlas_generation.identity, core.session_state(settings, ticket)['atlas_generation_id'])

    def test_same_file_fusion_keeps_both_requested_method_ranges(self):
        self.source.write_text(
            'package billing;\nclass Authorization {\n' +
            ''.join(f' int field{n};\n' for n in range(200)) +
            ' boolean first() { return true; }\n boolean authorize(int amount) {\n' +
            ''.join(f'  amount += {n};\n' for n in range(200)) +
            '  return amount > 42;\n }\n}\n', encoding='utf-8')
        pinned = self.publish()
        request = self.request()
        request['INVESTIGATION_REQUEST']['anchors'].append({'kind': 'symbol', 'value': 'billing.Authorization.first'})
        with mock.patch.object(index, 'read_generation_files', wraps=index.read_generation_files) as read, \
                mock.patch.object(core, '_source_line_offsets', wraps=core._source_line_offsets) as offsets:
            bundle = self.retrieve(pinned, request)
        self.assertEqual(1, read.call_count)
        self.assertEqual(1, offsets.call_count)
        content = core.pack_context(pinned, 'TWO', 1, bundle)
        self.assertIn('first() { return true; }', content)
        self.assertIn('return amount > 42;', content)
        self.assertEqual(2, len(bundle.trace['symbol_reads']))

    def test_legacy_same_file_symbols_keep_each_decisive_source_window(self):
        self.source.write_text(
            'class Authorization {\n int first() { return FIRST_DECISION; }\n' +
            ''.join(f' // gap {number}\n' for number in range(74)) +
            ' int second() {\n  return SECOND_DECISION;\n }\n' +
            ''.join(f' // tail {number}\n' for number in range(400)) + '}\n', encoding='utf-8')
        pinned = replace(self.publish(), hydrate_limit=2, max_regions_per_file=2,
                         candidate_limit=100, hard_context_chars=500_000)
        for version, structural in ((2, False), (3, False), (2, True), (3, True)):
            body = {'version': version, 'objective': 'Inspect both implementations'}
            if version == 3:
                body['hints'] = {'symbols': ['first', 'second'], 'repos': ['service']}
                body['coverage'] = {'production': 'required', 'tests': 'omit', 'relationships': 'omit'}
            else:
                body['symbols'] = [{'name': name, 'repos': ['service'], 'include': ['definition']}
                                   for name in ('first', 'second')]
            with self.subTest(version=version, structural=structural), \
                    mock.patch('brain.graph.graph_symbol_hits', side_effect=lambda settings, name, scope: [
                        core.SearchHit('service', 'Authorization.java', {'first': 2, 'second': 77}[name],
                                       name, 'structural definition', 110, ['structural graph']),
                    ] if structural else []), \
                    mock.patch.object(index, 'read_generation_files', wraps=index.read_generation_files) as batch:
                bundle = self.retrieve(pinned, {'CONTEXT_REQUEST': body})
            content = core.pack_context(pinned, 'LEGACY-METHODS', 1, bundle)
            self.assertIn('FIRST_DECISION', content)
            self.assertIn('SECOND_DECISION', content)
            self.assertEqual(1, batch.call_count, 'distinct windows share one verified source batch')
            limited = self.retrieve(replace(pinned, max_regions_per_file=1), {'CONTEXT_REQUEST': body})
            self.assertNotIn('SECOND_DECISION', core.pack_context(pinned, 'PARTIAL', 1, limited))
            self.assertNotEqual('coverage_satisfied', limited.trace['stop_reason'])
            self.assertTrue(any('Requested symbol source window deferred' in item for item in limited.unresolved))
            complete = self.retrieve(replace(pinned, max_regions_per_file=1, full_file_lines=1_000),
                                     {'CONTEXT_REQUEST': body})
            self.assertIn('SECOND_DECISION', core.pack_context(pinned, 'FULL-FILE', 1, complete))
            self.assertFalse(any('Requested symbol source window deferred' in item for item in complete.unresolved),
                             'one complete source file already satisfies the omitted candidate window')
            with mock.patch.object(index, 'read_generation_files', return_value={}):
                unavailable = self.retrieve(pinned, {'CONTEXT_REQUEST': body})
            self.assertFalse(unavailable.evidence)
            self.assertNotEqual('coverage_satisfied', unavailable.trace['stop_reason'])

    def test_far_apart_requested_methods_do_not_share_an_unrelated_source_window(self):
        self.source.write_text(
            'package billing;\nclass Authorization {\n' +
            ''.join(f' int field{n};\n' for n in range(200)) +
            ' boolean first() { return FIRST_DECISION; }\n' +
            ''.join(' // intervening unrelated source ' + 'x' * 100 + '\n' for _ in range(900)) +
            ' boolean second() { return SECOND_DECISION; }\n}\n', encoding='utf-8')
        pinned = replace(self.publish(), max_regions_per_file=1, max_regions_per_repo=1)
        request = self.request()
        request['INVESTIGATION_REQUEST']['anchors'] = [
            {'kind': 'symbol', 'value': f'billing.Authorization.{name}'} for name in ('second', 'first')]
        bundle = self.retrieve(pinned, request)
        content = core.pack_context(pinned, 'DISTANT', 1, bundle)
        self.assertIn('return FIRST_DECISION;', content)
        self.assertIn('return SECOND_DECISION;', content)
        self.assertEqual(2, len(bundle.trace['symbol_reads']))
        for report in bundle.trace['symbol_reads']:
            self.assertIsNone(report['next_lines'])
            self.assertEqual('complete_range', report['status'])
            start, end = map(int, report['returned_lines'].split('-'))
            requested, _ = map(int, report['requested_lines'].split('-'))
            self.assertLessEqual(start, requested)
            self.assertGreaterEqual(end, requested)

    def test_optional_window_cannot_displace_a_complete_requested_short_method(self):
        self.source.write_text(
            'package billing;\nclass Authorization {\n' +
            ''.join(' // unrelated context ' + 'x' * 1000 + '\n' for _ in range(100)) +
            ' boolean authorize(int amount) { return COMPLETE_DECISION; }\n' + '\n' * 400 + '}\n', encoding='utf-8')
        pinned = self.publish()
        bundle = self.retrieve(pinned)
        content = core.pack_context(pinned, 'SHORT', 1, bundle)
        self.assertIn('return COMPLETE_DECISION;', content)
        self.assertEqual('complete_range', bundle.trace['symbol_reads'][0]['status'])
        self.assertIsNone(bundle.trace['symbol_reads'][0]['next_lines'])

    def test_unicode_method_pages_continue_without_cutting_lines_or_claiming_full_coverage(self):
        self.source.write_text(
            'package billing;\nclass Authorization {\n boolean authorize(int amount) {\n' +
            ''.join(f'  // {number:04} ' + '证据' * 100 + '\n' for number in range(400)) +
            '  return amount > 42;\n }\n}\n', encoding='utf-8')
        pinned = self.publish()
        bundle = self.retrieve(pinned)
        report = bundle.trace['symbol_reads'][0]
        self.assertEqual('partial', report['status'])
        self.assertEqual('requested_symbols_incomplete', bundle.trace['stop_reason'])
        self.assertTrue(any(report['next_lines'] in warning for warning in bundle.unresolved))
        for request in (self.request('authorize'), {'CONTEXT_REQUEST': {
            'version': 2, 'objective': 'Inspect the implementation',
            'symbols': [{'name': 'authorize', 'repos': ['service'], 'include': ['definition']}],
        }}):
            with self.subTest(request=request):
                legacy = self.retrieve(pinned, request)
                self.assertEqual('partial', legacy.trace['symbol_reads'][0]['status'])
                self.assertEqual(report['next_lines'], legacy.trace['symbol_reads'][0]['next_lines'])
                self.assertEqual(bundle.evidence[0].content, legacy.evidence[0].content)
                self.assertTrue(any(report['next_lines'] in item for item in legacy.unresolved))
        contents = [bundle.evidence[0].content]
        for _ in range(10):
            if not report.get('next_lines'):
                break
            request = {'INVESTIGATION_REQUEST': {
                'version': 5, 'mode': 'root_cause', 'objective': 'Continue the pinned method source',
                'files': [{'repo': 'service', 'path': 'Authorization.java', 'lines': report['next_lines']}],
            }}
            continuation = self.retrieve(pinned, request)
            report = continuation.trace['file_reads'][0]
            contents.append(continuation.evidence[0].content)
            self.assertLessEqual(len(contents[-1].encode('utf-8')), 64_000)
        self.assertFalse(report.get('next_lines'))
        expected = '\n'.join(self.source.read_text(encoding='utf-8').splitlines()[:-1])
        self.assertEqual(expected, '\n'.join(contents))

    def test_old_generation_range_and_source_remain_pinned_after_new_publication(self):
        old = self.publish()
        self.source.write_text(self.source.read_text(encoding='utf-8').replace(
            'return total >= amount;', 'return NEW_GENERATION;'), encoding='utf-8')
        new = self.publish()
        for pinned, present, absent in ((old, 'return total >= amount;', 'return NEW_GENERATION;'),
                                        (new, 'return NEW_GENERATION;', 'return total >= amount;'),
                                        (old, 'return total >= amount;', 'return NEW_GENERATION;')):
            bundle = self.retrieve(pinned)
            content = core.pack_context(pinned, 'PIN', 1, bundle)
            self.assertIn(present, content)
            self.assertNotIn(absent, content)
            self.assertEqual('complete_range', bundle.trace['symbol_reads'][0]['status'])
        with mock.patch.object(index, 'read_generation_files', return_value={}):
            unavailable = self.retrieve(old)
        self.assertFalse(unavailable.evidence)
        self.assertEqual('unavailable', unavailable.trace['symbol_reads'][0]['status'])
        self.assertTrue(unavailable.unresolved)

    def test_corrupt_entity_and_missing_membership_cannot_authorize_full_ranges(self):
        pinned = self.publish()
        resolved = resolve_runtime_anchors(pinned, pinned.atlas_generation,
                                          self.request()['INVESTIGATION_REQUEST']['anchors'])
        anchors = {item['entity_id']: item for item in resolved['candidates']}
        self.assertEqual(1, len(core._requested_symbol_ranges(pinned, anchors)[0]))
        connection = connect(pinned)
        try:
            identifier = next(iter(anchors))
            connection.execute('UPDATE atlas_entities SET line_end=line_end+1 WHERE entity_id=?', (identifier,))
            connection.commit()
            self.assertEqual([], core._requested_symbol_ranges(pinned, anchors)[0])
            self.assertIn('identity', core._requested_symbol_ranges(pinned, anchors)[1])
            connection.execute('UPDATE atlas_entities SET line_end=line_end-1 WHERE entity_id=?', (identifier,))
            connection.execute('DELETE FROM generation_entities WHERE generation=? AND entity_id=?',
                               (pinned.atlas_generation.generation, identifier))
            connection.commit()
            self.assertEqual([], core._requested_symbol_ranges(pinned, anchors)[0])
        finally:
            connection.close()

    def test_exhausted_operation_budget_reports_incomplete_method_instead_of_full_coverage(self):
        pinned = replace(self.publish(), max_backend_operations=1)
        with mock.patch.object(core, '_requested_symbol_ranges', wraps=core._requested_symbol_ranges) as ranges:
            bundle = self.retrieve(pinned)
        self.assertEqual(0, ranges.call_count)
        self.assertEqual(1, bundle.trace['physical_backend_operations'])
        self.assertNotIn('symbol_reads', bundle.trace)
        self.assertTrue(any('Full requested symbol ranges unavailable' in warning for warning in bundle.unresolved))
        self.assertNotEqual('coverage_satisfied', bundle.trace['stop_reason'])

    def test_unexecuted_symbol_lookup_is_not_reported_as_absent_source(self):
        (self.root / 'service/Settlement.java').write_text(
            'package billing;\nclass Settlement {\n int settle() { return 42; }\n}\n', encoding='utf-8')
        pinned = replace(self.publish(), max_backend_operations=1)
        request = self.request()
        request['INVESTIGATION_REQUEST']['anchors'].append({'kind': 'symbol', 'value': 'billing.Settlement.settle'})
        parsed = core.parse_context_request(json.dumps(request))
        parsed['_evaluation_ablation'] = ['anchors']  # No route batch can pre-resolve the second input.
        bundle = core.retrieve_context(pinned, parsed)
        unresolved = '\n'.join(bundle.unresolved)
        self.assertNotIn('Search `billing.Settlement.settle` returned no code matches', unresolved)
        self.assertIn('billing.Settlement.settle', unresolved)
        self.assertIn('lookup was not executed', unresolved)
        self.assertNotEqual('coverage_satisfied', bundle.trace['stop_reason'])

    def test_unavailable_symbol_component_does_not_claim_source_is_absent(self):
        pinned = self.publish()
        generation = pinned.atlas_generation
        pinned = replace(pinned, atlas_generation=replace(generation, components={
            **generation.components, 'runtime_anchors': {**generation.component('runtime_anchors'), 'status': 'unavailable'},
        }))
        bundle = self.retrieve(pinned)
        self.assertNotIn('returned no code matches', '\n'.join(bundle.unresolved))
        self.assertIn('source availability is unknown', '\n'.join(bundle.unresolved))
        self.assertNotEqual('coverage_satisfied', bundle.trace['stop_reason'])

    def test_unexecuted_literal_lookup_is_not_reported_as_absent_source(self):
        pinned = replace(self.publish(), max_backend_operations=1)
        request = self.request()
        request['INVESTIGATION_REQUEST']['anchors'] = [
            {'kind': 'log_literal', 'value': 'FIRST_SIGNAL'}, {'kind': 'log_literal', 'value': 'SECOND_SIGNAL'},
        ]
        bundle = self.retrieve(pinned, request)
        self.assertNotIn('Search `SECOND_SIGNAL` returned no code matches', '\n'.join(bundle.unresolved))
        self.assertIn('Search `SECOND_SIGNAL` did not complete', '\n'.join(bundle.unresolved))

    def test_unavailable_lexical_lookup_does_not_claim_source_is_absent(self):
        self.source.write_text('class Authorization { String message = "PAYMENT LOST"; }\n', encoding='utf-8')
        pinned = self.publish()
        request = self.request()
        request['INVESTIGATION_REQUEST']['anchors'] = [{'kind': 'log_literal', 'value': 'PAYMENT LOST'}]
        request['INVESTIGATION_REQUEST']['objective'] = 'Find the exact payment failure log'
        generation = pinned.atlas_generation
        pinned = replace(pinned, atlas_generation=replace(generation, components={
            **generation.components, 'zoekt': {'status': 'unavailable'},
        }))
        # The registered source still exists, but the lexical serving database
        # cannot validate this snapshot. This is not a verified empty search.
        connection = index._connect(pinned)
        try:
            connection.execute('DELETE FROM indexed_snapshots')
            connection.commit()
        finally:
            connection.close()
        bundle = self.retrieve(pinned, request)
        unresolved = '\n'.join(bundle.unresolved)
        self.assertNotIn('Search `PAYMENT LOST` returned no code matches', unresolved)
        self.assertIn('source availability is unknown', unresolved)
        self.assertNotEqual('coverage_satisfied', bundle.trace['stop_reason'])

    def test_explicit_literal_survives_noisy_routing_reranking_and_final_delivery(self):
        self.source.unlink()
        source = self.root / 'service/guard.py'
        literal = "This ticket's pinned Atlas generation is unavailable"
        source.write_text('# snapshot refresh generation recovery\n' * 300 +
                          f'def guard():\n    raise RuntimeError({literal!r})\n', encoding='utf-8')
        (self.root / 'service/noise.py').write_text('# snapshot refresh generation recovery\n' * 300, encoding='utf-8')
        pinned = replace(self.publish(), hydrate_limit=1, full_file_lines=1, source_window_lines=20)
        route = {'repos': ['service'], 'candidates': [
            {'repo': 'service', 'path': path, 'line': line, 'text': 'snapshot refresh generation recovery',
             'kind': 'function', 'score': 10_000 if path == 'guard.py' else 20_000,
             'found_by': ['Atlas hierarchical router']}
            for path, line in [('guard.py', 299), ('noise.py', 100)]
        ]}
        request = self.request()
        request['INVESTIGATION_REQUEST'].update(
            objective='Investigate snapshot refresh generation recovery',
            anchors=[{'kind': 'log_literal', 'value': literal}],
        )
        def adversarial_rerank(_settings, _query, hits, **kwargs):
            for hit in hits:
                hit.score = -100_000 if 'requested anchor match' in hit.kind else 100_000
            return hits

        for warm in (False, True):
            with self.subTest(warm=warm), mock.patch('brain.atlas.route', return_value=route), \
                    mock.patch('brain.editions.current_edition', return_value='precision'), \
                    mock.patch('brain.models.rerank_candidates', side_effect=adversarial_rerank) as reranked:
                bundle = self.retrieve(pinned, request)
            self.assertEqual(1, reranked.call_count)
            self.assertEqual(1, len(bundle.evidence))
            self.assertEqual('guard.py', bundle.evidence[0].path)
            self.assertIn(literal, bundle.evidence[0].content)
            self.assertIn('requested anchor match', bundle.evidence[0].kind)
        core.start_session(pinned, 'EXACT-ERROR', 'Investigate the refresh error')
        source.write_text(source.read_text(encoding='utf-8').replace(literal, 'NEW_GENERATION_MESSAGE'), encoding='utf-8')
        newer = self.publish()
        self.assertNotEqual(pinned.atlas_generation.identity, newer.atlas_generation.identity)
        with mock.patch('brain.atlas.route', return_value=route):
            content, _, _ = core.create_context(pinned, 'EXACT-ERROR', json.dumps(request))
        state = core.session_state(pinned, 'EXACT-ERROR')
        record = next(row for row in state['evidence_records'] if row['path'] == 'guard.py')
        self.assertTrue(record['emitted_in_context'])
        self.assertIn(f"### 1. {record['public_id']} —", content)
        self.assertIn(f'raise RuntimeError({literal!r})', content)
        self.assertEqual(pinned.atlas_generation.identity, state['atlas_generation_id'])
        newer_bundle = self.retrieve(newer, request)
        self.assertFalse(any('requested anchor match' in item.kind for item in newer_bundle.evidence))
        self.assertNotIn(literal, '\n'.join(item.content for item in newer_bundle.evidence))

    def test_explicit_local_variable_match_survives_same_file_navigation_noise(self):
        self.source.unlink()
        (self.root / 'service/refresh.py').write_text('# refresh snapshot generation\n' * 300 +
            'should_publish = source_changed or component_repaired\n', encoding='utf-8')
        pinned = replace(self.publish(), full_file_lines=1, source_window_lines=20, max_regions_per_file=2)
        route = {'repos': ['service'], 'candidates': [
            {'repo': 'service', 'path': 'refresh.py', 'line': line, 'text': 'refresh snapshot generation',
             'kind': 'function', 'score': 10_000, 'found_by': ['Atlas hierarchical router']}
            for line in (50, 150)
        ]}
        request = self.request('should_publish')
        with mock.patch('brain.atlas.route', return_value=route):
            bundle = self.retrieve(pinned, request)
        self.assertTrue(any('should_publish = source_changed or component_repaired' in item.content
                            and 'requested anchor match' in item.kind for item in bundle.evidence))
        self.assertFalse(any('requested symbol definition' in item.kind for item in bundle.evidence))

    def test_explicit_error_delivers_enclosing_python_and_java_bodies_in_one_batch(self):
        self.source.write_text('class Authorization {\n boolean validate(int value) {\n' +
            ''.join(f'  value += {number};\n' for number in range(120)) +
            '  if (value < 0) throw new IllegalStateException("LEASE PIN MISSING");\n' +
            ''.join(f'  value -= {number};\n' for number in range(120)) +
            '  return value == 0; // JAVA_DECISIVE_TAIL\n }\n}\n', encoding='utf-8')
        (self.root / 'service/guard.py').write_text('def validate(value):\n' +
            ''.join(f'    value += {number}\n' for number in range(120)) +
            '    if value < 0: raise RuntimeError("LEASE PIN MISSING")\n' +
            ''.join(f'    value -= {number}\n' for number in range(120)) +
            '    return value == 0  # PYTHON_DECISIVE_TAIL\n', encoding='utf-8')
        pinned = replace(self.publish(), full_file_lines=1)
        request = self.request()
        request['INVESTIGATION_REQUEST'].update(objective='Explain the exact failure',
            anchors=[{'kind': 'log_literal', 'value': 'LEASE PIN MISSING'}])
        with mock.patch.object(index, 'read_generation_files', wraps=index.read_generation_files) as sources, \
                mock.patch.object(core, '_requested_symbol_ranges', wraps=core._requested_symbol_ranges) as ranges:
            bundle = self.retrieve(pinned, request)
        emitted = set()
        content = core.pack_context(pinned, 'ERROR-BODIES', 1, bundle, emitted_ids=emitted)
        for path, tail in [('Authorization.java', 'JAVA_DECISIVE_TAIL'), ('guard.py', 'PYTHON_DECISIVE_TAIL')]:
            evidence = next(item for item in bundle.evidence if item.path == path and tail in item.content)
            self.assertIn(core._evidence_id(evidence), emitted)
            self.assertIn(tail, content)
        self.assertEqual(1, sources.call_count)
        self.assertEqual(1, ranges.call_count)
        self.assertEqual(2, len(bundle.trace['symbol_reads']))
        self.assertTrue(all(row['status'] in {'complete_range', 'complete_file'} for row in bundle.trace['symbol_reads']))

    def test_exact_error_requested_relations_deliver_bodies_without_a_symbol_followup(self):
        self.source.unlink()
        path = self.root / 'service/recovery.py'
        source = ('def recover(error):\n' + '    value = 1\n' * 100 +
            '    message = "LEASE PIN MISSING"\n    return validate(error), message\n' + '# spacer\n' * 200 +
            'def retry_job():\n    result = recover("checkpoint")\n' + '    value = 1\n' * 180 +
            '    return "OLD_RETRY", result\n' + '# spacer\n' * 200 +
            'def validate(error):\n' + '    value = 1\n' * 180 + '    return "OLD_VALIDATOR", error\n')
        path.write_text(source, encoding='utf-8')
        for number in range(10):
            (self.root / f'service/noise{number}.py').write_text(
                '# Investigation failed early checkpoint retry safely without resetting\n' * 100, encoding='utf-8')
        old = replace(self.publish(), full_file_lines=1, source_window_lines=20)
        query = self.request()
        query['INVESTIGATION_REQUEST'].update(
            objective='Investigation failed after an early checkpoint. Can I retry safely without resetting?',
            anchors=[{'kind': 'log_literal', 'value': 'LEASE PIN MISSING'}], required=['callers', 'callees'])
        bundle = self.retrieve(old, query)
        packed = core.pack_context(old, 'ERROR-RELATIONS', 1, bundle)
        self.assertIn('OLD_RETRY', packed)
        self.assertIn('OLD_VALIDATOR', packed)
        self.assertIn('LEASE PIN MISSING', packed)
        self.assertLessEqual(bundle.trace['physical_backend_operations'], 20)
        self.assertLess(len(packed.encode('utf-8')), 100_000)
        self.assertFalse(any('not established' in value for value in bundle.unresolved), bundle.unresolved)

        core.start_session(old, 'ERROR-RELATIONS', 'Investigate the failure')
        first, _, _ = core.create_context(old, 'ERROR-RELATIONS', json.dumps(query))
        state = core.session_state(old, 'ERROR-RELATIONS')
        self.assertIn('OLD_RETRY', first)
        self.assertTrue(any(step['state'] == 'verified' and step['target'] == 'validate'
                            for step in state['investigation_runtime']['execution_flow']['steps']),
                        state['investigation_runtime']['execution_flow'])
        path.write_text(source.replace('OLD_RETRY', 'NEW_RETRY').replace('OLD_VALIDATOR', 'NEW_VALIDATOR'), encoding='utf-8')
        newer = replace(self.publish(), full_file_lines=1, source_window_lines=20)
        query['INVESTIGATION_REQUEST']['objective'] += ' Inspect its validation.'
        second, _, _ = core.create_context(newer, 'ERROR-RELATIONS', json.dumps(query))
        self.assertNotIn('NEW_RETRY', second)
        self.assertNotIn('NEW_VALIDATOR', second)
        self.assertEqual(old.atlas_generation.identity, core.session_state(newer, 'ERROR-RELATIONS')['atlas_generation_id'])
        current = self.retrieve(newer, query)
        self.assertIn('NEW_RETRY', '\n'.join(item.content for item in current.evidence))
        self.assertIn('NEW_VALIDATOR', '\n'.join(item.content for item in current.evidence))

    def test_literal_relationships_degrade_without_unique_complete_pinned_ownership(self):
        self.source.unlink()
        path = self.root / 'service/recovery.py'
        source = ('def recover():\n    message = "LEASE PIN MISSING"\n    return validate(), message\n' +
                  '# spacer\n' * 200 + 'def validate():\n    return "VALIDATOR_BODY"\n')
        query = self.request()
        query['INVESTIGATION_REQUEST'].update(objective='Explain the failure',
            anchors=[{'kind': 'log_literal', 'value': 'LEASE PIN MISSING'}], required=['callees'])
        for case in ('ambiguous', 'module', 'unregistered', 'clipped', 'operations'):
            with self.subTest(case=case):
                path.write_text(source + ('\ndef other():\n    return "LEASE PIN MISSING"\n' if case == 'ambiguous' else '')
                                if case != 'module' else 'message = "LEASE PIN MISSING"\n', encoding='utf-8')
                pinned = replace(self.publish(), full_file_lines=1, source_window_lines=20)
                if case == 'unregistered':
                    connection = connect(pinned)
                    try:
                        connection.execute('DELETE FROM generation_intelligence_files WHERE generation=?',
                                           (pinned.atlas_generation.generation,))
                        connection.commit()
                    finally:
                        connection.close()
                elif case == 'clipped':
                    pinned = replace(pinned, max_results=1)
                elif case == 'operations':
                    pinned = replace(pinned, max_effective_operations=1)
                bundle = self.retrieve(pinned, query)
                self.assertTrue(any('Literal relationships' in item or 'literal relationships' in item for item in bundle.unresolved),
                                (case, bundle.unresolved))
                self.assertFalse(bundle._resolved_relation_seeds)
                self.assertNotEqual('coverage_satisfied', bundle.trace['stop_reason'])
                self.assertFalse(any('requested symbol relationship source' in item.kind for item in bundle.evidence))
                self.assertIn('LEASE PIN MISSING', '\n'.join(item.content for item in bundle.evidence))

    def test_literal_relation_work_never_consumes_the_pinned_source_reserve(self):
        self.source.unlink()
        (self.root / 'service/recovery.py').write_text('def recover():\n    return "LEASE PIN MISSING"\n', encoding='utf-8')
        pinned = self.publish()
        query = self.request()
        query['INVESTIGATION_REQUEST'].update(objective='LEASE PIN MISSING',
            anchors=[{'kind': 'log_literal', 'value': 'LEASE PIN MISSING'}], required=['callers'])
        for limit in (2, 3, 4, 6):
            with self.subTest(limit=limit):
                bundle = self.retrieve(replace(pinned, max_backend_operations=limit), query)
                self.assertLessEqual(bundle.trace['physical_backend_operations'], limit)
                self.assertIn('LEASE PIN MISSING', '\n'.join(item.content for item in bundle.evidence))
                self.assertTrue(bundle.unresolved, 'A deferred or empty graph is not complete caller coverage.')

    def test_literal_source_survives_full_and_delta_byte_bounding_before_derived_bodies(self):
        pinned = replace(self.publish(), hard_context_chars=10_000)
        literal = core.Evidence('service', 'error.py', 1, 1, 'message = "LEASE PIN MISSING"',
                                'requested anchor match', 100, ['sqlite trigram index'])
        derived = [core.Evidence('service', f'callee{number}.py', 1, 1, '# ' + 'x' * 3500,
                                'definition, requested symbol relationship source', 100,
                                ['generation-validated qualified symbol']) for number in range(3)]
        bundle = core.ContextBundle('Inspect the error', evidence=[*derived, literal], atlas_generation=pinned.atlas_generation)
        bundle.trace['symbol_reads'] = [{'repo': item.repo, 'path': item.path, 'returned_lines': '1-1'}
                                        for item in bundle.evidence]
        ids = {core._evidence_id(item) for item in bundle.evidence}
        for mode in ('full', 'delta', 'known_delta'):
            with self.subTest(mode=mode):
                emitted = set()
                if mode == 'full':
                    content = core.pack_context(pinned, 'EXACT-DELIVERY', 1, bundle, emitted_ids=emitted)
                else:
                    content = core.pack_delta_context(pinned, 'EXACT-DELIVERY', 2, bundle,
                        {'context_id': 'CTX-002', 'base_context_id': 'CTX-001'},
                        ids if mode == 'delta' else set(), emitted_ids=emitted)
                self.assertLessEqual(len(content.encode('utf-8')), pinned.hard_context_chars)
                self.assertIn('LEASE PIN MISSING', content)
                self.assertIn(core._evidence_id(literal), emitted)
                self.assertLess(len(emitted), len(ids))

    def test_symbol_and_literal_on_same_owner_share_one_typed_relation_operation(self):
        self.source.write_text('package billing;\nclass Authorization {\n String recover() {\n'
            '  String message = "LEASE PIN MISSING";\n  return validate() + message;\n }\n'
            ' String validate() { return "VALIDATED"; }\n}\n', encoding='utf-8')
        pinned = self.publish()
        query = self.request('billing.Authorization.recover')
        query['INVESTIGATION_REQUEST'].update(required=['callees'], anchors=[
            {'kind': 'symbol', 'value': 'billing.Authorization.recover'},
            {'kind': 'log_literal', 'value': 'LEASE PIN MISSING'}])
        with mock.patch.object(investigation, '_execution_flow', wraps=investigation._execution_flow) as flow:
            bundle = self.retrieve(pinned, query)
        self.assertEqual(1, flow.call_count)
        self.assertEqual(bundle._resolved_relation_seeds['billing.Authorization.recover'],
                         bundle._resolved_relation_seeds['LEASE PIN MISSING'])
        self.assertIn('VALIDATED', '\n'.join(item.content for item in bundle.evidence))

    def test_delivered_callable_anchor_drives_pinned_caller_followup(self):
        self.source.unlink()
        path = self.root / 'service/recovery.py'
        path.write_text('def recover(error):\n' + '    value = 1\n' * 100 +
            '    message = "LEASE PIN MISSING"\n    return message, error\n' + '# spacer\n' * 200 +
            'def retry_job():\n    result = recover("checkpoint")\n' + '    value = 1\n' * 180 +
            '    return "OLD_RETRY", result\n', encoding='utf-8')
        old = replace(self.publish(), full_file_lines=1)
        query = self.request()
        query['INVESTIGATION_REQUEST'].update(objective='Inspect the failure',
            anchors=[{'kind': 'log_literal', 'value': 'LEASE PIN MISSING'}])
        bundle = self.retrieve(old, query)
        prefix = 'pinned symbol anchor (navigation only): '
        hints = [json.loads(channel.removeprefix(prefix)) for item in bundle.evidence
                 for channel in item.found_by if channel.startswith(prefix)]
        self.assertEqual(1, len(hints))
        anchor = hints[0]
        self.assertEqual('symbol', anchor['kind'])
        self.assertRegex(anchor['value'], r'^sha256:[0-9a-f]{64}$')
        resolved = resolve_runtime_anchors(old, old.atlas_generation, [anchor], use_cache=False)
        self.assertEqual(['recover'], [item['value'].rsplit(':', 1)[-1] for item in resolved['candidates']])
        self.assertEqual(['entity_name'], [item['method'] for item in resolved['candidates']])
        emitted = set()
        packed = core.pack_context(old, 'NAVIGATION', 1, bundle, emitted_ids=emitted)
        self.assertIn(prefix, packed)
        for item in bundle.evidence:
            self.assertEqual(core._evidence_id(item), core._evidence_id(replace(item, found_by=[])))
        core.start_session(old, 'NAVIGATION', 'Investigate the checkpoint failure')
        first, _, _ = core.create_context(old, 'NAVIGATION', json.dumps(query))
        self.assertIn(anchor['value'], first)
        path.write_text(path.read_text(encoding='utf-8').replace('LEASE PIN MISSING', 'NEW_FAILURE').replace('OLD_RETRY', 'NEW_RETRY'), encoding='utf-8')
        newer = self.publish()
        followup = self.request()
        followup['INVESTIGATION_REQUEST'].update(objective='Trace the observed recovery caller',
            anchors=[anchor], required=['callers'], base_context_id='CTX-001')
        second, _, _ = core.create_context(newer, 'NAVIGATION', json.dumps(followup))
        self.assertIn('OLD_RETRY', second)
        self.assertIn('recover("checkpoint")', second)
        self.assertNotIn('NEW_RETRY', second)
        state = core.session_state(newer, 'NAVIGATION')
        self.assertEqual(old.atlas_generation.identity, state['atlas_generation_id'])
        self.assertEqual(2, state['investigation_runtime']['wave'])
        self.assertFalse(resolve_runtime_anchors(newer, newer.atlas_generation, [anchor], use_cache=False)['candidates'])

    def test_entity_id_anchor_selects_one_overload_without_name_guessing(self):
        self.source.write_text('package billing;\nclass Authorization {\n'
            ' boolean authorize(int value) { return integerDecision(); }\n'
            ' boolean authorize(String value) { return stringDecision(); }\n'
            ' boolean integerDecision() { return true; }\n'
            ' boolean stringDecision() { return false; }\n}\n', encoding='utf-8')
        pinned = replace(self.publish(), full_file_lines=1)
        connection = connect(pinned)
        try:
            rows = connection.execute('SELECT e.entity_id,e.line_start FROM atlas_entities e '
                'JOIN generation_entities g ON g.entity_id=e.entity_id '
                'WHERE g.generation=? AND e.simple_name=? ORDER BY e.line_start',
                (pinned.atlas_generation.generation, 'authorize')).fetchall()
        finally:
            connection.close()
        self.assertEqual(2, len(rows))
        for identifier, line in rows:
            with self.subTest(line=line):
                anchor = {'kind': 'symbol', 'value': identifier}
                result = resolve_runtime_anchors(pinned, pinned.atlas_generation, [anchor], use_cache=False)
                self.assertEqual([identifier], [item['entity_id'] for item in result['candidates']])
                self.assertEqual(['entity_name'], [item['method'] for item in result['candidates']])
                self.assertEqual([identifier], investigation._qualified_symbol_queries([anchor]))
                request = self.request(identifier)
                request['INVESTIGATION_REQUEST']['required'] = ['callees']
                bundle = self.retrieve(pinned, request)
                expected = 'integerDecision' if line == 3 else 'stringDecision'
                self.assertTrue(any(expected in relation for relation in bundle.relationships), bundle.unresolved)
                self.assertTrue(any(identifier == key for key in bundle._resolved_relation_seeds.values()))

    def test_entity_id_anchor_cache_revalidates_owner_membership_and_registered_source(self):
        pinned = self.publish()
        connection = connect(pinned)
        try:
            identifier, parent = connection.execute('SELECT e.entity_id,e.parent_entity_id FROM atlas_entities e '
                'JOIN generation_entities g ON g.entity_id=e.entity_id WHERE g.generation=? AND e.simple_name=?',
                (pinned.atlas_generation.generation, 'authorize')).fetchone()
            anchor = {'kind': 'symbol', 'value': identifier}
            self.assertEqual([identifier], [row['entity_id'] for row in
                resolve_runtime_anchors(pinned, pinned.atlas_generation, [anchor])['candidates']])
            self.assertTrue(resolve_runtime_anchors(pinned, pinned.atlas_generation, [anchor])['cache_hit'])
            missing = {'kind': 'symbol', 'value': 'sha256:' + '0' * 64}
            absent = resolve_runtime_anchors(pinned, pinned.atlas_generation, [missing])
            self.assertEqual('ready', absent['status'])
            self.assertFalse(absent['candidates'])
            for table, field, where, args in [
                ('atlas_entities', 'fingerprint', 'entity_id=?', (identifier,)),
                ('atlas_entities', 'fingerprint', 'entity_id=?', (parent,)),
                ('generation_intelligence_files', 'blob_sha', 'generation=? AND repo=? AND path=?',
                 (pinned.atlas_generation.generation, 'service', 'Authorization.java')),
                ('generation_entities', 'snapshot_sha', 'generation=? AND entity_id=?',
                 (pinned.atlas_generation.generation, identifier)),
                ('atlas_runtime_anchors', 'fingerprint', "entity_id=? AND kind='symbol' AND method='atlas_entity_exact'", (identifier,)),
            ]:
                with self.subTest(table=table, field=field, args=args):
                    saved = connection.execute(f'SELECT {field} FROM {table} WHERE {where}', args).fetchall()
                    self.assertEqual(1, len(saved))
                    original = saved[0][0]
                    connection.execute(f'UPDATE {table} SET {field}=? WHERE {where}', ('corrupt', *args))
                    connection.commit()
                    for use_cache in (True, False):
                        result = resolve_runtime_anchors(pinned, pinned.atlas_generation, [anchor], use_cache=use_cache)
                        self.assertFalse(result['candidates'], result)
                    connection.execute(f'UPDATE {table} SET {field}=? WHERE {where}', (original, *args))
                    connection.commit()
                    restored = resolve_runtime_anchors(pinned, pinned.atlas_generation, [anchor])
                    if table == 'atlas_runtime_anchors':
                        # DML correctly invalidates this immutable generation's
                        # term publication marker; restoring a row is not repair.
                        self.assertEqual('degraded', restored['status'])
                        self.assertIn('term projection', restored['reason'])
                    else:
                        self.assertEqual([identifier], [row['entity_id'] for row in restored['candidates']], restored)
        finally:
            connection.close()
        unavailable = replace(pinned.atlas_generation, components={**pinned.atlas_generation.components,
            'runtime_anchors': {'status': 'unavailable'}})
        self.assertEqual('degraded', resolve_runtime_anchors(pinned, unavailable, [anchor])['status'])
        self.assertFalse(investigation._qualified_symbol_queries([{'kind': 'log_literal', 'value': identifier}]))

    def test_callable_navigation_is_omitted_with_its_source_in_full_and_delta_contexts(self):
        self.source.unlink()
        (self.root / 'service/guard.py').write_text('def check():\n' +
            '    value = 1\n' * 100 + '    fail("LEASE PIN MISSING")\n' +
            '    value += 1\n' * 100 + '    return value\n', encoding='utf-8')
        pinned = replace(self.publish(), full_file_lines=1, hydrate_limit=1)
        query = self.request()
        query['INVESTIGATION_REQUEST'].update(objective='Inspect the failure',
            anchors=[{'kind': 'log_literal', 'value': 'LEASE PIN MISSING'}])
        bundle = self.retrieve(pinned, query)
        self.assertEqual(1, len(bundle.evidence))
        evidence = bundle.evidence[0]
        prefix = 'pinned symbol anchor (navigation only): '
        self.assertTrue(any(channel.startswith(prefix) for channel in evidence.found_by))
        for delta in (False, True):
            for budget in (1_000, 20_000):
                with self.subTest(delta=delta, budget=budget):
                    bounded = replace(pinned, hard_context_chars=budget)
                    emitted = set()
                    progress = {'context_id': 'CTX-002', 'base_context_id': 'CTX-001'}
                    content = core.pack_delta_context(bounded, 'NAV', 2, bundle, progress,
                        {core._evidence_id(evidence)}, emitted_ids=emitted) if delta else core.pack_context(
                            bounded, 'NAV', 1, bundle, emitted_ids=emitted)
                    self.assertEqual(core._evidence_id(evidence) in emitted, prefix in content)
                    self.assertEqual(budget == 20_000, prefix in content)
                    self.assertLessEqual(len(content.encode('utf-8')), budget)

    def test_explicit_and_known_literal_pages_keep_their_callable_navigation(self):
        pinned = replace(self.publish(), full_file_lines=1)
        bundle = self.retrieve(pinned)
        marker = 'pinned symbol anchor (navigation only): '
        self.assertTrue(any(channel.startswith(marker) for item in bundle.evidence for channel in item.found_by))
        self.source.unlink()
        (self.root / 'service/guard.py').write_text('def check():\n    fail("LEASE PIN MISSING")\n' +
            '    value = 1\n' * 100 + '    return "DECISIVE_TAIL"\n', encoding='utf-8')
        pinned = replace(self.publish(), full_file_lines=1)
        query = self.request()
        query['INVESTIGATION_REQUEST'].update(objective='Inspect the failure',
            anchors=[{'kind': 'log_literal', 'value': 'LEASE PIN MISSING'}])
        bundle = self.retrieve(pinned, query)
        emitted = set()
        rendered = core.pack_delta_context(pinned, 'NAV', 2, bundle,
            {'context_id': 'CTX-002', 'base_context_id': 'CTX-001'}, set(), emitted_ids=emitted)
        self.assertIn(marker, rendered)
        self.assertIn('DECISIVE_TAIL', rendered)
        self.assertTrue(emitted)

    def test_requested_callee_keeps_local_validation_ahead_of_receiver_noise(self):
        self.source.unlink()
        (self.root / 'service/helpers.py').write_text('class Cache:\n    def get(self, key):\n        return key\n', encoding='utf-8')
        main = self.root / 'service/recovery.py'
        main.write_text('def retry_checkpoint(state):\n' +
            ''.join(f'    state.get("noise-{number}")\n' for number in range(24)) +
            '    result = validate_checkpoint(state)\n    return result\n' + '# spacer\n' * 200 +
            'def validate_checkpoint(state):\n    return "OLD_VALIDATION", state\n', encoding='utf-8')
        pinned = replace(self.publish(), full_file_lines=1)
        request = self.request('recovery.retry_checkpoint')
        request['INVESTIGATION_REQUEST']['required'] = ['callees']
        bundle = self.retrieve(pinned, request)
        self.assertTrue(any('validate_checkpoint' in row for row in bundle.relationships), bundle.relationships)
        rendered = core.pack_context(pinned, 'CALLEE', 1, bundle)
        self.assertIn('OLD_VALIDATION', rendered)
        self.assertTrue(any('bounded partial graph' in row for row in bundle.unresolved))
        self.assertLessEqual(bundle.trace['physical_backend_operations'], pinned.max_backend_operations)
        seed = bundle._resolved_relation_seeds['recovery.retry_checkpoint']
        flow = investigation._execution_flow(pinned, pinned.atlas_generation, [seed], bundle, outgoing_types=('CALLS',))
        noise = [row for row in flow['steps'] if row['target'] == 'get']
        # Legacy unresolved/reconciled rows are one lexical candidate, not two
        # branches. Neither may become dispatch proof or multiply by callsites.
        self.assertEqual(1, len(noise))
        self.assertTrue(all(row['collapsed_callsite_count'] == 23 for row in noise))
        self.assertTrue(all(row['state'] == 'candidate' and row['target_symbol'] is None for row in noise))
        self.assertFalse(any(row['path'] == 'helpers.py' for row in flow['steps']))
        core.start_session(pinned, 'CALLEE', 'Verify the actual checkpoint validator')
        first, _, _ = core.create_context(pinned, 'CALLEE', json.dumps(request))
        self.assertIn('OLD_VALIDATION', first)
        main.write_text(main.read_text(encoding='utf-8').replace('OLD_VALIDATION', 'NEW_VALIDATION'), encoding='utf-8')
        newer = self.publish()
        request['INVESTIGATION_REQUEST']['base_context_id'] = 'CTX-001'
        second, _, _ = core.create_context(newer, 'CALLEE', json.dumps(request))
        self.assertIn('OLD_VALIDATION', second)
        self.assertNotIn('NEW_VALIDATION', second)
        self.assertEqual(pinned.atlas_generation.identity, core.session_state(newer, 'CALLEE')['atlas_generation_id'])

    def test_callee_range_revalidates_file_and_owner_without_a_per_target_resolver(self):
        self.source.write_text('class Authorization {\n boolean authorize() { return decision(); }\n' +
            ' int field;\n' * 180 + ' boolean decision() {\n return true;\n }\n}\n', encoding='utf-8')
        pinned = replace(self.publish(), full_file_lines=1)
        result = resolve_runtime_anchors(pinned, pinned.atlas_generation,
            [{'kind': 'symbol', 'value': 'Authorization.authorize'}], use_cache=False)
        flow = investigation._execution_flow(pinned, pinned.atlas_generation,
            [result['candidates'][0]['entity_id']], core.ContextBundle('callee', atlas_generation=pinned.atlas_generation),
            outgoing_types=('CALLS',))
        target = next(row['target_symbol'] for row in flow['steps'] if row['target'] == 'decision')
        connection = connect(pinned)
        try:
            parent = connection.execute('SELECT parent_entity_id FROM atlas_entities WHERE entity_id=?',
                (target['entity_id'],)).fetchone()[0]
            for table, field, where, args in [
                ('generation_intelligence_files', 'blob_sha', 'generation=? AND repo=? AND path=?',
                 (pinned.atlas_generation.generation, 'service', 'Authorization.java')),
                ('atlas_entities', 'fingerprint', 'entity_id=?', (parent,)),
            ]:
                with self.subTest(table=table):
                    saved = connection.execute(f'SELECT {field} FROM {table} WHERE {where}', args).fetchone()[0]
                    with mock.patch.object(investigation, 'resolve_runtime_anchors', side_effect=AssertionError('per-target resolver')):
                        ranges, reason = core._requested_symbol_ranges(pinned, {target['entity_id']: target})
                    self.assertEqual([target['entity_id']], [row['entity_id'] for row in ranges])
                    self.assertIsNone(reason)
                    connection.execute(f'UPDATE {table} SET {field}=? WHERE {where}', ('corrupt', *args))
                    connection.commit()
                    ranges, reason = core._requested_symbol_ranges(pinned, {target['entity_id']: target})
                    self.assertFalse(ranges)
                    self.assertIn('identity unavailable', reason)
                    connection.execute(f'UPDATE {table} SET {field}=? WHERE {where}', (saved, *args))
                    connection.commit()
        finally:
            connection.close()

    def test_invalid_derived_callee_does_not_discard_a_valid_root_body(self):
        self.source.unlink()
        (self.root / 'service/root.py').write_text('from target import decision\ndef entry():\n    decision()\n' +
            '    value = 1\n' * 200 + '    return "ROOT_DECISIVE_TAIL"\n', encoding='utf-8')
        (self.root / 'service/target.py').write_text('def decision():\n    return "TARGET"\n', encoding='utf-8')
        pinned = replace(self.publish(), full_file_lines=1)
        connection = connect(pinned)
        try:
            connection.execute('UPDATE generation_intelligence_files SET blob_sha=? WHERE generation=? AND path=?',
                ('corrupt', pinned.atlas_generation.generation, 'target.py'))
            connection.commit()
        finally:
            connection.close()
        query = self.request('root.entry')
        query['INVESTIGATION_REQUEST']['required'] = ['callees']
        bundle = self.retrieve(pinned, query)
        self.assertIn('ROOT_DECISIVE_TAIL', core.pack_context(pinned, 'PARTIAL', 1, bundle))
        self.assertTrue(any('unavailable' in row for row in bundle.unresolved), bundle.unresolved)
        self.assertFalse(any('pinned symbol anchor' in channel for item in bundle.evidence
            if item.path == 'target.py' for channel in item.found_by))

    def test_enclosing_lookup_preserves_nested_ownership_and_rejects_line_ambiguity(self):
        self.source.write_text('class Authorization {\n'
            ' void a() { fail("TARGET"); } void b() { fail("TARGET"); }\n'
            ' Authorization() {\n  fail("CTOR");\n }\n String field = "FIELD";\n}\n', encoding='utf-8')
        (self.root / 'service/nested.py').write_text(
            'def outer():\n    def inner():\n        fail("TARGET")\n        return 42\n    return inner()\n'
            'class Config:\n    FIELD = "FIELD"\n', encoding='utf-8')
        pinned = self.publish()
        for path, line, names in [('nested.py', 3, ['inner']), ('nested.py', 7, []),
                                  ('Authorization.java', 2, []), ('Authorization.java', 4, ['Authorization']),
                                  ('Authorization.java', 6, [])]:
            with self.subTest(path=path, line=line):
                ranges, reason = core._requested_symbol_ranges(pinned, {}, enclosing=[('service', path, line)])
                self.assertEqual(names, [row['simple_name'] for row in ranges])
                if path == 'Authorization.java' and line == 2:
                    self.assertIn('ambiguous', reason)
                if names:
                    self.assertEqual([line], ranges[0]['enclosing_match_lines'])

    def test_java_callable_boundary_lines_do_not_claim_ownership_of_neighboring_fields(self):
        for source, line in [
            ('class Authorization {\n void check() {\n  int value = 1;\n } String field = "TARGET";\n}\n', 4),
            ('class Authorization {\n String field = "TARGET"; void check() {\n  int value = 1;\n }\n}\n', 2),
            ('class Authorization { void check() {} String field = "TARGET"; }\n', 1),
        ]:
            with self.subTest(source=source):
                self.source.write_text(source, encoding='utf-8')
                pinned = replace(self.publish(), full_file_lines=1)
                ranges, reason = core._requested_symbol_ranges(pinned, {}, enclosing=[('service', 'Authorization.java', line)])
                self.assertFalse(ranges)
                self.assertIn('ambiguous', reason)
                query = self.request()
                query['INVESTIGATION_REQUEST'].update(objective='Inspect TARGET',
                    anchors=[{'kind': 'log_literal', 'value': 'TARGET'}])
                bundle = self.retrieve(pinned, query)
                self.assertIn('"TARGET"', '\n'.join(item.content for item in bundle.evidence))
                self.assertFalse(any('enclosing callable source range' in channel for item in bundle.evidence for channel in item.found_by))

    def test_enclosing_expansion_preserves_the_original_match_window(self):
        self.source.unlink()
        rows = ['# preceding source'] * 100 + ['def check(value):'] + ['    value += 1'] * 100
        rows[190] = '    fail("LEASE PIN MISSING")'
        rows += ['    return value', '', 'def next_operation():'] + ['    value = 1'] * 100
        rows[240] = '    follow_up("AFTER_MATCH_WINDOW")'
        (self.root / 'service/guard.py').write_text('\n'.join(rows) + '\n', encoding='utf-8')
        pinned = replace(self.publish(), full_file_lines=1, hydrate_limit=1)
        query = self.request()
        query['INVESTIGATION_REQUEST'].update(objective='Inspect the failure',
            anchors=[{'kind': 'log_literal', 'value': 'LEASE PIN MISSING'}])
        bundle = self.retrieve(pinned, query)
        source = '\n'.join(item.content for item in bundle.evidence)
        self.assertIn('def check(value):', source)
        self.assertIn('AFTER_MATCH_WINDOW', source)

    def test_enclosing_scope_is_generation_pinned_and_corrupt_parent_falls_back_to_source(self):
        self.source.write_text('class Authorization {\n boolean check() {\n' +
            '  int value = 0;\n' * 100 + '  fail("OLD_LITERAL");\n' +
            '  value++;\n' * 100 + '  return OLD_DECISION;\n }\n}\n', encoding='utf-8')
        old = replace(self.publish(), full_file_lines=1)
        query = self.request()
        query['INVESTIGATION_REQUEST'].update(objective='Inspect OLD_LITERAL',
            anchors=[{'kind': 'log_literal', 'value': 'OLD_LITERAL'}])
        core.start_session(old, 'OLD-SCOPE', 'Inspect the original failure')
        self.source.write_text('class Authorization { void check() { fail("NEW_LITERAL"); } }\n', encoding='utf-8')
        newer = self.publish()
        content, _, _ = core.create_context(old, 'OLD-SCOPE', json.dumps(query))
        self.assertIn('OLD_DECISION', content)
        self.assertNotIn('NEW_LITERAL', content)
        self.assertEqual(old.atlas_generation.identity, core.session_state(old, 'OLD-SCOPE')['atlas_generation_id'])
        self.assertFalse(core._requested_symbol_ranges(newer, {}, enclosing=[('service', 'Authorization.java', 103)])[0])
        connection = connect(old)
        try:
            entity = connection.execute('SELECT e.entity_id,e.parent_entity_id FROM atlas_entities e '
                'JOIN generation_entities g ON g.entity_id=e.entity_id WHERE g.generation=? AND e.simple_name=?',
                (old.atlas_generation.generation, 'check')).fetchone()
            connection.execute('INSERT INTO generation_entities VALUES (?,?,?)',
                (newer.atlas_generation.generation, entity[0], newer.atlas_generation.snapshots['service']))
            connection.commit()
            # A canonical G1 entity falsely attached to G2 is not G2 source.
            self.assertFalse(core._requested_symbol_ranges(newer, {}, enclosing=[('service', 'Authorization.java', 103)])[0])
            connection.execute('UPDATE atlas_entities SET fingerprint=? WHERE entity_id=?', ('corrupt', entity[1]))
            connection.commit()
            degraded = self.retrieve(old, query)
            self.assertTrue(any('OLD_LITERAL' in item.content for item in degraded.evidence))
            self.assertNotIn('OLD_DECISION', '\n'.join(item.content for item in degraded.evidence))
            self.assertTrue(any('Enclosing source scope unavailable' in warning for warning in degraded.warnings))
            connection.execute('UPDATE atlas_entities SET line_end=line_end+1 WHERE entity_id=?', (entity[0],))
            connection.commit()
            ranges, reason = core._requested_symbol_ranges(old, {}, enclosing=[('service', 'Authorization.java', 103)])
            self.assertFalse(ranges)
            self.assertIn('identity', reason)
        finally:
            connection.close()

    def test_oversized_enclosing_body_keeps_each_exact_match_window(self):
        self.source.unlink()
        rows = ['def inspect(value):'] + ['    # ' + '数据' * 80 for _ in range(1_000)]
        rows[300] = '    fail("LEASE PIN MISSING FIRST")'
        rows[700] = '    fail("LEASE PIN MISSING SECOND")'
        rows.append('    return FINAL_TAIL')
        (self.root / 'service/large.py').write_text('\n'.join(rows) + '\n', encoding='utf-8')
        pinned = replace(self.publish(), full_file_lines=1, source_window_lines=20, hydrate_limit=2)
        query = self.request()
        query['INVESTIGATION_REQUEST'].update(objective='Inspect the failures',
            anchors=[{'kind': 'log_literal', 'value': 'LEASE PIN MISSING'}])
        bundle = self.retrieve(pinned, query)
        content = core.pack_context(pinned, 'LARGE-SCOPE', 1, bundle)
        self.assertIn('LEASE PIN MISSING FIRST', content)
        self.assertIn('LEASE PIN MISSING SECOND', content)
        self.assertNotIn('FINAL_TAIL', content)
        self.assertEqual(2, len(bundle.trace['symbol_reads']))
        self.assertTrue(all(row['status'] == 'window_only' and row['returned_lines']
                            and row['next_lines'] == '1-1002' for row in bundle.trace['symbol_reads']))
        self.assertTrue(any('kept its exact-match window' in warning for warning in bundle.warnings))

    def test_missing_scope_budget_keeps_literal_source_and_small_files_skip_lookup(self):
        self.source.unlink()
        (self.root / 'service/guard.py').write_text('def check():\n' + '    value = 1\n' * 100 +
            '    fail("LEASE PIN MISSING")\n' + '    value += 1\n' * 100 +
            '    return COMPLETE_TAIL\n', encoding='utf-8')
        pinned = self.publish()
        query = self.request()
        query['INVESTIGATION_REQUEST'].update(objective='Inspect the error',
            anchors=[{'kind': 'log_literal', 'value': 'LEASE PIN MISSING'}])
        with mock.patch.object(core, '_requested_symbol_ranges', wraps=core._requested_symbol_ranges) as ranges:
            complete = self.retrieve(pinned, query)
        self.assertEqual(0, ranges.call_count)
        self.assertIn('COMPLETE_TAIL', '\n'.join(item.content for item in complete.evidence))
        with mock.patch.object(core, '_requested_symbol_ranges', return_value=([], 'lookup budget exhausted')):
            degraded = self.retrieve(replace(pinned, full_file_lines=1), query)
        self.assertIn('LEASE PIN MISSING', '\n'.join(item.content for item in degraded.evidence))
        self.assertNotIn('COMPLETE_TAIL', '\n'.join(item.content for item in degraded.evidence))
        self.assertTrue(any('lookup budget exhausted' in warning for warning in degraded.warnings))

    def test_enclosing_lookup_sql_scope_does_not_fan_out_with_repository_count(self):
        from brain import catalog

        config = self.settings.config_path.read_text(encoding='utf-8')
        added = 1
        statement_counts = []
        for count in (10, 50, 100):
            for number in range(added, count):
                repo = self.root / f'noise{number}'
                repo.mkdir()
                (repo / 'Noise.java').write_text('class Noise {\n' + ''.join(
                    f' int sameName{item}() {{ return {item}; }}\n' for item in range(30)) + '}\n', encoding='utf-8')
                config += f"[[repositories]]\nname='noise{number}'\npath='noise{number}'\n"
            added = count
            self.settings.config_path.write_text(config, encoding='utf-8')
            self.settings = core.load_settings(self.settings.config_path)
            pinned = self.publish()
            statements = []
            def observed(settings):
                connection = connect(settings)
                connection.set_trace_callback(statements.append)
                return connection
            with self.subTest(repos=count), mock.patch.object(catalog, 'connect', side_effect=observed) as opened:
                ranges, reason = core._requested_symbol_ranges(pinned, {}, enclosing=[('service', 'Authorization.java', 303)])
                self.assertEqual(['authorize'], [item['simple_name'] for item in ranges])
                self.assertIsNone(reason)
                self.assertEqual(1, opened.call_count)
                self.assertEqual(3, len(statements))
                connection = connect(pinned)
                try:
                    plan = '\n'.join(str(row) for row in connection.execute('EXPLAIN QUERY PLAN ' + statements[0]))
                finally:
                    connection.close()
                self.assertIn('atlas_entities_repo_path', plan)
                self.assertNotIn('SCAN a', plan)
                statement_counts.append(len(statements))
        self.assertEqual([3, 3, 3], statement_counts)

    def test_callee_diversity_seeks_only_the_seed_at_repository_scale(self):
        self.source.write_text('class Authorization {\n void authorize() {\n' +
            '  noise();\n' * 100 + '  decision();\n }\n void noise() {}\n void decision() {}\n}\n', encoding='utf-8')
        config = self.settings.config_path.read_text(encoding='utf-8')
        added, counts = 1, []
        for count in (10, 50, 100):
            for number in range(added, count):
                repo = self.root / f'noise{number}'
                repo.mkdir()
                (repo / 'Noise.java').write_text('class Noise {\n' + ''.join(
                    f' void f{item}() {{ f{item + 1}(); }}\n' for item in range(30)) + '}\n', encoding='utf-8')
                config += f"[[repositories]]\nname='noise{number}'\npath='noise{number}'\n"
            added = count
            self.settings.config_path.write_text(config, encoding='utf-8')
            self.settings = core.load_settings(self.settings.config_path)
            pinned = self.publish()
            resolved = resolve_runtime_anchors(pinned, pinned.atlas_generation,
                [{'kind': 'symbol', 'value': 'Authorization.authorize'}], use_cache=False)
            statements = []
            class Observed:
                def __init__(self, settings):
                    self.connection = connect(settings)
                def set_trace_callback(self, callback):
                    self.connection.set_trace_callback(lambda query: (statements.append(query), callback(query)))
                def __getattr__(self, name):
                    return getattr(self.connection, name)
            with self.subTest(repos=count), mock.patch.object(investigation, 'connect', side_effect=Observed) as opened:
                flow = investigation._execution_flow(pinned, pinned.atlas_generation,
                    [resolved['candidates'][0]['entity_id']], core.ContextBundle('callees', atlas_generation=pinned.atlas_generation),
                    outgoing_types=('CALLS',))
                self.assertEqual({'noise', 'decision'}, {row['target'] for row in flow['steps']})
                self.assertEqual(2, len(flow['steps']))
                self.assertTrue(flow['truncated'])
                self.assertEqual(99, sum(row['collapsed_callsite_count'] for row in flow['steps']))
                self.assertEqual(1, opened.call_count)
                self.assertLessEqual(flow['database_operations'], flow['bounds']['database_query_limit'])
                counts.append(flow['database_operations'])
            connection = connect(pinned)
            try:
                plan = '\n'.join(str(row) for row in connection.execute('EXPLAIN QUERY PLAN ' + statements[0]))
            finally:
                connection.close()
            self.assertIn('atlas_edges_source', plan)
            self.assertNotIn('SCAN e', plan)
        self.assertEqual([counts[0]] * 3, counts)

    def test_unbound_receiver_cannot_mark_a_shared_valid_target_as_already_traversed(self):
        self.source.unlink()
        (self.root / 'service/noise.py').write_text('def noisy(state):\n    state.get()\n', encoding='utf-8')
        (self.root / 'service/direct.py').write_text('from target import get\ndef entry():\n    get()\n', encoding='utf-8')
        (self.root / 'service/target.py').write_text('def get():\n    finish()\ndef finish():\n    return 42\n', encoding='utf-8')
        pinned = self.publish()
        connection = connect(pinned)
        try:
            entities = dict(connection.execute('SELECT e.simple_name,e.entity_id FROM atlas_entities e '
                'JOIN generation_entities g ON g.entity_id=e.entity_id WHERE g.generation=? AND e.kind=?',
                (pinned.atlas_generation.generation, 'function')))
        finally:
            connection.close()
        sources = index.read_generation_files(pinned, pinned.atlas_generation,
            [('service', path) for path in ('noise.py', 'direct.py', 'target.py')], max_bytes=10_000, max_seconds=2)
        for seeds in (['noisy', 'entry'], ['entry', 'noisy']):
            with self.subTest(seeds=seeds):
                flow = investigation._execution_flow(pinned, pinned.atlas_generation,
                    [entities[name] for name in seeds], core.ContextBundle('shared target', atlas_generation=pinned.atlas_generation),
                    outgoing_types=('CALLS',), python_sources=sources)
                self.assertTrue(any(row['source_id'] == entities['get'] and row['target_id'] == entities['finish']
                    for row in flow['steps']), flow['steps'])
                self.assertTrue(all(row['state'] == 'candidate' and row['target_symbol'] is None
                    for row in flow['steps'] if row['source_id'] == entities['noisy']))

    def test_fuzzy_regex_results_do_not_gain_literal_priority(self):
        self.source.unlink()
        request = self.request()
        request['INVESTIGATION_REQUEST'].update(
            objective='Inspect the source', anchors=[{'kind': 'log_literal', 'value': 'PAYMENT.*LOST'}],
        )
        for value, literal in [('PAYMENT LOST', False), ('PAYMENT.*LOST', True)]:
            with self.subTest(value=value):
                (self.root / 'service/guard.py').write_text(f'value = {value!r}\n', encoding='utf-8')
                pinned = self.publish()
                bundle = self.retrieve(pinned, request)
                self.assertTrue(bundle.evidence)
                self.assertEqual(literal, any('requested anchor match' in item.kind for item in bundle.evidence))

    def test_many_explicit_files_only_mark_whole_output_regions_as_emitted(self):
        import re

        files = []
        for number in range(50):
            path = f'small{number:02d}.py'
            (self.root / 'service' / path).write_text(f'value = {number}\n', encoding='utf-8')
            files.append({'repo': 'service', 'path': path})
        pinned = replace(self.publish(), hard_context_chars=10_000,
                         max_backend_operations=50, max_effective_operations=50)
        request = {'INVESTIGATION_REQUEST': {'version': 5, 'mode': 'root_cause',
                                            'objective': 'Read the requested files', 'files': files}}
        bundle = self.retrieve(pinned, request)
        self.assertEqual(50, len(bundle.evidence))
        self.assertTrue(all(row['status'] == 'complete_file' for row in bundle.trace['file_reads']))
        emitted = set()
        content = core.pack_context(pinned, 'MANY', 1, bundle, emitted_ids=emitted)
        actual = {match[1] for line, outer in core._protocol_markdown_lines(content) if outer
                  and (match := re.match(r'^### [0-9]+\. (E[-0-9a-z]+) —', line))}
        self.assertLessEqual(len(content.encode('utf-8')), 10_000)
        self.assertEqual(actual, emitted)
        self.assertLess(len(actual), 50)
        for item in bundle.evidence:
            identifier = core._evidence_id(item)
            if identifier in actual:
                self.assertIn('\n'.join(core._source_markdown_block(item.content, 'python')), content)
            else:
                self.assertIn(identifier, content, 'omitted source must remain discoverable in its omission manifest')
        core.start_session(pinned, 'MANY', 'Read the requested pinned files')
        first, _, _ = core.create_context(pinned, 'MANY', json.dumps(request))
        state = core.session_state(pinned, 'MANY')
        delivered = {match[1] for line, outer in core._protocol_markdown_lines(first) if outer
                     and (match := re.match(r'^### (?:[0-9]+\. )?(E[0-9]+) —', line))}
        self.assertTrue(delivered, 'repeated metadata must not evict every tiny requested source file')
        for record in state['evidence_records']:
            self.assertEqual(record['public_id'] in delivered, record['emitted_in_context'])
        pending = next(record for record in state['evidence_records'] if not record['emitted_in_context'])
        follow_up = {'INVESTIGATION_REQUEST': {
            'version': 5, 'mode': 'root_cause', 'objective': 'Read one previously omitted source file',
            'base_context_id': state['last_context_id'], 'wave': 2,
            'files': [{'repo': pending['repo'], 'path': pending['path']}],
        }}
        second, _, _ = core.create_context(pinned, 'MANY', json.dumps(follow_up))
        self.assertIn(f"### {pending['public_id']} —", second)
        record = next(item for item in core.session_state(pinned, 'MANY')['evidence_records']
                      if item['public_id'] == pending['public_id'])
        self.assertTrue(record['emitted_in_context'])
        # The client can group remaining files, not recover them one by one.
        for _ in range(3):
            state = core.session_state(pinned, 'MANY')
            remaining = [item for item in state['evidence_records'] if not item['emitted_in_context']]
            if not remaining:
                break
            follow_up = {'INVESTIGATION_REQUEST': {
                'version': 5, 'mode': 'root_cause', 'objective': 'Read the remaining requested files',
                'base_context_id': state['last_context_id'],
                'files': [{'repo': item['repo'], 'path': item['path']} for item in remaining],
            }}
            batch, _, _ = core.create_context(pinned, 'MANY', json.dumps(follow_up))
            after = core.session_state(pinned, 'MANY')
            self.assertLess(sum(not item['emitted_in_context'] for item in after['evidence_records']), len(remaining))
            self.assertEqual(pinned.atlas_generation.identity, after['atlas_generation_id'])
            self.assertLessEqual(len(batch.encode('utf-8')), 10_000)
        self.assertTrue(all(item['emitted_in_context']
                            for item in core.session_state(pinned, 'MANY')['evidence_records']))
        roomy = replace(pinned, hard_context_chars=20_000)
        core.start_session(roomy, 'MANY-FULL', 'Read all requested pinned files')
        complete, _, _ = core.create_context(roomy, 'MANY-FULL', json.dumps(request))
        complete_ids = {match[1] for line, outer in core._protocol_markdown_lines(complete) if outer
                        and (match := re.match(r'^### (?:[0-9]+\. )?(E[0-9]+) —', line))}
        self.assertEqual(50, len(complete_ids), '20KB can hold all tiny files when reference metadata is not duplicated')
        self.assertLessEqual(len(complete.encode('utf-8')), 20_000)
        self.assertIn('Replacement status: `complete_replacement`', complete)
        self.assertTrue(all(item['emitted_in_context']
                            for item in core.session_state(roomy, 'MANY-FULL')['evidence_records']))

    def test_expired_request_reports_all_unexecuted_searches(self):
        from brain.retrieval.planner import compile_request

        pinned = self.publish()
        request = self.request()
        request['INVESTIGATION_REQUEST']['anchors'].append({'kind': 'symbol', 'value': 'billing.Settlement.settle'})
        parsed = core.parse_context_request(json.dumps(request))
        plan = replace(compile_request(parsed), timeout_ms=0)
        with mock.patch('brain.retrieval.compile_request', return_value=plan):
            bundle = core.retrieve_context(pinned, parsed)
        unresolved = '\n'.join(bundle.unresolved)
        for symbol in ('billing.Authorization.authorize', 'billing.Settlement.settle'):
            self.assertIn(f'Search `{symbol}` lookup was not executed', unresolved)
        self.assertNotIn('returned no code matches', unresolved)


if __name__ == '__main__':
    unittest.main()

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from brain import atlas, core, index, investigation, python_bindings
from brain.catalog import connect, current_generation_ref


class PythonBindingTests(unittest.TestCase):
    def build(self, files):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / 'repo').mkdir()
        for name, source in files.items():
            path = root / 'repo' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding='utf-8')
        config = root / 'brain.toml'
        config.write_text("[project]\nname='python-bindings'\n[graph]\nenabled=false\n"
                          "[experience]\nenabled=false\n[[repositories]]\nname='repo'\npath='repo'\n", encoding='utf-8')
        settings = core.load_settings(config)
        core.snapshot_indexes(settings)
        generation = current_generation_ref(settings)
        return replace(settings, atlas_generation=generation, atlas_generation_mode='pinned',
                       repositories=[replace(repo, source_sha=generation.snapshots[repo.name])
                                     for repo in settings.repositories])

    def inspect(self, settings, symbol='main.entry', required='callees'):
        request = core.parse_context_request(json.dumps({'INVESTIGATION_REQUEST': {
            'version': 5, 'mode': 'root_cause', 'objective': f'Read {symbol}',
            'anchors': [{'kind': 'symbol', 'value': symbol}], 'required': [required],
        }}))
        bundle = core.retrieve_context(settings, request)
        seeds = [item['entity_id'] for item in investigation.resolve_runtime_anchors(
            settings, settings.atlas_generation, request['anchors'])['candidates']]
        flow = investigation._execution_flow(settings, settings.atlas_generation, seeds, bundle,
            incoming_types=('CALLS',) if required == 'callers' else (),
            outgoing_types=('CALLS',) if required == 'callees' else ())
        return bundle, flow

    def test_import_alias_callers_deliver_the_complete_upstream_body(self):
        for declaration, call in (('from .helper import decision', 'decision()'),
                                  ('from .helper import decision as check', 'check()'),
                                  ('import package.helper as h', 'h.decision()'),
                                  ('from package import helper', 'helper.decision()'),
                                  ('from package import helper as h', 'h.decision()'),
                                  ('import package.helper', 'package.helper.decision()'),
                                  ('from . import helper as h', 'h.decision()')):
            with self.subTest(declaration=declaration):
                settings = self.build({'package/__init__.py': '',
                    'package/main.py': declaration + '\ndef entry():\n    value = ' + call + '\n' +
                        '    value += 1\n' * 180 + '    return "CALLER_DECISIVE_TAIL"\n',
                    'package/helper.py': 'def decision():\n    return 42\n',
                    'other.py': 'def decision():\n    return "UNRELATED"\n'})
                bundle, flow = self.inspect(replace(settings, full_file_lines=1), 'package.helper.decision', 'callers')
                self.assertIn('CALLER_DECISIVE_TAIL', core.pack_context(settings, 'CALLERS', 1, bundle))
                self.assertTrue(any(step['state'] == 'verified' and step['source_symbol']
                                    and step['source_symbol']['path'] == 'package/main.py'
                                    and step['target_symbol']['path'] == 'package/helper.py'
                                    for step in flow['steps']), flow)
                callers = [step for step in flow['steps'] if step['state'] == 'verified']
                self.assertEqual(1, len(callers), flow)
                hits, _ = core.trace_symbol(settings, 'decision')
                self.assertTrue(any(hit.path == 'package/main.py' and hit.kind == 'caller candidate' for hit in hits), hits)

    def test_incoming_alias_rejects_wrong_module_shadowing_and_package_exports(self):
        for bad in ('from other import decision as check\ndef entry():\n    return check()\n',
                    'from .helper import decision as check\ndef entry(check):\n    return check()\n',
                    'from .helper import decision as check\ndef entry():\n    check = replacement\n    return check()\n',
                    'if flag:\n    from .helper import decision as check\ndef entry():\n    return check()\n'):
            with self.subTest(bad=bad):
                settings = self.build({'pkg/__init__.py': '', 'pkg/bad.py': bad,
                    'pkg/good.py': 'from .helper import decision as check\ndef entry():\n    return check()\n',
                    'pkg/helper.py': 'def decision():\n    return 42\n', 'other.py': 'def decision():\n    return 0\n'})
                _, flow = self.inspect(settings, 'pkg.helper.decision', 'callers')
                self.assertEqual(['pkg/good.py'], [row['path'] for row in flow['steps'] if row['state'] == 'verified'], flow)
        for initializer in ('helper = replacement\n', 'def __getattr__(name):\n    return replacement\n', 'exec(code)\n'):
            with self.subTest(initializer=initializer):
                settings = self.build({'pkg/__init__.py': initializer,
                    'pkg/main.py': 'from . import helper as h\ndef entry():\n    return h.decision()\n',
                    'pkg/helper.py': 'def decision():\n    return 42\n'})
                _, flow = self.inspect(settings, 'pkg.helper.decision', 'callers')
                self.assertFalse(any(row['state'] == 'verified' for row in flow['steps']), flow)

    def test_incoming_alias_replays_only_delivered_pinned_source_and_rechecks_registration(self):
        settings = self.build({'pkg/__init__.py': '',
            'pkg/main.py': 'from . import helper as h\ndef entry():\n    h.decision()\n    return "G1_CALLER"\n',
            'pkg/helper.py': 'def decision():\n    return 42\n', 'other.py': 'def decision():\n    return 0\n'})
        original, _ = self.inspect(settings, 'pkg.helper.decision', 'callers')
        (settings.repositories[0].path / 'pkg/main.py').write_text(
            'from . import helper as h\ndef entry():\n    h.decision()\n    return "G2_CALLER"\n', encoding='utf-8')
        current = core.load_settings(settings.config_path)
        core.snapshot_indexes(current)
        generation = current_generation_ref(current)
        current = replace(current, atlas_generation=generation, atlas_generation_mode='pinned',
            repositories=[replace(repo, source_sha=generation.snapshots[repo.name]) for repo in current.repositories])
        for pinned, marker, forbidden in ((settings, 'G1_CALLER', 'G2_CALLER'), (current, 'G2_CALLER', 'G1_CALLER')):
            bundle, flow = self.inspect(pinned, 'pkg.helper.decision', 'callers')
            packed = core.pack_context(pinned, 'CALLER_PIN', 1, bundle)
            self.assertIn(marker, packed)
            self.assertNotIn(forbidden, packed)
            self.assertTrue(any(row['state'] == 'verified' for row in flow['steps']))
        seed = investigation.resolve_runtime_anchors(settings, settings.atlas_generation,
            [{'kind': 'symbol', 'value': 'pkg.helper.decision'}])['candidates'][0]['entity_id']
        with mock.patch.object(index, 'read_generation_files', side_effect=AssertionError('hidden source IO')), \
             mock.patch.object(index, 'query_generation_indexes', side_effect=AssertionError('hidden index discovery')):
            flow = investigation._execution_flow(settings, settings.atlas_generation, [seed], original,
                                                   incoming_types=('CALLS',), outgoing_types=())
            self.assertTrue(any(row['state'] == 'verified' for row in flow['steps']), flow)
            connection = connect(settings)
            try:
                connection.execute('DELETE FROM generation_intelligence_files WHERE generation=? AND path=?',
                                   (settings.atlas_generation.generation, 'pkg/main.py'))
                connection.commit()
            finally:
                connection.close()
            flow = investigation._execution_flow(settings, settings.atlas_generation, [seed], original,
                                                   incoming_types=('CALLS',), outgoing_types=())
            self.assertFalse(any(row['state'] == 'verified' for row in flow['steps']), flow)

    def test_wrong_module_candidates_do_not_spend_the_verified_caller_branch_budget(self):
        source = ''.join(f'from other import decision as wrong{n}\n' for n in range(12))
        source += 'from helper import decision as right\ndef entry():\n'
        source += ''.join(f'    wrong{n}()\n' for n in range(12)) + '    return right()\n'
        settings = self.build({'main.py': source, 'helper.py': 'def decision():\n    return 42\n',
                               'other.py': 'def decision():\n    return 0\n'})
        _, flow = self.inspect(settings, 'helper.decision', 'callers')
        self.assertEqual([len(source.splitlines())], [row['line'] for row in flow['steps'] if row['state'] == 'verified'], flow)

    def test_incoming_candidate_overflow_is_explicit_and_not_cached_as_no_callers(self):
        files = {f'a{n:02}.py': '# helper candidate noise\n' for n in range(30)}
        files.update({'helper.py': 'def decision():\n    return 42\n',
            'zcaller.py': 'from helper import decision as check\ndef entry():\n    return check()\n',
            'other.py': 'def decision():\n    return 0\n'})
        settings = self.build(files)
        for _ in range(2):
            bundle, _ = self.inspect(settings, 'helper.decision', 'callers')
            self.assertTrue(any('caller candidate' in reason and 'budget' in reason for reason in bundle.unresolved), bundle.unresolved)
            self.assertNotEqual('coverage_satisfied', bundle.trace['stop_reason'])
            self.assertLessEqual(bundle.trace['physical_backend_operations'], settings.max_backend_operations)

    def test_caller_candidate_buffers_are_not_trusted_after_index_failure(self):
        import time
        settings = self.build({'helper.py': 'def decision():\n    return 42\n'})
        sources = {}
        def failed(*args, **kwargs):
            kwargs['python_sources'][('repo', 'bad.py')] = 'unvalidated'
            return None
        with mock.patch.object(index, 'query_generation_indexes', side_effect=failed):
            reason = core._find_python_caller_sources(settings, settings.atlas_generation,
                [{'repo': 'repo', 'path': 'helper.py'}], sources, time.perf_counter() + 2)
        self.assertEqual({}, sources)
        self.assertIn('unavailable', reason)

    def test_absolute_module_members_require_unmodified_package_and_receiver(self):
        for declaration, call in (('from package import helper', 'helper.decision()'),
                                  ('import package.helper', 'package.helper.decision()')):
            for initializer in ('helper = replacement\n', 'def __getattr__(name):\n    return replacement\n'):
                with self.subTest(declaration=declaration, initializer=initializer):
                    settings = self.build({'package/__init__.py': initializer,
                        'package/main.py': declaration + '\ndef entry():\n    return ' + call + '\n',
                        'package/helper.py': 'def decision():\n    return 42\n'})
                    for required, symbol in (('callers', 'package.helper.decision'), ('callees', 'package.main.entry')):
                        _, flow = self.inspect(settings, symbol, required)
                        self.assertFalse(any(row['state'] == 'verified' for row in flow['steps']), flow)
        for body in ('def entry(package):\n    return package.helper.decision()\n',
                     'def entry():\n    package.helper = replacement\n    return package.helper.decision()\n',
                     'def entry():\n    return package.decision()\n',
                     'def entry(other):\n    package.helper.decision(); other.helper.decision()\n'):
            with self.subTest(body=body):
                settings = self.build({'package/__init__.py': '',
                    'package/main.py': 'import package.helper\n' + body,
                    'package/helper.py': 'def decision():\n    return 42\n'})
                _, flow = self.inspect(settings, 'package.main.entry')
                self.assertFalse(any(row['state'] == 'verified' for row in flow['steps']), flow)

    def test_canonical_path_symbol_can_request_alias_callers(self):
        settings = self.build({'pkg/__init__.py': 'def decision():\n    return 42\n',
            'main.py': 'from pkg import decision as check\ndef entry():\n    return check()\n',
            'other.py': 'def decision():\n    return 0\n'})
        symbol = 'repo:pkg/__init__.py:decision'
        resolved = investigation.resolve_runtime_anchors(settings, settings.atlas_generation,
            [{'kind': 'symbol', 'value': symbol}])
        self.assertEqual('entity_name', resolved['candidates'][0]['method'])
        bundle, flow = self.inspect(settings, symbol, 'callers')
        self.assertIn(symbol, bundle._resolved_relation_seeds, bundle.unresolved)
        self.assertTrue(any(row['state'] == 'verified' and row['path'] == 'main.py' for row in flow['steps']),
                        (bundle.unresolved, flow))
        self.assertFalse(bundle.unresolved, bundle.unresolved)
        warm = investigation.resolve_runtime_anchors(settings, settings.atlas_generation,
            [{'kind': 'symbol', 'value': symbol}])
        self.assertEqual(resolved['candidates'], warm['candidates'])
        connection = connect(settings)
        try:
            connection.execute('UPDATE generation_entities SET snapshot_sha=? WHERE generation=? AND entity_id=?',
                ('wrong-snapshot', settings.atlas_generation.generation, resolved['candidates'][0]['entity_id']))
            connection.commit()
        finally:
            connection.close()
        broken = investigation.resolve_runtime_anchors(settings, settings.atlas_generation,
            [{'kind': 'symbol', 'value': symbol}])
        self.assertFalse(broken['candidates'])

    def test_unrelated_python_name_is_not_verified_dispatch(self):
        for source in ('def entry():\n    return print("hello")\n',
                       'def entry(print):\n    return print("hello")\n'):
            with self.subTest(source=source):
                settings = self.build({'main.py': source, 'helper.py':
                    'def print(value):\n    return secret(value)\ndef secret(value):\n    return value\n'})
                bundle, flow = self.inspect(settings)
                self.assertTrue(flow['steps'])
                self.assertTrue(all(item['state'] == 'candidate' for item in flow['steps']), flow)
                self.assertFalse(any(item['target_symbol'] for item in flow['steps']))
                self.assertFalse(any(item['depth'] > 0 for item in flow['steps']))
                self.assertFalse(any(item.path == 'helper.py' for item in bundle.evidence))

    def test_parameter_and_local_assignment_do_not_bind_module_function(self):
        for entry in ('def entry(callback):\n    return callback()\n',
                      'def entry():\n    callback = provider\n    return callback()\n'):
            with self.subTest(entry=entry):
                settings = self.build({'main.py': entry +
                    'def callback():\n    return secret()\ndef secret():\n    return 42\n'})
                _, flow = self.inspect(settings)
                self.assertTrue(flow['steps'])
                self.assertTrue(all(item['state'] == 'candidate' for item in flow['steps']), flow)
                self.assertFalse(any(item['target_symbol'] for item in flow['steps']))
                self.assertFalse(any(item['depth'] > 0 for item in flow['steps']))

    def test_explicit_import_alias_delivers_callee_body_in_the_same_request(self):
        for declaration, call in (('from .helper import decision', 'decision()'),
                                  ('from .helper import decision as check', 'check()'),
                                  ('from . import helper', 'helper.decision()'),
                                  ('from . import helper as h', 'h.decision()'),
                                  ('from package import helper', 'helper.decision()'),
                                  ('from package import helper as h', 'h.decision()'),
                                  ('import package.helper', 'package.helper.decision()'),
                                  ('import package.helper as h', 'h.decision()')):
            with self.subTest(declaration=declaration):
                settings = self.build({'package/__init__.py': '',
                    'package/main.py': declaration + '\ndef entry():\n    return ' + call + '\n',
                    'package/helper.py': 'def decision():\n' + '    value = 1\n' * 180 + '    return "DECISIVE_TAIL"\n'})
                bundle, flow = self.inspect(replace(settings, full_file_lines=1), 'package.main.entry')
                self.assertIn('DECISIVE_TAIL', core.pack_context(settings, 'IMPORT', 1, bundle))
                self.assertTrue(any(item['state'] == 'verified' and item['target'] == 'decision'
                                    for item in flow['steps']), flow)

    def test_unshadowed_same_file_call_remains_navigable(self):
        settings = self.build({'main.py': 'def entry():\n    return decision()\ndef decision():\n    return 42\n'})
        bundle, flow = self.inspect(settings)
        self.assertTrue(any(item['target'] == 'decision' and item['state'] == 'verified' for item in flow['steps']))
        self.assertIn('return 42', core.pack_context(settings, 'LOCAL', 1, bundle))
        self.assertTrue(all(step['identity'] == step['source_edge_id'] for step in flow['steps']))

    def test_scope_rebindings_and_implicit_scopes_are_not_import_proof(self):
        bodies = [
            'def entry(decision):\n    return decision()\n',
            'def entry():\n    decision()\n    decision = replacement\n',
            'def entry(items):\n    for decision in items:\n        decision()\n',
            'def entry():\n    with resource() as decision:\n        decision()\n',
            'def entry():\n    try:\n        pass\n    except Exception as decision:\n        decision()\n',
            'def entry(value):\n    match value:\n        case {"call": decision}:\n            decision()\n',
            'def entry(items):\n    return [decision() for decision in items]\n',
            'def entry():\n    return lambda decision: decision()\n',
            'def entry():\n    (decision := replacement)\n    return decision()\n',
            'if condition:\n    from other import decision\ndef entry():\n    decision()\n',
            'from other import *\ndef entry():\n    decision()\n',
            'def entry():\n    exec(text)\n    decision()\n',
            'def entry():\n    global decision\n    from other import decision\n    return decision()\n',
            'def entry():\n    global decision\n    if flag:\n        from other import decision\n    return decision()\n',
            'def entry():\n    global decision\n    def decision():\n        return 42\n    return decision()\n',
        ]
        for body in bodies:
            with self.subTest(body=body):
                calls, _, _ = python_bindings.source_bindings('from helper import decision\n' + body)
                self.assertFalse(any(key[2] == 'decision' for key in calls), calls)
        calls, _, _ = python_bindings.source_bindings('def outer():\n    def decision():\n        return 42\n'
            '    def mutate():\n        nonlocal decision\n        decision = replacement\n'
            '    mutate()\n    return decision()\n')
        self.assertFalse(any(key[2] == 'decision' for key in calls), calls)
        for source in ('def decision():\n    pass\nclass Scope:\n    def decision(self):\n        pass\n'
                       '    def entry(self):\n        return decision()\n',
                       'def decision():\n    pass\ndef entry():\n    global decision\n    return decision()\n'):
            calls, _, _ = python_bindings.source_bindings(source)
            self.assertTrue(any(value == ('definition', '', 0, 'decision', 1) for value in calls.values()), calls)

    def test_imported_decorated_rebound_or_conditional_export_is_not_verified(self):
        for source in ('@wrap\ndef decision():\n    return 42\n',
                       'def decision():\n    return 42\ndecision = replacement\n',
                       'if condition:\n    def decision():\n        return 42\n'):
            with self.subTest(source=source):
                settings = self.build({'main.py': 'from helper import decision\ndef entry():\n    decision()\n',
                                       'helper.py': source})
                _, flow = self.inspect(settings)
                self.assertFalse(any(step['state'] == 'verified' or step['target_symbol'] for step in flow['steps']), flow)

    def test_legacy_trace_uses_the_same_proof_and_fallback_rejects_shadowing(self):
        for declaration, callee in (('from helper import decision as check\n', True), ('', False)):
            with self.subTest(declaration=declaration):
                settings = self.build({'main.py': declaration + 'def entry():\n    return check()\n',
                                       'helper.py': 'def decision():\n    return 42\ndef check():\n    return 0\n'})
                hits, relationships = core.trace_symbol(settings, 'entry')
                targets = [hit for hit in hits if hit.kind == 'callee candidate']
                self.assertEqual(callee, bool(targets), hits)
                if callee:
                    self.assertTrue(all(hit.path == 'helper.py' and 'decision' in hit.text for hit in targets))
                else:
                    self.assertTrue(any('binding unavailable' in row for row in relationships))
        for parameter, expected in (('', True), ('decision', False)):
            settings = self.build({'main.py': f'def entry({parameter}):\n    decision()\ndef decision():\n    return 42\n'})
            with mock.patch.object(atlas, 'symbol_call_edges', return_value=None):
                hits, _ = core.trace_symbol(settings, 'entry')
            self.assertEqual(expected, any(hit.kind == 'callee candidate' for hit in hits))

    def test_import_binding_keeps_old_generation_and_revalidates_warm_source(self):
        settings = self.build({'main.py': 'from helper import decision as check\ndef entry():\n    return check()\n',
                               'helper.py': 'def decision():\n    return "G1_ONLY"\n'})
        original, _ = self.inspect(settings)
        (settings.repositories[0].path / 'helper.py').write_text('def decision():\n    return "G2_ONLY"\n', encoding='utf-8')
        current = core.load_settings(settings.config_path)
        core.snapshot_indexes(current)
        generation = current_generation_ref(current)
        current = replace(current, atlas_generation=generation, atlas_generation_mode='pinned',
            repositories=[replace(repo, source_sha=generation.snapshots[repo.name]) for repo in current.repositories])
        with investigation.source_verification_scope():
            for pinned, marker, forbidden in ((settings, 'G1_ONLY', 'G2_ONLY'), (current, 'G2_ONLY', 'G1_ONLY')):
                bundle, flow = self.inspect(pinned)
                packed = core.pack_context(pinned, 'PIN', 1, bundle)
                self.assertIn(marker, packed)
                self.assertNotIn(forbidden, packed)
                self.assertTrue(any(step['state'] == 'verified' for step in flow['steps']), flow)
            seeds = [item['entity_id'] for item in investigation.resolve_runtime_anchors(settings, settings.atlas_generation,
                [{'kind': 'symbol', 'value': 'main.entry'}])['candidates']]
            with mock.patch.object(index, 'read_generation_files', side_effect=AssertionError('runtime hidden source IO')):
                flow = investigation._execution_flow(settings, settings.atlas_generation, seeds, original,
                                                       outgoing_types=('CALLS',))
                self.assertTrue(any(step['state'] == 'verified' for step in flow['steps']))
            connection = connect(settings)
            try:
                connection.execute('UPDATE generation_intelligence_files SET blob_sha=? WHERE generation=? AND path=?',
                    ('corrupt', settings.atlas_generation.generation, 'helper.py'))
                connection.commit()
            finally:
                connection.close()
            flow = investigation._execution_flow(settings, settings.atlas_generation, seeds, original,
                                                   outgoing_types=('CALLS',))
            self.assertFalse(any(step['state'] == 'verified' or step['target_symbol'] for step in flow['steps']))

    def test_projection_and_source_reads_are_bounded_and_request_local(self):
        for content in ('def invalid(:', '界' * 400_000, 'x = 1\n' * 100):
            with self.subTest(content=content[:20]), mock.patch.object(python_bindings, 'MAX_BINDING_NODES', 50):
                self.assertEqual(({}, {}, None), python_bindings.source_bindings(content))
        with investigation.source_verification_scope():
            cache = investigation._SOURCE_VERIFICATION_CACHE.get()[3]
            cache('def entry():\n    return 42\n')
            self.assertEqual(1, cache.cache_info().currsize)
        self.assertEqual(0, cache.cache_info().currsize)
        self.assertIsNone(investigation._SOURCE_VERIFICATION_CACHE.get())
        settings = self.build({'main.py': 'from helper import decision\ndef entry():\n    return decision()\n',
                               'helper.py': 'def decision():\n    return 42\n'})
        with mock.patch.object(index, 'read_generation_files', wraps=index.read_generation_files) as reads:
            bundle, _ = self.inspect(settings)
        locations = [key for call in reads.call_args_list for key in call.args[2]]
        self.assertEqual(1, locations.count(('repo', 'main.py')), locations)
        self.assertEqual(1, locations.count(('repo', 'helper.py')), locations)
        self.assertLessEqual(bundle.trace['physical_backend_operations'], settings.max_backend_operations)

    def test_relative_import_cannot_escape_package_and_module_mutation_is_unknown(self):
        for path, declaration, valid in (('main.py', 'from .helper import decision', False),
                                        ('pkg/main.py', 'from ..helper import decision', False),
                                        ('pkg/sub/main.py', 'from ..helper import decision', True)):
            with self.subTest(path=path):
                settings = self.build({path: declaration + '\ndef entry():\n    return decision()\n',
                    'helper.py': 'def decision():\n    return "ROOT_DECOY"\n',
                    'pkg/helper.py': 'def decision():\n    return "PACKAGE"\n'})
                _, flow = self.inspect(settings, path[:-3].replace('/', '.') + '.entry')
                self.assertEqual(valid, any(row['state'] == 'verified' for row in flow['steps']), flow)
        for mutation in ('setattr(h, "decision", decoy)', 'delattr(h, "decision")',
                         'vars(h)["decision"] = decoy', 'h.decision = decoy', 'del h.decision',
                         'h.__dict__["decision"] = decoy'):
            with self.subTest(mutation=mutation):
                settings = self.build({'main.py': 'import helper as h\ndef entry():\n    ' + mutation + '\n    h.decision()\n',
                                       'helper.py': 'def decision():\n    return 42\n'})
                _, flow = self.inspect(settings)
                self.assertFalse(any(row['state'] == 'verified' for row in flow['steps']), flow)

    def test_binding_seeks_do_not_fan_out_over_ten_fifty_or_hundred_repositories(self):
        settings = self.build({'main.py': 'from helper import decision as check\ndef entry():\n    return check()\n',
                               'helper.py': 'def decision():\n    return 42\n'})
        config = settings.config_path.read_text(encoding='utf-8')
        added, counts, incoming_counts = 1, [], []
        for count in (10, 50, 100):
            for number in range(added, count):
                repo = settings.config_path.parent / f'noise{number}'
                repo.mkdir()
                (repo / 'helper.py').write_text('def decision():\n    return "DECOY"\n' + ''.join(
                    f'def f{item}():\n    return {item}\n' for item in range(30)), encoding='utf-8')
                config += f"[[repositories]]\nname='noise{number}'\npath='noise{number}'\n"
            added = count
            settings.config_path.write_text(config, encoding='utf-8')
            current = core.load_settings(settings.config_path)
            core.snapshot_indexes(current)
            generation = current_generation_ref(current)
            current = replace(current, atlas_generation=generation, atlas_generation_mode='pinned',
                repositories=[replace(repo, source_sha=generation.snapshots[repo.name]) for repo in current.repositories])
            seed = investigation.resolve_runtime_anchors(current, generation,
                [{'kind': 'symbol', 'repo': 'repo', 'value': 'main.entry'}])['candidates'][0]['entity_id']
            sources = index.read_generation_files(current, generation, [('repo', 'main.py'), ('repo', 'helper.py')],
                                                   max_bytes=10_000, max_seconds=2)
            statements = []
            class Observed:
                def __init__(self, config):
                    self.connection = connect(config)
                def set_trace_callback(self, callback):
                    self.connection.set_trace_callback(lambda query: (statements.append(query), callback(query)))
                def __getattr__(self, key):
                    return getattr(self.connection, key)
            with self.subTest(repositories=count), mock.patch.object(investigation, 'connect', side_effect=Observed) as opened:
                flow = investigation._execution_flow(current, generation, [seed],
                    core.ContextBundle('scoped binding', atlas_generation=generation),
                    outgoing_types=('CALLS',), python_sources=sources)
                self.assertEqual([('repo', 'helper.py')], [(row['target_symbol']['repo'], row['target_symbol']['path'])
                                                          for row in flow['steps']])
                self.assertEqual(1, opened.call_count)
                self.assertLessEqual(flow['database_operations'], flow['bounds']['database_query_limit'])
                counts.append(flow['database_operations'])
                caller_seed = next(row['target_id'] for row in flow['steps'])
                before = len(statements)
                incoming = investigation._execution_flow(current, generation, [caller_seed],
                    core.ContextBundle('scoped caller binding', atlas_generation=generation),
                    incoming_types=('CALLS',), outgoing_types=(), python_sources=sources)
                self.assertEqual([('repo', 'main.py')], [(row['source_symbol']['repo'], row['source_symbol']['path'])
                                                       for row in incoming['steps']], incoming)
                self.assertLessEqual(incoming['database_operations'], incoming['bounds']['database_query_limit'])
                incoming_counts.append(incoming['database_operations'])
                caller_seek = next(query for query in statements[before:] if 'c.source_id AS neighbor_id' in query)
            seek = next(statement for statement in statements if 'AS ordinal,e.entity_id' in statement)
            connection = connect(current)
            try:
                plan = '\n'.join(str(row) for row in connection.execute('EXPLAIN QUERY PLAN ' + seek))
                caller_plan = '\n'.join(str(row) for row in connection.execute('EXPLAIN QUERY PLAN ' + caller_seek))
            finally:
                connection.close()
            self.assertIn('atlas_entities_repo_path', plan)
            self.assertNotIn('SCAN e', plan)
            self.assertIn('atlas_entities_repo_path', caller_plan)
            self.assertIn('atlas_edges_source', caller_plan)
            self.assertNotIn('SCAN e', caller_plan)
        self.assertEqual([counts[0]] * 3, counts)
        self.assertEqual([incoming_counts[0]] * 3, incoming_counts)

    def test_legacy_route_cache_cannot_promote_python_calls_without_source_proof(self):
        settings = self.build({'main.py': 'def entry():\n    return decision()\ndef decision():\n    return 42\n'})
        generation = settings.atlas_generation.generation
        connection = connect(settings)
        try:
            identifiers = [row[0] for row in connection.execute(
                "SELECT e.edge_id FROM atlas_edges e JOIN generation_edges g ON g.edge_id=e.edge_id "
                "WHERE g.generation=? AND e.edge_type='CALLS' AND json_extract(e.metadata_json,'$.resolved')=1",
                (generation,))]
            edges = atlas._valid_generation_edges(connection, generation, identifiers)
            self.assertTrue(edges)
            edge = next(iter(edges.values()))
            entities = atlas._valid_generation_entities(connection, generation, [edge['source_id'], edge['target_id']])
            source, target = entities[edge['source_id']], entities[edge['target_id']]
            payload = {'schema': atlas.ROUTER_SCHEMA_VERSION, 'generation': generation,
                       'entities': [], 'candidates': [], 'graph_edges': []}
            payload['cache_identity'] = atlas._route_cache_identity(payload)
            self.assertTrue(atlas._valid_cached_route(connection, generation, payload)[0])
            payload['graph_edges'] = [{'edge_id': edge['edge_id'], 'source_id': edge['source_id'],
                'target_id': edge['target_id'], 'edge_type': 'CALLS', 'source_repo': source['repo'],
                'repo': target['repo'], 'path': target['path'], 'line': target['line_start'], 'confidence': edge['confidence']}]
            payload['cache_identity'] = atlas._route_cache_identity(payload)
            self.assertFalse(atlas._valid_cached_route(connection, generation, payload)[0])
        finally:
            connection.close()

    def test_relative_module_import_rechecks_package_exports_on_each_pin(self):
        settings = self.build({'pkg/__init__.py': '',
            'pkg/main.py': 'from . import helper\ndef entry():\n    return helper.decision()\n',
            'pkg/helper.py': 'def decision():\n    return "G1_BODY"\n'})
        for initializer in ('helper = replacement\n', 'def __getattr__(name):\n    return replacement\n'):
            with self.subTest(initializer=initializer):
                (settings.repositories[0].path / 'pkg/__init__.py').write_text(initializer, encoding='utf-8')
                current = core.load_settings(settings.config_path)
                core.snapshot_indexes(current)
                generation = current_generation_ref(current)
                current = replace(current, atlas_generation=generation, atlas_generation_mode='pinned',
                    repositories=[replace(repo, source_sha=generation.snapshots[repo.name]) for repo in current.repositories])
                for pinned, expected in ((settings, True), (current, False)):
                    _, flow = self.inspect(pinned, 'pkg.main.entry')
                    self.assertEqual(expected, any(row['state'] == 'verified' for row in flow['steps']), flow)
        bundle, _ = self.inspect(settings, 'pkg.main.entry')
        seeds = [row['entity_id'] for row in investigation.resolve_runtime_anchors(settings, settings.atlas_generation,
            [{'kind': 'symbol', 'value': 'pkg.main.entry'}])['candidates']]
        bundle._python_package_sources[('repo', 'pkg/__init__.py')] = 'helper = replacement\n'
        with mock.patch.object(index, 'read_generation_files', side_effect=AssertionError('runtime source IO')):
            flow = investigation._execution_flow(settings, settings.atlas_generation, seeds, bundle, outgoing_types=('CALLS',))
        self.assertFalse(any(row['state'] == 'verified' for row in flow['steps']))

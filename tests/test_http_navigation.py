from __future__ import annotations

import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from brain import core, investigation, relations, semantic
from brain.atlas import build_atlas
from brain.catalog import AtlasGenerationRef, collect_generation_components, connect, current_generation_ref
from brain.ops import gc, refresh_brain


class HttpNavigationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        repositories = ['customer-client', 'customer-api', 'test-support', 'events', 'rules', 'config', 'contracts', 'jobs']
        for name in repositories:
            (self.root / name).mkdir()
        self.client = self.root / 'customer-client/LedgerPort.java'
        self.server = self.root / 'customer-api/ClearingHttpAdapter.java'
        self.client.write_text(
            '@FeignClient(name="customer-api", path="/risk")\ninterface LedgerPort {\n'
            ' @GetMapping({"/client-only/{id}", "/restrictions/{id}"})\n Object send(String id);\n}\n', encoding='utf-8')
        self.server.write_text(
            '@RestController\n@RequestMapping("/risk")\nclass ClearingHttpAdapter {\n'
            ' @GetMapping({"/server-only/{customerId}", "/restrictions/{customerId}"})\n'
            ' Object accept(String customerId) { return "DOWNSTREAM_DECISION"; }\n}\n', encoding='utf-8')
        config = self.root / 'brain.toml'
        config.write_text(
            "[project]\nname='http-navigation'\n[graph]\nenabled=false\n[experience]\nenabled=false\n" +
            ''.join(f"[[repositories]]\nname='{name}'\npath='{name}'\n" for name in repositories), encoding='utf-8')
        self.settings = core.load_settings(config)

    def request(self):
        return {'INVESTIGATION_REQUEST': {
            'version': 5, 'mode': 'flow_trace', 'objective': 'Trace LedgerPort outbound boundary to downstream handler',
            'required': ['cross repo integration'], 'anchors': [{'kind': 'symbol', 'value': 'LedgerPort'}],
        }}

    def refresh(self):
        refresh_brain(self.settings, fetch=False, discover=False)
        generation = current_generation_ref(self.settings)
        return replace(self.settings, atlas_generation=generation, atlas_generation_mode='pinned',
                       repositories=[replace(repo, source_sha=generation.snapshots[repo.name])
                                     for repo in self.settings.repositories])

    def test_second_http_mapping_reaches_downstream_source_despite_eight_repo_noise(self):
        for number in range(230):
            (self.root / f'test-support/LedgerNoise{number:03}.java').write_text(
                f'class LedgerNoise{number} {{ LedgerPort port; void run() {{ port.send("fixture"); }} }}\n', encoding='utf-8')
        pinned = self.refresh()
        bundle = core.retrieve_context(pinned, core.parse_context_request(json.dumps(self.request())))
        content = core.pack_context(pinned, 'HTTP', 1, bundle)
        self.assertIn('LedgerPort.java', content)
        self.assertIn('DOWNSTREAM_DECISION', content)
        self.assertIn('GET /risk/restrictions/{}', '\n'.join(bundle.relationships))

    def test_literal_method_alternatives_preserve_each_http_fact(self):
        clients, servers = relations._http_facts([
            ('customer-client', self.client.name, self.client.read_text(encoding='utf-8')),
            ('customer-api', self.server.name, self.server.read_text(encoding='utf-8')),
        ], {})
        self.assertEqual({'GET /risk/client-only/{}', 'GET /risk/restrictions/{}'}, {item.key for item in clients})
        self.assertEqual({'GET /risk/server-only/{}', 'GET /risk/restrictions/{}'}, {item.key for item in servers})

    def test_named_aliases_prefix_products_and_http_methods_are_bounded(self):
        source = ('@RestController\n@RequestMapping(path={"/v1", "/v2"}, method={RequestMethod.GET, RequestMethod.POST})\n'
                  'class Api {\n @RequestMapping(value={"/a", "/b"}, method={RequestMethod.GET, RequestMethod.DELETE})\n'
                  ' public String go() { return "ok"; }\n}\n')
        _, facts = relations._http_facts([('api', 'Api.java', source)], {})
        # Spring RequestMethodsRequestCondition.combine forms a union, not an
        # intersection, of the non-empty class/method conditions (Spring 6.2.11).
        self.assertEqual({f'{method} /{prefix}/{suffix}' for method in ('GET', 'POST', 'DELETE')
                          for prefix in ('v1', 'v2') for suffix in ('a', 'b')}, {item.key for item in facts})
        self.assertTrue(all(item.line == 4 for item in facts))
        client = '@FeignClient(name="api", url="https://api.invalid")\ninterface Port { @GetMapping(path="/a", produces="application/json") Object get(); }\n'
        facts, _ = relations._http_facts([('client', 'Port.java', client)], {})
        self.assertEqual(['GET /a'], [item.key for item in facts])
        self.assertEqual('api', facts[0].detail)

    def test_spring_method_conditions_reach_the_real_handler_and_agree_with_flow_verification(self):
        cases = [
            ('POST', '@RequestMapping(method=RequestMethod.GET)', '@PostMapping("/health")', True),
            ('GET', '@RequestMapping(method=RequestMethod.GET)', '@PostMapping("/health")', True),
            ('HEAD', '', '@GetMapping("/health")', True),
            ('GET', '', '@RequestMapping(path="/health", method=RequestMethod.HEAD)', False),
            ('OPTIONS', '', '@RequestMapping("/health")', False),
            ('OPTIONS', '', '@RequestMapping(path="/health", method=RequestMethod.OPTIONS)', True),
        ]
        for method, type_mapping, mapping, expected in cases:
            with self.subTest(method=method, mapping=mapping, type_mapping=type_mapping):
                self.client.write_text('@FeignClient(name="customer-api")\ninterface LedgerPort {\n'
                    f' @RequestMapping(path="/health", method=RequestMethod.{method}) Object call();\n}}\n', encoding='utf-8')
                self.server.write_text('@RestController\n' + type_mapping + '\nclass ClearingHttpAdapter {\n'
                    + mapping + '\n Object serve() { return "HTTP_HANDLER_EVIDENCE"; }\n}\n', encoding='utf-8')
                pinned = self.refresh()
                bundle = core.retrieve_context(pinned, core.parse_context_request(json.dumps(self.request())))
                links = relations._cached_relationships(pinned, pinned.atlas_generation)
                self.assertEqual(expected, any(item.kind == 'HTTP' and item.key == f'{method} /health' for item in links))
                if expected:
                    self.assertIn('HTTP_HANDLER_EVIDENCE', core.pack_context(pinned, 'METHODS', 1, bundle))
                evidence = [core.Evidence(repo, path.name, 1, len(path.read_text(encoding='utf-8').splitlines()),
                            path.read_text(encoding='utf-8'), 'code', 100, verification_content=path.read_text(encoding='utf-8'))
                            for repo, path in [('customer-api', self.server), ('customer-client', self.client)]]
                runtime = investigation.build_ticket_runtime(pinned, pinned.atlas_generation,
                    {'objective': 'Trace /health', 'anchors': [{'kind': 'endpoint', 'value': '/health'}]},
                    replace(bundle, evidence=evidence), {'coverage_map': {}, 'stable_identities': {}},
                    context_id='CTX-001', next_best_evidence=None)
                self.assertEqual(expected, runtime['coverage'].get('cross_repo_integration') == 'verified')

    def test_dynamic_malformed_and_non_path_literals_do_not_become_http_routes(self):
        bodies = [
            'path=ROOT + "/wrong"', 'value={"/wrong", ROUTE}', 'path=ROUTE, produces="/wrong"',
            'path="/a", value="/b"', 'path="${missing}"', 'path="/wrong", nested=@Other(value="x")',
            'path={"/a", "/b"', 'path="/a", path="/b"', 'path="/a\\\\b"',
            'path=', 'path=, produces="application/json"',
        ]
        for body in bodies:
            with self.subTest(body=body):
                source = f'@RestController\nclass Api {{\n @GetMapping({body})\n Object get() {{ return null; }}\n}}\n'
                _, facts = relations._http_facts([('api', 'Api.java', source)], {})
                self.assertFalse(facts)
        source = ('@RestController\nclass Api {\n'
                  ' String docs = "@GetMapping(\\"/wrong\\")";\n'
                  ' // @GetMapping("/wrong")\n'
                  ' @GetMapping(produces="application/json", name="/not-a-path")\n'
                  ' Object get() { return null; }\n}\n')
        _, facts = relations._http_facts([('api', 'Api.java', source)], {})
        self.assertEqual(['GET /'], [item.key for item in facts])
        self.assertNotEqual(relations._route('/a/{id:[0-9]+}'), relations._route('/a/{id:[a-z]+}'))
        self.assertEqual(relations._route('/a/{id:[0-9]{3}}'), relations._route('/a/{other:[0-9]{3}}'))

    def test_other_types_and_nested_comments_cannot_borrow_the_first_controller_prefix(self):
        source = ('@RestController\n@RequestMapping("/safe")\nclass Api {\n'
                  ' @GetMapping("/own") Object own() { return null; }\n'
                  ' class Nested { @GetMapping("/wrong") Object nested() { return null; } }\n}\n'
                  'class Other { @GetMapping("/wrong") Object other() { return null; } }\n')
        _, facts = relations._http_facts([('api', 'Api.java', source)], {})
        self.assertEqual(['GET /safe/own'], [item.key for item in facts])
        source = ('@RestController\nclass Api {\n'
                  ' /* nested /* comment */ @GetMapping("/wrong") */\n'
                  ' @GetMapping(path=["/one", "/two"]) fun get() {}\n'
                  ' @GetMapping("/$dynamic") fun dynamic() {}\n'
                  ' @GetMapping("${route:/wrong}") fun interpolation() {}\n}\n')
        _, facts = relations._http_facts([('api', 'Api.kt', source)], {'api': {'route': ('/wrong', 'app.yml', 1)}})
        self.assertEqual(['GET /one', 'GET /two'], [item.key for item in facts])

    def test_multiple_mapping_annotations_fail_closed_without_falling_through(self):
        # Spring accepts only one mapping per element. Do not infer the chosen
        # runtime annotation from source ordering/reflection implementation details.
        for header, member in [
            # Duplicate raw RequestMapping is invalid Java; composed mapping
            # siblings below are the realistic compile-valid ambiguity case.
            ('@RequestMapping("/first")\n@RequestMapping("/second")', '@GetMapping("/x")'),
            ('', '@GetMapping("/x")\n@PostMapping("/y")'),
            ('', '@RequestMapping(path=UNKNOWN)\n@GetMapping("/fallback")'),
            ('', '@CustomGetMapping("/first")\n@GetMapping("/fallback")'),
            ('@CustomRequestMapping("/prefix")', '@GetMapping("/fallback")'),
        ]:
            with self.subTest(header=header, member=member):
                source = f'@RestController\n{header}\nclass Api {{\n{member}\n Object go() {{ return null; }}\n}}\n'
                self.assertFalse(relations._http_facts([('api', 'Api.java', source)], {})[1])
                self.assertFalse(investigation._verification_endpoints('api', 'Api.java', source))
        source = '@RestController\nclass Api { @GetMappingFactory("/fake") Object go() {} }'
        self.assertFalse(relations._http_facts([('api', 'Api.java', source)], {})[1])

    def test_second_top_level_type_reaches_downstream_without_borrowing_another_type(self):
        self.client.write_text('@FeignClient(name="wrong-api", path="/wrong")\ninterface OtherPort {\n'
            ' @GetMapping("/other") Object other();\n}\n' + self.client.read_text(encoding='utf-8'), encoding='utf-8')
        self.server.write_text('@RestController\n@RequestMapping("/wrong")\nclass OtherApi {\n'
            ' @GetMapping("/other") Object other() { return null; }\n}\n' + self.server.read_text(encoding='utf-8'), encoding='utf-8')
        pinned = self.refresh()
        bundle = core.retrieve_context(pinned, core.parse_context_request(json.dumps(self.request())))
        content = core.pack_context(pinned, 'MULTI-TYPE', 1, bundle)
        self.assertIn('DOWNSTREAM_DECISION', content)
        self.assertIn('GET /risk/restrictions/{}', '\n'.join(bundle.relationships))
        evidence = [core.Evidence(repo, path.name, 1, len(path.read_text(encoding='utf-8').splitlines()),
                    path.read_text(encoding='utf-8'), 'code', 100, verification_content=path.read_text(encoding='utf-8'))
                    for repo, path in [('customer-api', self.server), ('customer-client', self.client)]]
        runtime = investigation.build_ticket_runtime(pinned, pinned.atlas_generation,
            self.request()['INVESTIGATION_REQUEST'], replace(bundle, evidence=evidence),
            {'coverage_map': {}, 'stable_identities': {}}, context_id='CTX-001', next_best_evidence=None)
        self.assertEqual('verified', runtime['coverage'].get('cross_repo_integration'))
        for name, line, expected in [('OtherPort', 2, {'/wrong/other'}),
                                     ('LedgerPort', 6, {'/risk/client-only/{}', '/risk/restrictions/{}'})]:
            with self.subTest(name=name):
                steps = investigation._source_http_steps(replace(bundle, evidence=evidence), [{
                    'kind': 'symbol', 'value': name, 'repo': 'customer-client', 'path': self.client.name, 'line': line}])
                self.assertEqual(expected, {item['key'] for item in steps})

    def test_top_level_type_ownership_ignores_nested_mapping_headers_and_literal_braces(self):
        source = ('@RestController\n@RequestMapping(path={"/first", "/alias"})\nclass First {\n'
                  ' @GetMapping("/x") @Other(values={"}", "{"}) Object x() { return null; }\n'
                  ' @RestController @RequestMapping("/nested") class Nested {\n'
                  '  @GetMapping("/not-an-outer-method") Object no() { return null; }\n }\n}\n'
                  '@RestController\n@RequestMapping("/second")\nclass Second {\n'
                  ' @GetMapping("/y") Object y() { return null; }\n}\n'
                  'class Plain { @GetMapping("/not-a-controller") Object no() {} }\n')
        _, facts = relations._http_facts([('api', 'Api.java', source)], {})
        self.assertEqual({'GET /first/x', 'GET /alias/x', 'GET /second/y'}, {item.key for item in facts})
        kotlin = ('@FeignClient(name="api", path="/one")\ninterface First {\n'
                  ' @GetMapping("/a") fun a(): String\n @GetMapping("/b") fun b(): String\n}\n'
                  '@RestController\n@RequestMapping("/two")\nclass Second {\n'
                  ' @GetMapping(path=["/x", "/y"]) fun x() = "ok"\n}\n')
        clients, servers = relations._http_facts([('api', 'Api.kt', kotlin)], {})
        self.assertEqual({'GET /one/a', 'GET /one/b'}, {item.key for item in clients})
        self.assertEqual({'GET /two/x', 'GET /two/y'}, {item.key for item in servers})
        bodyless = ('@RestController\n@RequestMapping("/marker")\nclass Marker\n'
                    'class Plain { @GetMapping("/not-a-controller") fun no() {} }\n'
                    '@RestController\nclass Real { @GetMapping("/real") fun yes() {} }\n')
        _, facts = relations._http_facts([('api', 'Api.kt', bodyless)], {})
        self.assertEqual(['GET /real'], [item.key for item in facts])

    def test_http_type_traversal_is_disjoint_at_enterprise_metadata_scale(self):
        source = ''.join('@RestController\n@RequestMapping("/p' + str(number) + '")\nclass Api' + str(number) +
                         ' { @GetMapping("/x") Object x() { return null; } }\n' for number in range(1_000))
        spans = relations._http_top_level_types(relations._structure_mask(source))
        self.assertEqual(1_000, len(spans))
        self.assertTrue(all(left[3] < right[0] for left, right in zip(spans, spans[1:])))
        self.assertLessEqual(sum(closing - header for header, _, _, closing, _ in spans), len(source))
        with mock.patch.object(relations, '_http_top_level_types', wraps=relations._http_top_level_types) as scopes, \
                mock.patch.object(relations, '_http_method_mappings', wraps=relations._http_method_mappings) as members:
            _, facts = relations._http_facts([('api', 'Api.java', source)], {})
        self.assertEqual(1, scopes.call_count)
        self.assertEqual(1_000, members.call_count)
        self.assertEqual(1_000, len(facts))
        self.assertEqual(1_000, len({item.key for item in facts}))
        for repositories in (10, 50, 100):
            with self.subTest(repositories=repositories):
                _, facts = relations._http_facts(((f'api-{number}', 'Api.java', source)
                                                 for number in range(repositories)), {})
                self.assertEqual(relations.MAX_FACTS_PER_ANALYZER, len(facts))

    def test_generated_identifier_does_not_cause_quadratic_method_scanning(self):
        source = '@RestController\nclass Api { String ' + 'x' * 80_000 + ';\n @GetMapping("/safe") Object go() {}\n}'
        started = time.monotonic()
        _, facts = relations._http_facts([('api', 'Api.java', source)], {})
        self.assertEqual(['GET /safe'], [item.key for item in facts])
        # Generous relative to this 80 KB parse; catches suffix-by-suffix regex
        # retries on generated identifiers without being a throughput benchmark.
        self.assertLess(time.monotonic() - started, 5.0)
        with mock.patch.object(relations, '_http_method_mappings', wraps=relations._http_method_mappings) as members:
            self.assertEqual(([], []), relations._http_facts([('api', 'Plain.java', source.replace('@RestController', ''))], {}))
        members.assert_not_called()

    def test_local_composed_mapping_aliases_do_not_fall_through_to_a_builtin_sibling(self):
        for alias, declarations in [
            ('RiskEndpoint', '@RequestMapping("/first")\n@interface RiskEndpoint {}\n'),
            ('RiskLookup', '@RiskEndpoint\n@interface RiskLookup {}\n@RequestMapping("/first")\n@interface RiskEndpoint {}\n'),
        ]:
            with self.subTest(alias=alias):
                source = (declarations + '@RestController\nclass Api {\n @' + alias +
                          '\n @GetMapping("/second") Object go() {}\n}\n')
                self.assertFalse(relations._http_facts([('api', 'Api.java', source)], {})[1])
                self.assertFalse(investigation._verification_endpoints('api', 'Api.java', source))

    def test_legacy_ambiguous_endpoint_edges_cannot_become_verified_execution_steps(self):
        self.client.write_text('@FeignClient(name="customer-api")\ninterface LedgerPort {\n'
                               ' @GetMapping("/x") Object call();\n}\n', encoding='utf-8')
        for prefix, mappings, expected in [
            ('', '@GetMapping("/x")\n @PostMapping("/y")', False),
            ('', '@GetMapping("/x")', True),
            ('@RequestMapping("/first")\n@interface RiskEndpoint {}\n', '@RiskEndpoint\n @GetMapping("/x")', False),
        ]:
            with self.subTest(mappings=mappings):
                source = prefix + '@RestController\nclass ClearingHttpAdapter {\n ' + mappings + '\n Object go() {}\n}\n'
                self.server.write_text(source, encoding='utf-8')
                pinned = self.refresh()
                generation = pinned.atlas_generation
                connection = connect(pinned)
                try:
                    entities = dict(connection.execute(
                        "SELECT e.entity_id,e.kind FROM atlas_entities e JOIN generation_entities g ON e.entity_id=g.entity_id "
                        "WHERE g.generation=?", (generation.generation,)))
                finally:
                    connection.close()
                seeds = [identity for identity, kind in entities.items() if kind == 'endpoint']
                self.assertTrue(seeds, 'Legacy immutable Atlas endpoints remain navigation candidates')
                bundle = core.ContextBundle('AMBIGUOUS', atlas_generation=generation, evidence=[core.Evidence(
                    'customer-api', self.server.name, 1, len(source.splitlines()), source, 'code', 100, verification_content=source)])
                execution = investigation._execution_flow(pinned, generation, seeds, bundle)
                if 'PostMapping' in mappings:
                    leaves = [step for step in execution['steps'] if step['edge_type'] == 'CALLS_ENDPOINT'
                              and step['target_id'] not in entities]
                    self.assertTrue(leaves, execution)
                    self.assertTrue(all(step['state'] == 'candidate' for step in leaves))
                exposed = [step for step in execution['steps'] if step['edge_type'] == 'EXPOSES_ENDPOINT'
                           and step['repo'] == 'customer-api']
                self.assertTrue(exposed, execution)
                self.assertEqual(expected, any(step['state'] == 'verified' for step in exposed), exposed)
                self.assertTrue(all(step['evidence_authority'] == (
                    'exact_source' if step['state'] == 'verified' else 'atlas_candidate') for step in exposed))
                if expected:
                    self.assertTrue(any(step['target'] == 'go' and step['state'] == 'verified' for step in exposed))
                    for other_pin in (None, replace(generation, identity='different-generation', generation=generation.generation + 1)):
                        with mock.patch.object(investigation, 'connect', side_effect=AssertionError('must reject before graph I/O')):
                            rejected = investigation._execution_flow(pinned, generation, seeds, replace(bundle, atlas_generation=other_pin))
                        self.assertEqual('degraded', rejected['status'])
                        self.assertEqual([], rejected['steps'])
                        self.assertEqual(0, rejected['database_operations'])

    def test_cross_repo_same_name_cannot_verify_endpoint_handler_ownership(self):
        self.client.write_text('@FeignClient(name="customer-api")\ninterface LedgerPort {\n'
                               ' @GetMapping("/x") default Object go() { return null; }\n}\n', encoding='utf-8')
        self.server.write_text('@RestController\nclass ClearingHttpAdapter {\n'
                               ' @GetMapping("/x")\n Object go() { return "SERVER"; }\n}\n', encoding='utf-8')
        pinned = self.refresh()
        generation = pinned.atlas_generation
        connection = connect(pinned)
        try:
            entities = {row[0]: {'repo': row[1], 'path': row[2], 'kind': row[3], 'parent': row[4]}
                        for row in connection.execute(
                            "SELECT e.entity_id,e.repo,e.path,e.kind,e.parent_entity_id FROM atlas_entities e "
                            "JOIN generation_entities g ON e.entity_id=g.entity_id WHERE g.generation=?", (generation.generation,))}
        finally:
            connection.close()
        seeds = [identity for identity, item in entities.items() if item['kind'] == 'endpoint']
        evidence = [core.Evidence(repo, path.name, 1, len(path.read_text(encoding='utf-8').splitlines()),
                    path.read_text(encoding='utf-8'), 'code', 100, verification_content=path.read_text(encoding='utf-8'))
                    for repo, path in [('customer-api', self.server), ('customer-client', self.client)]]
        bundle = core.ContextBundle('OWNERS', atlas_generation=generation, evidence=evidence)
        execution = investigation._execution_flow(pinned, generation, seeds, bundle)
        self.assertTrue(execution['steps'], execution)
        exposed = [step for step in execution['steps'] if step['edge_type'] == 'EXPOSES_ENDPOINT']
        self.assertTrue(any(step['target'] == 'go' and step['state'] == 'verified' for step in exposed))
        cross_repo = [step for step in exposed if entities[step['source_id']]['repo'] != entities[step['target_id']]['repo']]
        self.assertTrue(cross_repo)
        self.assertTrue(all(step['state'] == 'candidate' for step in cross_repo), cross_repo)
        for step in exposed:
            if step['state'] == 'verified':
                self.assertEqual(step['target_id'], entities[step['source_id']]['parent'])
                self.assertEqual(entities[step['source_id']]['repo'], entities[step['target_id']]['repo'])
                self.assertEqual(entities[step['source_id']]['path'], entities[step['target_id']]['path'])

    def test_excessive_alternatives_and_fact_count_do_not_create_unbounded_products(self):
        body = 'path={' + ','.join(f'"/p{n}"' for n in range(9)) + '}'
        self.assertIsNone(relations._annotation_paths(body, {'path'}))
        self.assertIsNone(relations._annotation_paths('path="/' + 'a' * 9_000 + '"', {'path'}))
        source = ('@RestController\n@RequestMapping(path={' + ','.join(f'"/p{n}"' for n in range(8)) +
                  '})\nclass Api {\n @GetMapping(path={' + ','.join(f'"/s{n}"' for n in range(8)) +
                  '}) Object get() { return null; }\n}\n')
        _, facts = relations._http_facts([('api', 'Api.java', source)], {})
        self.assertEqual(64, len(facts))
        with mock.patch.object(relations, 'MAX_FACTS_PER_ANALYZER', 5):
            _, facts = relations._http_facts([('api', 'Api.java', source)], {})
        self.assertEqual(5, len(facts))

    def test_endpoint_authority_uses_literal_routes_not_neighboring_annotation_strings(self):
        generation = AtlasGenerationRef(1, 'g1', None, 's1', {'customer-api': 'snapshot'}, {}, {})
        for body, value, expected in [
            ('path="/real", produces="application/json"', '/application/json', False),
            ('params="/fake", path="/real"', '/fake', False),
            ('path=PREFIX + "/fake"', '/fake', False),
            ('path="${route:/fallback}"', '/fallback', False),
            ('path="/Real"', '/real', False),
            ('path="/real"', '/real', True),
        ]:
            with self.subTest(body=body):
                source = f'@RestController\nclass Api {{\n @GetMapping({body}) Object go() {{ return null; }}\n}}\n'
                evidence = core.Evidence('customer-api', 'Api.java', 1, 4, source, 'code', 100, verification_content=source)
                bundle = core.ContextBundle('HTTP', evidence=[evidence], atlas_generation=generation)
                with investigation.source_verification_scope():
                    self.assertEqual(expected, investigation._verified_value_location(
                        bundle, 'customer-api', 'Api.java', 3, value, kind='endpoint', direction='inbound'))
                    self.assertFalse(investigation._verified_value_location(
                        bundle, 'customer-api', 'Api.java', 3, value, kind='endpoint', direction='outbound'))

    def test_same_path_get_and_post_cannot_claim_verified_cross_repo_integration(self):
        self.client.write_text('@FeignClient(name="customer-api")\ninterface LedgerPort {\n'
                               ' @GetMapping("/health") Object go();\n}\n', encoding='utf-8')
        for method, expected in [('Post', False), ('Get', True)]:
            with self.subTest(method=method):
                self.server.write_text('@RestController\nclass ClearingHttpAdapter {\n'
                                       f' @{method}Mapping("/health") Object go() {{ return null; }}\n}}\n', encoding='utf-8')
                pinned = self.refresh()
                evidence = [core.Evidence(repo, path.name, 1, 4, path.read_text(encoding='utf-8'), 'code', 100,
                                         verification_content=path.read_text(encoding='utf-8'))
                            for repo, path in [('customer-api', self.server), ('customer-client', self.client)]]
                bundle = core.ContextBundle('HTTP', evidence=evidence, atlas_generation=pinned.atlas_generation)
                runtime = investigation.build_ticket_runtime(
                    pinned, pinned.atlas_generation,
                    {'objective': 'Trace /health', 'anchors': [{'kind': 'endpoint', 'value': '/health'}]},
                    bundle, {'coverage_map': {}, 'stable_identities': {}}, context_id='CTX-001', next_best_evidence=None)
                self.assertEqual(expected, runtime['coverage'].get('cross_repo_integration') == 'verified')
                self.assertEqual(expected, runtime['integration_flow']['status'] == 'ready')

    def test_feign_effective_path_pairs_from_pinned_source_without_reindexing(self):
        pinned = self.refresh()
        generation = pinned.atlas_generation
        # Persisted legacy Java facts omit the Feign prefix; query-time source
        # verification must recover the effective route without rewriting them.
        old_facts = self._ids(pinned, 'generation_integration_facts', 'fact_id')
        evidence = [core.Evidence(repo, path.name, 1, len(path.read_text(encoding='utf-8').splitlines()),
                                 path.read_text(encoding='utf-8'), 'code', 100,
                                 verification_content=path.read_text(encoding='utf-8'))
                    for repo, path in [('customer-api', self.server), ('customer-client', self.client)]]
        bundle = core.ContextBundle('HTTP', evidence=evidence, atlas_generation=generation)
        request = {'objective': 'Trace restrictions', 'anchors': [
            {'kind': 'endpoint', 'value': '/risk/restrictions/{customerId}'}]}
        self.client.write_text('// newer working tree cannot replace the old pin\n', encoding='utf-8')
        runtime = investigation.build_ticket_runtime(pinned, generation, request, bundle,
                    {'coverage_map': {}, 'stable_identities': {}}, context_id='CTX-001', next_best_evidence=None)
        self.assertEqual('verified', runtime['coverage'].get('cross_repo_integration'))
        self.assertEqual('ready', runtime['integration_flow']['status'])
        source_steps = [item for item in runtime['integration_flow']['steps']
                        if (item.get('provenance') or {}).get('extractor') == 'literal-http-source-v1']
        self.assertTrue(any(item['direction'] == 'outbound' for item in source_steps))
        self.assertTrue(all(item['entity_id'] is None for item in source_steps))
        self.assertEqual(['outbound', 'inbound'], [item['direction'] for item in source_steps
                                                 if item['key'] == '/risk/restrictions/{}'])
        self.assertEqual(old_facts, self._ids(pinned, 'generation_integration_facts', 'fact_id'))
        for anchor in ({'kind': 'endpoint', 'value': '/restrictions/{id}'}, {'kind': 'symbol', 'value': 'LedgerPort'}):
            linked = investigation.build_ticket_runtime(pinned, generation,
                {'objective': 'Trace LedgerPort', 'anchors': [anchor]}, bundle,
                {'coverage_map': {}, 'stable_identities': {}}, context_id='CTX-001', next_best_evidence=None)
            self.assertEqual('verified', linked['coverage'].get('cross_repo_integration'))
        self.assertFalse(investigation._source_http_steps(bundle, [{'kind': 'symbol', 'value': 'UnrelatedService'}]))
        root_client = '@FeignClient(name="customer-api", path="/risk")\ninterface LedgerPort {\n @GetMapping Object go();\n}\n'
        root_evidence = replace(evidence[1], content=root_client, verification_content=root_client)
        root_steps = investigation._source_http_steps(replace(bundle, evidence=[root_evidence]), [{
            'kind': 'endpoint', 'value': '/', 'repo': 'customer-client', 'path': self.client.name, 'line': 3,
            'provenance': {'direction': 'outbound'}}])
        self.assertEqual(['/risk'], [item['key'] for item in root_steps])
        for changed_bundle in (replace(bundle, evidence=evidence[:1]),
                               replace(bundle, evidence=[replace(item, verification_content=None, line_start=4)
                                                         for item in evidence]),
                               replace(bundle, atlas_generation=replace(generation, generation=generation.generation + 1, identity='g2')),
                               replace(bundle, atlas_generation=None)):
            state = investigation.build_ticket_runtime(pinned, generation, request, changed_bundle,
                        {'coverage_map': {}, 'stable_identities': {}}, context_id='CTX-001', next_best_evidence=None)
            self.assertNotEqual('verified', state['coverage'].get('cross_repo_integration'))

    def test_source_http_projection_is_file_byte_step_and_scope_bounded(self):
        generation = AtlasGenerationRef(1, 'g1', None, 's1', {'customer-api': 'snapshot'}, {}, {})
        source = '@RestController\nclass Api {\n @GetMapping("/health") Object go() { return null; }\n}\n'
        evidence = [core.Evidence('customer-api', f'Api{number}.java', 1, 4, source, 'code', 100, verification_content=source)
                    for number in range(100)]
        noise = [replace(evidence[0], path=f'Noise{number}.java', content='class Noise {}',
                         verification_content='class Noise {}') for number in range(100)]
        bundle = core.ContextBundle('HTTP', evidence=[*noise, *evidence], atlas_generation=generation)
        anchors = [{'kind': 'endpoint', 'value': '/health'}]
        with mock.patch.object(investigation, '_verification_endpoints', wraps=investigation._verification_endpoints) as parsed, \
                investigation.source_verification_scope():
            steps = investigation._source_http_steps(bundle, anchors)
        self.assertEqual(investigation.MAX_FLOW_BRANCH, parsed.call_count)
        self.assertEqual(investigation.MAX_FLOW_BRANCH, len(steps))
        self.assertEqual([], investigation._source_http_steps(bundle, [{'kind': 'endpoint', 'value': '/absent'}]))
        self.assertFalse(investigation._source_http_steps(replace(bundle, evidence=[
            replace(evidence[0], verification_content=None)]), anchors))
        with mock.patch.object(investigation, 'MAX_REFRESH_FILE_BYTES', 16):
            self.assertFalse(investigation._source_http_steps(bundle, anchors))
        ambiguous = '@RestController class First { @GetMapping("/x") Object go() {} } class Other { @GetMapping("/x") Object no() {} }'
        self.assertFalse(investigation._verification_endpoints('customer-api', 'Api.java', ambiguous))
        dense = '@RestController\nclass Api {\n' + ''.join(
            f' @GetMapping("/p{number}") Object get{number}() {{ return null; }}\n' for number in range(150)) + '}\n'
        large = replace(evidence[0], content=dense, verification_content=dense, line_end=153)
        self.assertEqual(investigation.MAX_FLOW_STEPS, len(investigation._source_http_steps(replace(bundle, evidence=[large]), [
            {'kind': 'symbol', 'value': 'Api', 'repo': 'customer-api', 'path': large.path, 'line': 2}])))

    def test_extractor_upgrade_refreshes_only_relationships_and_reuses_semantic_and_old_pins(self):
        original_paths = relations._annotation_paths
        original_build = semantic.build_semantic_index
        embedded = []

        def legacy_paths(*args, **kwargs):
            values = original_paths(*args, **kwargs)
            return values[:1] if values else values

        def embed(cards):
            embedded.extend(cards)
            return [[1.0, 0.5, 0.25] for _ in cards]

        def build(settings, *, progress=None):
            return original_build(settings, embed=embed, pack_id='http-test-pack', progress=progress)

        with mock.patch('brain.editions.current_edition', return_value='semantic'), \
                mock.patch.object(semantic, 'build_semantic_index', side_effect=build):
            with mock.patch.object(relations, 'RELATIONSHIP_EXTRACTOR_VERSION', 'legacy-http-first-value'), \
                    mock.patch.object(relations, '_annotation_paths', side_effect=legacy_paths):
                old = self.refresh()
            core.start_session(self.settings, 'OLD-HTTP', 'Trace LedgerPort')
            old_semantic = old.atlas_generation.component('semantic')
            old_cards = self._ids(old, 'generation_cards', 'card_id')
            old_entities = self._ids(old, 'generation_entities', 'entity_id')
            self.assertTrue(embedded)
            embedded.clear()
            self.assertIsNone(relations._cached_relationships(self.settings))
            with mock.patch.object(relations, 'analyze_relationships', wraps=relations.analyze_relationships) as analyze:
                new = self.refresh()
                self.assertEqual(1, analyze.call_count)
                self.refresh()
                self.assertEqual(1, analyze.call_count)
            self.assertFalse(embedded)
            self.assertEqual(old_semantic['content_hash'], new.atlas_generation.component('semantic')['content_hash'])
            self.assertEqual('ready', new.atlas_generation.component('semantic')['status'])
            self.assertEqual(old_cards, self._ids(new, 'generation_cards', 'card_id'))
            self.assertEqual(old_entities, self._ids(new, 'generation_entities', 'entity_id'))
            self.assertNotEqual(old.atlas_generation.identity, new.atlas_generation.identity)
            self.assertFalse(any(item.kind == 'HTTP' for item in relations._cached_relationships(old, old.atlas_generation)))
            self.assertTrue(any(item.kind == 'HTTP' for item in relations._cached_relationships(new, new.atlas_generation)))
            self.assertEqual(old.atlas_generation.identity, core.session_state(self.settings, 'OLD-HTTP')['atlas_generation_id'])
            old_artifact = self.settings.state_dir / old.atlas_generation.component('relationships')['artifact_ref']
            retained = gc(self.settings, dry_run=False, keep_recent=1)
            self.assertFalse(retained['reachability_gc_blocked'])
            self.assertFalse(retained['semantic_gc_blocked'])
            self.assertIn(old.atlas_generation.generation, retained['pinned_generations'])
            self.assertTrue(old_artifact.is_file())
            self.assertTrue(relations._cached_relationships(old, old.atlas_generation))
            (self.settings.runs_dir / 'OLD-HTTP/session.json').unlink()
            unpinned = gc(self.settings, dry_run=True, keep_recent=1)
            self.assertIn(str(old_artifact.parent), [item['path'] for item in unpinned['remove']])

    def _ids(self, pinned, table, column):
        connection = connect(pinned)
        try:
            return {row[0] for row in connection.execute(f'SELECT {column} FROM {table} WHERE generation=?',
                                                       (pinned.atlas_generation.generation,))}
        finally:
            connection.close()

    def test_current_build_rejects_old_projection_without_invalidating_old_ticket_reads(self):
        with mock.patch.object(relations, 'RELATIONSHIP_EXTRACTOR_VERSION', 'legacy-http'):
            old = self.refresh()
        self.assertTrue(relations._cached_relationships(old, old.atlas_generation))
        self.assertIsNone(relations._cached_relationships(self.settings))
        state = core.load_index_state(self.settings)
        payload = build_atlas(self.settings, state)
        self.assertFalse(any('target_repo' in item['metadata'] for item in payload['edges']))
        self.assertTrue(any(item['edge_type'] == 'EXPOSES_ENDPOINT' for item in payload['edges']))
        components = collect_generation_components(self.settings, state, atlas_payload=payload)
        self.assertEqual('unavailable', components['relationships']['status'])

    def test_warm_relationship_cache_cannot_skip_snapshot_identity_validation(self):
        pinned = self.refresh()
        self.assertTrue(relations._cached_relationships(self.settings))
        path = self.settings.state_dir / 'relationships.json'
        before = path.stat()
        changed = replace(self.settings, repositories=[replace(repo, source_sha='snapshot-two')
                                                       for repo in self.settings.repositories])
        self.assertIsNone(relations._cached_relationships(changed))
        self.assertEqual((before.st_mtime_ns, before.st_size), (path.stat().st_mtime_ns, path.stat().st_size))
        self.assertTrue(relations._cached_relationships(pinned, pinned.atlas_generation))
        for repositories in (self.settings.repositories[:1], [*self.settings.repositories,
                              replace(self.settings.repositories[0], name='new-repository', source_sha='new-snapshot')]):
            altered = replace(self.settings, repositories=repositories)
            self.assertEqual(relations._cached_relationships(pinned, pinned.atlas_generation),
                             relations._cached_relationships(altered, pinned.atlas_generation))

    def test_get_client_reaches_unrestricted_request_mapping_without_workspace_scan(self):
        self.client.write_text('@FeignClient(name="logical-service")\ninterface LedgerPort {\n'
                               ' @GetMapping("/health") Object go();\n}\n', encoding='utf-8')
        self.server.write_text('@RestController\nclass ClearingHttpAdapter {\n'
                               ' @RequestMapping("/health") Object go() { return null; }\n}\n', encoding='utf-8')
        pinned = self.refresh()
        links = relations._cached_relationships(pinned, pinned.atlas_generation)
        self.assertTrue(any(item.kind == 'HTTP' and item.key == 'GET /health'
                            and item.target == 'customer-api' for item in links))

    def test_relationship_removal_cannot_be_resurrected_by_parent_edge_reuse(self):
        old = self.refresh()
        old_edges = self._ids(old, 'generation_edges', 'edge_id')
        state = core.load_index_state(self.settings)
        path = self.settings.state_dir / 'relationships.json'
        projection = json.loads(path.read_text(encoding='utf-8'))
        projection['relationships'] = []
        projection['payload_hash'] = relations._relationship_payload_hash(projection)
        path.write_text(json.dumps(projection), encoding='utf-8')
        payload = build_atlas(self.settings, state)
        self.assertFalse(any('target_repo' in item['metadata'] or 'caller_repo' in item['metadata'] for item in payload['edges']))
        self.assertTrue(any(item['edge_type'] == 'EXPOSES_ENDPOINT' for item in payload['edges']))
        self.assertLess(len(payload['edges']), len(old_edges))
        self.assertEqual(old_edges, self._ids(old, 'generation_edges', 'edge_id'))

    def test_analyzer_failure_preserves_published_generation_and_its_sources(self):
        with mock.patch.object(relations, 'RELATIONSHIP_EXTRACTOR_VERSION', 'legacy-http'):
            old = self.refresh()
        old_artifact = self.settings.state_dir / old.atlas_generation.component('relationships')['artifact_ref']
        old_bytes = old_artifact.read_bytes()
        with mock.patch.object(relations, 'analyze_relationships', side_effect=RuntimeError('HTTP analyzer failed')):
            with self.assertRaisesRegex(RuntimeError, 'HTTP analyzer failed'):
                self.refresh()
        self.assertEqual(old.atlas_generation.identity, current_generation_ref(self.settings).identity)
        self.assertEqual(old_bytes, old_artifact.read_bytes())


if __name__ == '__main__':
    unittest.main()

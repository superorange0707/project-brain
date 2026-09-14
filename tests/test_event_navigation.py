from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from brain import core, investigation
from brain.catalog import current_generation_ref
from brain.retrieval.models import RetrievalTrace


class EventNavigationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ('producer', 'consumer', 'noise'):
            (self.root / name).mkdir()
        (self.root / 'producer/CustomerUpdatedEvent.java').write_text(
            'package demo;\nrecord CustomerUpdatedEvent(String id) {}\n', encoding='utf-8')
        self.publisher = self.root / 'producer/Publisher.java'
        self.publisher.write_text(
            'package demo;\nclass Publisher {\n void publish(CustomerUpdatedEvent event) {\n'
            '  kafkaTemplate.send("customer.updated", event);\n }\n}\n', encoding='utf-8')
        self.consumer = self.root / 'consumer/zz-AuditConsumer.java'
        self.consumer.write_text(
            'package demo;\nclass AuditConsumer {\n @KafkaListener(topics="customer.updated")\n'
            ' void accept(Object payload) { audit.record(payload); }\n}\n', encoding='utf-8')
        config = self.root / 'brain.toml'
        config.write_text("[project]\nname='event-navigation'\n[graph]\nenabled=false\n[experience]\nenabled=false\n" +
                          ''.join(f"[[repositories]]\nname='{name}'\npath='{name}'\n" for name in ('producer', 'consumer', 'noise')),
                          encoding='utf-8')
        self.settings = core.load_settings(config)

    def request(self):
        return {'INVESTIGATION_REQUEST': {
            'version': 5, 'mode': 'flow_trace', 'objective': 'Trace CustomerUpdatedEvent class and all consumers',
            'required': ['relationships'], 'anchors': [{'kind': 'event', 'value': 'CustomerUpdatedEvent'}],
        }}

    def test_event_payload_reaches_topic_only_consumer_despite_card_noise(self):
        for number in range(230):
            (self.root / f'noise/Noise{number:03}.java').write_text(
                f'class Noise{number} {{ String topic = "customer.updated"; }}\n', encoding='utf-8')
        core.snapshot_indexes(self.settings)
        generation = current_generation_ref(self.settings)
        pinned = replace(self.settings, atlas_generation=generation, atlas_generation_mode='pinned')
        bundle = core.retrieve_context(pinned, core.parse_context_request(json.dumps(self.request())))
        content = core.pack_context(pinned, 'EVENT', 1, bundle)
        self.assertIn('audit.record(payload);', content,
                      [(item.repo, item.path) for item in bundle.evidence])
        self.assertIn('kafkaTemplate.send("customer.updated", event);', content)

    def test_eight_repo_event_query_retains_topic_only_consumer_in_noisy_repo(self):
        from tests.test_investigation_v1 import InvestigationV1Tests

        case = InvestigationV1Tests(methodName='runTest')
        case.setUp()
        self.addCleanup(case.tearDown)
        root = case.root / 'rules-engine/src/main/java/demo'
        (root / 'zz-AuditConsumer.java').write_text(
            'package demo;\nclass AuditConsumer {\n @KafkaListener(topics="customer.updated")\n'
            ' void accept(Object payload) { audit(payload); }\n}\n', encoding='utf-8')
        for number in range(230):
            (root / f'NoiseConsumer{number:03}.java').write_text(
                f'package demo; class NoiseConsumer{number:03} {{ String topic = "customer.updated"; '
                'void note() { /* customer updated */ } }\n', encoding='utf-8')
        generation = case.publish('sha-event-topic-recall', 'ETR')
        pinned = replace(case.settings, atlas_generation=generation, atlas_generation_mode='pinned')
        bundle = core.retrieve_context(pinned, core.parse_context_request(json.dumps(self.request())))
        content = core.pack_context(pinned, 'EVENT-NOISE', 1, bundle)
        self.assertIn('audit(payload);', content, [(item.repo, item.path) for item in bundle.evidence])
        self.assertNotEqual('coverage_satisfied', bundle.trace['stop_reason'])
        self.assertTrue(any('payload association' in warning for warning in bundle.warnings))

    def test_topic_pair_requires_exact_case_literal_and_not_pattern_or_expression(self):
        cases = [
            ('"Orders.Created"', 'topics="orders.created"', False),
            ('"orders-.*"', 'topicPattern="orders-.*"', False),
            ('TOPIC', 'topics="TOPIC"', False),
            ('"orders" + suffix', 'topics="orders"', False),
            ('"orders"', 'topics="orders" + suffix', False),
            ('"orders"', 'topics="orders"', True),
        ]
        for outbound, inbound, verified in cases:
            with self.subTest(outbound=outbound, inbound=inbound):
                source = 'class Publisher { void publish() { kafkaTemplate.send(' + outbound + ', event); } }\n'
                target = 'class Consumer { @KafkaListener(' + inbound + ') void consume() {} }\n'
                self.publisher.write_text(source, encoding='utf-8')
                self.consumer.write_text(target, encoding='utf-8')
                core.snapshot_indexes(self.settings)
                generation = current_generation_ref(self.settings)
                bundle = core.ContextBundle('Trace topics', atlas_generation=generation, evidence=[
                    core.Evidence('producer', 'Publisher.java', 1, 1, source, 'code', 100, verification_content=source),
                    core.Evidence('consumer', 'zz-AuditConsumer.java', 1, 1, target, 'code', 100, verification_content=target),
                ])
                runtime = investigation.build_ticket_runtime(
                    self.settings, generation, {'objective': 'Trace topics', 'anchors': [
                        {'kind': 'topic', 'value': outbound.split(' +')[0].strip('"')}]},
                    bundle, {'coverage_map': {}, 'stable_identities': {}}, context_id='CTX-001', next_best_evidence=None)
                self.assertTrue(runtime['integration_flow']['steps'])
                self.assertEqual(verified, runtime['coverage'].get('cross_repo_integration') == 'verified', runtime['integration_flow'])

    def test_topic_source_verification_is_case_direction_location_and_byte_bounded(self):
        content = 'class Consumer {\n @KafkaListener(\n topics = {"orders", "Billing"},\n groupId="group")\n void consume() {}\n}\n'
        self.consumer.write_text(content, encoding='utf-8')
        core.snapshot_indexes(self.settings)
        generation = current_generation_ref(self.settings)
        evidence = core.Evidence('consumer', 'zz-AuditConsumer.java', 1, 6, content, 'code', 100, verification_content=content)
        bundle = core.ContextBundle('topic', atlas_generation=generation, evidence=[evidence])
        with investigation.source_verification_scope():
            def verify(value='orders', line=2, direction='inbound', selected=bundle):
                return investigation._verified_value_location(selected, 'consumer', 'zz-AuditConsumer.java', line,
                                                              value, kind='topic', direction=direction)
            self.assertTrue(verify())
            self.assertTrue(verify('Billing', 3))
            self.assertFalse(verify('billing'))
            self.assertFalse(verify(direction='outbound'))
            self.assertFalse(verify(line=4))
            self.assertFalse(verify(selected=replace(bundle, evidence=[replace(evidence, line_end=2)])))
            self.assertFalse(verify(selected=replace(bundle, atlas_generation=None)))
            self.assertFalse(verify(selected=replace(bundle, evidence=[replace(evidence, kind='knowledge')])))
        self.assertEqual(frozenset(), investigation._literal_java_topics('//' + 'x' * investigation.MAX_REFRESH_FILE_BYTES + content))
        for source in ('// @KafkaListener(topics="orders")\nclass C {}',
                       'class C { String example = "@KafkaListener(topics=\\"orders\\")"; }',
                       'class C { @KafkaListener(id="""\n, topics="orders"\n""") void consume() {} }',
                       'class C { @KafkaListener(topicPattern="orders") void consume() {} }',
                       'class C { @KafkaListener(topics="${orders}") void consume() {} }'):
            self.assertEqual(frozenset(), investigation._literal_java_topics(source))

    def test_kotlin_literal_topics_remain_verifiable_without_promoting_nested_comments(self):
        source = 'class Consumer {\n @KafkaListener(topics=["orders", "billing"])\n fun accept(value: String) {}\n}\n'
        self.consumer.with_suffix('.kt').write_text(source, encoding='utf-8')
        core.snapshot_indexes(self.settings)
        generation = current_generation_ref(self.settings)
        evidence = core.Evidence('consumer', 'zz-AuditConsumer.kt', 1, 4, source, 'code', 100, verification_content=source)
        bundle = core.ContextBundle('orders', atlas_generation=generation, evidence=[evidence])
        with investigation.source_verification_scope():
            self.assertTrue(investigation._verified_value_location(bundle, 'consumer', 'zz-AuditConsumer.kt', 2,
                                                                  'orders', kind='topic', direction='inbound'))
            self.assertFalse(investigation._verified_value_location(bundle, 'consumer', 'zz-AuditConsumer.kt', 2,
                                                                   'orders', kind='topic', direction='outbound'))
        nested = '/* outer /* inner */ @KafkaListener(topics=["fake"]) */\n' + source
        self.assertEqual({'orders', 'billing'}, {row[0] for row in investigation._literal_java_topics(nested, kotlin=True)})
        interpolated = 'val example = "${call(\"text\")}"\n' + source
        self.assertEqual(frozenset(), investigation._literal_java_topics(interpolated, kotlin=True))

    def test_topic_lookup_is_bounded_uses_pinned_sources_and_reports_failure(self):
        core.snapshot_indexes(self.settings)
        generation = current_generation_ref(self.settings)
        pinned = replace(self.settings, atlas_generation=generation, atlas_generation_mode='pinned')
        anchors = self.request()['INVESTIGATION_REQUEST']['anchors']
        hits = [core.SearchHit('producer', 'Publisher.java', 3, 'void publish(CustomerUpdatedEvent event)',
                               'code', 100, ['lexical anchor 1'])]
        self.publisher.write_text('class Publisher { void changed() {} }', encoding='utf-8')
        self.consumer.write_text('class Changed {}', encoding='utf-8')
        cache = {}
        with mock.patch.object(investigation, 'resolve_runtime_anchors', wraps=investigation.resolve_runtime_anchors) as resolve:
            peers, reason = core._topic_peer_hits(pinned, anchors, hits, cache)
            self.assertIsNone(reason)
            self.assertIn('zz-AuditConsumer.java', [hit.path for hit in peers])
            self.assertEqual(1, resolve.call_count)
            self.assertEqual([{'kind': 'topic', 'value': 'customer.updated'}], resolve.call_args.args[2])
            self.assertIn('kafkaTemplate.send', cache[('producer', 'Publisher.java')])
        token = core._ACTIVE_RETRIEVAL_TRACE.set(RetrievalTrace(max_physical_backend_operations=0))
        try:
            with mock.patch.object(investigation, 'resolve_runtime_anchors', side_effect=AssertionError('budget bypass')):
                self.assertEqual(([], 'physical operation budget'), core._topic_peer_hits(pinned, anchors, hits, {}))
        finally:
            core._ACTIVE_RETRIEVAL_TRACE.reset(token)
        with mock.patch('brain.index.read_generation_files', return_value=None):
            peers, reason = core._topic_peer_hits(pinned, anchors, hits, {})
            self.assertEqual([], peers)
            self.assertIn('source unavailable', reason)
        missing = replace(generation, components={})
        peers, reason = core._topic_peer_hits(replace(pinned, atlas_generation=missing),
                                              [{'kind': 'topic', 'value': 'customer.updated'}], [], {})
        self.assertEqual([], peers)
        self.assertIn('unavailable', reason)
        with mock.patch.object(investigation, 'resolve_runtime_anchors', return_value={'status': 'ready', 'candidates': []}):
            peers, reason = core._topic_peer_hits(pinned, [{'kind': 'topic', 'value': 'customer.updated'}], [], {})
            self.assertEqual([], peers)
            self.assertIn('no generation-validated topic', reason)
        connection = investigation.connect(pinned)
        try:
            connection.execute("UPDATE atlas_runtime_anchors SET fingerprint='corrupt' WHERE kind='topic'")
            connection.commit()
        finally:
            connection.close()
        peers, reason = core._topic_peer_hits(pinned, [{'kind': 'topic', 'value': 'customer.updated'}], [], {})
        self.assertEqual([], peers)
        self.assertIn('incompatible', reason)

    def test_topic_peers_remain_on_ticket_generation_after_refresh(self):
        core.snapshot_indexes(self.settings)
        core.start_session(self.settings, 'EVENT-OLD', 'Trace event consumers')
        self.publisher.write_text(self.publisher.read_text().replace('customer.updated', 'customer.revised'), encoding='utf-8')
        self.consumer.write_text(self.consumer.read_text().replace('customer.updated', 'customer.revised').replace(
            'audit.record(payload);', 'audit.revised(payload);'), encoding='utf-8')
        core.snapshot_indexes(self.settings)
        core.start_session(self.settings, 'EVENT-NEW', 'Trace event consumers')
        for ticket, expected, forbidden in [('EVENT-OLD', 'audit.record(payload);', 'audit.revised(payload);'),
                                             ('EVENT-NEW', 'audit.revised(payload);', 'audit.record(payload);')]:
            content, _, _ = core.create_context(self.settings, ticket, json.dumps(self.request()))
            self.assertIn(expected, content)
            self.assertNotIn(forbidden, content)

    def test_request_topic_identity_survives_normalized_lookup_and_retained_candidates(self):
        core.snapshot_indexes(self.settings)
        generation = current_generation_ref(self.settings)
        pinned = replace(self.settings, atlas_generation=generation, atlas_generation_mode='pinned')
        bundle = core.retrieve_context(pinned, core.parse_context_request(json.dumps(self.request())))
        exact = {'objective': 'Trace topic', 'anchors': [{'kind': 'topic', 'value': 'customer.updated'}]}
        state = {'coverage_map': {}, 'stable_identities': {}}
        first = investigation.build_ticket_runtime(pinned, generation, exact, bundle,
                                                    state,
                                                    context_id='CTX-001', next_best_evidence=None)
        self.assertEqual('verified', first['coverage'].get('cross_repo_integration'))
        different_case = {**exact, 'anchors': [{'kind': 'topic', 'value': 'Customer.Updated'}]}
        second = investigation.build_ticket_runtime(pinned, generation, different_case, bundle,
                                                     {'investigation_runtime': first, 'coverage_map': first['coverage'],
                                                      'stable_identities': state['stable_identities']},
                                                     context_id='CTX-002', next_best_evidence=None)
        self.assertNotEqual('verified', second['coverage'].get('cross_repo_integration'))
        self.assertFalse(any(item['state'] == 'verified' for item in second['integration_flow']['steps']
                             if item['kind'] == 'topic'))

    def test_same_file_event_and_topic_are_navigation_not_payload_authority(self):
        self.publisher.write_text('class OtherEvent {}\nclass Publisher { void publish(CustomerUpdatedEvent event) {}\n'
                                  'void unrelated(OtherEvent other) { kafkaTemplate.send("unrelated", other); } }\n', encoding='utf-8')
        self.consumer.write_text('class Consumer { @KafkaListener(topics="unrelated") void accept(Object value) {} }\n', encoding='utf-8')
        core.snapshot_indexes(self.settings)
        generation = current_generation_ref(self.settings)
        pinned = replace(self.settings, atlas_generation=generation, atlas_generation_mode='pinned')
        request = core.parse_context_request(json.dumps(self.request()))
        bundle = core.retrieve_context(pinned, request)
        runtime = investigation.build_ticket_runtime(pinned, generation, request, bundle,
                                                     {'coverage_map': {}, 'stable_identities': {}},
                                                     context_id='CTX-001', next_best_evidence=None)
        self.assertTrue(any('payload association' in warning for warning in bundle.warnings))
        self.assertNotEqual('verified', runtime['coverage'].get('cross_repo_integration'))


if __name__ == '__main__':
    unittest.main()

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


class ConfigNavigationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'config').mkdir()
        (self.root / 'service').mkdir()
        (self.root / 'config/application.properties').write_text('billing.auth.feature.enabled=true\n', encoding='utf-8')
        self.path = self.root / 'service/zz-RuntimeSettings.java'
        self.path.write_text('@ConfigurationProperties(prefix="billing.auth.feature")\n'
                             'class RuntimeSettings { boolean enabled; }\n', encoding='utf-8')
        config = self.root / 'brain.toml'
        config.write_text("[project]\nname='config-navigation'\n[graph]\nenabled=false\n[experience]\nenabled=false\n"
                          "[[repositories]]\nname='config'\npath='config'\n[[repositories]]\nname='service'\npath='service'\n",
                          encoding='utf-8')
        self.settings = core.load_settings(config)

    def request(self, key='billing.auth.feature.enabled'):
        return {'INVESTIGATION_REQUEST': {
            'version': 5, 'mode': 'root_cause', 'objective': f'Trace {key} ConfigurationProperties member',
            'required': ['configuration'], 'anchors': [{'kind': 'config_key', 'value': key}],
        }}

    def test_config_prefix_owner_survives_card_noise_without_claiming_member_binding(self):
        for number in range(230):
            (self.root / f'service/Noise{number:03}.java').write_text(
                f'@ConfigurationProperties(prefix="billing.auth.feature.noise{number:03}")\n'
                f'class Noise{number} {{ boolean irrelevant; }}\n', encoding='utf-8')
        core.snapshot_indexes(self.settings)
        generation = current_generation_ref(self.settings)
        pinned = replace(self.settings, atlas_generation=generation, atlas_generation_mode='pinned')
        bundle = core.retrieve_context(pinned, core.parse_context_request(json.dumps(self.request())))
        content = core.pack_context(pinned, 'CONFIG', 1, bundle)
        self.assertIn('boolean enabled;', content)
        self.assertTrue(any(item.path == 'zz-RuntimeSettings.java' and 'prefix' in item.kind for item in bundle.evidence))
        self.assertTrue(any('binding' in warning and 'not' in warning for warning in bundle.warnings))

    def test_old_ticket_keeps_configuration_source_and_missing_member_is_not_proven(self):
        core.snapshot_indexes(self.settings)
        core.start_session(self.settings, 'CONFIG-OLD', 'Inspect configuration binding')
        self.path.write_text('@ConfigurationProperties(prefix="billing.auth.feature")\n'
                             'class RuntimeSettings { boolean differentMember; }\n', encoding='utf-8')
        core.snapshot_indexes(self.settings)
        core.start_session(self.settings, 'CONFIG-NEW', 'Inspect configuration binding')
        for ticket, expected, forbidden in (('CONFIG-OLD', 'boolean enabled;', 'boolean differentMember;'),
                                             ('CONFIG-NEW', 'boolean differentMember;', 'boolean enabled;')):
            content, _, _ = core.create_context(self.settings, ticket, json.dumps(self.request()))
            self.assertIn(expected, content)
            self.assertNotIn(forbidden, content)
            self.assertIn('not established by a prefix match', content)

    def test_prefix_lookup_uses_one_bounded_resolver_and_keeps_failure_explicit(self):
        core.snapshot_indexes(self.settings)
        generation = current_generation_ref(self.settings)
        pinned = replace(self.settings, atlas_generation=generation, atlas_generation_mode='pinned')
        anchors = [{'kind': 'config_key', 'value': 'billing.auth.feature.enabled'}]
        with mock.patch.object(investigation, 'resolve_runtime_anchors', wraps=investigation.resolve_runtime_anchors) as resolve:
            hits, reason = core._configuration_prefix_hits(pinned, anchors)
            self.assertIsNone(reason)
            self.assertEqual(['zz-RuntimeSettings.java'], [hit.path for hit in hits])
            self.assertEqual(1, resolve.call_count)
            self.assertEqual(['billing.auth.feature', 'billing.auth', 'billing'],
                             [item['value'] for item in resolve.call_args.args[2]])
        token = core._ACTIVE_RETRIEVAL_TRACE.set(RetrievalTrace(max_physical_backend_operations=0))
        try:
            with mock.patch.object(investigation, 'resolve_runtime_anchors', side_effect=AssertionError('budget bypass')):
                self.assertEqual(([], 'physical operation budget'), core._configuration_prefix_hits(pinned, anchors))
        finally:
            core._ACTIVE_RETRIEVAL_TRACE.reset(token)
        missing = replace(generation, components={})
        hits, reason = core._configuration_prefix_hits(replace(pinned, atlas_generation=missing), anchors)
        self.assertEqual([], hits)
        self.assertIn('unavailable', reason)
        connection = investigation.connect(pinned)
        try:
            connection.execute("UPDATE atlas_runtime_anchors SET fingerprint='corrupt' WHERE kind='config_key' AND path='zz-RuntimeSettings.java'")
            connection.commit()
        finally:
            connection.close()
        hits, reason = core._configuration_prefix_hits(pinned, anchors)
        self.assertEqual([], hits)
        self.assertIn('incompatible', reason)


if __name__ == '__main__':
    unittest.main()

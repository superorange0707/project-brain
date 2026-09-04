from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from brain.agent import archive_final_solution, create_m365_agent_kit, response_preview
from brain.atlas import _file_intelligence, _prioritized_routing_terms, build_atlas, route
from brain.catalog import collect_generation_components, current_generation_ref, publish_generation
from brain.core import (
    BrainError,
    ContextBundle,
    Evidence,
    _publish_first_useful_checkpoint,
    add_external_evidence,
    create_context,
    create_feedback,
    load_settings,
    parse_context_request,
    prefetch_ticket,
    protocol_request_signature,
    request_preview,
    retrieve_context,
    search,
    session_state,
    snapshot_indexes,
    start_session,
)
from brain.index import write_state
from brain.evaluation import _peak_rss_mb, evaluate_golden, evaluate_m365_response
from brain.investigation import (
    HARD_MAX_WAVES,
    MAX_ANCHOR_CANDIDATES,
    MAX_REFRESH_FILE_BYTES,
    build_ticket_runtime,
    build_generation_intelligence,
    resolve_runtime_anchors,
    _exact_evidence_anchors,
    _allocate,
    _boolean_assignments,
    _bounded_anchor_queries,
    _config_file_intelligence,
    _execution_flow,
    _flow_cache_identity,
    _hypotheses,
    _integration_flow,
    _java_file_intelligence,
    _program_slice,
    _surfaces,
    validate_stable_identity_registry,
    _verified_stack_frame,
    _verified_value_location,
)
from brain.retrieval import compile_request
from brain.ops import gc, refresh_brain
from brain.platforms import native_command
from brain.semantic import (
    ATLAS_CARD_VERSION,
    CARD_VERSION,
    CHUNK_SCHEMA_VERSION,
    SEMANTIC_EMBEDDING_INPUT_VERSION,
    SEMANTIC_SHARD_MANIFEST_VERSION,
    build_semantic_index,
    search_semantic,
)
from brain.ui import _session_artifacts


class InvestigationV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        api = self.root / "customer-api"
        client = self.root / "customer-client"
        events = self.root / "customer-events"
        eligibility = self.root / "eligibility-service"
        rules = self.root / "rules-engine"
        configuration = self.root / "configuration-service"
        contracts = self.root / "shared-contracts"
        support = self.root / "test-support"
        (api / "src/main/java/demo").mkdir(parents=True)
        (api / "src/test/java/demo").mkdir(parents=True)
        (client / "src/main/java/demo").mkdir(parents=True)
        (events / "src/main/java/demo").mkdir(parents=True)
        (events / "src/main/resources").mkdir(parents=True)
        for repository in (eligibility, rules, configuration, contracts, support):
            (repository / "src/main/java/demo").mkdir(parents=True)
        (api / "src/main/java/demo/CustomerController.java").write_text(
            """package demo;
import org.springframework.web.bind.annotation.*;
@RestController
@RequestMapping("/customers")
class CustomerController {
  private final CustomerService service;
  @GetMapping("/{id}") Customer find(String id) {
    if (id == null) throw new IllegalArgumentException("id");
    return service.find(id);
  }
}
class CustomerService { Customer find(String id) { return repository.findById(id).orElseThrow(); } }
@Entity @Table(name="customers", schema="customer") class Customer { String id; }
interface CustomerRepository extends JpaRepository<Customer, String> {}
""",
            encoding="utf-8",
        )
        (api / "src/test/java/demo/CustomerControllerTest.java").write_text(
            """class CustomerControllerTest {
  MockMvc mvc;
  void getsCustomer() { mvc.perform(get("/customers/{id}")); verify(service).find("1"); }
}
""",
            encoding="utf-8",
        )
        (client / "src/main/java/demo/CustomerClient.java").write_text(
            """package demo;
@FeignClient(name="customer-api", url="${customer.api.url}")
interface CustomerClient { @GetMapping("/customers/{id}") Customer find(String id); }
""",
            encoding="utf-8",
        )
        (events / "src/main/java/demo/CustomerEvents.java").write_text(
            """package demo;
record CustomerUpdatedEvent(String id) {}
class CustomerPublisher { void publish(CustomerUpdatedEvent event) { kafkaTemplate.send("customer.updated", event); } }
class CustomerListener { @KafkaListener(topics="customer.updated") void consume(CustomerUpdatedEvent event) { service.find(event.id()); } }
""",
            encoding="utf-8",
        )
        (events / "src/main/resources/application.properties").write_text(
            "customer.api.url=http://customer-api\nspring.kafka.consumer.group-id=customer-events\nfeature.customer.enabled=true\n",
            encoding="utf-8",
        )
        (events / "src/main/resources/application.yml").write_text(
            "spring:\n  kafka:\n    consumer:\n      group-id: customer-events\n",
            encoding="utf-8",
        )
        (events / "src/main/resources/settings.toml").write_text(
            "[spring.datasource]\nurl = 'jdbc:postgresql://customer'\n",
            encoding="utf-8",
        )
        (events / "src/main/resources/beans.xml").write_text(
            "<beans><property name=\"not.a.spring.key\" value=\"wrong\"/></beans>\n",
            encoding="utf-8",
        )
        (events / "src/main/java/demo/CustomerEvents.java").write_text(
            """package demo;
record CustomerUpdatedEvent(String id) {}
class CustomerPublisher { void publish(CustomerUpdatedEvent event) { kafkaTemplate.send("customer.updated", event); } }
class CustomerListener {
  @KafkaListener(topics={"customer.updated"}, groupId="billing")
  @Cacheable(cacheNames="customers", key="#id", condition="#id != null")
  void consume(CustomerUpdatedEvent event) { service.find(event.id()); }
}
""",
            encoding="utf-8",
        )
        (eligibility / "src/main/java/demo/EligibilityListener.java").write_text(
            "class EligibilityListener { @KafkaListener(topics=\"customer.updated\") void on(CustomerUpdatedEvent event) { rules.evaluate(event.id()); } }\n",
            encoding="utf-8",
        )
        (rules / "src/main/java/demo/EligibilityRules.java").write_text(
            "class EligibilityRules { boolean evaluate(String id) { return id != null; } }\n",
            encoding="utf-8",
        )
        (configuration / "src/main/java/demo/CustomerConfiguration.java").write_text(
            "@ConfigurationProperties(\"feature.customer\") class CustomerConfiguration { boolean enabled; }\n",
            encoding="utf-8",
        )
        (contracts / "src/main/java/demo/CustomerUpdatedEvent.java").write_text(
            "package demo; record CustomerUpdatedEvent(String id) {}\n",
            encoding="utf-8",
        )
        (support / "src/main/java/demo/CustomerFixtures.java").write_text(
            "class CustomerFixtures { static String customerId() { return \"customer-1\"; } }\n",
            encoding="utf-8",
        )
        self.config = self.root / "brain.toml"
        self.config.write_text(
            "[project]\nname='v1-investigation'\n[graph]\nenabled=false\n[experience]\nenabled=false\n"
            "[[repositories]]\nname='customer-api'\npath='customer-api'\n"
            "[[repositories]]\nname='customer-client'\npath='customer-client'\n"
            "[[repositories]]\nname='customer-events'\npath='customer-events'\n"
            "[[repositories]]\nname='eligibility-service'\npath='eligibility-service'\n"
            "[[repositories]]\nname='rules-engine'\npath='rules-engine'\n"
            "[[repositories]]\nname='configuration-service'\npath='configuration-service'\n"
            "[[repositories]]\nname='shared-contracts'\npath='shared-contracts'\n"
            "[[repositories]]\nname='test-support'\npath='test-support'\n",
            encoding="utf-8",
        )
        self.settings = load_settings(self.config)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def embed(cards: list[str]) -> list[list[float]]:
        return [[float("customers" in card.casefold()), float("customer.updated" in card.casefold()), 1.0] for card in cards]

    def publish(self, sha: str, marker: str) -> object:
        for repo in self.settings.repositories:
            snapshot = self.settings.state_dir / "snapshots" / repo.name / sha
            if not snapshot.exists():
                shutil.copytree(repo.path, snapshot)
            if repo.name == "customer-api":
                target = snapshot / "src/main/java/demo/CustomerController.java"
                if marker not in target.read_text(encoding="utf-8"):
                    target.write_text(target.read_text(encoding="utf-8") + f"\n// {marker}\n", encoding="utf-8")
            repo.source_path = snapshot
            repo.source_sha = sha
            repo.source_ref = "refs/heads/main"
            repo.source_status = "current"
        state, _ = snapshot_indexes(self.settings, changed_only=True, publish=False)
        atlas = build_atlas(self.settings, state)
        build_semantic_index(
            self.settings, embed=self.embed, pack_id="v1-test-pack", atlas_cards=atlas["cards"],
        )
        components = collect_generation_components(self.settings, state, atlas_payload=atlas)
        manifest = publish_generation(self.settings, state, components=components, atlas_payload=atlas)
        for value in state.values():
            if isinstance(value, dict):
                value["generation"] = manifest["generation"]
        write_state(self.settings, state)
        generation = current_generation_ref(self.settings)
        self.assertIsNotNone(generation)
        return generation

    @staticmethod
    def request(objective: str, *, base: str | None = None, wave: int | None = None) -> str:
        value = {
            "version": 5,
            "mode": "root_cause",
            "objective": objective,
            "runtime_facts": [],
            "hypotheses": ["CustomerController may delegate to CustomerService"],
            "required": ["main execution flow", "tests", "cross repo integration"],
            "resolve": ["CustomerController", "/customers/{id}", "customer.updated"],
            "anchors": [
                {"kind": "symbol", "value": "CustomerController"},
                {"kind": "stack_frame", "value": "demo.CustomerController.find(CustomerController.java:8)"},
                {"kind": "endpoint", "value": "/customers/{id}"},
                {"kind": "topic", "value": "customer.updated"},
            ],
            "base_context_id": base,
            "checkpoint": base is None,
            "wave": wave,
        }
        return json.dumps({"INVESTIGATION_REQUEST": value})

    def test_generation_components_java_spring_extraction_and_incremental_reuse(self) -> None:
        generation = self.publish("sha-g1", "G1_ONLY")
        self.assertEqual("ready", generation.component("runtime_anchors")["status"])
        self.assertEqual("ready", generation.component("java_intelligence")["status"])

        self.assertEqual("runtime-anchor-v3", generation.component("runtime_anchors")["schema_version"])
        self.assertEqual("java-spring-v3", generation.component("java_intelligence")["schema_version"])

        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            kinds = {row[0] for row in connection.execute(
                "SELECT DISTINCT f.kind FROM generation_integration_facts g "
                "JOIN atlas_integration_facts f ON f.fact_id=g.fact_id WHERE g.generation=?",
                (generation.generation,),
            )}
            counts = connection.execute(
                "SELECT (SELECT COUNT(*) FROM generation_runtime_anchors WHERE generation=?),"
                "(SELECT COUNT(*) FROM generation_integration_facts WHERE generation=?)",
                (generation.generation, generation.generation),
            ).fetchone()
            facts = {(row[0], row[1]) for row in connection.execute(
                "SELECT f.kind,f.key_value FROM generation_integration_facts g "
                "JOIN atlas_integration_facts f ON f.fact_id=g.fact_id WHERE g.generation=?",
                (generation.generation,),
            )}
            endpoint_owners = list(connection.execute(
                "SELECT f.key_value,e.kind,parent.kind,parent.simple_name FROM generation_integration_facts g "
                "JOIN atlas_integration_facts f ON f.fact_id=g.fact_id "
                "LEFT JOIN atlas_entities e ON e.entity_id=f.entity_id "
                "LEFT JOIN atlas_entities parent ON parent.entity_id=e.parent_entity_id "
                "WHERE g.generation=? AND f.kind='endpoint' AND f.key_value='/customers/{id}'",
                (generation.generation,),
            ))
        finally:
            connection.close()
        self.assertTrue({"endpoint", "topic", "config_key", "table", "schema", "persistence_entity", "test_reference"}.issubset(kinds))
        self.assertGreater(counts[0], 0)
        self.assertGreater(counts[1], 0)
        self.assertIn(("topic", "customer.updated"), facts)
        self.assertNotIn(("topic", "billing"), facts)
        self.assertIn(("cache", "customers"), facts)
        self.assertNotIn(("cache", "#id"), facts)
        self.assertNotIn(("cache", "#id != null"), facts)
        self.assertIn(("config_key", "spring.kafka.consumer.group-id"), facts)
        self.assertIn(("config_key", "spring.datasource.url"), facts)
        self.assertNotIn(("config_key", "not.a.spring.key"), facts)
        self.assertTrue(any(
            kind == "endpoint" and parent_kind == "method" and name == "find"
            for _, kind, parent_kind, name in endpoint_owners
        ))

        typed = resolve_runtime_anchors(
            self.settings, generation, [{"kind": "event", "value": "CustomerUpdatedEvent"}],
        )
        self.assertTrue(typed["candidates"])
        self.assertEqual({"event"}, {item["kind"] for item in typed["candidates"]})

        resolved = resolve_runtime_anchors(
            self.settings, generation, ["CustomerController", "/customers/{id}", "customer.updated"],
        )
        self.assertEqual("ready", resolved["status"])
        self.assertFalse(resolved["cache_hit"])
        self.assertLessEqual(len(resolved["candidates"]), MAX_ANCHOR_CANDIDATES)
        self.assertTrue(any(item["repo"] == "customer-api" for item in resolved["candidates"]))
        self.assertTrue(any(item["repo"] == "customer-events" for item in resolved["candidates"]))
        cached = resolve_runtime_anchors(
            self.settings, generation, ["CustomerController", "/customers/{id}", "customer.updated"],
        )
        self.assertTrue(cached["cache_hit"])
        self.assertEqual(resolved["candidates"], cached["candidates"])
        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            connection.execute(
                "UPDATE atlas_retrieval_cache SET payload_json='not-json' WHERE generation=?",
                (generation.generation,),
            )
            connection.commit()
        finally:
            connection.close()
        recovered = resolve_runtime_anchors(
            self.settings, generation, ["CustomerController", "/customers/{id}", "customer.updated"],
        )
        self.assertFalse(recovered["cache_hit"])
        self.assertEqual(resolved["candidates"], recovered["candidates"])
        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            row = next(
                candidate for candidate in connection.execute(
                    "SELECT cache_key,payload_json FROM atlas_retrieval_cache WHERE generation=?",
                    (generation.generation,),
                )
                if candidate[1].startswith("{")
            )
            poisoned = json.loads(row[1])
            poisoned["generation"] = "not-a-generation"
            if poisoned["candidates"]:
                poisoned["candidates"][0]["repo"] = "poisoned-repo"
            connection.execute(
                "UPDATE atlas_retrieval_cache SET payload_json=? WHERE generation=? AND cache_key=?",
                (json.dumps(poisoned), generation.generation, row[0]),
            )
            connection.commit()
        finally:
            connection.close()
        safe = resolve_runtime_anchors(
            self.settings, generation, ["CustomerController", "/customers/{id}", "customer.updated"],
        )
        self.assertFalse(safe["cache_hit"])
        self.assertFalse(any(item["repo"] == "poisoned-repo" for item in safe["candidates"]))

        exact = resolve_runtime_anchors(self.settings, generation, ["CustomerController"])
        self.assertTrue(exact["candidates"])
        resolve_runtime_anchors(self.settings, generation, ["CustomerController"])
        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            cache_rows = list(connection.execute(
                "SELECT cache_key,payload_json FROM atlas_retrieval_cache WHERE generation=?",
                (generation.generation,),
            ))
            cache_key, payload_json = next(
                row for row in cache_rows
                if row[1].startswith("{")
                and (payload := json.loads(row[1])).get("inputs") == ["CustomerController"]
            )
            payload = json.loads(payload_json)
            existing_ids = {item["identity"] for item in payload["candidates"]}
            all_anchor_ids = {
                row[0] for row in connection.execute(
                    "SELECT anchor_id FROM generation_runtime_anchors WHERE generation=?",
                    (generation.generation,),
                )
            }
            unrelated = next(iter(all_anchor_ids - existing_ids))
            self.assertNotIn(unrelated, existing_ids)
            payload["candidates"][0]["identity"] = unrelated
            payload["candidates"][0]["method"] = "exact"
            connection.execute(
                "UPDATE atlas_retrieval_cache SET payload_json=? WHERE generation=? AND cache_key=?",
                (json.dumps(payload), generation.generation, cache_key),
            )
            connection.commit()
        finally:
            connection.close()
        repaired = resolve_runtime_anchors(self.settings, generation, ["CustomerController"])
        self.assertFalse(repaired["cache_hit"])
        self.assertNotIn(unrelated, {item["identity"] for item in repaired["candidates"]})

        state, _ = snapshot_indexes(self.settings, changed_only=True, publish=False)
        repeated = build_atlas(self.settings, state)
        self.assertEqual(0, repeated["v1_build"]["parsed_files"])
        self.assertGreater(repeated["v1_build"]["reused_files"], 0)
        components = collect_generation_components(self.settings, state, atlas_payload=repeated)
        reused_manifest = publish_generation(self.settings, state, components=components, atlas_payload=repeated)
        self.assertEqual(generation.identity, reused_manifest["identity"])

        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            connection.execute(
                "UPDATE generation_components SET schema_version='obsolete' "
                "WHERE generation=? AND component='java_intelligence'",
                (generation.generation,),
            )
            connection.commit()
        finally:
            connection.close()
        incompatible_parent = build_atlas(self.settings, state)
        self.assertGreater(incompatible_parent["v1_build"]["parsed_files"], 0)

    def test_poisoned_shared_terms_cannot_be_resealed_by_an_unrelated_generation(self) -> None:
        from brain.atlas import ATLAS_SCHEMA_VERSION, _hash

        metadata = {"subject": "ABC-1 update customer contract"}
        values = (
            "customer-api", "c" * 40, "2026-01-01T00:00:00+00:00", "ABC-1",
            "src/main/java/demo/CustomerController.java", "", "modified", 1, 1,
        )
        change = {
            "change_id": _hash(
                "change", ATLAS_SCHEMA_VERSION, *values, json.dumps(metadata, sort_keys=True),
            ),
            "repo": values[0], "commit_sha": values[1], "committed_at": values[2],
            "ticket": values[3], "path": values[4], "old_path": None,
            "status": values[6], "additions": values[7], "deletions": values[8],
            "metadata": metadata,
        }
        omitted = {
            "git_failures": 0, "oversized_paths": 0, "row_limit_reached": 0,
            "budget_exhausted_repos": 0, "operations": 0, "output_bytes": 0,
        }
        with mock.patch("brain.atlas._change_rows", return_value=([change], omitted)):
            generation = self.publish("sha-projection-one", "projection base")
        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            card_id = connection.execute(
                "SELECT c.card_id FROM generation_cards g JOIN atlas_cards c ON c.card_id=g.card_id "
                "WHERE g.generation=? AND c.repo='test-support' AND c.level='repo' LIMIT 1",
                (generation.generation,),
            ).fetchone()[0]
            anchor_id = connection.execute(
                "SELECT a.anchor_id FROM generation_runtime_anchors g "
                "JOIN atlas_runtime_anchors a ON a.anchor_id=g.anchor_id "
                "WHERE g.generation=? AND a.repo='customer-client' LIMIT 1",
                (generation.generation,),
            ).fetchone()[0]
        finally:
            connection.close()

        cases = (
            ("card", "atlas_card_terms", "card_id", card_id, "atlas-card-terms-v1"),
            ("change", "atlas_change_terms", "change_id", change["change_id"], "atlas-change-terms-v1"),
            ("anchor", "atlas_runtime_anchor_terms", "anchor_id", anchor_id, "runtime-anchor-terms-v1"),
        )
        for kind, table, identity_column, identity, schema in cases:
            with self.subTest(kind=kind):
                connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
                try:
                    connection.execute(
                        f"INSERT INTO {table}({identity_column},schema_version,term) VALUES (?,?,?)",
                        (identity, schema, f"forged-{kind}-term"),
                    )
                    connection.commit()
                finally:
                    connection.close()
                with mock.patch("brain.atlas._change_rows", return_value=([change], omitted)):
                    with self.assertRaisesRegex(RuntimeError, f"Atlas {kind} routing projection"):
                        self.publish("sha-projection-two", "unrelated source change")
                connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
                try:
                    connection.execute(
                        f"DELETE FROM {table} WHERE {identity_column}=? AND schema_version=? AND term=?",
                        (identity, schema, f"forged-{kind}-term"),
                    )
                    connection.commit()
                finally:
                    connection.close()

    def test_runtime_anchor_term_projection_and_warm_cache_are_physically_bounded(self) -> None:
        from brain.investigation import RUNTIME_ANCHOR_TERM_SCHEMA_VERSION

        generation = self.publish("sha-anchor-index", "ANCHOR_INDEX")
        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            term = connection.execute(
                "SELECT term FROM atlas_runtime_anchor_terms WHERE schema_version=? ORDER BY term LIMIT 1",
                (RUNTIME_ANCHOR_TERM_SCHEMA_VERSION,),
            ).fetchone()[0]
            plan = " ".join(str(row[3]) for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT a.anchor_id FROM atlas_runtime_anchor_terms t "
                "JOIN generation_runtime_anchors g ON g.anchor_id=t.anchor_id "
                "JOIN atlas_runtime_anchors a ON a.anchor_id=g.anchor_id "
                "WHERE g.generation=? AND t.schema_version=? AND t.term=? LIMIT 8",
                (generation.generation, RUNTIME_ANCHOR_TERM_SCHEMA_VERSION, term),
            ))
        finally:
            connection.close()
        self.assertIn("atlas_runtime_anchor_terms_lookup", plan)

        cold = resolve_runtime_anchors(self.settings, generation, ["CustomerController"])
        warm = resolve_runtime_anchors(self.settings, generation, ["CustomerController"])
        self.assertFalse(cold["cache_hit"])
        self.assertTrue(warm["cache_hit"])
        self.assertLess(warm["database_operations"], cold["database_operations"])

        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            connection.execute(
                "DELETE FROM generation_runtime_anchor_indexes WHERE generation=?",
                (generation.generation,),
            )
            connection.commit()
        finally:
            connection.close()
        degraded = resolve_runtime_anchors(self.settings, generation, ["CustomerController"])
        self.assertEqual("degraded", degraded["status"])
        self.assertIn("term projection", degraded["reason"])

    def test_prefetch_and_retained_anchor_priors_revalidate_full_content_identity(self) -> None:
        generation = self.publish("sha-anchor-prior", "ANCHOR_PRIOR")
        start_session(self.settings, "ANCHOR-PRIOR", "CustomerController")
        prefetch = session_state(self.settings, "ANCHOR-PRIOR")["prefetch"]
        self.assertTrue(prefetch["anchor_ids"])
        anchor_id = str(prefetch["anchor_ids"][0])
        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            original = connection.execute(
                "SELECT normalized FROM atlas_runtime_anchors WHERE anchor_id=?", (anchor_id,),
            ).fetchone()[0]
            connection.execute(
                "UPDATE atlas_runtime_anchors SET normalized='poisoned-anchor-prior' WHERE anchor_id=?",
                (anchor_id,),
            )
            connection.commit()
        finally:
            connection.close()
        request = {"objective": "CustomerController", "_prefetch": prefetch}
        bundle = ContextBundle("CustomerController", atlas_generation=generation)
        poisoned_prefetch = build_ticket_runtime(
            self.settings, generation, request, bundle, {}, context_id="CTX-001",
        )
        self.assertEqual("degraded", poisoned_prefetch["anchors"]["status"])
        self.assertIn("content identity", poisoned_prefetch["anchors"]["reason"])
        self.assertNotIn(anchor_id, {
            item["identity"] for item in poisoned_prefetch["anchors"]["candidates"]
        })

        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            connection.execute(
                "UPDATE atlas_runtime_anchors SET normalized=? WHERE anchor_id=?", (original, anchor_id),
            )
            from brain.catalog import _term_projection_hash
            from brain.investigation import RUNTIME_ANCHOR_TERM_SCHEMA_VERSION

            anchor_count = connection.execute(
                "SELECT COUNT(*) FROM generation_runtime_anchors WHERE generation=?",
                (generation.generation,),
            ).fetchone()[0]
            term_count = connection.execute(
                "SELECT COUNT(*) FROM generation_runtime_anchors g "
                "JOIN atlas_runtime_anchor_terms t ON t.anchor_id=g.anchor_id "
                "WHERE g.generation=? AND t.schema_version=?",
                (generation.generation, RUNTIME_ANCHOR_TERM_SCHEMA_VERSION),
            ).fetchone()[0]
            projection_hash = _term_projection_hash(
                connection, generation.generation, kind="anchor",
                schema_version=RUNTIME_ANCHOR_TERM_SCHEMA_VERSION,
            )
            connection.execute(
                "INSERT INTO generation_runtime_anchor_indexes"
                "(generation,schema_version,anchor_count,term_count,projection_hash) VALUES (?,?,?,?,?)",
                (
                    generation.generation, RUNTIME_ANCHOR_TERM_SCHEMA_VERSION,
                    anchor_count, term_count, projection_hash,
                ),
            )
            connection.commit()
        finally:
            connection.close()
        first_state: dict[str, object] = {}
        first = build_ticket_runtime(
            self.settings, generation, {"objective": "CustomerController"}, bundle, first_state,
            context_id="CTX-001",
        )
        self.assertIn(anchor_id, {item["identity"] for item in first["anchors"]["candidates"]})
        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            connection.execute(
                "UPDATE atlas_runtime_anchors SET normalized='poisoned-retained-prior' WHERE anchor_id=?",
                (anchor_id,),
            )
            connection.commit()
        finally:
            connection.close()
        retained = build_ticket_runtime(
            self.settings, generation, {"objective": "unrelated opaque investigation"},
            ContextBundle("unrelated", atlas_generation=generation),
            {"investigation_runtime": first, "stable_identities": first_state["stable_identities"]},
            context_id="CTX-002",
        )
        self.assertEqual("degraded", retained["anchors"]["status"])
        self.assertIn("content identity", retained["anchors"]["reason"])
        self.assertNotIn(anchor_id, {item["identity"] for item in retained["anchors"]["candidates"]})

    def test_anchor_and_integration_serving_reject_same_id_content_poison(self) -> None:
        generation = self.publish("sha-content-poison", "CONTENT_POISON")
        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            anchor_id = connection.execute(
                "SELECT a.anchor_id FROM generation_runtime_anchors g "
                "JOIN atlas_runtime_anchors a ON a.anchor_id=g.anchor_id "
                "WHERE g.generation=? AND a.normalized='customercontroller' LIMIT 1",
                (generation.generation,),
            ).fetchone()[0]
            connection.execute(
                "UPDATE atlas_runtime_anchors SET method='poisoned-method' WHERE anchor_id=?",
                (anchor_id,),
            )
            fact_id = connection.execute(
                "SELECT f.fact_id FROM generation_integration_facts g "
                "JOIN atlas_integration_facts f ON f.fact_id=g.fact_id "
                "WHERE g.generation=? AND f.normalized='/customers/{id}' LIMIT 1",
                (generation.generation,),
            ).fetchone()[0]
            connection.execute(
                "UPDATE atlas_integration_facts SET framework='poisoned-framework' WHERE fact_id=?",
                (fact_id,),
            )
            connection.commit()
        finally:
            connection.close()

        anchors = resolve_runtime_anchors(
            self.settings, generation, ["CustomerController"], use_cache=False,
        )
        self.assertEqual("degraded", anchors["status"])
        self.assertEqual([], anchors["candidates"])
        self.assertIn("content identity", anchors["reason"])
        integration = _integration_flow(
            self.settings, generation, [{"value": "/customers/{id}"}],
            ContextBundle("poison", atlas_generation=generation),
        )
        self.assertEqual("degraded", integration["status"])
        self.assertEqual([], integration["steps"])
        self.assertIn("content identity", integration["reason"])

    def test_fresh_pinned_search_hashes_only_selected_repo_blobs(self) -> None:
        generation = self.publish("sha-g1", "G1_ONLY")
        pinned = replace(
            self.settings, atlas_generation=generation, atlas_generation_mode="pinned",
        )
        connection = sqlite3.connect(self.settings.state_dir / "search.sqlite3")
        try:
            other_blobs = {str(row[0]) for row in connection.execute(
                "SELECT DISTINCT blob FROM file_membership WHERE repo<>'customer-api' AND snapshot_sha='sha-g1'"
            )}
        finally:
            connection.close()
        from brain import index as search_index
        from brain.core import _lexical_generation_ready
        from brain.index import query_index

        search_index._SNAPSHOT_INTEGRITY_CACHE.clear()
        with mock.patch("brain.index._blob_identity_valid", wraps=search_index._blob_identity_valid) as verified:
            self.assertTrue(_lexical_generation_ready(pinned, pinned.repo("customer-api")))
            self.assertFalse(verified.call_args_list)
            hits = query_index(
                pinned, pinned.repo("customer-api"), "CustomerController", max_results=20,
                snapshot_sha="sha-g1",
            )
        self.assertTrue(hits)
        self.assertTrue(verified.call_args_list)
        self.assertFalse({str(call.args[0]) for call in verified.call_args_list} & other_blobs)

    def test_method_request_mapping_is_not_reclassified_as_next_class_prefix(self) -> None:
        source = """class A {
  @RequestMapping(path=\"/a\", method=RequestMethod.GET)
  String a() { return \"a\"; }
}
class B {
  @GetMapping(\"/b\") String b() { return \"b\"; }
}
"""
        module, entities, _, _ = _file_intelligence("customer-api", "src/A.java", "blob", source)
        _, facts = _java_file_intelligence(
            "customer-api", "src/A.java", "blob", module["module_id"], source, entities,
        )
        self.assertEqual(
            {"/a", "/b"},
            {str(item["key"]) for item in facts if item["kind"] == "endpoint"},
        )

    def test_dense_source_fails_before_unbounded_atlas_rows_are_materialized(self) -> None:
        from brain.atlas import AtlasCapacityError, MAX_ATLAS_ENTITIES_PER_FILE

        source = "".join(
            f"def generated_{number}():\n    return {number}\n"
            for number in range(MAX_ATLAS_ENTITIES_PER_FILE + 1)
        )
        with self.assertRaisesRegex(AtlasCapacityError, "derived-row budget"):
            _file_intelligence("customer-api", "src/generated.py", "blob", source)

    def test_masked_java_tail_does_not_change_atlas_identity(self) -> None:
        source = '@RestController class A { @GetMapping("/a") String a(){ return "a"; } }'
        baseline = _file_intelligence("customer-api", "src/A.java", "blob", source)
        padded = _file_intelligence(
            "customer-api", "src/A.java", "blob", source + " /*" + ("x" * 900_000) + "*/",
        )
        self.assertEqual(baseline, padded)

    def test_stack_frame_declaring_class_must_own_the_method_line(self) -> None:
        source = """package demo;
class CustomerController {
  Customer find(String id) { return null; }
}
"""
        evidence = Evidence(
            "customer-api", "src/CustomerController.java", 1, 4, source, "code", 100,
            verification_content=source,
        )
        self.assertTrue(_verified_stack_frame(
            "demo.CustomerController.find(CustomerController.java:3)", evidence,
        )[0])
        self.assertFalse(_verified_stack_frame(
            "evil.Wrong.find(CustomerController.java:3)", evidence,
        )[0])

    def test_test_only_calls_cannot_verify_main_execution_flow(self) -> None:
        repository = self.root / "test-only-flow"
        path = repository / "src/test/java/demo/OnlyFlowTest.java"
        path.parent.mkdir(parents=True)
        source = "class OnlyFlowTest { void start(){ middle(); } void middle(){ end(); } void end(){} }\n"
        path.write_text(source, encoding="utf-8")
        config = self.root / "test-only-flow.toml"
        config.write_text(
            "[project]\nname='test-only-flow'\n[graph]\nenabled=false\n[experience]\nenabled=false\n"
            "[[repositories]]\nname='test-only-flow'\npath='test-only-flow'\n",
            encoding="utf-8",
        )
        settings = load_settings(config)
        settings.repo("test-only-flow").source_sha = "sha-test-only"
        state, _ = snapshot_indexes(settings, changed_only=False, publish=False)
        atlas = build_atlas(settings, state)
        publish_generation(
            settings, state,
            components=collect_generation_components(settings, state, atlas_payload=atlas),
            atlas_payload=atlas,
        )
        generation = current_generation_ref(settings)
        self.assertIsNotNone(generation)
        bundle = ContextBundle(
            "test-only", evidence=[Evidence(
                "test-only-flow", "src/test/java/demo/OnlyFlowTest.java", 1, 1,
                source.strip(), "code", 100, verification_content=source,
            )], atlas_generation=generation,
        )
        runtime = build_ticket_runtime(
            settings, generation,
            {"objective": "Trace start", "anchors": [{"kind": "symbol", "value": "start"}]},
            bundle, {"coverage_map": {}, "stable_identities": {}}, context_id="CTX-001",
            next_best_evidence=None,
        )
        self.assertNotEqual("verified", runtime["coverage"].get("main_execution_flow"))
        self.assertFalse(runtime["execution_flow"]["steps"])

    def test_ambiguous_java_method_names_cannot_verify_the_wrong_execution_flow(self) -> None:
        repository = self.root / "ambiguous-flow"
        path = repository / "src/main/java/demo/Ambiguous.java"
        path.parent.mkdir(parents=True)
        source = """class A {
  void start(){ foo(); }
  void foo(){ bar(); }
  void bar(){}
}
class B {
  void foo(){ wrong(); }
  void wrong(){}
}
"""
        path.write_text(source, encoding="utf-8")
        config = self.root / "ambiguous-flow.toml"
        config.write_text(
            "[project]\nname='ambiguous-flow'\n[graph]\nenabled=false\n[experience]\nenabled=false\n"
            "[[repositories]]\nname='ambiguous-flow'\npath='ambiguous-flow'\n",
            encoding="utf-8",
        )
        settings = load_settings(config)
        settings.repo("ambiguous-flow").source_sha = "sha-ambiguous"
        state, _ = snapshot_indexes(settings, changed_only=False, publish=False)
        atlas = build_atlas(settings, state)
        publish_generation(
            settings, state,
            components=collect_generation_components(settings, state, atlas_payload=atlas),
            atlas_payload=atlas,
        )
        generation = current_generation_ref(settings)
        self.assertIsNotNone(generation)
        bundle = ContextBundle(
            "ambiguous flow", evidence=[Evidence(
                "ambiguous-flow", "src/main/java/demo/Ambiguous.java", 1, 9,
                source, "code", 100, verification_content=source,
            )], atlas_generation=generation,
        )
        runtime = build_ticket_runtime(
            settings, generation,
            {"objective": "Trace start", "anchors": [{"kind": "symbol", "value": "start"}]},
            bundle, {"coverage_map": {}, "stable_identities": {}}, context_id="CTX-001",
            next_best_evidence=None,
        )
        self.assertEqual("verified", runtime["coverage"].get("main_execution_flow"))
        self.assertFalse(any(
            item.get("target") == "wrong" for item in runtime["execution_flow"].get("steps") or []
        ))

    def test_receiver_dispatch_stays_candidate_without_exact_type_resolution(self) -> None:
        repository = self.root / "receiver-flow"
        source_root = repository / "src/main/java/demo"
        source_root.mkdir(parents=True)
        first = """class A {
  ExternalB other;
  void start(){ other.foo(); }
  void foo(){ bar(); }
  void bar(){}
}
"""
        second = "class ExternalB {}\n"
        (source_root / "A.java").write_text(first, encoding="utf-8")
        (source_root / "B.java").write_text(second, encoding="utf-8")
        config = self.root / "receiver-flow.toml"
        config.write_text(
            "[project]\nname='receiver-flow'\n[graph]\nenabled=false\n[experience]\nenabled=false\n"
            "[[repositories]]\nname='receiver-flow'\npath='receiver-flow'\n",
            encoding="utf-8",
        )
        settings = load_settings(config)
        state, _ = snapshot_indexes(settings, changed_only=False, publish=False)
        atlas = build_atlas(settings, state)
        publish_generation(
            settings, state,
            components=collect_generation_components(settings, state, atlas_payload=atlas),
            atlas_payload=atlas,
        )
        generation = current_generation_ref(settings)
        self.assertIsNotNone(generation)
        bundle = ContextBundle("receiver", evidence=[
            Evidence("receiver-flow", "src/main/java/demo/A.java", 1, 6, first, "code", 100, verification_content=first),
            Evidence("receiver-flow", "src/main/java/demo/B.java", 1, 1, second, "code", 100, verification_content=second),
        ], atlas_generation=generation)
        runtime = build_ticket_runtime(
            settings, generation,
            {"objective": "Trace start", "anchors": [{"kind": "symbol", "value": "start"}]},
            bundle, {"coverage_map": {}, "stable_identities": {}}, context_id="CTX-001",
            next_best_evidence=None,
        )
        self.assertNotEqual("verified", runtime["coverage"].get("main_execution_flow"))
        self.assertFalse(any(
            item.get("target") == "foo" and item.get("state") == "verified"
            for item in runtime["execution_flow"].get("steps") or []
        ))

        positive_source = first.replace("other.foo()", "this.foo()")
        (source_root / "A.java").write_text(positive_source, encoding="utf-8")
        positive_settings = load_settings(config)
        positive_state, _ = snapshot_indexes(positive_settings, changed_only=True, publish=False)
        positive_atlas = build_atlas(positive_settings, positive_state)
        publish_generation(
            positive_settings, positive_state,
            components=collect_generation_components(
                positive_settings, positive_state, atlas_payload=positive_atlas,
            ),
            atlas_payload=positive_atlas,
        )
        positive_generation = current_generation_ref(positive_settings)
        positive_bundle = ContextBundle("this receiver", evidence=[
            Evidence("receiver-flow", "src/main/java/demo/A.java", 1, 6, positive_source,
                     "code", 100, verification_content=positive_source),
            Evidence("receiver-flow", "src/main/java/demo/B.java", 1, 1, second,
                     "code", 100, verification_content=second),
        ], atlas_generation=positive_generation)
        positive = build_ticket_runtime(
            positive_settings, positive_generation,
            {"objective": "Trace start", "anchors": [{"kind": "symbol", "value": "start"}]},
            positive_bundle, {"coverage_map": {}, "stable_identities": {}}, context_id="CTX-002",
            next_best_evidence=None,
        )
        self.assertEqual("verified", positive["coverage"].get("main_execution_flow"))

        cross_class_source = "class A { void start(){ foo(); } }\n"
        cross_class_target = "class B { void foo(){ bar(); } void bar(){} }\n"
        (source_root / "A.java").write_text(cross_class_source, encoding="utf-8")
        (source_root / "B.java").write_text(cross_class_target, encoding="utf-8")
        cross_settings = load_settings(config)
        cross_state, _ = snapshot_indexes(cross_settings, changed_only=True, publish=False)
        cross_atlas = build_atlas(cross_settings, cross_state)
        publish_generation(
            cross_settings, cross_state,
            components=collect_generation_components(cross_settings, cross_state, atlas_payload=cross_atlas),
            atlas_payload=cross_atlas,
        )
        cross_generation = current_generation_ref(cross_settings)
        cross_bundle = ContextBundle("cross class", evidence=[
            Evidence("receiver-flow", "src/main/java/demo/A.java", 1, 1, cross_class_source,
                     "code", 100, verification_content=cross_class_source),
            Evidence("receiver-flow", "src/main/java/demo/B.java", 1, 1, cross_class_target,
                     "code", 100, verification_content=cross_class_target),
        ], atlas_generation=cross_generation)
        cross_runtime = build_ticket_runtime(
            cross_settings, cross_generation,
            {"objective": "Trace start", "anchors": [{"kind": "symbol", "value": "start"}]},
            cross_bundle, {"coverage_map": {}, "stable_identities": {}}, context_id="CTX-003",
            next_best_evidence=None,
        )
        self.assertNotEqual("verified", cross_runtime["coverage"].get("main_execution_flow"))

    def test_endpoint_integration_requires_feign_target_to_match_inbound_repository(self) -> None:
        api = self.root / "collision-api" / "src/main/java/demo/HealthController.java"
        client = self.root / "collision-client" / "src/main/java/demo/HealthClient.java"
        api.parent.mkdir(parents=True)
        client.parent.mkdir(parents=True)
        inbound = '@RestController class HealthController { @GetMapping("/health") String health(){ return "ok"; } }\n'
        outbound = '@FeignClient(name="unrelated-payments") interface HealthClient { @GetMapping("/health") String health(); }\n'
        api.write_text(inbound, encoding="utf-8")
        client.write_text(outbound, encoding="utf-8")
        config = self.root / "collision-flow.toml"
        config.write_text(
            "[project]\nname='collision-flow'\n[graph]\nenabled=false\n[experience]\nenabled=false\n"
            "[[repositories]]\nname='customer-api'\npath='collision-api'\n"
            "[[repositories]]\nname='customer-client'\npath='collision-client'\n",
            encoding="utf-8",
        )

        def runtime_for_sources() -> dict[str, object]:
            settings = load_settings(config)
            state, _ = snapshot_indexes(settings, changed_only=False, publish=False)
            atlas = build_atlas(settings, state)
            publish_generation(
                settings, state,
                components=collect_generation_components(settings, state, atlas_payload=atlas),
                atlas_payload=atlas,
            )
            generation = current_generation_ref(settings)
            current_outbound = client.read_text(encoding="utf-8")
            bundle = ContextBundle("endpoint integration", evidence=[
                Evidence("customer-api", "src/main/java/demo/HealthController.java", 1, 1,
                         inbound, "code", 100, verification_content=inbound),
                Evidence("customer-client", "src/main/java/demo/HealthClient.java", 1, 1,
                         current_outbound, "code", 100, verification_content=current_outbound),
            ], atlas_generation=generation)
            return build_ticket_runtime(
                settings, generation,
                {"objective": "Trace /health", "anchors": [{"kind": "endpoint", "value": "/health"}]},
                bundle, {"coverage_map": {}, "stable_identities": {}}, context_id="CTX-001",
                next_best_evidence=None,
            )

        unrelated = runtime_for_sources()
        self.assertNotEqual("verified", unrelated["coverage"].get("cross_repo_integration"))
        client.write_text(outbound.replace("unrelated-payments", "customer-api"), encoding="utf-8")
        linked = runtime_for_sources()
        self.assertEqual("verified", linked["coverage"].get("cross_repo_integration"))
        self.assertEqual(2, len(linked["coverage_proofs"]["cross_repo_integration"]))

    def test_outbound_feign_route_is_not_a_production_entry_or_first_useful_checkpoint(self) -> None:
        repository = self.root / "outbound-client"
        path = repository / "src/main/java/demo/PaymentsClient.java"
        path.parent.mkdir(parents=True)
        source = '@FeignClient(name="payments") interface PaymentsClient { @GetMapping("/pay") String pay(); }\n'
        path.write_text(source, encoding="utf-8")
        config = self.root / "outbound-client.toml"
        config.write_text(
            "[project]\nname='outbound-client'\n[graph]\nenabled=false\n[experience]\nenabled=false\n"
            "[[repositories]]\nname='outbound-client'\npath='outbound-client'\n",
            encoding="utf-8",
        )
        settings = load_settings(config)
        state, _ = snapshot_indexes(settings, changed_only=False, publish=False)
        atlas = build_atlas(settings, state)
        publish_generation(
            settings, state,
            components=collect_generation_components(settings, state, atlas_payload=atlas),
            atlas_payload=atlas,
        )
        generation = current_generation_ref(settings)
        bundle = ContextBundle("outbound", evidence=[Evidence(
            "outbound-client", "src/main/java/demo/PaymentsClient.java", 1, 1,
            source, "code", 100, verification_content=source,
        )], atlas_generation=generation)
        runtime = build_ticket_runtime(
            settings, generation,
            {"objective": "Trace /pay", "anchors": [{"kind": "endpoint", "value": "/pay"}]},
            bundle, {"coverage_map": {}, "stable_identities": {}}, context_id="CTX-001",
            next_best_evidence=None,
        )
        self.assertNotEqual("verified", runtime["coverage"].get("production_entry_point"))
        self.assertIsNone(runtime["first_useful_checkpoint"])

    def test_stable_public_identity_registry_rejects_collisions_before_a_wave(self) -> None:
        formats = {
            "evidence": "E0001", "anchors": "A001", "flows": "F001",
            "hypotheses": "H001", "blockers": "B001", "contexts": "CTX-001",
        }
        for namespace, public_id in formats.items():
            with self.subTest(namespace=namespace):
                state = {"stable_identities": {namespace: {"one": public_id, "two": public_id}}}
                with self.assertRaisesRegex(ValueError, "duplicate public IDs"):
                    validate_stable_identity_registry(state)

        sparse = {
            "evidence": {"one": "E0001", "three": "E0003"},
            "contexts": {"one": "CTX-001", "three": "CTX-003"},
        }
        validate_stable_identity_registry({"stable_identities": sparse})
        self.assertEqual("E0004", _allocate(sparse, "evidence", "four", "E", 4))
        self.assertEqual("CTX-004", _allocate(sparse, "contexts", "four", "CTX-", 3))

        self.publish("sha-g1", "G1_ONLY")
        start_session(self.settings, "COLLISION", "Reject lineage collisions.")
        create_context(self.settings, "COLLISION", self.request("Trace first collision wave", wave=1))
        state_path = self.settings.runs_dir / "COLLISION" / "session.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        evidence_registry = state["stable_identities"]["evidence"]
        evidence_registry["poisoned-second-identity"] = next(iter(evidence_registry.values()))
        state_path.write_text(json.dumps(state), encoding="utf-8")
        with mock.patch("brain.core.retrieve_context") as retrieve:
            with self.assertRaisesRegex(BrainError, "stable identity registry is corrupt"):
                create_context(
                    self.settings, "COLLISION",
                    self.request("Trace second collision wave", base=state["last_context_id"], wave=2),
                )
        retrieve.assert_not_called()

    def test_protocol_v5_context_lineage_rejects_unknown_duplicate_and_cyclic_rows(self) -> None:
        valid = {
            "generation": 1,
            "stable_identities": {"contexts": {"sha256:first": "CTX-001", "sha256:second": "CTX-002"}},
            "context_lineage": [
                {"context_id": "CTX-001", "base_context_id": None, "number": 1, "kind": "checkpoint",
                 "content_hash": "sha256:first", "protocol_version": 5, "generation": 1},
                {"context_id": "CTX-002", "base_context_id": "CTX-001", "number": 2, "kind": "delta",
                 "content_hash": "sha256:second", "protocol_version": 5, "generation": 1},
            ],
            "last_context_id": "CTX-002",
        }
        validate_stable_identity_registry(valid)
        poisons = []
        unknown = json.loads(json.dumps(valid))
        unknown["context_lineage"][1]["context_id"] = "CTX-999"
        poisons.append(unknown)
        duplicate = json.loads(json.dumps(valid))
        duplicate["context_lineage"][1]["context_id"] = "CTX-001"
        poisons.append(duplicate)
        cycle = json.loads(json.dumps(valid))
        cycle["context_lineage"][0]["base_context_id"] = "CTX-002"
        poisons.append(cycle)
        wrong_hash = json.loads(json.dumps(valid))
        wrong_hash["context_lineage"][1]["content_hash"] = "sha256:forged"
        poisons.append(wrong_hash)
        wrong_generation = json.loads(json.dumps(valid))
        wrong_generation["context_lineage"][1]["generation"] = 2
        poisons.append(wrong_generation)
        for poisoned in poisons:
            with self.subTest(poison=poisoned["context_lineage"]):
                with self.assertRaisesRegex(ValueError, "context lineage"):
                    validate_stable_identity_registry(poisoned)

    def test_exact_authority_excludes_external_diff_path_and_compound_matches(self) -> None:
        generation = self.publish("sha-g1", "G1_ONLY")
        bundle = ContextBundle(
            "authority",
            evidence=[
                Evidence(
                    "customer-api", "src/CustomerController.java", 1, 1,
                    "class Different { void run() { Handler handler = factory.create(); } }", "code", 100,
                ),
                Evidence("customer-api", "(working tree diff)", 1, 1, "CustomerController", "local diff", 100),
                Evidence("external", "external-001.md", 1, 1, "CustomerController", "user-supplied external evidence", 100),
                Evidence("knowledge", "PROJECT_MAP.md", 1, 1, "CustomerController", "knowledge", 70),
            ],
            atlas_generation=generation,
        )
        path_only = _exact_evidence_anchors(
            {"anchors": [{"kind": "symbol", "value": "CustomerController"}]}, bundle, generation,
        )
        self.assertEqual([], path_only)
        compound = _exact_evidence_anchors(
            {"anchors": [{"kind": "symbol", "value": "Different Handler"}]}, bundle, generation,
        )
        self.assertTrue(compound)
        self.assertEqual({"inferred_candidate"}, {item["evidence_authority"] for item in compound})
        self.assertFalse(any(item["provenance"]["exact_source"] for item in compound))
        file_hint = _exact_evidence_anchors(
            {"anchors": [{"kind": "file_hint", "value": "CustomerController.java"}]}, bundle, generation,
        )
        self.assertEqual("exact_source", file_hint[0]["evidence_authority"])
        surfaces = _surfaces(bundle, {"steps": []})
        self.assertEqual(
            [{"repo": "customer-api", "path": "src/CustomerController.java", "state": "verified", "confidence": 1.0,
              "evidence_authority": "exact_source"}],
            surfaces["implementation"],
        )
        program_slice = _program_slice(bundle)
        self.assertTrue(program_slice["statements"])
        self.assertEqual(
            {"src/CustomerController.java"},
            {item["path"] for item in program_slice["statements"]},
        )
        self.assertFalse(_verified_value_location(
            bundle, "customer-api", "src/CustomerController.java", 1, "CustomerService", kind="symbol",
        ))
        self.assertTrue(_verified_value_location(
            bundle, "customer-api", "src/CustomerController.java", 1, "Different", kind="symbol",
        ))
        hypotheses = _hypotheses(
            [], ["CustomerController definitely routes externally"],
            ContextBundle("external", evidence=[
                Evidence("external", "note.md", 1, 1, "CustomerController routes externally", "user-supplied external evidence", 100),
            ], atlas_generation=generation),
            {},
        )
        self.assertEqual("untested", hypotheses[0]["status"])
        split_terms = _hypotheses(
            [], ["alphawidget delegates betagateway"],
            ContextBundle("split", evidence=[
                Evidence("customer-api", "one.java", 1, 1, "alphawidget", "code", 100),
                Evidence("customer-api", "two.java", 1, 1, "betagateway", "code", 100),
            ], atlas_generation=generation),
            {},
        )
        self.assertEqual("untested", split_terms[0]["status"])
        self.assertEqual([], split_terms[0]["supporting_evidence"])

        for fake in (
            'class Fake { def spec = / @GetMapping("/ghost") / }',
            'class Fake { def spec = $/ @KafkaListener(topics="ghost") /$ }',
            "class Fake { def spec = ''' @GetMapping(\"/ghost\") ''' }",
        ):
            anchors, facts = _java_file_intelligence(
                "customer-api", "src/Fake.groovy", "blob", None, fake,
            )
            self.assertTrue(anchors or facts)
            self.assertFalse(any(item["provenance"].get("exact_source") for item in [*anchors, *facts]))
            groovy = ContextBundle(
                "groovy", evidence=[Evidence(
                    "customer-api", "src/Fake.groovy", 1, 1, fake, "code", 100,
                )], atlas_generation=generation,
            )
            self.assertFalse(_verified_value_location(
                groovy, "customer-api", "src/Fake.groovy", 1, "ghost", kind="endpoint",
            ))
        self.assertEqual(set(), _boolean_assignments(
            "Fake.groovy", "def docs = /\nfeature.enabled = true\n/", "feature.enabled",
        ))
        self.assertEqual(set(), _boolean_assignments(
            "Fake.groovy", "def docs = $/\nfeature.enabled = true\n/$", "feature.enabled",
        ))

        java_text_block = '''class Real {
String docs = """
@FeignClient(name="ghost")
@RequestMapping("/ghost")
""";
@GetMapping("/real") Object real() { return null; }
}'''
        _, java_facts = _java_file_intelligence(
            "customer-api", "src/Real.java", "blob", None, java_text_block,
        )
        endpoints = [item for item in java_facts if item["kind"] == "endpoint"]
        self.assertEqual([("/real", "inbound")], [(item["key"], item["direction"]) for item in endpoints])

        config_literals = {
            "application.yml": "docs: |\n  feature.enabled: true\nreal.enabled: false\n",
            "folded.yml": "docs: >-\n  feature.enabled: true\nreal.enabled: false\n",
            "application.toml": "docs = '''\nfeature.enabled = true\n'''\nreal.enabled = false\n",
            "application.properties": "docs=first \\\n+ feature.enabled=true\nreal.enabled=false\n",
            "application.xml": '<![CDATA[<property name="feature.enabled" value="true"/>]]>',
        }
        for config_path, config_content in config_literals.items():
            self.assertEqual(set(), _boolean_assignments(config_path, config_content, "feature.enabled"))
            _, config_facts = _config_file_intelligence(
                "customer-api", config_path, "blob", None, config_content,
            )
            self.assertNotIn("feature.enabled", {item["key"] for item in config_facts})

        window = ContextBundle(
            "window", evidence=[Evidence(
                "customer-api", "src/Long.java", 80, 82,
                'docs\n@GetMapping("/ghost")\nfeatureEnabled = true', "code", 100,
            )], atlas_generation=generation,
        )
        self.assertFalse(_verified_value_location(
            window, "customer-api", "src/Long.java", 81, "/ghost", kind="endpoint",
        ))
        window_hypothesis = _hypotheses(
            [], ["featureEnabled is true"], window, {},
        )[0]
        self.assertNotEqual("supported", window_hypothesis["status"])
        prior = _hypotheses(
            [], ["featureEnabled is true"], ContextBundle("prior", atlas_generation=generation),
            {"hypotheses": {}, "evidence": {}},
        )[0]
        prior.update({
            "candidate_evidence": ["E0001"], "supporting_evidence": ["E0001"],
            "contradicting_evidence": [],
        })
        retained = _hypotheses(
            [prior],
            ["featureEnabled is true"], ContextBundle("delta", atlas_generation=generation),
            {"hypotheses": {}, "evidence": {}}, {"E0001"},
        )[0]
        self.assertEqual("untested", retained["status"])
        self.assertEqual(["E0001"], retained["candidate_evidence"])
        self.assertEqual([], retained["supporting_evidence"])

    def test_component_compatibility_and_publication_rollback_fail_closed(self) -> None:
        generation = self.publish("sha-g1", "G1_ONLY")
        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            seed = str(connection.execute(
                "SELECT entity_id FROM generation_entities WHERE generation=? ORDER BY entity_id LIMIT 1",
                (generation.generation,),
            ).fetchone()[0])
        finally:
            connection.close()
        incompatible_graph = replace(
            generation,
            components={
                **generation.components,
                "typed_graph": {**generation.component("typed_graph"), "schema_version": "future-incompatible"},
            },
        )
        flow = _execution_flow(
            self.settings, incompatible_graph, [seed],
            ContextBundle("incompatible graph", atlas_generation=incompatible_graph),
        )
        self.assertEqual("degraded", flow["status"])
        self.assertEqual([], flow["steps"])
        self.assertEqual([], route(
            self.settings, "CustomerController", {"objective": "CustomerController"}, incompatible_graph,
        )["entities"])
        incompatible_hierarchy = replace(
            generation,
            components={
                **generation.components,
                "hierarchy": {**generation.component("hierarchy"), "schema_version": "1"},
            },
        )
        self.assertEqual([], route(
            self.settings, "CustomerController", {"objective": "CustomerController"}, incompatible_hierarchy,
        )["entities"])
        without_anchors = replace(
            generation,
            components={name: value for name, value in generation.components.items() if name != "runtime_anchors"},
        )
        self.assertEqual(
            "degraded", resolve_runtime_anchors(self.settings, without_anchors, ["CustomerController"])["status"],
        )
        wrong_schema = replace(
            generation,
            components={
                **generation.components,
                "runtime_anchors": {**generation.component("runtime_anchors"), "schema_version": "wrong"},
            },
        )
        self.assertEqual(
            "degraded", resolve_runtime_anchors(self.settings, wrong_schema, ["CustomerController"])["status"],
        )

        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            connection.execute(
                "DELETE FROM generation_runtime_anchors WHERE generation=? AND anchor_id=("
                "SELECT anchor_id FROM generation_runtime_anchors WHERE generation=? LIMIT 1)",
                (generation.generation, generation.generation),
            )
            connection.commit()
        finally:
            connection.close()
        incompatible = resolve_runtime_anchors(self.settings, generation, ["CustomerController"])
        self.assertEqual("degraded", incompatible["status"])
        self.assertIn("membership", incompatible["reason"])

        same_state, _ = snapshot_indexes(self.settings, changed_only=True, publish=False)
        same_atlas = build_atlas(self.settings, same_state)
        same_components = collect_generation_components(self.settings, same_state, atlas_payload=same_atlas)
        recovered_manifest = publish_generation(
            self.settings, same_state, components=same_components, atlas_payload=same_atlas,
        )
        recovered = current_generation_ref(self.settings)
        self.assertNotEqual(generation.generation, recovered.generation)
        self.assertEqual(generation.generation, recovered_manifest["components"]["lexical"]["details"]["recovery_of"])
        recovery_connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            recovered_anchor_count = recovery_connection.execute(
                "SELECT COUNT(*) FROM generation_runtime_anchors WHERE generation=?", (recovered.generation,),
            ).fetchone()[0]
        finally:
            recovery_connection.close()
        self.assertEqual(len(same_atlas["runtime_anchors"]), recovered_anchor_count)

        for repo in self.settings.repositories:
            snapshot = self.settings.state_dir / "snapshots" / repo.name / "sha-g2-rollback"
            shutil.copytree(repo.path, snapshot)
            repo.source_path = snapshot
            repo.source_sha = "sha-g2-rollback"
            repo.source_ref = "refs/heads/main"
            repo.source_status = "current"
        state, _ = snapshot_indexes(self.settings, changed_only=True, publish=False)
        atlas = build_atlas(self.settings, state)
        components = collect_generation_components(self.settings, state, atlas_payload=atlas)
        poisoned_graph = json.loads(json.dumps(atlas))
        poisoned_graph["edges"][0]["edge_type"] = "REFERENCES"
        with self.assertRaisesRegex(sqlite3.IntegrityError, "Atlas payload"):
            publish_generation(
                self.settings, state, components=components, atlas_payload=poisoned_graph,
            )
        self.assertEqual(recovered.generation, current_generation_ref(self.settings).generation)
        atlas["runtime_anchors"][0]["normalized"] = "mutated-after-component-registration"
        with self.assertRaisesRegex(ValueError, "content identity"):
            publish_generation(self.settings, state, components=components, atlas_payload=atlas)
        current = current_generation_ref(self.settings)
        self.assertIsNotNone(current)
        self.assertEqual(recovered.generation, current.generation)

    def test_serving_rejects_persisted_edge_with_same_id_and_poisoned_semantics(self) -> None:
        generation = self.publish("sha-edge-poison", "EDGE_POISON")
        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            row = connection.execute(
                "SELECT e.edge_id,e.source_id,e.repo,e.path FROM generation_edges g "
                "JOIN atlas_edges e ON e.edge_id=g.edge_id "
                "JOIN generation_entities t ON t.generation=g.generation AND t.entity_id=e.target_id "
                "WHERE g.generation=? AND e.edge_type='DEFINES' ORDER BY e.edge_id LIMIT 1",
                (generation.generation,),
            ).fetchone()
            self.assertIsNotNone(row)
            edge_id, source_id, repo_name, relative_path = row
            connection.execute(
                "UPDATE atlas_edges SET edge_type='CALLS' WHERE edge_id=?", (edge_id,),
            )
            connection.commit()
        finally:
            connection.close()
        repository = self.settings.repo(str(repo_name))
        content = (repository.source_path / str(relative_path)).read_text(encoding="utf-8")
        bundle = ContextBundle(
            "poisoned persisted edge",
            evidence=[Evidence(
                str(repo_name), str(relative_path), 1, max(1, content.count("\n") + 1),
                content, "code", 100, verification_content=content,
            )],
            atlas_generation=generation,
        )
        flow = _execution_flow(self.settings, generation, [str(source_id)], bundle)
        self.assertEqual("degraded", flow["status"])
        self.assertIn("content identity", flow["reason"])
        self.assertEqual([], flow["steps"])
        routed = route(
            self.settings, "CustomerController", {"objective": "CustomerController"}, generation,
        )
        self.assertFalse(any(item.get("edge_id") == edge_id for item in routed["graph_edges"]))

    def test_atlas_shared_rows_are_content_addressed_and_poison_fails_publication(self) -> None:
        generation_one = self.publish("sha-g1", "G1_ONLY")
        kotlin = self.root / "customer-api/src/main/java/demo/Extra.kt"
        kotlin.write_text("package demo\nclass Extra\n", encoding="utf-8")
        generation_two = self.publish("sha-g2", "G2_ONLY")
        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            first_languages = {
                row[0] for row in connection.execute(
                    "SELECT m.language FROM generation_modules g JOIN atlas_modules m ON m.module_id=g.module_id "
                    "WHERE g.generation=? AND m.repo='customer-api' AND m.path='src/main/java/demo'",
                    (generation_one.generation,),
                )
            }
            second_languages = {
                row[0] for row in connection.execute(
                    "SELECT m.language FROM generation_modules g JOIN atlas_modules m ON m.module_id=g.module_id "
                    "WHERE g.generation=? AND m.repo='customer-api' AND m.path='src/main/java/demo'",
                    (generation_two.generation,),
                )
            }
            self.assertNotIn("kotlin", first_languages)
            self.assertIn("kotlin", second_languages)
            module_id = connection.execute(
                "SELECT m.module_id FROM generation_modules g JOIN atlas_modules m ON m.module_id=g.module_id "
                "WHERE g.generation=? LIMIT 1", (generation_two.generation,),
            ).fetchone()[0]
            connection.execute("UPDATE atlas_modules SET name='poisoned' WHERE module_id=?", (module_id,))
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(RuntimeError, "immutable Atlas row mismatch"):
            self.publish("sha-g3", "G3_ONLY")
        self.assertEqual(generation_two.generation, current_generation_ref(self.settings).generation)

    def test_comments_and_text_literals_never_become_verified_runtime_authority(self) -> None:
        comment_sources = {
            "CommentOnly.java": "/*\n" + (" filler\n" * 8) + "@GetMapping(\"/ghost/not-real\")\nfeature.enabled=true\n*/\n",
            "First.java": "class First { // planned call Second()\n}\n",
            "Second.java": "class Second { String fake = \"Third()\"; }\n",
            "Third.java": "class Third {}\n",
            "Publisher.java": "class Publisher { // kafkaTemplate.send(\"fake.topic\", event)\n}\n",
            "Listener.java": "class Listener { // @KafkaListener(topics=\"fake.topic\")\n}\n",
            "TextBlock.java": 'class TextBlock { String fake = \"\"\"\n@GetMapping("/text/ghost")\n\"\"\"; }\n',
        }
        root = self.root / "customer-api" / "src/main/java/demo"
        for name, content in comment_sources.items():
            root.joinpath(name).write_text(content, encoding="utf-8")
        xml_content = '<!--\n@GetMapping("/xml/ghost")\nfeature.enabled=true\n-->\n'
        xml_path = self.root / "customer-api" / "src/main/resources/app.xml"
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        xml_path.write_text(xml_content, encoding="utf-8")
        integration_path = self.root / "customer-api" / "src/integrationTest/java/demo/FixtureController.java"
        integration_path.parent.mkdir(parents=True, exist_ok=True)
        integration_content = 'class FixtureController { @GetMapping("/integration-only") void run() {} }\n'
        integration_path.write_text(integration_content, encoding="utf-8")
        generation = self.publish("sha-comments", "COMMENT_ONLY")
        evidence = [
            Evidence("customer-api", f"src/main/java/demo/{name}", 1, content.count("\n") + 1,
                     content, "code", 100, ["exact test fixture"])
            for name, content in comment_sources.items()
        ]
        evidence.extend([
            Evidence("customer-api", "src/main/resources/app.xml", 1, 4, xml_content, "code", 100, ["exact test fixture"]),
            Evidence("customer-api", "src/integrationTest/java/demo/FixtureController.java", 1, 1,
                     integration_content, "code", 100, ["exact test fixture"]),
        ])
        bundle = ContextBundle("comment authority", evidence=evidence, atlas_generation=generation)
        request = {
            "objective": "Trace First /ghost/not-real fake.topic feature.enabled", "runtime_facts": [],
            "resolve": ["First"], "hypotheses": ["feature.enabled is true"],
            "anchors": [
                {"kind": "symbol", "value": "First"},
                {"kind": "endpoint", "value": "/ghost/not-real"},
                {"kind": "endpoint", "value": "/text/ghost"},
                {"kind": "endpoint", "value": "/xml/ghost"},
                {"kind": "endpoint", "value": "/integration-only"},
                {"kind": "topic", "value": "fake.topic"},
            ],
        }
        runtime = build_ticket_runtime(
            self.settings, generation, request, bundle,
            {"coverage_map": {}, "stable_identities": {}}, context_id="CTX-COMMENT-001",
        )
        self.assertFalse(any(
            item.get("value") in {"/ghost/not-real", "/text/ghost", "/xml/ghost", "fake.topic"}
            and item.get("evidence_authority") == "exact_source"
            for item in runtime["anchors"]["candidates"]
        ))
        self.assertNotEqual("verified", runtime["coverage"].get("production_entry_point"))
        self.assertTrue(any(
            item["path"].startswith("src/integrationTest/")
            for item in runtime["surfaces"]["test"]
        ))
        self.assertNotEqual("verified", runtime["coverage"].get("main_execution_flow"))
        self.assertNotEqual("verified", runtime["coverage"].get("cross_repo_integration"))
        self.assertIsNone(runtime["first_useful_checkpoint"])
        self.assertEqual("unresolved", runtime["hypothesis_ledger"]["items"][0]["status"])
        self.assertNotEqual(
            "exact pinned boolean assignment", runtime["hypothesis_ledger"]["items"][0]["verification"],
        )

    def test_first_useful_checkpoint_metadata_names_only_whole_emitted_regions(self) -> None:
        from brain.core import _evidence_id, _publish_first_useful_checkpoint

        sources: dict[str, str] = {}
        source_root = self.root / "customer-api" / "src/main/java/demo"
        for index in range(3):
            content = (
                f'class Big{index} {{ @GetMapping("/big-{index}") void run() {{}} }}\n'
                + (" " * 7_700) + "\n"
            )
            name = f"Big{index}.java"
            source_root.joinpath(name).write_text(content, encoding="utf-8")
            sources[name] = content
        generation = self.publish("sha-large-checkpoint", "LARGE_CHECKPOINT")
        evidence = [
            Evidence("customer-api", f"src/main/java/demo/{name}", 1, 2, content, "code", 100, ["exact"])
            for name, content in sources.items()
        ]
        bundle = ContextBundle("large checkpoint", evidence=evidence, atlas_generation=generation)
        request = {
            "objective": "Trace large endpoints",
            "anchors": [{"kind": "endpoint", "value": f"/big-{index}"} for index in range(3)],
            "runtime_facts": [], "resolve": [],
        }
        directory = self.settings.runs_dir / "CHECKPOINT-BUDGET"
        directory.mkdir(parents=True)
        state = {"ticket": "CHECKPOINT-BUDGET", "active_artifacts": [], "stable_identities": {}}
        checkpoint = _publish_first_useful_checkpoint(
            self.settings, "CHECKPOINT-BUDGET", 1, "CTX-001", None,
            bundle, request, "request-signature", state, directory, None,
        )
        self.assertIsNotNone(checkpoint)
        content = (directory / checkpoint["artifact"]).read_text(encoding="utf-8")
        self.assertLessEqual(len(content.encode("utf-8")), 24_000)
        self.assertTrue(all(f"### {identifier} —" in content for identifier in checkpoint["evidence_ids"]))
        self.assertEqual(
            set(checkpoint["internal_evidence_ids"]),
            {_evidence_id(item) for item in evidence if any(
                f"`{item.repo}:{item.path}:" in line for line in content.splitlines()
            )},
        )
        self.assertLess(len(checkpoint["internal_evidence_ids"]), len(evidence))

    def test_first_useful_checkpoint_publication_is_atomic_and_revalidates_artifacts(self) -> None:
        from brain import core as core_module

        self.publish("sha-checkpoint-atomic", "G1_ONLY")
        request = self.request("Trace CustomerController G1_ONLY atomically", wave=1)
        original_write = core_module._atomic_generated_text_write
        for ticket, failure in (("CHECKPOINT-WRITE", "write"), ("CHECKPOINT-SAVE", "save")):
            start_session(self.settings, ticket, "Trace the production entry point atomically.")

            def fail_checkpoint_handoff(settings, path: Path, content: str) -> None:
                if path.parent.parent == self.settings.generated_dir / "handoffs" and "checkpoint-" in path.name:
                    raise OSError("injected checkpoint handoff failure")
                original_write(settings, path, content)

            patcher = (
                mock.patch("brain.core._atomic_generated_text_write", side_effect=fail_checkpoint_handoff)
                if failure == "write"
                else mock.patch("brain.core.save_session", side_effect=OSError("injected checkpoint save failure"))
            )
            with patcher:
                with self.assertRaisesRegex(OSError, "injected checkpoint"):
                    create_context(self.settings, ticket, request)
            state = session_state(self.settings, ticket)
            self.assertFalse(state.get("progressive_checkpoint"))
            self.assertFalse(list((self.settings.runs_dir / ticket).glob("checkpoint-[0-9][0-9][0-9].md")))
            self.assertFalse(list((self.settings.generated_dir / "handoffs" / ticket).glob("checkpoint-*.md")))

        start_session(self.settings, "CHECKPOINT-CORRUPT", "Validate persisted checkpoint bytes.")
        with mock.patch("brain.investigation.build_ticket_runtime", side_effect=RuntimeError("stop after checkpoint")):
            with self.assertRaisesRegex(RuntimeError, "stop after checkpoint"):
                create_context(self.settings, "CHECKPOINT-CORRUPT", request)
        failed = session_state(self.settings, "CHECKPOINT-CORRUPT")
        artifact = self.settings.runs_dir / "CHECKPOINT-CORRUPT" / failed["progressive_checkpoint"]["artifact"]
        artifact.write_text("corrupt", encoding="utf-8")
        with self.assertRaisesRegex(BrainError, "checkpoint artifact is corrupt"):
            create_context(self.settings, "CHECKPOINT-CORRUPT", request)
        artifact.write_bytes(b"x" * (core_module.MAX_CHECKPOINT_ARTIFACT_BYTES + 1))
        with self.assertRaisesRegex(BrainError, "checkpoint artifact is corrupt"):
            create_context(self.settings, "CHECKPOINT-CORRUPT", request)
        fake = "# FAKE UNPINNED EVIDENCE\n"
        artifact.write_bytes(fake.encode("utf-8"))
        handoff = Path(failed["progressive_checkpoint"]["handoff_artifact"])
        handoff.write_bytes(fake.encode("utf-8"))
        fake_hash = "sha256:" + hashlib.sha256(fake.encode("utf-8")).hexdigest()
        failed["progressive_checkpoint"]["content_hash"] = fake_hash
        for item in failed["context_lineage"]:
            if item.get("context_id") == failed["progressive_checkpoint"]["checkpoint_id"]:
                item["content_hash"] = fake_hash
        core_module.save_session(self.settings, "CHECKPOINT-CORRUPT", failed)
        with self.assertRaisesRegex(BrainError, "does not match pinned evidence"):
            create_context(self.settings, "CHECKPOINT-CORRUPT", request)

    def test_protocol_v5_multiwave_generation_pin_and_corrupt_semantic_fail_closed(self) -> None:
        generation_one = self.publish("sha-g1", "G1_ONLY")
        (self.settings.state_dir / "edition.json").write_text(json.dumps({"edition": "semantic"}), encoding="utf-8")
        semantic_one = self.settings.state_dir / str(generation_one.component("semantic")["artifact_ref"])
        start_session(self.settings, "TICKET-A", "Trace CustomerController and customer.updated on G1.")
        first, _, _ = create_context(self.settings, "TICKET-A", self.request("Trace CustomerController G1_ONLY", wave=1))
        state_one = session_state(self.settings, "TICKET-A")
        self.assertEqual("CTX-001", state_one["last_context_id"])
        self.assertIn("Protocol v5 investigation state", first)
        self.assertIn("E0001", first)
        self.assertEqual(generation_one.generation, state_one["investigation_runtime"]["generation"])
        self.assertEqual(1, state_one["investigation_runtime"]["wave"])
        self.assertEqual(state_one["coverage_map"], state_one["investigation_runtime"]["coverage"])
        self.assertTrue(state_one["investigation_runtime"]["anchors"]["candidates"])
        self.assertTrue(any(
            item["kind"] == "stack_frame" and item["method"] == "exact_lexical_verified"
            for item in state_one["investigation_runtime"]["anchors"]["candidates"]
        ))
        self.assertIn("### Serving state", first)
        self.assertEqual("derived_navigation_only", state_one["investigation_runtime"]["program_slice"]["statements"][0]["evidence_authority"])
        self.assertLessEqual(len(state_one["investigation_runtime"]["program_slice"]["statements"]), 160)
        first_anchor_ids = {
            item["identity"]: item["anchor_id"] for item in state_one["investigation_runtime"]["anchors"]["candidates"]
        }
        first_flow_ids = {
            name: state_one["investigation_runtime"][name]["flow_id"]
            for name in ("execution_flow", "integration_flow")
        }
        first_checkpoint_id = state_one["progressive_checkpoint"]["checkpoint_id"]
        first_implementation_surface = {
            (item["repo"], item["path"])
            for item in state_one["investigation_runtime"]["surfaces"]["implementation"]
        }
        self.assertEqual(generation_one.generation, state_one["prefetch"]["generation"])
        state_path = self.settings.runs_dir / "TICKET-A" / "session.json"
        corrupted_state = json.loads(state_path.read_text(encoding="utf-8"))
        cached_flow = (corrupted_state.get("investigation_runtime") or {}).get("execution_flow") or {}
        if cached_flow.get("steps"):
            cached_flow["steps"][0]["path"] = "poisoned/flow.java"
            cached_flow["cache_identity"] = _flow_cache_identity(
                str(cached_flow["schema_version"]), cached_flow["steps"],
            )
        cached_integration = (corrupted_state.get("investigation_runtime") or {}).get("integration_flow") or {}
        if cached_integration.get("steps"):
            cached_integration["steps"][0]["key"] = "poisoned.integration"
            cached_integration["cache_identity"] = _flow_cache_identity(
                str(cached_integration["schema_version"]), cached_integration["steps"],
            )
        state_path.write_text(json.dumps(corrupted_state, indent=2) + "\n", encoding="utf-8")

        generation_two = self.publish("sha-g2", "G2_ONLY")
        start_session(self.settings, "TICKET-B", "Trace CustomerController on G2.")
        semantic_one_snapshots = json.loads(semantic_one.read_text(encoding="utf-8"))["snapshots"]
        self.assertEqual(8, len(semantic_one_snapshots))
        self.assertEqual({"sha-g1"}, set(semantic_one_snapshots.values()))
        second, _, _ = create_context(
            self.settings, "TICKET-A", self.request("Trace customer.updated without generation substitution", base="CTX-001", wave=2),
        )
        state_two = session_state(self.settings, "TICKET-A")
        self.assertTrue(second.startswith("# PROJECT BRAIN CONTEXT DELTA\n"))
        self.assertEqual("CTX-002", state_two["last_context_id"])
        self.assertEqual(generation_one.generation, state_two["investigation_runtime"]["generation"])
        self.assertIn(f"Atlas generation: `{generation_one.generation}`", second)
        self.assertNotIn("G2_ONLY", second)
        self.assertEqual(generation_one.generation, state_two["request_history"][-1]["retrieval"]["generation"])
        self.assertEqual(first_anchor_ids, {
            item["identity"]: item["anchor_id"] for item in state_two["investigation_runtime"]["anchors"]["candidates"]
            if item["identity"] in first_anchor_ids
        })
        self.assertEqual(first_flow_ids, {
            name: state_two["investigation_runtime"][name]["flow_id"]
            for name in ("execution_flow", "integration_flow")
        })
        self.assertEqual(first_checkpoint_id, state_two["progressive_checkpoint"]["checkpoint_id"])
        self.assertEqual(first_checkpoint_id, state_two["investigation_runtime"]["first_useful_checkpoint"]["checkpoint_id"])
        self.assertEqual(1, len(list((self.settings.runs_dir / "TICKET-A").glob("checkpoint-[0-9][0-9][0-9].md"))))
        for component in ("anchors", "execution_flow", "integration_flow"):
            rows = (
                state_two["investigation_runtime"][component].get("candidates")
                if component == "anchors" else state_two["investigation_runtime"][component].get("steps")
            ) or []
            self.assertTrue(all(item.get("evidence_ids") for item in rows if item.get("evidence_authority") == "exact_source"))
        self.assertTrue(all(
            item.get("evidence_ids")
            for name in ("implementation", "test", "impact", "contract", "config_data")
            for item in state_two["investigation_runtime"]["surfaces"].get(name) or []
            if item.get("state") == "verified"
        ))
        self.assertTrue(first_implementation_surface.issubset({
            (item["repo"], item["path"])
            for item in state_two["investigation_runtime"]["surfaces"]["implementation"]
        }))
        removed = state_two["investigation_runtime"]["delta_state"]["removed"]
        self.assertNotIn("anchors", removed)
        self.assertNotIn("surface:implementation", removed)
        self.assertNotIn("explicit_requested", state_two["coverage_map"])
        self.assertFalse(any(
            item.get("path") == "poisoned/flow.java"
            for item in state_two["investigation_runtime"]["execution_flow"].get("steps") or []
        ))
        self.assertFalse(any(
            item.get("key") == "poisoned.integration"
            for item in state_two["investigation_runtime"]["integration_flow"].get("steps") or []
        ))

        third, _, _ = create_context(
            self.settings, "TICKET-A", self.request("Confirm G1 CustomerService test surface", base="CTX-002", wave=3),
        )
        state_three = session_state(self.settings, "TICKET-A")
        self.assertEqual(generation_one.generation, state_three["investigation_runtime"]["generation"])
        self.assertEqual(3, state_three["investigation_runtime"]["wave"])
        self.assertIn(f"Atlas generation: `{generation_one.generation}`", third)
        self.assertNotIn("G2_ONLY", third)

        justified = json.loads(state_path.read_text(encoding="utf-8"))
        justified_runtime = justified["investigation_runtime"]
        justified_runtime["stop_reason"] = "default_wave_limit"
        justified_hypothesis = justified_runtime["hypothesis_ledger"]["items"][0]
        justified_hypothesis["status"] = "contradicted"
        justified_hypothesis["contradicting_evidence"] = ["E0001"]
        state_path.write_text(json.dumps(justified, indent=2) + "\n", encoding="utf-8")
        semantic_one.unlink()
        fourth, _, _ = create_context(
            self.settings, "TICKET-A", self.request("Challenge the remaining G1 hypothesis", base="CTX-003", wave=4),
        )
        state_four = session_state(self.settings, "TICKET-A")
        self.assertEqual(generation_one.generation, state_four["investigation_runtime"]["generation"])
        self.assertEqual(4, state_four["investigation_runtime"]["wave"])
        self.assertNotIn("G2_ONLY", fourth)
        self.assertTrue(any("Semantic" in warning for warning in state_four["request_history"][-1]["retrieval"]["warnings"]))
        self.assertEqual("unavailable", state_four["investigation_runtime"]["serving_state"]["semantic"])

        ticket_b, _, _ = create_context(self.settings, "TICKET-B", self.request("Trace CustomerController G2_ONLY", wave=1))
        self.assertIn("G2_ONLY", ticket_b)
        self.assertNotIn("G1_ONLY", ticket_b)
        self.assertEqual(generation_two.generation, session_state(self.settings, "TICKET-B")["generation"])
        self.assertEqual(generation_two.generation, session_state(self.settings, "TICKET-B")["prefetch"]["generation"])

        with self.assertRaisesRegex(BrainError, "hard four-wave limit"):
            create_context(self.settings, "TICKET-A", self.request("Wrong wave", base="CTX-004", wave=4))
        self.assertEqual(4, HARD_MAX_WAVES)

    def test_first_legacy_protocol_v5_request_signature_binds_resolved_generation(self) -> None:
        generation = self.publish("sha-g1", "G1_ONLY")
        start_session(self.settings, "MIGRATED", "Migrate this source-pinned session.")
        state_path = self.settings.runs_dir / "MIGRATED" / "session.json"
        legacy = json.loads(state_path.read_text(encoding="utf-8"))
        legacy["generation"] = None
        legacy["atlas_generation_id"] = None
        legacy["generation_mode"] = "legacy_source_pin"
        state_path.write_text(json.dumps(legacy, indent=2) + "\n", encoding="utf-8")
        request_text = self.request("Trace CustomerController G1_ONLY", wave=1)
        plan = request_preview(request_text, self.settings)
        unbound = protocol_request_signature(plan, "MIGRATED", legacy)

        create_context(self.settings, "MIGRATED", request_text)
        migrated = session_state(self.settings, "MIGRATED")
        self.assertEqual(generation.identity, migrated["atlas_generation_id"])
        self.assertNotEqual(unbound, migrated["request_history"][-1]["signature"])
        self.assertEqual(
            protocol_request_signature(plan, "MIGRATED", migrated),
            migrated["request_history"][-1]["signature"],
        )

    def test_legacy_session_rejects_snapshot_parent_traversal_before_prefetch_or_retrieval(self) -> None:
        self.publish("sha-g1", "G1_ONLY")
        start_session(self.settings, "LEGACY-ESCAPE", "Do not escape pinned sources.")
        state_path = self.settings.runs_dir / "LEGACY-ESCAPE" / "session.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        escaped = self.settings.state_dir / ".." / "escaped-snapshot"
        escaped.mkdir()
        (escaped / "secret.py").write_text("ACCEPTED_ESCAPE = True\n", encoding="utf-8")
        state.update({"generation": None, "atlas_generation_id": None, "generation_mode": "legacy_source_pin"})
        state["sources"]["customer-api"].update({"sha": "legacy-escape", "snapshot": str(escaped)})
        state.pop("source_signature", None)
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(BrainError, "Pinned source snapshot"):
            prefetch_ticket(self.settings, "LEGACY-ESCAPE")
        with self.assertRaisesRegex(BrainError, "Pinned source snapshot"):
            create_context(self.settings, "LEGACY-ESCAPE", self.request("Find ACCEPTED_ESCAPE", wave=1))

    def test_legacy_session_rejects_symlinked_snapshot(self) -> None:
        self.publish("sha-g1", "G1_ONLY")
        start_session(self.settings, "LEGACY-LINK", "Do not follow snapshot links.")
        outside = self.root / "outside-snapshot"
        outside.mkdir()
        link = self.settings.state_dir / "snapshots" / "customer-api" / "legacy-link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"directory symlinks unavailable: {error}")
        state_path = self.settings.runs_dir / "LEGACY-LINK" / "session.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.update({"generation": None, "atlas_generation_id": None, "generation_mode": "legacy_source_pin"})
        state["sources"]["customer-api"].update({"sha": "legacy-link", "snapshot": str(link)})
        state.pop("source_signature", None)
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(BrainError, "Pinned source snapshot"):
            create_context(self.settings, "LEGACY-LINK", self.request("Find outside evidence", wave=1))

    def test_restart_same_ticket_resets_runtime_lineage_and_budgets(self) -> None:
        generation_one = self.publish("sha-g1", "G1_ONLY")
        start_session(self.settings, "RESTART", "Start on G1.")
        create_context(self.settings, "RESTART", self.request("Trace CustomerController G1_ONLY", wave=1))
        create_context(
            self.settings, "RESTART",
            self.request("Trace customer.updated", base="CTX-001", wave=2),
        )
        create_context(
            self.settings, "RESTART",
            self.request("Trace repository persistence", base="CTX-002", wave=3),
        )
        state_path = self.settings.runs_dir / "RESTART" / "session.json"
        wave_three = json.loads(state_path.read_text(encoding="utf-8"))
        wave_three["investigation_runtime"]["stop_reason"] = "continue"
        wave_three["investigation_runtime"]["hypothesis_ledger"]["items"][0]["status"] = "contradicted"
        wave_three["no_progress_rounds"] = 0
        state_path.write_text(json.dumps(wave_three, indent=2) + "\n", encoding="utf-8")
        create_context(
            self.settings, "RESTART",
            self.request("Challenge the remaining hypothesis", base="CTX-003", wave=4),
        )
        before = session_state(self.settings, "RESTART")
        self.assertEqual(4, before["investigation_runtime"]["wave"])
        self.assertTrue(before["request_history"])
        old_context = self.settings.runs_dir / "RESTART" / "context-004.md"
        self.assertTrue(old_context.is_file())
        before["external_evidence"] = 1
        restart_state_path = self.settings.runs_dir / "RESTART" / "session.json"
        restart_state_path.write_text(json.dumps(before, indent=2) + "\n", encoding="utf-8")
        (self.settings.runs_dir / "RESTART" / "external-001.md").write_text(
            "OLD_EXTERNAL_SHOULD_NOT_LEAK\n", encoding="utf-8",
        )

        start_session(self.settings, "RESTART", "Fresh run on the same pinned generation.")
        reset = session_state(self.settings, "RESTART")
        self.assertEqual(0, reset["requests"])
        self.assertNotIn("investigation_runtime", reset)
        self.assertNotIn("request_history", reset)
        self.assertNotIn("evidence_records", reset)
        self.assertEqual(1, reset["external_evidence_baseline"])
        fresh, _, _ = create_context(self.settings, "RESTART", self.request("Trace CustomerController G1_ONLY", wave=1))
        self.assertNotIn("OLD_EXTERNAL_SHOULD_NOT_LEAK", fresh)
        self.assertEqual(1, session_state(self.settings, "RESTART")["investigation_runtime"]["wave"])

        generation_two = self.publish("sha-g2", "G2_ONLY")
        start_session(self.settings, "RESTART", "Fresh run after G2 publication.")
        restarted, _, _ = create_context(
            self.settings, "RESTART", self.request("Trace CustomerController G2_ONLY", wave=1),
        )
        restarted_state = session_state(self.settings, "RESTART")
        self.assertEqual(1, restarted_state["investigation_runtime"]["wave"])
        self.assertEqual(generation_two.generation, restarted_state["generation"])
        self.assertNotEqual(generation_one.generation, restarted_state["generation"])
        self.assertIn("G2_ONLY", restarted)
        active_names = {item["name"] for item in _session_artifacts(self.settings, "RESTART")}
        self.assertEqual(
            {
                "ticket.md", "start.md", "request-001.yml", "context-001.md", "trace-001.json",
                "checkpoint-001.md", "checkpoint-delta-001.md",
            },
            active_names,
        )
        self.assertTrue(old_context.is_file())
        self.assertNotIn("external-001.md", active_names)

    def test_legacy_session_writers_preserve_all_artifact_visibility(self) -> None:
        self.publish("sha-g1", "G1_ONLY")
        start_session(self.settings, "LEGACY", "Legacy ticket state.")
        create_context(self.settings, "LEGACY", self.request("Trace CustomerController", wave=1))
        state_path = self.settings.runs_dir / "LEGACY" / "session.json"
        legacy = json.loads(state_path.read_text(encoding="utf-8"))
        legacy.pop("active_artifacts", None)
        state_path.write_text(json.dumps(legacy, indent=2) + "\n", encoding="utf-8")
        before = {item["name"] for item in _session_artifacts(self.settings, "LEGACY")}
        self.assertTrue({"ticket.md", "start.md", "context-001.md"}.issubset(before))

        create_context(
            self.settings, "LEGACY",
            self.request("Trace customer.updated", base="CTX-001", wave=2),
        )
        create_feedback(self.settings, "LEGACY", notes="Reviewed.", include_diff=False)
        source = self.root / "runtime-note.txt"
        source.write_text("Observed runtime fact.\n", encoding="utf-8")
        add_external_evidence(self.settings, "LEGACY", source, kind="runtime")
        archive_final_solution(self.settings, "LEGACY", """FINAL_SOLUTION
## Ticket interpretation and remaining assumptions
The change is bounded.
## Verified current behavior
Pinned source verifies it.
## Ordered execution flow and integration flow
The ordered flow is established.
## Root cause
The verified branch omits the case.
## Exact repositories, files, symbols, and configuration/data
Exact paths are listed.
## Suggested production changes
Update the existing branch.
## Impact and test surfaces; tests and assertions
Exact assertions are listed.
## Validation commands
Run approved tests.
## Edge cases and compatibility risks
Compatibility is preserved.
## Implementation order
Source, tests, validation.
## Remaining assumptions
No additional assumptions.
""")

        after_state = session_state(self.settings, "LEGACY")
        self.assertNotIn("active_artifacts", after_state)
        after = {item["name"] for item in _session_artifacts(self.settings, "LEGACY")}
        self.assertTrue(before.issubset(after))
        self.assertTrue({
            "context-002.md", "request-002.yml", "trace-002.json",
            "feedback-001.md", "external-001.md", "final-solution.md",
        }.issubset(after))

    def test_v5_lineage_uses_only_public_evidence_ids_and_marks_incomplete_checkpoint(self) -> None:
        self.publish("sha-g1", "G1_ONLY")
        start_session(self.settings, "LINEAGE", "Trace stable evidence IDs.")
        first, _, _ = create_context(
            self.settings, "LINEAGE", self.request("Trace CustomerController G1_ONLY", wave=1),
        )
        first_state = session_state(self.settings, "LINEAGE")
        public_ids = {record["public_id"] for record in first_state["evidence_records"]}
        self.assertTrue(public_ids)
        self.assertTrue(all(re.fullmatch(r"E[0-9]{4,}", value) for value in public_ids))
        self.assertNotRegex(first, r"E-[0-9a-f]{8}")
        heading_ids = set(re.findall(r"^### \d+\. (E[0-9]+) —", first, re.M))
        self.assertTrue(heading_ids.issubset(public_ids))

        state_path = self.settings.runs_dir / "LINEAGE" / "session.json"
        injected = json.loads(state_path.read_text(encoding="utf-8"))
        original = injected["evidence_records"][0]
        injected["evidence_records"].append({
            **original, "evidence_id": "E-deadbeefdeadbeefdeadbeef", "public_id": "E9999",
            "content_hash": original["content_hash"],
        })
        injected["stable_identities"]["evidence"]["poisoned:E9999"] = "E9999"
        poisoned_hypothesis = injected["investigation_runtime"]["hypothesis_ledger"]["items"][0]
        poisoned_hypothesis["status"] = "supported"
        poisoned_hypothesis["supporting_evidence"] = ["E9999"]
        poisoned_hypothesis["candidate_evidence"] = ["E9999"]
        poisoned_frontier = injected["investigation_runtime"]["evidence_frontier"]["items"][0]
        poisoned_frontier["known"] = ["E9999"]
        poisoned_frontier["verified_evidence"] = ["E9999"]
        state_path.write_text(json.dumps(injected, indent=2) + "\n", encoding="utf-8")
        second_request = json.loads(self.request("Trace customer.updated lineage", base="CTX-001", wave=2))
        second_request["INVESTIGATION_REQUEST"]["hypotheses"] = []
        second, _, _ = create_context(
            self.settings, "LINEAGE", json.dumps(second_request),
        )
        self.assertNotIn("E9999", second)
        self.assertNotRegex(second, r"E-[0-9a-f]{8}")
        second_runtime = session_state(self.settings, "LINEAGE")["investigation_runtime"]
        retained_hypothesis = next(
            item for item in second_runtime["hypothesis_ledger"]["items"]
            if item["identity"] == poisoned_hypothesis["identity"]
        )
        self.assertNotEqual("supported", retained_hypothesis["status"])
        self.assertNotIn("E9999", retained_hypothesis["supporting_evidence"])
        self.assertTrue(all(
            "E9999" not in [*(item.get("known") or []), *(item.get("verified_evidence") or [])]
            for item in second_runtime["evidence_frontier"]["items"]
        ))

        limited = replace(self.settings, hard_context_chars=40_000)
        recovery, _, _ = create_context(
            limited, "LINEAGE",
            self.request("Recover retained evidence", base="stale-context", wave=3),
        )
        self.assertIn("Replacement status: `incomplete_non_replacing`", recovery)
        self.assertIn("Do not replace accumulated client evidence", recovery)
        self.assertIn("Retained evidence manifest", recovery)
        self.assertNotRegex(recovery, r"E-[0-9a-f]{8}")

    def test_bounded_full_and_delta_metadata_names_only_emitted_evidence(self) -> None:
        from brain.core import _evidence_id, pack_context, pack_delta_context

        limited = replace(self.settings, hard_context_chars=18_000, soft_target_chars=100_000)
        evidence = [
            Evidence(
                "customer-api", f"src/main/java/demo/Large{index}.java", 1, 1,
                f"class Large{index} {{ /* {'x' * 7_000} */ }}", "code", 100,
                ["exact test fixture"],
            )
            for index in range(3)
        ]
        public = {_evidence_id(item): f"E{index:04d}" for index, item in enumerate(evidence, 1)}
        manifest = [
            {
                "evidence_id": public[_evidence_id(item)], "repo": item.repo, "path": item.path,
                "line_start": item.line_start, "line_end": item.line_end, "status": "included",
            }
            for item in evidence
        ]
        progress = {
            "context_id": "CTX-001", "base_context_id": None, "operations": 1,
            "new_evidence": 3, "known_evidence": 0, "no_progress_rounds": 0,
            "history": [], "coverage": {}, "coverage_map": {}, "protocol_version": 5,
            "new_evidence_ids": list(public.values()), "superseded_evidence_ids": [],
            "checkpoint": True, "checkpoint_replacement": "complete_replacement",
            "retained_evidence_manifest": manifest, "evidence_public_ids": public,
        }
        bundle = ContextBundle("bounded protocol", evidence=evidence)
        full = pack_context(limited, "BOUNDED", 1, bundle, progress)
        full_headings = set(re.findall(r"^### \d+\. (E[0-9]+) —", full, re.M))
        embedded = set(re.search(r"^- Embedded evidence IDs: `([^`]*)`$", full, re.M).group(1).split(", "))
        omitted = set(re.search(r"^- Omitted evidence IDs due to byte limit: `([^`]*)`$", full, re.M).group(1).split(", "))
        embedded.discard("none")
        omitted.discard("none")
        self.assertEqual(full_headings, embedded)
        self.assertTrue(omitted)
        self.assertEqual(set(public.values()), embedded | omitted)
        self.assertIn("Replacement status: `incomplete_non_replacing`", full)
        for identifier in omitted:
            self.assertRegex(full, rf"(?m)^- `{identifier}` .* — `omitted_by_byte_limit`$")

        delta_progress = {
            "context_id": "CTX-002", "base_context_id": "CTX-001", "evidence_public_ids": public,
            "coverage_changes": {}, "memory_changes": {}, "superseded_evidence_ids": [],
        }
        delta = pack_delta_context(
            limited, "BOUNDED", 2, bundle, delta_progress, set(public),
        )
        delta_headings = set(re.findall(r"^### (E[0-9]+) —", delta, re.M))
        delta_embedded = set(re.search(r"^- Embedded evidence IDs: `([^`]*)`$", delta, re.M).group(1).split(", "))
        delta_omitted = set(re.search(r"^- Omitted evidence IDs due to byte limit: `([^`]*)`$", delta, re.M).group(1).split(", "))
        delta_embedded.discard("none")
        delta_omitted.discard("none")
        self.assertEqual(delta_headings, delta_embedded)
        self.assertTrue(delta_omitted)
        self.assertEqual(set(public.values()), delta_embedded | delta_omitted)

    def test_coverage_verification_requires_generation_valid_exact_evidence_proofs(self) -> None:
        from brain.core import _evidence_id

        generation = self.publish("sha-g1", "G1_ONLY")
        poisoned_state = {
            "coverage_map": {
                key: "verified" for key in (
                    "production_entry_point", "main_execution_flow", "cross_repo_integration",
                    "configuration", "data_schema", "tests", "history", "impact_surface", "contract_surface",
                )
            },
            "stable_identities": {},
        }
        empty = ContextBundle("poisoned coverage", atlas_generation=generation)
        poisoned = build_ticket_runtime(
            self.settings, generation, {"objective": "NoSuchRuntimeAnchor", "anchors": []},
            empty, poisoned_state, context_id="CTX-001",
        )
        self.assertFalse(any(value == "verified" for value in poisoned["coverage"].values()))
        self.assertTrue(poisoned["evidence_frontier"]["items"])
        self.assertNotEqual("coverage_satisfied", poisoned["stop_reason"])

        source_path = "src/main/java/demo/CustomerController.java"
        snapshot = Path(self.settings.repo("customer-api").source_path)
        content = (snapshot / source_path).read_text(encoding="utf-8")
        evidence = Evidence(
            "customer-api", source_path, 1, len(content.splitlines()), content,
            "code", 100, ["exact test fixture"], verification_content=content,
        )
        proof_state: dict[str, object] = {"coverage_map": {}, "stable_identities": {}}
        first = build_ticket_runtime(
            self.settings, generation,
            {"objective": "Trace /customers/{id}", "anchors": [{"kind": "endpoint", "value": "/customers/{id}"}]},
            ContextBundle("proof", evidence=[evidence], atlas_generation=generation),
            proof_state, context_id="CTX-001",
        )
        self.assertIn("production_entry_point", first["coverage_proofs"])
        surface_runtime = json.loads(json.dumps(first))
        surface_runtime["surfaces"]["test"].append({
            "repo": "customer-api", "path": source_path, "state": "verified",
            "confidence": 1.0, "evidence_authority": "exact_source", "evidence_ids": [],
        })
        surface_runtime["program_slice"]["statements"].append({
            "identity": "sha256:forged-slice", "repo": "customer-api", "path": source_path,
            "line": 1, "summary": "forged retained statement", "state": "candidate",
        })
        surface_state = {
            **proof_state, "investigation_runtime": surface_runtime, "coverage_map": first["coverage"],
        }
        reclassified = build_ticket_runtime(
            self.settings, generation,
            {"objective": "Trace /customers/{id}", "anchors": [{"kind": "endpoint", "value": "/customers/{id}"}]},
            ContextBundle("proof", evidence=[evidence], atlas_generation=generation),
            surface_state, context_id="CTX-002",
        )
        self.assertFalse(any(item.get("path") == source_path for item in reclassified["surfaces"]["test"]))
        self.assertTrue(any(item.get("path") == source_path for item in reclassified["surfaces"]["implementation"]))
        self.assertFalse(any(
            item.get("identity") == "sha256:forged-slice"
            for item in reclassified["program_slice"]["statements"]
        ))
        internal = _evidence_id(evidence)
        public_id = first["coverage_proofs"]["production_entry_point"][0]
        semantically_poisoned = json.loads(json.dumps(first))
        unrelated_keys = ("cross_repo_integration", "tests", "configuration", "data_schema")
        semantically_poisoned["coverage_proofs"].update({key: [public_id] for key in unrelated_keys})
        semantically_poisoned["coverage"].update({key: "verified" for key in unrelated_keys})
        hypothesis_identity = "sha256:" + hashlib.sha256(
            b"hypothesis\0unrelated feature is true",
        ).hexdigest()
        semantically_poisoned["hypothesis_ledger"]["items"] = [{
            "identity": hypothesis_identity,
            "hypothesis_id": _allocate(
                proof_state["stable_identities"], "hypotheses", hypothesis_identity, "H", 3,
            ),
            "statement": "unrelated feature is true", "origin": "poisoned session",
            "status": "supported", "candidate_evidence": [public_id],
            "supporting_evidence": [public_id], "contradicting_evidence": [],
            "verification": "poisoned session classification",
        }]
        semantically_poisoned["evidence_frontier"]["items"][0].update({
            "known": [public_id], "verified_evidence": [public_id],
        })
        valid_record = {
            "evidence_id": internal, "public_id": public_id, "repo": "customer-api", "path": source_path,
            "line_start": 1, "line_end": len(content.splitlines()),
            "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "generation": generation.generation,
        }
        poisoned_state = {
            **proof_state,
            "investigation_runtime": semantically_poisoned,
            "coverage_map": semantically_poisoned["coverage"],
            "sources": {"customer-api": {"sha": "sha-g1", "snapshot": str(snapshot)}},
            "evidence_records": [valid_record],
        }
        semantically_revalidated = build_ticket_runtime(
            self.settings, generation, {"objective": "NoSuchRuntimeAnchor", "anchors": []},
            empty, poisoned_state, context_id="CTX-002",
        )
        self.assertTrue(all(
            semantically_revalidated["coverage"].get(key) != "verified" for key in unrelated_keys
        ))
        retained_hypothesis = semantically_revalidated["hypothesis_ledger"]["items"][0]
        self.assertNotEqual("supported", retained_hypothesis["status"])
        self.assertEqual([], retained_hypothesis["supporting_evidence"])
        self.assertTrue(all(
            public_id not in [*(item.get("known") or []), *(item.get("verified_evidence") or [])]
            for item in semantically_revalidated["evidence_frontier"]["items"]
        ))

        proof_state.update({
            "investigation_runtime": first,
            "coverage_map": first["coverage"],
            "sources": {
                "customer-api": {"sha": "sha-g1", "snapshot": str(snapshot)},
            },
            "evidence_records": [{
                "evidence_id": internal, "public_id": public_id, "repo": "customer-api", "path": source_path,
                "line_start": 1, "line_end": len(content.splitlines()),
                "content_hash": "corrupt-proof", "generation": generation.generation,
            }],
        })
        second = build_ticket_runtime(
            self.settings, generation, {"objective": "NoSuchRuntimeAnchor", "anchors": []},
            empty, proof_state, context_id="CTX-002",
        )
        self.assertNotEqual("verified", second["coverage"].get("production_entry_point"))
        self.assertNotIn("production_entry_point", second["coverage_proofs"])

    def test_wave_two_lexical_only_prior_evidence_is_batch_validated_against_its_pin(self) -> None:
        from brain import index as index_module
        from brain.core import _evidence_id
        from brain.investigation import _validated_prior_evidence_ids, stable_evidence_id

        generation_one = self.publish("sha-lexical-prior-g1", "LEXICAL_PRIOR_G1")
        repo = "customer-api"
        path = "src/main/java/demo/CustomerController.java"
        content = "\n".join(
            (Path(self.settings.repo(repo).source_path) / path).read_text(encoding="utf-8").splitlines()
        )
        evidence = Evidence(
            repo, path, 1, len(content.splitlines()), content,
            "code", 100, ["exact test fixture"], verification_content=content,
        )
        state: dict[str, object] = {"coverage_map": {}, "stable_identities": {}}
        public_id = stable_evidence_id(state, evidence)
        state.update({
            "investigation_runtime": {"coverage_proofs": {"production_entry_point": [public_id]}},
            "sources": {repo: {"sha": generation_one.snapshots[repo], "snapshot": ""}},
            "evidence_records": [{
                "evidence_id": _evidence_id(evidence), "public_id": public_id,
                "repo": repo, "path": path, "line_start": 1, "line_end": len(content.splitlines()),
                "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "generation": generation_one.generation,
            }],
        })
        self.publish("sha-lexical-prior-g2", "LEXICAL_PRIOR_G2")
        with mock.patch("brain.index._connect", wraps=index_module._connect) as opened:
            valid = _validated_prior_evidence_ids(
                self.settings, generation_one, state,
                ContextBundle("wave two", atlas_generation=generation_one),
                state["stable_identities"],
            )
        self.assertEqual(1, opened.call_count)
        self.assertEqual({public_id}, valid)

    def test_investigation_memory_rebuilds_verified_fields_from_pinned_proofs(self) -> None:
        self.publish("sha-g1", "G1_ONLY")
        start_session(self.settings, "MEMORY-PROOF", "Reject forged verified memory.")
        create_context(
            self.settings, "MEMORY-PROOF",
            self.request("Trace CustomerController memory proof", wave=1),
        )
        state_path = self.settings.runs_dir / "MEMORY-PROOF" / "session.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        memory = state["investigation_memory"]
        memory["verified_facts"].append({
            "evidence_id": "E9999", "reference": "evil:fake.py:1-1",
            "kind": "forged", "verified_by": ["forged session"],
        })
        memory["verified_references"].append("evil:fake.py:1-1")
        memory["implementation_surface"].append("evil:fake.py")
        memory["test_surface"].append("evil:fake_test.py")
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        second, _, _ = create_context(
            self.settings, "MEMORY-PROOF",
            self.request(
                "Trace customer.updated memory proof",
                base=state["last_context_id"], wave=2,
            ),
        )
        self.assertNotIn("E9999", second)
        self.assertNotIn("evil:fake", second)
        retained = session_state(self.settings, "MEMORY-PROOF")["investigation_memory"]
        self.assertFalse(any(item.get("evidence_id") == "E9999" for item in retained["verified_facts"]))
        self.assertNotIn("evil:fake.py:1-1", retained["verified_references"])
        self.assertNotIn("evil:fake.py", retained["implementation_surface"])
        self.assertNotIn("evil:fake_test.py", retained["test_surface"])

    def test_new_cross_wave_hypothesis_is_not_hidden_behind_retained_ledger_items(self) -> None:
        generation = self.publish("sha-hypothesis-bound", "HYPOTHESIS_BOUND")
        bundle = ContextBundle("hypotheses", atlas_generation=generation)
        state: dict[str, object] = {"coverage_map": {}, "stable_identities": {}}
        first = build_ticket_runtime(
            self.settings, generation,
            {"objective": "Bound hypotheses", "hypotheses": [f"prior hypothesis {index}" for index in range(50)]},
            bundle, state, context_id="CTX-001",
        )
        self.assertEqual(50, len(first["hypothesis_ledger"]["items"]))
        state["investigation_runtime"] = first
        second = build_ticket_runtime(
            self.settings, generation,
            {"objective": "Bound hypotheses", "hypotheses": ["new second-wave hypothesis"]},
            bundle, state, context_id="CTX-002",
        )
        statements = {item["statement"] for item in second["hypothesis_ledger"]["items"]}
        self.assertEqual(51, len(statements))
        self.assertIn("new second-wave hypothesis", statements)

    def test_gc_retains_then_reclaims_v1_generation_memberships(self) -> None:
        generation_one = self.publish("sha-g1", "G1_ONLY")
        start_session(self.settings, "PINNED", "Keep G1.")
        generation_two = self.publish("sha-g2", "G2_ONLY")
        report = gc(self.settings, dry_run=True, keep_recent=1)
        old_path = str(self.settings.state_dir / "generations" / f"generation-{generation_one.generation:06d}")
        self.assertNotIn(old_path, [item["path"] for item in report["remove"]])
        (self.settings.runs_dir / "PINNED" / "session.json").unlink()
        report = gc(self.settings, dry_run=True, keep_recent=1)
        self.assertIn(old_path, [item["path"] for item in report["remove"]])
        gc(self.settings, dry_run=False, keep_recent=1)
        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM generation_runtime_anchors WHERE generation=?", (generation_one.generation,),
            ).fetchone()[0])
            self.assertGreater(connection.execute(
                "SELECT COUNT(*) FROM generation_runtime_anchors WHERE generation=?", (generation_two.generation,),
            ).fetchone()[0], 0)
        finally:
            connection.close()

    def test_gc_blocks_inconsistent_atlas_session_generation_and_snapshot_paths(self) -> None:
        generation_one = self.publish("sha-gc-safe-1", "GC_SAFE_ONE")
        start_session(self.settings, "GC-SAFE", "Keep the exact first generation.")
        generation_two = self.publish("sha-gc-safe-2", "GC_SAFE_TWO")
        self.publish("sha-gc-safe-3", "GC_SAFE_THREE")
        state_path = self.settings.runs_dir / "GC-SAFE" / "session.json"
        original = json.loads(state_path.read_text(encoding="utf-8"))
        snapshot = Path(original["sources"]["customer-api"]["snapshot"])
        self.assertTrue(snapshot.is_dir())

        wrong_number = json.loads(json.dumps(original))
        wrong_number["generation"] = generation_two.generation
        state_path.write_text(json.dumps(wrong_number), encoding="utf-8")
        report = gc(self.settings, dry_run=False, keep_recent=1)
        self.assertTrue(report["reachability_gc_blocked"])
        self.assertTrue(snapshot.is_dir())

        wrong_path = json.loads(json.dumps(original))
        wrong_path["sources"]["customer-api"]["snapshot"] = str(self.root / "outside-snapshot")
        state_path.write_text(json.dumps(wrong_path), encoding="utf-8")
        report = gc(self.settings, dry_run=False, keep_recent=1)
        self.assertTrue(report["reachability_gc_blocked"])
        self.assertTrue(snapshot.is_dir())

        external = self.root / "outside-session.json"
        external.write_text(json.dumps(original), encoding="utf-8")
        state_path.unlink()
        try:
            state_path.symlink_to(external)
        except OSError as error:
            self.skipTest(f"symbolic links unavailable: {error}")
        report = gc(self.settings, dry_run=False, keep_recent=1)
        self.assertTrue(report["reachability_gc_blocked"])
        self.assertTrue(snapshot.is_dir())
        state_path.unlink()
        state_path.write_bytes(b"x" * (4 * 1024 * 1024 + 1))
        report = gc(self.settings, dry_run=False, keep_recent=1)
        self.assertTrue(report["reachability_gc_blocked"])
        self.assertTrue(snapshot.is_dir())
        self.assertIsNotNone(generation_one)

    def test_gc_blocks_noncanonical_generation_entries_without_deleting_anything(self) -> None:
        self.publish("sha-gc-canonical", "GC_CANONICAL")
        generations = self.settings.state_dir / "generations"
        rogue = generations / "generation-000000x"
        rogue.mkdir()
        marker = rogue / "must-remain.txt"
        marker.write_text("preserve", encoding="utf-8")

        for dry_run in (True, False):
            report = gc(self.settings, dry_run=dry_run, keep_recent=1)
            self.assertEqual([], report["remove"])
            self.assertTrue(report["reachability_gc_blocked"])
            self.assertIn("non-canonical", report["reachability_gc_blocked"][0])
            self.assertEqual(b"preserve", marker.read_bytes())

    def test_gc_fails_closed_when_the_session_root_is_substituted(self) -> None:
        generation_one = self.publish("sha-gc-root-one", "GC_ROOT_ONE")
        start_session(self.settings, "GC-ROOT-PIN", "Keep the first generation pinned.")
        self.publish("sha-gc-root-two", "GC_ROOT_TWO")
        generation_one_root = (
            self.settings.state_dir / "generations" /
            f"generation-{generation_one.generation:06d}"
        )
        preserved = self.settings.runs_dir.with_name("runs-preserved")
        outside = self.root / "outside-runs"
        outside.mkdir()
        self.settings.runs_dir.rename(preserved)
        try:
            self.settings.runs_dir.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            preserved.rename(self.settings.runs_dir)
            self.skipTest(f"symbolic links unavailable: {error}")
        try:
            for dry_run in (True, False):
                report = gc(self.settings, dry_run=dry_run, keep_recent=1)
                self.assertEqual([], report["remove"])
                self.assertTrue(report["reachability_gc_blocked"])
                self.assertTrue(generation_one_root.is_dir())
        finally:
            self.settings.runs_dir.unlink(missing_ok=True)
            preserved.rename(self.settings.runs_dir)

    def test_future_session_schema_is_never_downgraded_or_rewritten(self) -> None:
        from brain import core as core_module

        self.publish("sha-future-session", "FUTURE_SESSION")
        ticket = "FUTURE-SCHEMA"
        start_session(self.settings, ticket, "Preserve a future session exactly.")
        directory = self.settings.runs_dir / ticket
        state_path = directory / "session.json"
        future = json.loads(state_path.read_text(encoding="utf-8"))
        future["session_schema_version"] = 999
        state_path.write_text(json.dumps(future, sort_keys=True), encoding="utf-8")
        future_bytes = state_path.read_bytes()
        with self.assertRaisesRegex(BrainError, "newer session schema"):
            core_module.save_session(
                self.settings, ticket,
                {"ticket": ticket, "session_schema_version": 3, "marker": "downgraded"},
            )
        self.assertEqual(future_bytes, state_path.read_bytes())
        before = {
            str(path.relative_to(directory)): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()
        }
        final_solution = """FINAL_SOLUTION
## Ticket interpretation and remaining assumptions
Bounded.
## Verified current behavior
Verified.
## Ordered execution flow and integration flow
Ordered.
## Root cause
Known.
## Exact repositories, files, symbols, and configuration/data
Exact.
## Suggested production changes
Change.
## Impact and test surfaces; tests and assertions
Tests.
## Validation commands
Validate.
## Edge cases and compatibility risks
Preserved.
## Implementation order
Ordered.
## Remaining assumptions
None.
"""

        for operation in (
            lambda: prefetch_ticket(self.settings, ticket),
            lambda: create_context(
                self.settings, ticket, self.request("Do not downgrade this session", wave=1),
            ),
            lambda: start_session(self.settings, ticket, "Do not restart a future session."),
            lambda: archive_final_solution(self.settings, ticket, final_solution),
        ):
            with self.assertRaisesRegex(BrainError, "newer session schema"):
                operation()
            after = {
                str(path.relative_to(directory)): path.read_bytes()
                for path in directory.rglob("*") if path.is_file()
            }
            self.assertEqual(before, after)

        for dry_run in (True, False):
            report = gc(self.settings, dry_run=dry_run, keep_recent=1)
            self.assertEqual([], report["remove"])
            self.assertTrue(report["reachability_gc_blocked"])
            self.assertEqual(before, {
                str(path.relative_to(directory)): path.read_bytes()
                for path in directory.rglob("*") if path.is_file()
            })

    def test_missing_pinned_semantic_shard_is_explicitly_unavailable(self) -> None:
        generation = self.publish("sha-g1", "G1_ONLY")
        shard_path = self.settings.state_dir / "semantic-shards" / "corrupt.usearch"
        shard_path.parent.mkdir(parents=True, exist_ok=True)
        shard_path.write_bytes(b"corrupt-vector-index")
        semantic_state = {
            "backend": "usearch", "pack_id": "v1-test-pack", "dimension": 3,
            "stale": False,
            "chunk_schema_version": CHUNK_SCHEMA_VERSION,
            "card_version": CARD_VERSION,
            "embedding_input_version": SEMANTIC_EMBEDDING_INPUT_VERSION,
            "atlas_card_version": ATLAS_CARD_VERSION,
            "shard_manifest_version": SEMANTIC_SHARD_MANIFEST_VERSION,
            "shards": [{
                "repo": "customer-api", "snapshot": "sha-g1", "path": str(shard_path),
                "entries": [{"path": "src/main/java/demo/CustomerController.java", "chunk_id": "c1", "line": 1}],
            }],
        }

        class BrokenIndex:
            @classmethod
            def restore(cls, path: str, view: bool = True) -> object:
                raise ValueError("corrupt shard")

        class FakeNumpy:
            float32 = object()

            @staticmethod
            def asarray(value: object, dtype: object = None) -> object:
                return value

        serving: dict[str, str] = {}
        with mock.patch("brain.semantic._serving_state", return_value=semantic_state), mock.patch(
            "brain.semantic._usearch", return_value=(BrokenIndex, FakeNumpy),
        ):
            results = search_semantic(
                self.settings, "CustomerController", repos={"customer-api"}, embed=self.embed,
                generation=generation, serving_status=serving,
            )
        self.assertEqual([], results)
        self.assertEqual("unavailable", serving["status"])

    def test_gc_preserves_semantic_shards_when_retained_manifest_is_corrupt(self) -> None:
        generation_one = self.publish("sha-g1", "G1_ONLY")
        start_session(self.settings, "SEMANTIC-PIN", "Keep the G1 Semantic component reachable.")
        semantic_path = self.settings.state_dir / str(generation_one.component("semantic")["artifact_ref"])
        semantic_state = json.loads(semantic_path.read_text(encoding="utf-8"))
        retained_shards = [Path(item["path"]) for item in semantic_state["shards"]]
        self.assertTrue(all(path.is_file() for path in retained_shards))
        self.publish("sha-g2", "G2_ONLY")
        semantic_path.write_text("not-json", encoding="utf-8")
        dry_run = gc(self.settings, dry_run=True, keep_recent=1)
        self.assertTrue(dry_run["semantic_gc_blocked"])
        self.assertFalse(any(
            item["kind"] == "semantic_shard" and Path(item["path"]) in retained_shards
            for item in dry_run["remove"]
        ))
        gc(self.settings, dry_run=False, keep_recent=1)
        self.assertTrue(all(path.is_file() for path in retained_shards))

    def test_protocol_validation_m365_v4_kit_and_cockpit_contract(self) -> None:
        parsed = parse_context_request(self.request("Trace CustomerController", wave=1))
        self.assertEqual(5, parsed["version"])
        self.assertEqual("root_cause", parsed["mode"])
        nested = parse_context_request(json.dumps({"INVESTIGATION_REQUEST": {
            "version": 5, "mode": "flow_trace", "objective": "Trace a production frame",
            "anchors": {"symbols": ["CustomerController"], "stack_frames": ["demo.CustomerController.find(CustomerController.java:8)"]},
            "hypotheses": [{"id": "H1", "statement": "The controller delegates to the service"}],
        }}))
        self.assertEqual(["The controller delegates to the service"], nested["hypotheses"])
        self.assertEqual(["symbol", "stack_frame"], [item["kind"] for item in nested["anchors"]])
        with self.assertRaisesRegex(BrainError, "unknown keys"):
            parse_context_request(json.dumps({"INVESTIGATION_REQUEST": {
                "version": 5, "mode": "root_cause", "objective": "x", "unbounded": True,
            }}))
        with self.assertRaisesRegex(BrainError, "invalid kind or value"):
            parse_context_request(json.dumps({"INVESTIGATION_REQUEST": {
                "version": 5, "mode": "root_cause", "objective": "x",
                "anchors": [{"kind": "shell", "value": "run tests"}],
            }}))
        with self.assertRaisesRegex(BrainError, "up to 500 characters"):
            parse_context_request(json.dumps({"INVESTIGATION_REQUEST": {
                "version": 5, "mode": "root_cause", "objective": "x", "runtime_facts": ["x" * 501],
            }}))

        stack_frame = "demo.CustomerController.find(CustomerController.java:7)"
        prioritized = parse_context_request(json.dumps({"INVESTIGATION_REQUEST": {
            "version": 5, "mode": "root_cause", "objective": "Trace the production failure",
            "resolve": [f"resolve-{index:02d}" for index in range(12)],
            "runtime_facts": [f"runtime-fact-{index:02d}" for index in range(49)],
            "anchors": [{"kind": "stack_frame", "value": stack_frame}],
        }}))
        self.assertEqual(stack_frame, prioritized["searches"][0]["query"])
        self.assertEqual(stack_frame, compile_request(prioritized).operations[0].value)
        bounded = _bounded_anchor_queries([
            *prioritized["anchors"], *prioritized["resolve"],
            *prioritized["runtime_facts"], prioritized["objective"],
        ])
        self.assertEqual(("stack_frame", stack_frame), bounded[0])
        routing_request = {
            **prioritized,
            "anchors": [
                *({"kind": "symbol", "value": f"noise.anchor.{index:02d}"} for index in range(49)),
                {"kind": "stack_frame", "value": "zfinal.lowrank.stackframe"},
            ],
        }
        self.assertIn("zfinal.lowrank.stackframe", _prioritized_routing_terms(
            routing_request["objective"], routing_request,
        ))

        prioritized_generation = self.publish("sha-anchor-priority", "ANCHOR_PRIORITY")
        runtime = build_ticket_runtime(
            self.settings, prioritized_generation, prioritized,
            ContextBundle(prioritized["objective"], atlas_generation=prioritized_generation),
            {"coverage_map": {}, "stable_identities": {}}, context_id="CTX-ANCHOR-PRIORITY",
        )
        self.assertEqual(stack_frame, runtime["anchors"]["inputs"][0])
        routed = route(
            self.settings, prioritized["objective"], prioritized, prioritized_generation,
            repo_limit=16, entity_limit=80,
        )
        self.assertIn("customer-api", routed["repos"])
        self.assertTrue(any(item["repo"] == "customer-api" for item in routed["candidates"]))
        kit = create_m365_agent_kit(self.settings)
        self.assertEqual("1.0.0", kit["manifest"]["manifest_version"])
        self.assertEqual(4, kit["manifest"]["agent_kit_version"])
        self.assertEqual(5, kit["manifest"]["context_request_protocol"])
        self.assertEqual(5, kit["manifest"]["investigation_protocol"])
        self.assertEqual([1, 2, 3, 4], kit["manifest"]["legacy_protocols"])
        self.assertTrue(Path(kit["protocol_path"]).is_file())
        self.assertIn("INTAKE", kit["instructions"])
        self.assertIn("Program Slice Lite", kit["protocol"])
        prompt = (Path(__file__).parents[1] / "brain" / "prompt.md").read_text(encoding="utf-8")
        self.assertIn("version: 5", prompt)
        self.assertIn("mode: root_cause", prompt)
        self.assertIn("New requests use version 5", prompt)
        self.assertNotIn("INVESTIGATION_REQUEST:\n  version: 4", prompt)

        generation = self.publish("sha-g1", "G1_ONLY")
        start_content, _ = start_session(self.settings, "PROMPT-V5", "Use the default operating protocol.")
        copied = re.search(r"```yaml\n(INVESTIGATION_REQUEST:[\s\S]*?)\n```", start_content)
        self.assertIsNotNone(copied)
        self.assertEqual(5, parse_context_request(copied.group(1))["version"])
        create_context(self.settings, "PROMPT-V5", copied.group(1))
        self.assertEqual(1, session_state(self.settings, "PROMPT-V5")["investigation_runtime"]["wave"])
        start_session(self.settings, "DUPLICATE", "Trace G1.")
        request = self.request("Trace CustomerController G1_ONLY", wave=1)
        create_context(self.settings, "DUPLICATE", request)
        duplicate = response_preview(request, self.settings, "DUPLICATE")
        self.assertEqual(1, duplicate["duplicate_of"])
        self.assertEqual(generation.generation, session_state(self.settings, "DUPLICATE")["generation"])

    def test_first_useful_fails_closed_for_groovy_and_recovers_a_pending_long_window(self) -> None:
        late_source = '@GetMapping("/late")\npublic Customer late() { return null; }'
        late_full_source = "\n" * 100 + late_source
        late_path = self.root / "customer-api/src/main/java/demo/LateController.java"
        late_path.write_text(late_full_source, encoding="utf-8")
        generation = self.publish("sha-g1", "G1_ONLY")
        start_session(self.settings, "GROOVY-CHECKPOINT", "Do not trust Groovy literals as entry points.")
        groovy_state = session_state(self.settings, "GROOVY-CHECKPOINT")
        groovy = 'class Fake { def spec = / @GetMapping("/ghost") / }'
        groovy_bundle = ContextBundle(
            "ghost", evidence=[Evidence(
                "customer-api", "src/Fake.groovy", 101, 101, groovy, "code", 100,
            )], atlas_generation=generation,
        )
        self.assertIsNone(_publish_first_useful_checkpoint(
            self.settings, "GROOVY-CHECKPOINT", 1, "CTX-001", None, groovy_bundle,
            {"objective": "Trace /ghost", "runtime_facts": [], "resolve": [], "anchors": []},
            "groovy-signature", groovy_state, self.settings.runs_dir / "GROOVY-CHECKPOINT", None,
        ))

        start_session(self.settings, "PENDING-CHECKPOINT", "Recover a durable early checkpoint.")
        request_text = self.request("Trace /late endpoint", wave=1)
        request = parse_context_request(request_text)
        pending_state = session_state(self.settings, "PENDING-CHECKPOINT")
        signature = protocol_request_signature(
            request_preview(request_text, self.settings), "PENDING-CHECKPOINT", pending_state,
        )
        late_bundle = ContextBundle(
            "late", evidence=[Evidence(
                "customer-api", "src/main/java/demo/LateController.java", 101, 102,
                late_source, "code", 100, [], late_full_source,
            )], atlas_generation=generation,
        )
        published = _publish_first_useful_checkpoint(
            self.settings, "PENDING-CHECKPOINT", 1, "CTX-001", None, late_bundle,
            request, signature, pending_state, self.settings.runs_dir / "PENDING-CHECKPOINT", None,
        )
        self.assertIsNotNone(published)
        self.assertEqual("pending", published["continuation_status"])
        self.assertIn("LateController.java:101-102", (
            self.settings.runs_dir / "PENDING-CHECKPOINT" / published["artifact"]
        ).read_text(encoding="utf-8"))
        with self.assertRaisesRegex(BrainError, "pending or failed continuation"):
            create_context(
                self.settings, "PENDING-CHECKPOINT",
                self.request("Change plan before completing /late", wave=1),
            )
        create_context(self.settings, "PENDING-CHECKPOINT", request_text)
        recovered = session_state(self.settings, "PENDING-CHECKPOINT")["progressive_checkpoint"]
        self.assertEqual(published["checkpoint_id"], recovered["checkpoint_id"])
        self.assertEqual("published", recovered["continuation_status"])
        self.assertTrue((
            self.settings.runs_dir / "PENDING-CHECKPOINT" / recovered["continuation_artifact"]
        ).is_file())

    def test_protocol_v5_progress_and_stale_base_checkpoint_recovery(self) -> None:
        self.publish("sha-g1", "G1_ONLY")
        start_session(self.settings, "RECOVERY", "Trace CustomerController and retain exact evidence.")
        events: list[dict[str, object]] = []
        early_state: dict[str, object] = {}

        def observe(event: dict[str, object]) -> None:
            events.append(event)
            if event.get("phase") == "first_useful_checkpoint":
                checkpoint_path = self.settings.runs_dir / "RECOVERY" / str(event["checkpoint_artifact"])
                self.assertTrue(checkpoint_path.is_file())
                self.assertIn("G1_ONLY", checkpoint_path.read_text(encoding="utf-8"))
                early_state.update(session_state(self.settings, "RECOVERY"))

        first, _, _ = create_context(
            self.settings, "RECOVERY", self.request("Trace CustomerController G1_ONLY", wave=1), progress=observe,
        )
        self.assertIn("G1_ONLY", first)
        self.assertEqual("retrieving", early_state["status"])
        self.assertEqual(0, early_state["requests"])
        phases = [str(item["phase"]) for item in events]
        expected = [
            "wave_started", "first_useful_checkpoint", "anchors_resolved", "flow_built",
            "evidence_verified", "packing_context",
        ]
        self.assertEqual(expected, [phase for phase in phases if phase in expected])
        self.assertIn(phases[-1], {"wave_complete", "investigation_complete"})
        published = session_state(self.settings, "RECOVERY")["progressive_checkpoint"]
        self.assertEqual("published", published["continuation_status"])
        continuation = self.settings.runs_dir / "RECOVERY" / published["continuation_artifact"]
        self.assertTrue(continuation.is_file())
        self.assertIn(f"Base context ID: `{published['checkpoint_id']}`", continuation.read_text(encoding="utf-8"))

        start_session(self.settings, "OBJECTIVE-ONLY", "Trace the production endpoint without supplied anchors.")
        objective_only = json.loads(self.request("Trace production endpoint /customers/{id}", wave=1))
        objective_only["INVESTIGATION_REQUEST"]["anchors"] = []
        objective_only["INVESTIGATION_REQUEST"]["resolve"] = []
        objective_events: list[dict[str, object]] = []
        create_context(
            self.settings, "OBJECTIVE-ONLY", json.dumps(objective_only), progress=objective_events.append,
        )
        self.assertIn("first_useful_checkpoint", {event["phase"] for event in objective_events})
        self.assertTrue(session_state(self.settings, "OBJECTIVE-ONLY")["progressive_checkpoint"]["artifact"])

        start_session(self.settings, "ENDPOINT-ONLY", "Trace only the production route.")
        endpoint_only = json.loads(self.request("Trace /customers/{id}", wave=1))
        endpoint_only["INVESTIGATION_REQUEST"]["resolve"] = []
        endpoint_only["INVESTIGATION_REQUEST"]["anchors"] = [
            {"kind": "endpoint", "value": "/customers/{id}"},
        ]
        create_context(self.settings, "ENDPOINT-ONLY", json.dumps(endpoint_only))
        endpoint_runtime = session_state(self.settings, "ENDPOINT-ONLY")["investigation_runtime"]
        exact_endpoints = [
            item for item in endpoint_runtime["anchors"]["candidates"]
            if item.get("kind") == "endpoint" and item.get("value") == "/customers/{id}"
            and item.get("evidence_authority") == "exact_source"
        ]

        self.assertTrue(exact_endpoints)
        self.assertTrue(any(item.get("entity_id") for item in exact_endpoints))
        self.assertNotEqual("verified", endpoint_runtime["coverage"].get("main_execution_flow"))

        start_session(self.settings, "CHECKPOINT-FAILURE", "Verify retryable progressive recovery.")
        retry_request = self.request("Trace CustomerController G1_ONLY after a continuation failure", wave=1)
        with mock.patch("brain.investigation.build_ticket_runtime", side_effect=RuntimeError("injected continuation failure")):
            with self.assertRaisesRegex(RuntimeError, "injected continuation failure"):
                create_context(self.settings, "CHECKPOINT-FAILURE", retry_request)
        failed = session_state(self.settings, "CHECKPOINT-FAILURE")
        self.assertEqual("waiting_for_ai", failed["status"])
        self.assertEqual("failed", failed["progressive_checkpoint"]["continuation_status"])
        failed_checkpoint_id = failed["progressive_checkpoint"]["checkpoint_id"]
        create_context(self.settings, "CHECKPOINT-FAILURE", retry_request)
        retried = session_state(self.settings, "CHECKPOINT-FAILURE")
        self.assertEqual("published", retried["progressive_checkpoint"]["continuation_status"])
        self.assertEqual(failed_checkpoint_id, retried["progressive_checkpoint"]["checkpoint_id"])
        lineage_ids = [item["context_id"] for item in retried["context_lineage"]]
        self.assertEqual(len(lineage_ids), len(set(lineage_ids)))
        self.assertEqual("checkpoint", retried["context_lineage"][-1]["kind"])
        self.assertEqual(1, len(retried["continuation_failures"]))
        self.assertEqual(1, len(list((self.settings.runs_dir / "CHECKPOINT-FAILURE").glob("checkpoint-[0-9][0-9][0-9].md"))))

        for failure_ticket, target in (
            ("PACK-FAILURE", "brain.core.pack_context"),
            ("METRIC-FAILURE", "brain.metrics.record_metric"),
        ):
            start_session(self.settings, failure_ticket, "Retry the exact failed continuation.")
            failed_request = self.request("Trace CustomerController G1_ONLY after a late failure", wave=1)
            with mock.patch(target, side_effect=RuntimeError("injected post-runtime failure")):
                with self.assertRaisesRegex(RuntimeError, "injected post-runtime failure"):
                    create_context(self.settings, failure_ticket, failed_request)
            late_failed = session_state(self.settings, failure_ticket)
            self.assertEqual(0, late_failed["requests"])
            self.assertFalse(late_failed.get("investigation_runtime"))
            self.assertEqual("failed", late_failed["progressive_checkpoint"]["continuation_status"])
            self.assertFalse(list((self.settings.runs_dir / failure_ticket).glob("checkpoint-delta-*.md")))
            self.assertFalse(list((self.settings.generated_dir / "handoffs" / failure_ticket).glob("checkpoint-delta-*.md")))
            create_context(self.settings, failure_ticket, failed_request)
            late_retried = session_state(self.settings, failure_ticket)
            self.assertEqual(1, late_retried["requests"])
            self.assertEqual(1, late_retried["investigation_runtime"]["wave"])
            self.assertEqual("published", late_retried["progressive_checkpoint"]["continuation_status"])
        recovered, _, _ = create_context(
            self.settings, "RECOVERY",
            self.request("Recover with a stale context base", base="CTX-STALE", wave=2),
        )
        self.assertTrue(recovered.startswith("# PROJECT BRAIN CONTEXT\n"))
        self.assertIn("G1_ONLY", recovered)
        state = session_state(self.settings, "RECOVERY")
        self.assertEqual("base_mismatch", state["request_history"][-1]["retrieval"]["checkpoint_reason"])
        self.assertEqual("CTX-002", state["last_context_id"])

    def test_completion_events_follow_authoritative_session_publication(self) -> None:
        from brain import core as core_module

        self.publish("sha-event-order", "EVENT_ORDER")
        ticket = "EVENT-ORDER"
        start_session(self.settings, ticket, "Trace CustomerController before reporting completion.")
        events: list[dict[str, object]] = []
        real_save = core_module.save_session
        save_calls = 0

        def fail_final_publication(settings: object, value: str, state: dict[str, object]) -> None:
            nonlocal save_calls
            save_calls += 1
            if save_calls == 2:
                raise OSError("injected final session publication failure")
            real_save(settings, value, state)

        with mock.patch("brain.core.save_session", side_effect=fail_final_publication):
            with self.assertRaisesRegex(OSError, "final session publication failure"):
                create_context(
                    self.settings, ticket,
                    self.request("Trace CustomerController EVENT_ORDER", wave=1),
                    progress=events.append,
                )
        phases = {str(event.get("phase")) for event in events}
        self.assertIn("first_useful_checkpoint", phases)
        self.assertNotIn("continuation_published", phases)
        self.assertNotIn("wave_complete", phases)
        self.assertNotIn("investigation_complete", phases)
        persisted = session_state(self.settings, ticket)
        self.assertEqual(0, persisted["requests"])
        self.assertEqual("failed", persisted["progressive_checkpoint"]["continuation_status"])

    def test_refresh_inputs_and_10_50_100_repo_scale_are_bounded(self) -> None:
        for count in (10, 50, 100):
            scale_root = self.root / f"scale-{count}"
            config_lines = ["[project]", f"name='scale-{count}'", "[graph]", "enabled=false", "[experience]", "enabled=false"]
            current_files: dict[tuple[str, str], str] = {}
            modules: list[dict[str, object]] = []
            entities: list[dict[str, object]] = []
            for index in range(count):
                name = f"repo-{index:03d}"
                repository = scale_root / name
                repository.mkdir(parents=True)
                content = "@RestController class ScaleController { @GetMapping(\"/scale\") String get(){ return \"ok\"; } }\n"
                if count == 10 and index == 0:
                    content += "// bounded filler\n" * 70_000
                (repository / "ScaleController.java").write_text(content, encoding="utf-8")
                config_lines.extend(["[[repositories]]", f"name='{name}'", f"path='scale-{count}/{name}'"])
                blob = hashlib.sha256(content.encode()).hexdigest()
                current_files[(name, "ScaleController.java")] = blob
                module, file_entities, _, _ = _file_intelligence(
                    name, "ScaleController.java", blob, content,
                )
                modules.append(module)
                entities.extend(file_entities)
            scale_config = self.root / f"scale-{count}.toml"
            scale_config.write_text("\n".join(config_lines) + "\n", encoding="utf-8")
            settings = load_settings(scale_config)
            payload = build_generation_intelligence(
                settings, current_files=current_files, unchanged=[], parent_generation=None,
                modules=modules, entities=entities,
            )
            self.assertEqual(count, payload["v1_build"]["parsed_files"])
            self.assertEqual(int(count == 10), payload["v1_build"]["truncated_files"])
            self.assertEqual(MAX_REFRESH_FILE_BYTES, payload["v1_build"]["max_file_bytes"])
            self.assertLessEqual(len(payload["runtime_anchors"]), count * 256)
            self.assertLessEqual(len(payload["integration_facts"]), count * 256)

    def test_end_to_end_10_50_100_refresh_delta_and_retrieval_are_bounded(self) -> None:
        from brain import atlas as atlas_module
        from brain import index as index_module

        scale_root = self.root / "end-to-end-scale"
        config = self.root / "end-to-end-scale.toml"
        rows = [
            "[project]", "name='end-to-end-scale'", "[graph]", "enabled=false",
            "[experience]", "enabled=false",
        ]
        configured = 0
        metrics: list[dict[str, object]] = []
        for count in (10, 50, 100):
            for index in range(configured, count):
                name = f"repo-{index:03d}"
                repository = scale_root / name
                repository.mkdir(parents=True)
                (repository / "ScaleService.java").write_text(
                    f"final class ScaleService{index:03d} {{ String marker = \"SCALE_NEEDLE_{index:03d}\"; }}\n",
                    encoding="utf-8",
                )
                rows.extend(["[[repositories]]", f"name='{name}'", f"path='end-to-end-scale/{name}'"])
            config.write_text("\n".join(rows) + "\n", encoding="utf-8")
            settings = load_settings(config)
            rss_before = _peak_rss_mb()
            refresh_started = time.perf_counter()
            with mock.patch("brain.atlas._file_intelligence", wraps=atlas_module._file_intelligence) as parsed:
                refresh_brain(settings, fetch=False, discover=False)
            refresh_ms = (time.perf_counter() - refresh_started) * 1_000
            expected_changed = count - configured
            self.assertLessEqual(parsed.call_count, expected_changed)
            generation = current_generation_ref(settings)
            self.assertIsNotNone(generation)
            pinned = replace(settings, atlas_generation=generation, atlas_generation_mode="pinned")
            marker = f"SCALE_NEEDLE_{count - 1:03d}"
            self.assertIsNotNone(index_module.query_generation_indexes(
                pinned, generation, pinned.repositories, marker,
                max_results=pinned.max_results,
                max_candidate_files=max(100, count),
                max_hits=pinned.candidate_limit,
                max_bytes=8 * 1024 * 1024,
                max_seconds=2.0,
            ))
            request = parse_context_request(json.dumps({"INVESTIGATION_REQUEST": {
                "version": 5, "mode": "root_cause", "objective": f"Locate {marker}",
                "runtime_facts": [], "hypotheses": [], "required": ["production entry point"],
                "resolve": [], "anchors": [{"kind": "log_literal", "value": marker}],
                "base_context_id": None, "checkpoint": False, "wave": 1,
            }}))
            retrieval_started = time.perf_counter()
            with mock.patch("brain.core._zoekt_manifest_hash", return_value=None), mock.patch(
                "brain.index._connect", wraps=index_module._connect,
            ) as opened:
                bundle = retrieve_context(pinned, request)
            retrieval_ms = (time.perf_counter() - retrieval_started) * 1_000
            self.assertTrue(any(marker in item.content for item in bundle.evidence))
            self.assertLessEqual(opened.call_count, 3)
            self.assertLessEqual(bundle.trace["physical_backend_operations"], 3)
            self.assertLess(bundle.trace["bytes_read"], 100_000)
            self.assertLess(refresh_ms, 30_000)
            self.assertLess(retrieval_ms, 10_000)
            rss_after = _peak_rss_mb()
            if rss_before is not None and rss_after is not None:
                self.assertLess(rss_after - rss_before, 512)
            metrics.append({
                "repositories": count, "changed_repositories": expected_changed,
                "atlas_files_parsed": parsed.call_count, "refresh_ms": round(refresh_ms, 3),
                "retrieval_ms": round(retrieval_ms, 3), "sqlite_opens": opened.call_count,
                "physical_operations": bundle.trace["physical_backend_operations"],
                "bytes_read": bundle.trace["bytes_read"], "peak_rss_mb": rss_after,
            })
            configured = count

        last = scale_root / "repo-099" / "ScaleService.java"
        last.write_text(last.read_text(encoding="utf-8") + "// SCALE_DELTA_100\n", encoding="utf-8")
        settings = load_settings(config)
        delta_started = time.perf_counter()
        with mock.patch("brain.atlas._file_intelligence", wraps=atlas_module._file_intelligence) as parsed_delta:
            refresh_brain(settings, fetch=False, discover=False)
        delta_ms = (time.perf_counter() - delta_started) * 1_000
        self.assertEqual(1, parsed_delta.call_count)
        self.assertLess(delta_ms, 30_000)
        metrics.append({
            "repositories": 100, "changed_repositories": 1,
            "atlas_files_parsed": parsed_delta.call_count, "delta_refresh_ms": round(delta_ms, 3),
            "peak_rss_mb": _peak_rss_mb(),
        })
        print(json.dumps({"synthetic_scale_metrics": metrics}, sort_keys=True))

    def test_atlas_refresh_parses_each_authoritative_blob_before_requesting_the_next(self) -> None:
        self.publish("sha-stream-one", "STREAM_ONE")
        next_sha = "sha-stream-two"
        for repo in self.settings.repositories:
            snapshot = self.settings.state_dir / "snapshots" / repo.name / next_sha
            shutil.copytree(Path(repo.source_path), snapshot)
            repo.source_path = snapshot
            repo.source_sha = next_sha
        api_snapshot = Path(self.settings.repo("customer-api").source_path)
        for index in range(40):
            (api_snapshot / f"Stream{index:03d}.java").write_text(
                f"class Stream{index:03d} {{ String marker = \"STREAM_{index:03d}\"; }}\n" + "// bounded filler\n" * 2_000,
                encoding="utf-8",
            )
        state, _ = snapshot_indexes(self.settings, changed_only=True, publish=False)
        from brain import index as lexical_index
        from brain import atlas as atlas_module

        real_contents = lexical_index.indexed_snapshot_contents
        real_parser = atlas_module._file_intelligence
        awaiting: dict[str, tuple[str, str] | None] = {"key": None}
        peak_pending = 0

        def guarded_contents(*args: object, **kwargs: object):
            nonlocal peak_pending
            for key, content in real_contents(*args, **kwargs):
                if awaiting["key"] is not None:
                    raise AssertionError("Atlas requested another full blob before parsing the prior blob")
                awaiting["key"] = key
                peak_pending = max(peak_pending, 1)
                yield key, content
                if awaiting["key"] is not None:
                    raise AssertionError("Atlas retained an unparsed full blob across iterator advancement")

        def guarded_parser(repo: str, path: str, blob: str, content: str):
            self.assertEqual((repo, path), awaiting["key"])
            awaiting["key"] = None
            return real_parser(repo, path, blob, content)

        with mock.patch("brain.index.indexed_snapshot_contents", side_effect=guarded_contents), mock.patch(
            "brain.atlas._file_intelligence", side_effect=guarded_parser,
        ):
            atlas = build_atlas(self.settings, state)
        self.assertEqual(1, peak_pending)
        self.assertIsNone(awaiting["key"])
        self.assertGreaterEqual(atlas["v1_build"]["parsed_files"], 40)

    def test_v1_refresh_cache_exhaustion_never_rereads_a_poisoned_snapshot_path(self) -> None:
        repository = self.root / "authoritative-v1"
        source_path = repository / "src/main/java/demo/App.java"
        source_path.parent.mkdir(parents=True)
        source_path.write_text(
            '@RestController class Good { @GetMapping("/good") String get(){ return "good"; } }\n',
            encoding="utf-8",
        )
        git = native_command("git")
        for command in (
            [git, "init", "-q"],
            [git, "config", "user.email", "brain@example.invalid"],
            [git, "config", "user.name", "Project Brain Test"],
            [git, "add", "."],
            [git, "commit", "-qm", "authoritative source"],
        ):
            subprocess.run(command, cwd=repository, check=True)
        commit = subprocess.run(
            [git, "rev-parse", "HEAD"], cwd=repository, check=True,
            text=True, stdout=subprocess.PIPE,
        ).stdout.strip()
        poisoned = self.root / "poisoned-v1"
        poisoned_path = poisoned / "src/main/java/demo/App.java"
        poisoned_path.parent.mkdir(parents=True)
        poisoned_path.write_text(
            '@RestController class Evil { @GetMapping("/evil") String get(){ return "evil"; } }\n',
            encoding="utf-8",
        )
        config = self.root / "authoritative-v1.toml"
        config.write_text(
            "[project]\nname='authoritative-v1'\n[graph]\nenabled=false\n[experience]\nenabled=false\n"
            "[[repositories]]\nname='authoritative-v1'\npath='authoritative-v1'\n",
            encoding="utf-8",
        )
        settings = load_settings(config)
        repo = settings.repo("authoritative-v1")
        repo.source_sha = commit
        repo.source_ref = "HEAD"
        repo.source_path = poisoned
        state, _ = snapshot_indexes(settings, changed_only=False, publish=False)
        with mock.patch("brain.investigation.MAX_REFRESH_CONTENT_CACHE_BYTES", 0):
            atlas = build_atlas(settings, state)
        authority = json.dumps({
            "anchors": atlas["runtime_anchors"], "facts": atlas["integration_facts"],
        })
        self.assertIn("/good", authority)
        self.assertNotIn("/evil", authority)
        names = {str(item.get("simple_name")) for item in atlas["entities"]}
        self.assertIn("Good", names)
        self.assertNotIn("Evil", names)

    def test_pinned_semantic_generation_keeps_immutable_pack_id_until_gc(self) -> None:
        generation = self.publish("sha-pack-retention", "PACK_RETENTION")
        from brain.models import install_pack, remove_pack, verify_pack

        original = self.root / "pack-retained-original"
        replacement = self.root / "pack-retained-replacement"
        original.mkdir()
        replacement.mkdir()
        base = {
            "pack_id": "v1-test-pack", "capability": "embedding", "model_family": "test",
            "upstream_model": "test-only", "upstream_revision": "1", "license": "MIT",
            "runtime_name": "deterministic-test", "runtime_revision": "1", "minimum_brain_version": "0.6.1",
            "embedding_dimension": 3, "test_only": True,
        }
        (original / "manifest.json").write_text(json.dumps({**base, "query_instruction": "query-v1"}), encoding="utf-8")
        (replacement / "manifest.json").write_text(json.dumps({**base, "query_instruction": "query-v2"}), encoding="utf-8")
        install_pack(self.settings, original)
        verify_pack(self.settings, "v1-test-pack")
        with self.assertRaisesRegex(ValueError, "pack ID .* is immutable"):
            install_pack(self.settings, replacement)
        with self.assertRaisesRegex(ValueError, "retained by an Atlas generation"):
            remove_pack(self.settings, "v1-test-pack")
        hits = search_semantic(self.settings, "customers", generation=generation, embed=self.embed)
        self.assertTrue(hits)
        self.assertTrue(verify_pack(self.settings, "v1-test-pack")["verified"])

    def test_integrated_brain_and_m365_evaluation_metrics(self) -> None:
        self.publish("sha-g1", "G1_ONLY")
        suite = self.root / "v1-golden.json"
        m365 = """FINAL_SOLUTION
## Ticket interpretation
The ticket is bounded.
## Verified current behavior
Evidence E0001 verifies behavior.
## Execution flow and integration flow
The ordered flow is established.
## Root cause
The branch omits the case.
## Exact repository and files
Exact repository paths are listed.
## Suggested production changes
Update the existing pattern.
## Tests and assertions
Exact tests are listed.
## Validation commands
Run approved tests.
## Edge cases and compatibility risks
Compatibility is preserved.
## Implementation order
Source, tests, validation.
## Remaining assumptions
None.
"""
        suite.write_text(json.dumps({
            "name": "v1-java-m365",
            "cases": [{
                "id": "customer-flow",
                "split": "holdout",
                "request": self.request("Trace CustomerController /customers/{id}", wave=1),
                "expect": {
                    "required_files": [
                        "customer-api:src/main/java/demo/CustomerController.java",
                        "customer-client:src/main/java/demo/CustomerClient.java",
                    ],
                    "required_anchors": ["CustomerController"],
                    "execution_steps": ["DEFINES"],
                    "integration_repositories": ["customer-api", "customer-client"],
                    "required_surfaces": ["test:customer-api:src/test/java/demo/CustomerControllerTest.java"],
                    "required_evidence_ids": ["E0001"],
                    "m365_response": m365,
                },
            }],
        }), encoding="utf-8")
        def artifact_tree(root: Path) -> dict[str, bytes]:
            return {
                str(path.relative_to(root)): path.read_bytes()
                for path in root.rglob("*") if path.is_file() and not path.is_symlink()
            } if root.is_dir() else {}

        before_runs = artifact_tree(self.settings.runs_dir)
        before_generated = artifact_tree(self.settings.generated_dir)
        from brain.atlas import record_investigation
        from brain.catalog import connect

        record_investigation(self.settings, "REAL-PRIOR", {
            "generation": 1,
            "status": "completed",
            "investigation_memory": {"objective": "preserve this real production prior"},
            "atlas_entity_ids": ["entity-real"],
            "evidence_manifest": [],
        })
        connection = connect(self.settings)
        try:
            prior_records = list(connection.execute(
                "SELECT record_id,ticket,objective,outcome,updated_at "
                "FROM investigation_records ORDER BY record_id"
            ))
        finally:
            connection.close()
        from brain.core import retrieve_context as real_retrieve_context
        from brain.locks import workspace_lock_mode

        def locked_retrieve(settings, request, *args, **kwargs):
            self.assertEqual("exclusive", workspace_lock_mode(settings))
            return real_retrieve_context(settings, request, *args, **kwargs)

        from brain import atlas as atlas_module

        with mock.patch("brain.core.retrieve_context", side_effect=locked_retrieve), mock.patch.object(
            atlas_module, "MAX_INVESTIGATION_RECORDS", 1,
        ):
            report = evaluate_golden(self.settings, suite)
        for metric in (
            "anchor_top1_accuracy", "anchor_recall_at_5", "execution_flow_step_recall",
            "execution_flow_order_accuracy", "integration_repo_recall", "surface_recall",
            "program_slice_statement_count", "hypothesis_supported_rate", "frontier_blocker_count",
            "first_useful_checkpoint_rate", "m365_evidence_id_recall", "m365_final_contract_coverage",
            "m365_repeated_retrieval_requests", "m365_unsupported_authority_claims",
        ):
            self.assertIn(metric, report["summary"])
        self.assertEqual(1.0, report["summary"]["m365_evidence_id_recall"])
        self.assertEqual(0, evaluate_m365_response(m365, ["E0001"])["repeated_retrieval_requests"])
        anchors_disabled = evaluate_golden(self.settings, suite, evaluation_ablation={"anchors"})
        self.assertEqual(0.0, anchors_disabled["summary"]["anchor_recall_at_5"])
        graph_disabled = evaluate_golden(self.settings, suite, evaluation_ablation={"graph_flow"})
        self.assertEqual(0.0, graph_disabled["summary"]["execution_flow_step_recall"])
        slice_disabled = evaluate_golden(self.settings, suite, evaluation_ablation={"program_slice"})
        self.assertEqual(0.0, slice_disabled["summary"]["program_slice_statement_count"])
        from brain.atlas import route as real_route

        observed_ablations: list[set[str]] = []

        def route_spy(settings, objective, request, *args, **kwargs):
            observed_ablations.append(set(request.get("_evaluation_ablation") or []))
            return real_route(settings, objective, request, *args, **kwargs)

        with mock.patch("brain.atlas.route", side_effect=route_spy):
            evaluate_golden(self.settings, suite, evaluation_ablation={"generation_cache"})
        self.assertTrue(observed_ablations)
        self.assertTrue(all("generation_cache" in values for values in observed_ablations))
        self.assertEqual(before_runs, artifact_tree(self.settings.runs_dir))
        self.assertEqual(before_generated, artifact_tree(self.settings.generated_dir))
        connection = connect(self.settings)
        try:
            self.assertEqual(prior_records, list(connection.execute(
                "SELECT record_id,ticket,objective,outcome,updated_at "
                "FROM investigation_records ORDER BY record_id"
            )))
        finally:
            connection.close()
        with mock.patch("brain.core.create_context", side_effect=RuntimeError("injected evaluation failure")):
            with self.assertRaisesRegex(RuntimeError, "evaluation failure"):
                evaluate_golden(self.settings, suite, evaluation_ablation={"anchors"})
        self.assertEqual(before_runs, artifact_tree(self.settings.runs_dir))
        self.assertEqual(before_generated, artifact_tree(self.settings.generated_dir))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

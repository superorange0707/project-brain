from __future__ import annotations

import base64
import gc as garbage_collector
import hashlib
import http.server
import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import ssl
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
import unittest
import warnings
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock
from urllib.error import URLError

from brain.cli import main
from brain import cli as cli_module
from brain import core as core_module
from brain.agent import archive_final_solution, create_m365_agent_kit, response_preview
from brain.core import (
    BrainError,
    ContextBundle,
    Evidence,
    add_external_evidence,
    chunk_text,
    create_context,
    create_feedback,
    create_learning_template,
    deliver,
    discover_and_configure_repositories,
    doctor,
    generate_map,
    git_history,
    load_settings,
    load_index_state,
    path_hits,
    parse_context_request,
    request_preview,
    request_repair_prompt,
    read_source,
    retrieve_context,
    Repository,
    SearchHit,
    search,
    session_state,
    session_dir,
    simple_yaml_load,
    start_session,
    snapshot_indexes,
    symbol_hits,
    trace_symbol,
)
from brain.relations import generate_relationship_map
from brain import sync as sync_module
from brain.sync import _ssh_endpoint, sync_repositories
from brain.graph import graph_symbol_hits, index_graph
from brain.experience import build_experience_index, evaluate_sessions, render_similar_cases, similar_cases
from brain import experience as experience_module
from brain.metrics import benchmark_report, machine_profile
from brain.metrics import trace_metadata
from brain.catalog import current_generation
from brain.catalog import connect as catalog_connect
from brain.editions import current_edition, set_edition
from brain.editions import capabilities
from brain.models import DEFAULT_RUNTIME_REQUEST_TIMEOUT_SECONDS, DEFAULT_RUNTIME_STARTUP_TIMEOUT_SECONDS, EMBEDDING_BATCH_PARITY_TOLERANCE, RERANKER_BATCH_PARITY_TOLERANCE, DeterministicRuntime, LlamaCppRuntime, ManagedLlamaCppRuntime, OFFICIAL_PACKS, _same_vectors, rerank_candidates
from brain.models import _open_model_download, _rerank_batched, _reranker_parity_indices, _reranker_tuning, autotune_pack, benchmark_pack, install_pack, install_pack_url, install_release_descriptor, managed_runtime_loopback_status, model_download_ssl_context, runtime_for_pack, validate_manifest, verify_pack
from brain.semantic import ATLAS_CARD_VERSION, CARD_VERSION, CHUNK_SCHEMA_VERSION, Chunk, SEMANTIC_CARD_CODE_CHARS, SEMANTIC_EMBEDDING_INPUT_VERSION, SEMANTIC_SHARD_MANIFEST_VERSION, _bounded_embedding_batches, _excluded, _injected_pack_identity, _shard_sha256, build_semantic_index, chunk_source, search_semantic
from brain.ops import dashboard_status, freshness, gc, model_operation, model_status, refresh_brain
from brain.platforms import native_command, platform_id
from brain.evaluation import evaluate_golden
from brain.retrieval import compile_request, explain_plan
from brain.retrieval.models import RetrievalTrace


REQUEST = """
Here is my next request:
```yaml
CONTEXT_REQUEST:
  objective: >
    Determine why jurisdiction changes do not recalculate eligibility online.
  searches:
    - query: "JURISDICTION_CHANGED"
      repos: []
  symbols:
    - name: "EligibilityEvaluator"
      repos: [trading-service]
      include: [definition, implementations, tests]
    - name: "recalculate"
      repos: [trading-service]
      include: [definition, callers, callees, tests]
  files: []
  history: []
```
"""

FINAL_SOLUTION = """FINAL_SOLUTION
## Ticket interpretation and remaining assumptions
The ticket asks for a bounded production change.
## Verified current behavior
Pinned exact source establishes the current behavior.
## Ordered execution flow and integration flow
The ordered call and integration path is documented.
## Root cause
The verified branch omits the required case.
## Exact repositories, files, symbols, and configuration/data
The exact repository paths and symbols are listed.
## Suggested production changes
Update the existing branch using the local pattern.
## Impact and test surfaces; tests and assertions
The affected tests and exact assertions are listed.
## Validation commands
Run the repository-approved test command.
## Edge cases and compatibility risks
Backward compatibility and edge cases are covered.
## Implementation order
Apply source, tests, then validation.
## Remaining assumptions
No unverified production assumptions remain.
"""


class PinnedHistoryTests(unittest.TestCase):
    def test_learning_template_paths_are_collision_resistant_and_portable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "service"
            repository.mkdir()
            config = root / "brain.toml"
            config.write_text(
                "[project]\nname='portable-learning-paths'\n"
                "[[repositories]]\nname='service'\npath='service'\n",
                encoding="utf-8",
            )
            settings = load_settings(config)
            slash = create_learning_template(settings, "A/B")
            dash = create_learning_template(settings, "A-B")
            reserved = create_learning_template(settings, "CON")
            oversized = create_learning_template(settings, "T" * 500)

            self.assertNotEqual(slash, dash)
            self.assertEqual("# A/B", slash.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual("# A-B", dash.read_text(encoding="utf-8").splitlines()[0])
            self.assertNotEqual("CON.md", reserved.name.upper())
            self.assertLessEqual(len(oversized.name.encode("utf-8")), 128)
            self.assertEqual("# A/B", slash.read_text(encoding="utf-8").splitlines()[0])

    def test_git_history_inherits_core_process_time_and_output_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".git").mkdir()
            repository = Repository(name="service", path=root, source_sha="g1")
            completed = subprocess.CompletedProcess(["git"], 0, "partial", "")
            completed.output_truncated = True
            with mock.patch("brain.core.run_bounded_process", return_value=completed) as execute:
                self.assertEqual("", git_history(repository, "needle"))
            self.assertEqual(2, execute.call_count)
            self.assertTrue(all(call.kwargs["timeout"] == 30.0 for call in execute.call_args_list))
            self.assertTrue(all(call.kwargs["max_stdout_bytes"] == 8 * 1024 * 1024 for call in execute.call_args_list))

    def test_ticket_paths_handle_windows_reserved_long_and_case_colliding_identities(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "service"
            repository.mkdir()
            config = root / "brain.toml"
            config.write_text(
                "[project]\nname='portable-ticket-paths'\n[[repositories]]\nname='service'\npath='service'\n",
                encoding="utf-8",
            )
            settings = load_settings(config)
            self.assertNotEqual("CON", session_dir(settings, "CON").name.upper())
            self.assertLessEqual(len(session_dir(settings, "T" * 500).name.encode("utf-8")), 128)

            # Emulate a case-insensitive lookup by placing the first ticket in
            # the lower-cased legacy path. The second identity must not reuse it.
            legacy = settings.runs_dir / "case-ticket"
            legacy.mkdir(parents=True)
            (legacy / "session.json").write_text(json.dumps({"ticket": "CASE-TICKET"}), encoding="utf-8")
            self.assertNotEqual(legacy, session_dir(settings, "case-ticket"))

    def test_git_history_uses_immutable_source_sha_before_movable_ref(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".git").mkdir()
            repository = Repository(
                name="service", path=root, source_sha="g1-immutable-sha", source_ref="refs/heads/main",
            )
            completed = subprocess.CompletedProcess([], 0, "history", "")
            with mock.patch("brain.core.run", return_value=completed) as execute:
                self.assertEqual("history", git_history(repository, "needle"))
            self.assertEqual("g1-immutable-sha", execute.call_args.args[0][2])

    def test_external_or_working_tree_evidence_never_marks_repository_coverage_verified(self) -> None:
        from brain.core import _coverage

        generation = mock.Mock(snapshots={"service": "sha-g1"})
        bundle = ContextBundle(
            "external only",
            evidence=[
                Evidence("external", "config/app.yaml", 1, 1, "enabled: true", "user-supplied external evidence", 100, []),
                Evidence("service", "src/integrationTest/java/ThingTest.java", 1, 1, "class ThingTest {}", "local diff", 100, []),
            ],
            atlas_generation=generation,
        )
        self.assertEqual(
            {"production_source": False, "tests": False, "configuration": False},
            {key: _coverage(bundle)[key] for key in ("production_source", "tests", "configuration")},
        )

    def test_sanitized_ticket_collisions_remain_isolated_under_concurrent_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "brain.toml"
            repository = root / "service"
            repository.mkdir()
            config.write_text(
                "[project]\nname='ticket-collision'\n[[repositories]]\nname='service'\npath='service'\n",
                encoding="utf-8",
            )
            settings = load_settings(config)
            tickets = ("ABC / 1", "ABC ? 1")
            failures: list[BaseException] = []

            def create(ticket: str) -> None:
                try:
                    start_session(settings, ticket, f"Investigate {ticket}")
                except BaseException as error:  # pragma: no cover - asserted below
                    failures.append(error)

            threads = [threading.Thread(target=create, args=(ticket,)) for ticket in tickets]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertLessEqual(len(failures), 1)
            for ticket in tickets:
                if session_state(settings, ticket).get("ticket") != ticket:
                    start_session(settings, ticket, f"Investigate {ticket}")
            directories = {session_dir(settings, ticket) for ticket in tickets}
            self.assertEqual(2, len(directories))
            self.assertEqual(set(tickets), {session_state(settings, ticket)["ticket"] for ticket in tickets})

    def test_common_gradle_non_unit_source_sets_are_test_paths(self) -> None:
        from brain.platforms import is_test_path

        for source_set in (
            "integrationTest", "functionalTest", "componentTest", "contractTest",
            "performanceTest", "acceptanceTest", "smokeTest", "e2e", "it", "testFixtures",
        ):
            self.assertTrue(is_test_path(f"src/{source_set}/java/demo/Fixture.java"), source_set)
        for path in (
            "src/main/java/Contest.java", "src/main/java/Latest.java",
            "src/main/java/Implicit.java", "src/contest.py",
        ):
            self.assertFalse(is_test_path(path), path)
        self.assertTrue(is_test_path("src/main/java/demo/CustomerServiceTest.java"))
        self.assertTrue(is_test_path("src/componentTest/java/demo/CustomerService.java"))


class BrainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        customer = self.root / "customer-service"
        trading = self.root / "trading-service"
        risk = self.root / "risk-service"
        batch = self.root / "batch-service"
        (customer / "src/main/java/demo").mkdir(parents=True)
        (trading / "src/main/java/demo").mkdir(parents=True)
        (trading / "src/test/java/demo").mkdir(parents=True)
        (risk / "src/main/java/demo").mkdir(parents=True)
        (batch / "src/main/java/demo").mkdir(parents=True)
        (customer / "src/main/java/demo/CustomerEvent.java").write_text(
            """package demo;
public record CustomerEvent(Type type) {
    enum Type { ADDRESS_CHANGED, JURISDICTION_CHANGED }
}
""",
            encoding="utf-8",
        )
        (customer / "src/main/java/demo/CustomerPublisher.java").write_text(
            '''class CustomerPublisher {
    void publish(CustomerEvent event) { kafkaTemplate.send("customer.updated", event); }
}
''',
            encoding="utf-8",
        )
        (trading / "src/main/java/demo/EligibilityEvaluator.java").write_text(
            """package demo;
public interface EligibilityEvaluator { void recalculate(String customerId); }
""",
            encoding="utf-8",
        )
        (trading / "src/main/java/demo/TradingEligibilityService.java").write_text(
            """package demo;
@Service
public class TradingEligibilityService implements EligibilityEvaluator {
    private final RiskClient riskClient;
    public void recalculate(String customerId) {
        riskClient.getRestrictions(customerId);
        save(customerId);
    }
    private void save(String customerId) {}
}
""",
            encoding="utf-8",
        )
        (trading / "src/main/java/demo/CustomerChangedListener.java").write_text(
            """package demo;
public class CustomerChangedListener {
    private final TradingEligibilityService service;
    @KafkaListener(topics = "${topics.customer}")
    public void handle(CustomerEvent event) {
        if (event.type() == ADDRESS_CHANGED) service.recalculate("id");
        // bug: JURISDICTION_CHANGED is ignored
    }
}
""",
            encoding="utf-8",
        )
        (trading / "src/main/java/demo/RiskClient.java").write_text(
            '''@FeignClient(name = "${services.risk}", path = "/risk")
interface RiskClient {
    @GetMapping("/restrictions/{id}") Object getRestrictions(String id);
}
''',
            encoding="utf-8",
        )
        (trading / "src/main/resources").mkdir(parents=True)
        (trading / "src/main/resources/application.properties").write_text(
            "topics.customer=customer.updated\nservices.risk=risk-service\n",
            encoding="utf-8",
        )
        (trading / "deploy/templates").mkdir(parents=True)
        (trading / "deploy/templates/configmap.tpl").write_text(
            "transaction-cache-ttl: {{ .Values.cacheTtl }}\n", encoding="utf-8"
        )
        (risk / "src/main/java/demo/RiskController.java").write_text(
            '''@RestController
@RequestMapping("/risk")
class RiskController {
    @GetMapping("/restrictions/{customerId}") Object restrictions(String customerId) { return null; }
}
''',
            encoding="utf-8",
        )
        (batch / "src/main/java/demo/NightlyJob.java").write_text(
            "@Scheduled(cron = \"0 0 0 * * *\") class NightlyJob {}\n",
            encoding="utf-8",
        )
        (trading / "src/test/java/demo/CustomerChangedListenerTest.java").write_text(
            """class CustomerChangedListenerTest {
    void addressChangeRecalculates() { service.recalculate("id"); }
}
""",
            encoding="utf-8",
        )
        (trading / "pom.xml").write_text(
            """<project xmlns="http://maven.apache.org/POM/4.0.0"><groupId>demo</groupId><artifactId>trading-service</artifactId><dependencies><dependency>
<groupId>demo</groupId><artifactId>risk-client</artifactId></dependency></dependencies></project>""",
            encoding="utf-8",
        )
        (risk / "pom.xml").write_text(
            '<project xmlns="http://maven.apache.org/POM/4.0.0"><groupId>demo</groupId><artifactId>risk-client</artifactId></project>',
            encoding="utf-8",
        )
        (self.root / "knowledge").mkdir()
        (self.root / "knowledge/PROJECT_MAP.md").write_text(
            "customer-service publishes customer changes; trading-service owns eligibility.\n", encoding="utf-8"
        )
        (self.root / "knowledge/glossary.md").write_text(
            "Jurisdiction means the regulatory country used for eligibility.\n", encoding="utf-8"
        )
        self.config = self.root / "brain.toml"
        self.config.write_text(
            """[project]
name = "demo"
[search]
max_results = 100
[context]
source_window_lines = 40
full_file_lines = 100
soft_target_chars = 100000
[delivery]
clipboard_chunk_chars = 1000
[graph]
enabled = false
[knowledge]
path = "knowledge"
[[repositories]]
name = "customer-service"
path = "customer-service"
[[repositories]]
name = "trading-service"
path = "trading-service"
[[repositories]]
name = "risk-service"
path = "risk-service"
[[repositories]]
name = "batch-service"
path = "batch-service"
""",
            encoding="utf-8",
        )
        self.settings = load_settings(self.config)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_dependency_free_yaml_parser(self) -> None:
        parsed = simple_yaml_load(REQUEST[REQUEST.index("CONTEXT_REQUEST:"):])
        request = parsed["CONTEXT_REQUEST"]
        self.assertEqual(["trading-service"], request["symbols"][0]["repos"])
        self.assertIn("implementations", request["symbols"][0]["include"])
        self.assertIn("jurisdiction changes", request["objective"])

    def test_repository_and_zoekt_identities_cannot_escape_managed_state(self) -> None:
        from brain.backends import zoekt
        from brain.platforms import filesystem_component

        unsafe = self.root / "unsafe.toml"
        unsafe.write_text(
            self.config.read_text(encoding="utf-8").replace(
                'name = "customer-service"', 'name = "../../outside"', 1,
            ),
            encoding="utf-8",
        )
        upgraded = load_settings(unsafe)
        self.assertEqual("../../outside", upgraded.repositories[0].name)
        paths = [
            zoekt.shard_path(self.settings.state_dir, repo, snapshot)
            for repo, snapshot in (("../outside", "sha"), ("service", "../sha"), ("C:\\outside", "sha"))
        ]
        self.assertTrue(all(path.is_relative_to((self.settings.state_dir / "zoekt").resolve()) for path in paths))
        self.assertEqual(len(paths), len(set(paths)))
        for value in ("con", "con.txt", "lpt1", "repo."):
            encoded = filesystem_component(value)
            self.assertNotIn(encoded.split(".", 1)[0].casefold(), {"con", "lpt1"})
            self.assertFalse(encoded.endswith((".", " ")))
        self.assertFalse((self.root / "outside").exists())

    def test_automatic_retrieval_excludes_sensitive_source_paths(self) -> None:
        secret = self.root / "trading-service/src/main/resources/credentials.json"
        secret.write_text('{"token":"do-not-index"}', encoding="utf-8")
        self.assertEqual([], search(self.settings, "do-not-index", ["trading-service"], fixed=True))
        self.assertEqual([], path_hits(self.settings, "credentials", ["trading-service"]))

    def test_request_validation_handles_markdown_fence(self) -> None:
        request = parse_context_request(REQUEST)
        self.assertEqual(1, request["version"])
        self.assertEqual(1, len(request["searches"]))
        self.assertEqual(2, len(request["symbols"]))

    def test_json_request_and_dry_run_plan(self) -> None:
        payload = json.dumps({
            "version": 1,
            "CONTEXT_REQUEST": {
                "objective": "Locate the eligibility implementation and tests.",
                "searches": [],
                "symbols": [{
                    "name": "EligibilityEvaluator",
                    "repos": ["trading-service"],
                    "include": ["definition", "implementations", "tests"],
                }],
                "files": [],
                "history": [],
            },
        })
        plan = request_preview(payload, self.settings)
        self.assertTrue(plan["valid"])
        self.assertEqual(3, plan["operation_count"])
        self.assertEqual({"definition", "implementations", "tests"}, {item["kind"] for item in plan["actions"]})
        self.assertIn('"version": 1', plan["normalized_json"])

    def test_request_preview_rejects_unknown_repositories_and_builds_repair_prompt(self) -> None:
        invalid = REQUEST.replace("trading-service", "invented-service")
        with self.assertRaisesRegex(BrainError, "Unknown repositories") as caught:
            request_preview(invalid, self.settings)
        repair = request_repair_prompt(str(caught.exception))
        self.assertIn("Validation error", repair)
        self.assertIn("version: 1", repair)

    def test_v3_objective_only_empty_hints_and_fail_closed_validation(self) -> None:
        objective_only = """CONTEXT_REQUEST:
  version: 3
  objective: Locate the production flow and tests for EligibilityEvaluator.
"""
        preview = request_preview(objective_only, self.settings)
        self.assertEqual(3, preview["protocol_version"])
        self.assertGreater(preview["operation_count"], 0)
        self.assertLessEqual(preview["effective_operation_count"], self.settings.max_effective_operations)
        self.assertEqual([], preview["request"]["hints"]["repos"])

        empty_hints = parse_context_request("""CONTEXT_REQUEST:
  version: 3
  objective: Determine the root cause.
  hints:
    repos: []
    literals: []
    symbols: []
    paths: []
    files: []
    history: []
""")
        self.assertEqual(3, empty_hints["version"])
        self.assertTrue(empty_hints["searches"])
        with self.assertRaisesRegex(BrainError, "unknown keys"):
            parse_context_request(objective_only.replace("objective:", "unexpected: true\n  objective:"))
        repair = request_repair_prompt("bad request")
        self.assertIn("version: 3", repair)
        self.assertNotIn("searches: []", repair)

    def test_operation_fusion_and_effective_budget_are_explainable(self) -> None:
        request = {
            "version": 1,
            "objective": "Find EligibilityEvaluator.",
            "searches": [{"query": "eligibility", "repos": []}] * 30,
            "symbols": [
                {"name": "EligibilityEvaluator", "repos": [], "include": ["definition", "callers"]},
                {"name": "EligibilityEvaluator", "repos": [], "include": ["tests", "implementations"]},
            ],
            "paths": [], "files": [], "history": [], "expand": [],
        }
        explained = explain_plan(compile_request(request, max_effective_operations=15))
        self.assertEqual(34, explained["requested_operations"])
        self.assertEqual(2, explained["effective_operations"])
        symbol = next(item for item in explained["operations"] if item["kind"] == "symbol")
        self.assertEqual(["callers", "definition", "implementations", "tests"], symbol["includes"])

    def test_failed_zoekt_attempt_consumes_budget_before_fallback(self) -> None:
        trace = RetrievalTrace(max_physical_backend_operations=1)
        token = core_module._ACTIVE_RETRIEVAL_TRACE.set(trace)
        attempts = 0

        def failed_zoekt(*_args: object, **kwargs: object) -> None:
            nonlocal attempts
            reserve = kwargs.get("reserve")
            self.assertTrue(callable(reserve) and reserve())
            attempts += 1
            return None

        try:
            with mock.patch("brain.backends.zoekt.search", side_effect=failed_zoekt), mock.patch(
                "brain.core.search_repo", side_effect=AssertionError("fallback exceeded the physical budget"),
            ):
                self.assertEqual([], search(
                    self.settings, "PHYSICAL_BUDGET_ZOEKT_FAILURE", ["trading-service"], fixed=True,
                ))
        finally:
            core_module._ACTIVE_RETRIEVAL_TRACE.reset(token)
        self.assertEqual(1, attempts)
        self.assertEqual(1, trace.physical_backend_operations)
        self.assertEqual(1, trace.subprocess_count)
        self.assertEqual("physical_budget", trace.stop_reason)

    def test_cross_repo_search_symbol_and_trace(self) -> None:
        hits = search(self.settings, "JURISDICTION_CHANGED", fixed=True)
        self.assertEqual({"customer-service", "trading-service"}, {hit.repo for hit in hits})
        symbols = symbol_hits(self.settings, "EligibilityEvaluator", ["trading-service"])
        self.assertTrue(any(hit.path.endswith("EligibilityEvaluator.java") for hit in symbols))
        traced, relationships = trace_symbol(self.settings, "recalculate", ["trading-service"])
        self.assertTrue(any(hit.kind == "caller" for hit in traced))
        self.assertTrue(any("CustomerChangedListener.java" in relation for relation in relationships))

    def test_map_extracts_framework_and_maven_facts(self) -> None:
        facts = generate_map(self.settings)
        self.assertIn("@KafkaListener", facts)
        self.assertIn("@Service", facts)
        self.assertIn("demo:risk-client", facts)
        self.assertTrue((self.root / "generated/PROJECT_FACTS.md").is_file())

    def test_relationship_map_builds_cross_repo_runtime_workflow(self) -> None:
        ghost = self.root / "customer-service/src/main/java/demo/Ghost.java"
        ghost.write_text(
            'class Ghost { String text = "kafkaTemplate.send(\\"ghost.topic\\")"; '
            '// kafkaTemplate.send("ghost.topic");\n}\n',
            encoding="utf-8",
        )
        relationships = generate_relationship_map(self.settings)
        self.assertIn("customer-service → trading-service", relationships)
        self.assertIn("trading-service → risk-service", relationships)
        self.assertIn("`KAFKA` `customer.updated`", relationships)
        self.assertIn("`HTTP` `GET /risk/restrictions/{}`", relationships)
        self.assertIn("customer-service --KAFKA:customer.updated--> trading-service --HTTP:GET /risk/restrictions/{}--> risk-service", relationships)
        cached = json.loads((self.root / "state/relationships.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(cached["relationships"]), 3)
        self.assertEqual(2, cached["version"])
        self.assertNotIn("ghost.topic", relationships)
        with mock.patch("brain.relations.analyze_relationships", side_effect=AssertionError("unexpected source rescan")):
            self.assertEqual(relationships, generate_relationship_map(self.settings))

    def test_relationship_working_tree_reader_enforces_the_open_handle_byte_limit(self) -> None:
        from brain import relations as relations_module

        source = self.root / "relationship-limit.java"
        source.write_bytes(b"123456789")
        with mock.patch("brain.relations.MAX_RELATIONSHIP_FILE_BYTES", 8), self.assertRaisesRegex(
            RuntimeError, "file budget exceeded",
        ):
            relations_module._bounded_relationship_source(source)

    def test_each_repo_gets_its_own_search_result_budget(self) -> None:
        self.settings.max_results = 1
        hits = search(self.settings, "class", fixed=True)
        self.assertEqual({"customer-service", "trading-service", "risk-service", "batch-service"}, {hit.repo for hit in hits})

    def test_structural_backend_json_is_used_when_available(self) -> None:
        backend = self.root / "fake-backend"
        backend.write_bytes(b"synthetic backend")
        backend.chmod(0o755)
        response = json.dumps({"cols": ["qn", "label", "file", "lines", "in", "out"], "rows": [["demo.EligibilityEvaluator", "Interface", "src/main/java/demo/EligibilityEvaluator.java", "2-2", 0, 1]]})

        def bounded_invoke(command, _cwd, **_kwargs):
            output = "codebase-memory-mcp 0.10.5\n" if "--version" in command else response
            return subprocess.CompletedProcess(command, 0, output, "")

        self.settings.graph_enabled = True
        with mock.patch.dict(os.environ, {"PROJECT_BRAIN_GRAPH_BIN": str(backend)}), mock.patch(
            "brain.graph.run_bounded_process", side_effect=bounded_invoke,
        ):
            indexed = index_graph(self.settings, changed_only=False)
            hits = graph_symbol_hits(self.settings, "EligibilityEvaluator", ["trading-service"])
        self.assertTrue(all(item.status == "indexed" for item in indexed))
        self.assertEqual("EligibilityEvaluator.java", Path(hits[0].path).name)
        self.assertIn("structural graph", hits[0].found_by[0])

    def test_structural_index_is_deferred_then_built_for_the_relevant_repo(self) -> None:
        backend = self.root / "lazy-backend"
        backend.write_bytes(b"synthetic backend")
        backend.chmod(0o755)
        response = json.dumps({"cols": ["qn", "label", "file", "lines"], "rows": [["demo.EligibilityEvaluator", "Interface", "src/main/java/demo/EligibilityEvaluator.java", "2-2"]]})

        def bounded_invoke(command, _cwd, **_kwargs):
            output = "codebase-memory-mcp 0.10.5\n" if "--version" in command else response
            return subprocess.CompletedProcess(command, 0, output, "")

        self.settings.graph_enabled = True
        with mock.patch.dict(os.environ, {"PROJECT_BRAIN_GRAPH_BIN": str(backend)}), mock.patch(
            "brain.graph.run_bounded_process", side_effect=bounded_invoke,
        ):
            deferred = index_graph(self.settings, defer_lazy=True)
            self.assertEqual("deferred", deferred[0].status)
            self.assertFalse((self.settings.state_dir / "graphs.json").exists())
            hits = symbol_hits(self.settings, "EligibilityEvaluator", ["trading-service"])
        self.assertTrue((self.settings.state_dir / "graphs.json").is_file())
        self.assertTrue(any("structural graph" in " ".join(hit.found_by) for hit in hits))

    def test_structural_project_identity_is_collision_resistant_and_output_is_bounded(self) -> None:
        from brain import graph as graph_module

        self.assertNotEqual(
            graph_module._project(self.settings, "a.b"),
            graph_module._project(self.settings, "a-b"),
        )
        backend = self.root / "bounded-graph-backend"
        backend.write_bytes(b"synthetic")
        backend.chmod(0o755)
        completed = subprocess.CompletedProcess([str(backend)], 1, "{}", "")
        completed.output_truncated = True
        with mock.patch.dict(os.environ, {"PROJECT_BRAIN_GRAPH_BIN": str(backend)}), mock.patch(
            "brain.graph.run_bounded_process", return_value=completed,
        ) as invoked:
            payload, error = graph_module._invoke(self.settings, "trace_path", {"depth": 4})
        self.assertIsNone(payload)
        self.assertIsNotNone(error)
        self.assertEqual(graph_module.GRAPH_MAX_OUTPUT_BYTES, invoked.call_args.kwargs["max_stdout_bytes"])

    def test_structural_backend_never_writes_through_symlinked_managed_cache(self) -> None:
        from brain import graph as graph_module

        backend = self.root / "graph-backend-cache-safety"
        backend.write_bytes(b"synthetic")
        backend.chmod(0o755)
        target = self.settings.repo("customer-service").path
        before = {
            str(path.relative_to(target)): path.read_bytes()
            for path in target.rglob("*") if path.is_file()
        }
        cache = self.settings.state_dir / "codebase-memory"
        try:
            cache.symlink_to(target, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"symbolic links unavailable: {error}")

        def poisoned_backend(_command, _cwd, **kwargs):
            Path(kwargs["environment"]["CBM_CACHE_DIR"]).joinpath("backend-poison").write_text(
                "external-backend-write", encoding="utf-8",
            )
            return subprocess.CompletedProcess([], 0, "{}", "")

        with mock.patch.dict(os.environ, {"PROJECT_BRAIN_GRAPH_BIN": str(backend)}), mock.patch(
            "brain.graph.run_bounded_process", side_effect=poisoned_backend,
        ) as invoked:
            payload, error = graph_module._invoke(self.settings, "trace_path", {"depth": 4})
        self.assertIsNone(payload)
        self.assertEqual("unsafe managed cache directory", error)
        invoked.assert_not_called()
        after = {
            str(path.relative_to(target)): path.read_bytes()
            for path in target.rglob("*") if path.is_file()
        }
        self.assertEqual(before, after)

    def test_structural_index_stages_backend_writes_before_capacity_validation(self) -> None:
        from brain import graph as graph_module

        backend = self.root / "graph-backend-staging"
        backend.write_bytes(b"synthetic")
        backend.chmod(0o755)
        cache = self.settings.state_dir / "codebase-memory"
        cache.mkdir(parents=True)
        (cache / "published").write_text("old", encoding="utf-8")

        def staged_backend(_command, _cwd, **kwargs):
            staged = Path(kwargs["environment"]["CBM_CACHE_DIR"])
            self.assertNotEqual(cache, staged)
            (staged / "new-backend-state").write_text("new", encoding="utf-8")
            return subprocess.CompletedProcess([], 0, "{}", "")

        with mock.patch.dict(os.environ, {"PROJECT_BRAIN_GRAPH_BIN": str(backend)}), mock.patch(
            "brain.graph._projected_graph_cache_bytes", return_value=1,
        ), mock.patch("brain.ops.remaining_write_capacity", return_value=1_000_000_000), mock.patch(
            "brain.graph.run_bounded_process", side_effect=staged_backend,
        ), mock.patch("brain.ops.ensure_write_capacity", side_effect=OSError("quota")):
            payload, error = graph_module._invoke(
                self.settings, "index_repository", {"repo_path": str(self.root)},
            )
        self.assertIsNone(payload)
        self.assertEqual("graph cache exceeded managed write capacity", error)
        self.assertEqual("old", (cache / "published").read_text(encoding="utf-8"))
        self.assertFalse((cache / "new-backend-state").exists())
        self.assertEqual([], list(self.settings.state_dir.glob(".graph-cache-stage-*")))

    def test_structural_index_cleanup_failure_does_not_misreport_committed_cache(self) -> None:
        from brain import graph as graph_module

        backend = self.root / "graph-backend-commit"
        backend.write_bytes(b"synthetic")
        backend.chmod(0o755)
        cache = self.settings.state_dir / "codebase-memory"
        cache.mkdir(parents=True)
        (cache / "published").write_text("old", encoding="utf-8")

        def staged_backend(_command, _cwd, **kwargs):
            staged = Path(kwargs["environment"]["CBM_CACHE_DIR"])
            (staged / "published").write_text("new", encoding="utf-8")
            return subprocess.CompletedProcess([], 0, "{}", "")

        real_rmtree = shutil.rmtree

        def cleanup(path, *args, **kwargs):
            if Path(path).name == "previous":
                raise OSError("simulated old-cache cleanup failure")
            return real_rmtree(path, *args, **kwargs)

        with mock.patch.dict(os.environ, {"PROJECT_BRAIN_GRAPH_BIN": str(backend)}), mock.patch(
            "brain.graph._projected_graph_cache_bytes", return_value=1,
        ), mock.patch("brain.ops.remaining_write_capacity", return_value=1_000_000_000), mock.patch(
            "brain.graph.run_bounded_process", side_effect=staged_backend,
        ), mock.patch("brain.ops.ensure_write_capacity"), mock.patch(
            "brain.graph.shutil.rmtree", side_effect=cleanup,
        ):
            payload, error = graph_module._invoke(
                self.settings, "index_repository", {"repo_path": str(self.root)},
            )
        self.assertEqual({}, payload)
        self.assertIsNone(error)
        self.assertEqual("new", (cache / "published").read_text(encoding="utf-8"))

    def test_ripgrep_rejects_one_giant_output_line_before_decoding(self) -> None:
        from brain.backends import ripgrep

        class Process:
            def __init__(self):
                self.stdout = io.BytesIO(b"file.py:1:" + b"x" * (ripgrep.MAX_RIPGREP_LINE_BYTES + 1) + b"\n")

            def poll(self):
                return None

            def wait(self, timeout=None):
                return 0

        with mock.patch("brain.backends.ripgrep.trusted_path_executable", return_value=Path("/usr/bin/rg")), mock.patch(
            "brain.backends.ripgrep.start_managed_process", return_value=Process(),
        ), mock.patch("brain.backends.ripgrep.terminate_process_tree") as terminated:
            result = ripgrep.search(self.root, "needle", fixed=True, max_results=10)
        self.assertIsNotNone(result)
        rows, metrics = result
        self.assertEqual([], rows)
        self.assertTrue(metrics["timed_out"])
        self.assertLessEqual(metrics["bytes_scanned"], ripgrep.MAX_RIPGREP_LINE_BYTES + 1)
        terminated.assert_called()

    def test_ripgrep_timeout_interrupts_a_producer_with_no_stdout(self) -> None:
        from brain.backends import ripgrep

        released = threading.Event()

        class Output:
            def readline(self, _limit: int) -> bytes:
                released.wait(2)
                return b""

            def close(self) -> None:
                released.set()

        class Process:
            def __init__(self) -> None:
                self.stdout = Output()
                self.terminated = False

            def poll(self):
                return -15 if self.terminated else None

            def wait(self, timeout=None):
                if not self.terminated:
                    raise subprocess.TimeoutExpired("rg", timeout)
                return -15

        process = Process()

        def terminate(target, *, graceful_timeout=1.0):
            target.terminated = True
            released.set()

        started = time.monotonic()
        with mock.patch("brain.backends.ripgrep.trusted_path_executable", return_value=Path("/usr/bin/rg")), mock.patch(
            "brain.backends.ripgrep.start_managed_process", return_value=process,
        ), mock.patch("brain.backends.ripgrep.terminate_process_tree", side_effect=terminate):
            result = ripgrep.search(
                self.root, "needle", fixed=True, max_results=10, timeout_seconds=.05,
            )
        elapsed = time.monotonic() - started
        self.assertIsNotNone(result)
        rows, metrics = result
        self.assertEqual([], rows)
        self.assertTrue(metrics["timed_out"])
        self.assertLess(elapsed, .5)

    def test_repository_discovery_and_python_fallback_have_global_bounds(self) -> None:
        discovery_root = self.root / "discovery-bound"
        (discovery_root / "a" / "b").mkdir(parents=True)
        with (
            mock.patch("brain.core.MAX_REPOSITORY_DISCOVERY_ENTRIES", 1),
            self.assertRaisesRegex(BrainError, "discovery exceeded"),
        ):
            core_module.discover_git_repositories([discovery_root])

        repository = self.settings.repo("trading-service")
        with (
            mock.patch("brain.core.MAX_FALLBACK_SEARCH_BYTES", 1),
            self.assertRaisesRegex(BrainError, "byte budget"),
        ):
            core_module._python_search(repository, "eligibility", True, 10)

        started = time.monotonic()
        self.assertEqual([], core_module._python_search(repository, r"(a+)+$", False, 10))
        self.assertLess(time.monotonic() - started, 0.1)

    def test_sync_git_commands_use_hard_stdout_and_stderr_ceilings(self) -> None:
        completed = subprocess.CompletedProcess(["git"], 1, "", "noisy")
        completed.output_truncated = True
        with mock.patch("brain.sync.run_bounded_process", return_value=completed) as invoked:
            result = sync_module._git(self.settings.repo("trading-service"), "status")
        self.assertEqual(125, result.returncode)
        self.assertEqual(sync_module.MAX_GIT_COMMAND_STDOUT_BYTES, invoked.call_args.kwargs["max_stdout_bytes"])
        self.assertEqual(sync_module.MAX_GIT_COMMAND_STDERR_BYTES, invoked.call_args.kwargs["max_stderr_bytes"])

    def test_first_refresh_snapshots_even_uncommitted_repositories(self) -> None:
        _, updated = snapshot_indexes(self.settings, changed_only=True)
        self.assertEqual({"customer-service", "trading-service", "risk-service", "batch-service"}, set(updated))
        self.assertEqual({"customer-service", "trading-service", "risk-service", "batch-service"}, set(load_index_state(self.settings)))

    def test_snapshot_index_serves_exact_content_and_path_queries_without_scanning(self) -> None:
        for repo in self.settings.repositories:
            repo.source_path = repo.path
            repo.source_sha = f"snapshot-{repo.name}"
        state, updated = snapshot_indexes(self.settings, changed_only=True)

        self.assertEqual(set(state), set(updated))
        self.assertTrue((self.settings.state_dir / "search.sqlite3").is_file())
        self.assertTrue(all(item["backend"] == "sqlite fts5 trigram" for item in state.values()))
        self.assertEqual(1, benchmark_report(self.settings)["events"]["index"]["samples"])
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(0, main(["-c", str(self.config), "benchmark", "--json"]))
        self.assertEqual(1, json.loads(output.getvalue())["events"]["index"]["samples"])
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(0, main(["-c", str(self.config), "benchmark", "--machine", "--json"]))
        machine = json.loads(output.getvalue())["machine"]
        self.assertEqual(1, machine["schema_version"])
        self.assertNotIn("hostname", machine)
        self.assertTrue((self.settings.state_dir / "machine-profile.json").is_file())
        with mock.patch("brain.core.search_repo", side_effect=AssertionError("scanner should not run")):
            hits = search(self.settings, "JURISDICTION_CHANGED", fixed=True)
            paths = path_hits(self.settings, "application.properties", ["trading-service"])
        self.assertEqual({"customer-service", "trading-service"}, {hit.repo for hit in hits})
        self.assertEqual(["src/main/resources/application.properties"], [hit.path for hit in paths])

        self.assertEqual([], snapshot_indexes(self.settings, changed_only=True)[1])
        self.settings.repo("trading-service").source_sha = "snapshot-trading-service-2"
        self.assertEqual(["trading-service"], snapshot_indexes(self.settings, changed_only=True)[1])

    def test_storage_guard_prevents_new_index_write_before_low_disk(self) -> None:
        self.settings.minimum_free_disk_gb = 1_000_000
        with self.assertRaisesRegex(OSError, "free-disk guard"):
            snapshot_indexes(self.settings)

    @unittest.skipIf(os.name == "nt", "POSIX permission model")
    def test_brain_owned_state_directories_are_private(self) -> None:
        for path in (self.settings.state_dir, self.settings.runs_dir, self.settings.generated_dir):
            self.assertEqual(0, stat.S_IMODE(path.stat().st_mode) & 0o077)
        self.assertFalse((self.settings.state_dir / "search.sqlite3").exists())

    def test_non_git_snapshot_fails_whole_build_on_item_or_aggregate_capacity_limit(self) -> None:
        from brain import index as index_module

        with mock.patch("brain.ops.remaining_write_capacity", return_value=100), self.assertRaisesRegex(
            OSError, "managed write capacity",
        ):
            snapshot_indexes(self.settings, changed_only=False)
        snapshots = self.settings.state_dir / "snapshots"
        self.assertFalse(list(snapshots.rglob("snapshot-working-tree-*")) if snapshots.exists() else [])
        with mock.patch.object(index_module, "_NONGIT_SNAPSHOT_MAX_ITEMS", 1), self.assertRaisesRegex(
            RuntimeError, "item or time limit",
        ):
            snapshot_indexes(self.settings, changed_only=False)
        self.assertFalse(list(snapshots.rglob("snapshot-working-tree-*")) if snapshots.exists() else [])

    def test_lexical_actual_growth_is_checked_before_transaction_commit(self) -> None:
        from brain.index import query_index

        state, _ = snapshot_indexes(self.settings, publish=False)
        repo = self.settings.repo("trading-service")
        previous_snapshot = str(state[repo.name]["sha"])
        self.settings.repositories = [repo]
        repo.source_path = repo.path
        repo.source_sha = "quota-rejected-lexical-generation"
        source = repo.path / "src/main/java/demo/TradingEligibilityService.java"
        source.write_text(source.read_text(encoding="utf-8") + "\n// quota candidate\n", encoding="utf-8")
        with mock.patch(
            "brain.ops.ensure_write_capacity", side_effect=[None, OSError("actual lexical quota")],
        ), self.assertRaisesRegex(OSError, "actual lexical quota"):
            snapshot_indexes(self.settings, changed_only=True, publish=False)
        self.assertTrue(query_index(
            self.settings, repo, "RiskClient", max_results=5, snapshot_sha=previous_snapshot,
        ))
        self.assertIsNone(query_index(
            self.settings, repo, "quota candidate", max_results=5,
            snapshot_sha="quota-rejected-lexical-generation",
        ))

    def test_semantic_actual_growth_is_checked_before_state_publication(self) -> None:
        snapshot_indexes(self.settings, publish=False)
        state_path = self.settings.state_dir / "semantic-index.json"
        with mock.patch(
            "brain.ops.ensure_write_capacity", side_effect=OSError("actual semantic quota"),
        ), self.assertRaisesRegex(OSError, "actual semantic quota"):
            build_semantic_index(
                self.settings,
                embed=lambda cards: [[1.0, 0.0] for _ in cards],
                pack_id="quota-test",
            )
        self.assertFalse(state_path.exists())

    def test_benchmark_reads_only_bounded_metric_tail_and_caps_rows(self) -> None:
        from brain import metrics as metrics_module

        path = self.settings.state_dir / "metrics.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            b"old-prefix-without-a-complete-row" * 400_000
            + b'\n{"event":"tail","duration_ms":7}\n'
        )
        with mock.patch.object(Path, "read_text", side_effect=AssertionError("unbounded read")):
            report = benchmark_report(self.settings)
        self.assertEqual(1, report["events"]["tail"]["samples"])
        metrics_module.record_metric(self.settings, "bounded", diagnostic="x" * (metrics_module.MAX_METRIC_FIELD_BYTES + 1))
        self.assertLessEqual(
            len(path.read_bytes().splitlines()[-1]) + 1,
            metrics_module.MAX_METRIC_ROW_BYTES,
        )

        path.unlink()
        with mock.patch.object(metrics_module, "MAX_METRIC_HISTORY_BYTES", 1_024):
            threads = [
                threading.Thread(
                    target=metrics_module.record_metric,
                    args=(self.settings, f"concurrent-{number}"),
                    kwargs={"diagnostic": "x" * 160},
                )
                for number in range(40)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        raw = path.read_bytes()
        self.assertLessEqual(len(raw), 1_024)
        rows = [json.loads(line) for line in raw.splitlines()]
        self.assertTrue(rows)
        self.assertTrue(all(str(row["event"]).startswith("concurrent-") for row in rows))

    def test_golden_evaluation_suite_is_single_read_and_hard_bounded(self) -> None:
        from brain import evaluation as evaluation_module

        oversized = self.root / "oversized-evaluation.json"
        oversized.write_bytes(b"x" * (evaluation_module.MAX_EVALUATION_SUITE_BYTES + 1))
        with mock.patch.object(Path, "read_text", side_effect=AssertionError("unbounded read")), mock.patch.object(
            Path, "read_bytes", side_effect=AssertionError("second full read"),
        ), self.assertRaisesRegex(ValueError, "suite exceeds its byte limit"):
            evaluate_golden(self.settings, oversized)

        too_many = self.root / "too-many-evaluation-cases.json"
        too_many.write_text(json.dumps({
            "cases": [
                {"id": f"case-{number}", "request": {"objective": "bounded"}}
                for number in range(evaluation_module.MAX_EVALUATION_CASES + 1)
            ],
        }), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "case limit"):
            evaluate_golden(self.settings, too_many)

    def test_ui_storage_summary_never_uses_exact_unbounded_quota_scan(self) -> None:
        from brain import ops as ops_module

        crowded = self.settings.state_dir / "crowded"
        crowded.mkdir(parents=True)
        for number in range(80):
            (crowded / f"{number:03d}.bin").write_bytes(b"x")
        ops_module._STORAGE_STATUS_CACHE.clear()
        with mock.patch.object(ops_module, "MAX_STORAGE_STATUS_ENTRIES", 50), mock.patch(
            "brain.ops._directory_bytes", side_effect=AssertionError("exact quota scan reached UI status"),
        ):
            summary = ops_module.storage(self.settings)
        self.assertFalse(summary["complete"])
        self.assertLessEqual(summary["scanned_entries"], 50)

        ops_module._STORAGE_STATUS_CACHE.clear()
        with mock.patch("brain.ops.status_probe", return_value=None) as probed:
            ops_module.storage(self.settings)
            ops_module.storage(self.settings)
        probed.assert_called_once_with(self.settings)

    def test_lexical_catalog_projection_preflights_global_atlas_limit_before_mutation(self) -> None:
        from brain import atlas as atlas_module
        from brain.catalog import record_index_catalog

        state, _ = snapshot_indexes(self.settings, changed_only=False, publish=False)
        database = self.settings.state_dir / "catalog.sqlite3"
        connection = sqlite3.connect(database)
        try:
            before = connection.execute(
                "SELECT repo,sha,path,blob_sha FROM snapshot_files ORDER BY repo,sha,path"
            ).fetchall()
        finally:
            connection.close()
        with mock.patch.object(atlas_module, "MAX_ATLAS_FILES", 0), self.assertRaisesRegex(
            RuntimeError, "projection exceeds",
        ):
            record_index_catalog(self.settings, state)
        connection = sqlite3.connect(database)
        try:
            after = connection.execute(
                "SELECT repo,sha,path,blob_sha FROM snapshot_files ORDER BY repo,sha,path"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(before, after)

    def test_authoritative_capacity_scan_fails_closed_at_its_entry_budget(self) -> None:
        from brain import ops as ops_module

        crowded = self.settings.state_dir / "capacity-crowded"
        crowded.mkdir(parents=True)
        for number in range(20):
            (crowded / f"{number:03d}.bin").write_bytes(b"x")
        with mock.patch.object(ops_module, "MAX_CAPACITY_SCAN_ENTRIES", 10), self.assertRaisesRegex(
            OSError, "capacity scan budget exceeded",
        ):
            ops_module.ensure_write_capacity(self.settings)

    def test_context_and_retrieval_config_cannot_disable_global_bounds(self) -> None:
        project = self.root / "oversized-config"
        repository = project / "repository"
        repository.mkdir(parents=True)
        config = project / "brain.toml"
        config.write_text(
            "[project]\nname='bounded'\n"
            "[context]\nhard_context_chars=999999999\nsource_window_lines=999999999\n"
            "[delivery]\nclipboard_chunk_chars=999999999\n"
            "[search]\ncandidate_limit=999999999\n"
            "[[repositories]]\nname='repository'\npath='repository'\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(BrainError, "hard_context_chars must be between"):
            load_settings(config)
        self.assertFalse((project / "state").exists())

        many = project / "many.toml"
        many.write_text(
            "[project]\nname='too-many'\n" + "".join(
                f"[[repositories]]\nname='repo-{number}'\npath='repository'\n"
                for number in range(101)
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(BrainError, "at most 100 repositories"):
            load_settings(many)
        self.assertFalse((project / "state").exists())

    def test_session_publication_rejects_symlink_escape_and_oversize_atomically(self) -> None:
        outside = self.root / "outside-session"
        outside.mkdir()
        outside_state = outside / "session.json"
        outside_state.write_text(json.dumps({"ticket": "SEC-1", "marker": "original"}), encoding="utf-8")
        symlink = self.settings.runs_dir / "SEC-1"
        try:
            symlink.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"symbolic links unavailable: {error}")
        with self.assertRaisesRegex(BrainError, "escapes the managed runs directory"):
            core_module.save_session(self.settings, "SEC-1", {"ticket": "SEC-1", "marker": "overwritten"})
        self.assertEqual("original", json.loads(outside_state.read_text(encoding="utf-8"))["marker"])

        symlink.unlink()
        core_module.save_session(self.settings, "SIZE-1", {"ticket": "SIZE-1", "marker": "valid"})
        state_path = session_dir(self.settings, "SIZE-1") / "session.json"
        original = state_path.read_bytes()
        with self.assertRaisesRegex(BrainError, "exceeds its byte limit"):
            core_module.save_session(self.settings, "SIZE-1", {
                "ticket": "SIZE-1", "padding": "x" * core_module.MAX_SESSION_STATE_BYTES,
            })
        self.assertEqual(original, state_path.read_bytes())
        self.assertEqual("valid", session_state(self.settings, "SIZE-1")["marker"])

        core_module.save_session(self.settings, "SEC-2", {"ticket": "SEC-2", "marker": "valid"})
        child_target = self.root / "outside-child.txt"
        child_target.write_text("outside-original", encoding="utf-8")
        child_directory = session_dir(self.settings, "SEC-2")
        (child_directory / "ticket.md").symlink_to(child_target)
        (child_directory / "session.json.writing").symlink_to(child_target)
        start_session(self.settings, "SEC-2", "managed replacement")
        self.assertEqual("outside-original", child_target.read_text(encoding="utf-8"))
        self.assertFalse((child_directory / "ticket.md").is_symlink())
        self.assertEqual("managed replacement\n", (child_directory / "ticket.md").read_text(encoding="utf-8"))

        nested_target = self.root / "outside-nested"
        nested_target.mkdir()
        (child_directory / "delivery").symlink_to(nested_target, target_is_directory=True)
        with self.assertRaisesRegex(BrainError, "parent must not be a symbolic link"):
            core_module._atomic_session_text_write(
                self.settings, "SEC-2", child_directory / "delivery/part-001.txt", "blocked",
            )
        self.assertFalse((nested_target / "part-001.txt").exists())

        injected = self.root / "outside-state.json"
        injected.write_text(json.dumps({"ticket": "SEC-3", "marker": "OUTSIDE"}), encoding="utf-8")
        injected_directory = self.settings.runs_dir / "SEC-3"
        injected_directory.mkdir()
        (injected_directory / "session.json").symlink_to(injected)
        with self.assertRaisesRegex(BrainError, "Session state must not be a symbolic link"):
            session_state(self.settings, "SEC-3")

        secret = self.root / "outside-secret.txt"
        secret.write_text("TOP_SECRET", encoding="utf-8")
        core_module.save_session(self.settings, "SEC-4", {
            "ticket": "SEC-4",
            "delivery": {"parts": [str(secret)], "current": 1},
        })
        with mock.patch("brain.core.clipboard_write") as copied:
            with self.assertRaisesRegex(BrainError, "Invalid delivery artifact"):
                core_module.move_delivery(self.settings, "SEC-4", 0)
        copied.assert_not_called()

        start_session(self.settings, "SEC-HANDOFF", "Protect target repositories from handoff writes.")
        handoffs = self.settings.generated_dir / "handoffs"
        target_repo = self.settings.repo("customer-service").path
        before = {
            str(path.relative_to(target_repo)): path.read_bytes()
            for path in target_repo.rglob("*") if path.is_file()
        }
        handoffs.symlink_to(target_repo, target_is_directory=True)
        with self.assertRaisesRegex(BrainError, "parent must not be a symbolic link"):
            deliver(self.settings, "SEC-HANDOFF", "bounded handoff", "m365", copy=False)
        after = {
            str(path.relative_to(target_repo)): path.read_bytes()
            for path in target_repo.rglob("*") if path.is_file()
        }
        self.assertEqual(before, after)

    def test_direct_reads_and_managed_writes_reject_racing_path_substitution(self) -> None:
        from brain.index import _read_source_bytes
        from brain.platforms import atomic_managed_bytes_write as real_managed_write

        source = self.root / "race-source.py"
        source.write_text("SAFE_SOURCE\n", encoding="utf-8")
        secret = self.root / "race-secret.txt"
        secret.write_text("TOP_SECRET_OUTSIDE\n", encoding="utf-8")
        original_open = os.open
        swapped = False

        def swap_leaf(name: object, flags: int, *args: object, **kwargs: object) -> int:
            nonlocal swapped
            if not swapped and Path(name) == source:
                swapped = True
                source.unlink()
                source.symlink_to(secret)
            return original_open(name, flags, *args, **kwargs)

        try:
            with mock.patch("brain.platforms.os.open", side_effect=swap_leaf):
                with self.assertRaises(OSError):
                    _read_source_bytes(source)
        finally:
            if source.is_symlink():
                source.unlink()
            source.write_text("SAFE_SOURCE\n", encoding="utf-8")

        swapped = False
        with mock.patch("brain.platforms.os.open", side_effect=swap_leaf):
            content, omitted = core_module._bounded_text_file(source, 1024)
        self.assertTrue(omitted)
        self.assertEqual("", content)
        self.assertNotIn("TOP_SECRET_OUTSIDE", content)
        if source.is_symlink():
            source.unlink()

        start_session(self.settings, "RACE-WRITE", "Keep writes inside managed state.")
        ticket_directory = session_dir(self.settings, "RACE-WRITE")
        detached = ticket_directory.with_name(ticket_directory.name + "-detached")
        outside = self.root / "outside-write"
        outside.mkdir()

        def swap_parent(root: Path, path: Path, payload: bytes) -> None:
            root.rename(detached)
            try:
                root.symlink_to(outside, target_is_directory=True)
            except OSError as error:
                detached.rename(root)
                self.skipTest(f"directory symlinks unavailable: {error}")
            try:
                real_managed_write(root, path, payload)
            finally:
                root.unlink()
                detached.rename(root)

        with mock.patch("brain.core.atomic_managed_bytes_write", side_effect=swap_parent):
            with self.assertRaisesRegex(BrainError, "changed during publication"):
                core_module._atomic_session_text_write(
                    self.settings, "RACE-WRITE", ticket_directory / "race.md", "OUTSIDE-WRITE",
                )
        self.assertFalse((outside / "race.md").exists())

    def test_delivery_rejects_cross_ticket_generated_handoffs(self) -> None:
        start_session(self.settings, "SEC-A", "Session A")
        start_session(self.settings, "SEC-B", "Session B")
        b_handoffs, _ = deliver(self.settings, "SEC-B", FINAL_SOLUTION, "m365", copy=False)
        state_a = session_state(self.settings, "SEC-A")
        state_a["delivery"] = {"parts": [str(b_handoffs[0])], "current": 1}
        core_module.save_session(self.settings, "SEC-A", state_a)
        with mock.patch("brain.core.clipboard_write") as copied:
            with self.assertRaisesRegex(BrainError, "does not belong to this session"):
                core_module.move_delivery(self.settings, "SEC-A", 0)
        copied.assert_not_called()

    def test_git_manifest_indexes_only_new_blobs_and_hydrates_missing_snapshot_files(self) -> None:
        repo = self.settings.repo("trading-service")
        for other in self.settings.repositories:
            if other is not repo:
                other.source_path = other.path
                other.source_sha = f"snapshot-{other.name}"
        subprocess.run(["git", "init", "-q"], cwd=repo.path, check=True)
        subprocess.run(["git", "config", "user.email", "brain@example.invalid"], cwd=repo.path, check=True)
        subprocess.run(["git", "config", "user.name", "Project Brain Test"], cwd=repo.path, check=True)
        hidden = repo.path / "hidden.md"
        hidden.write_text("ONLY_IN_GIT_OBJECT\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo.path, check=True)
        subprocess.run(["git", "commit", "-qm", "Initial snapshot"], cwd=repo.path, check=True)
        repo.source_path = repo.path
        repo.source_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo.path, text=True, capture_output=True, check=True
        ).stdout.strip()
        hidden.unlink()

        state, _ = snapshot_indexes(self.settings, changed_only=True)
        hits = search(self.settings, "ONLY_IN_GIT_OBJECT", ["trading-service"], fixed=True)
        self.assertEqual("hidden.md", hits[0].path)
        self.assertIn("ONLY_IN_GIT_OBJECT", read_source(self.settings, hits[0]).content)
        self.assertGreater(state["trading-service"]["changed_blobs"], 1)

        changed = repo.path / "src/main/java/demo/TradingEligibilityService.java"
        changed.write_text(changed.read_text(encoding="utf-8") + "// one blob changed\n", encoding="utf-8")
        subprocess.run(["git", "add", str(changed.relative_to(repo.path))], cwd=repo.path, check=True)
        subprocess.run(["git", "commit", "-qm", "Change one file"], cwd=repo.path, check=True)
        repo.source_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo.path, text=True, capture_output=True, check=True
        ).stdout.strip()
        state, updated = snapshot_indexes(self.settings, changed_only=True)
        self.assertEqual(["trading-service"], updated)
        self.assertEqual(1, state["trading-service"]["changed_blobs"])

    def test_git_manifest_parses_bounded_output_without_whole_output_split(self) -> None:
        from brain import index as index_module

        repo = self.settings.repo("trading-service")
        (repo.path / ".git").mkdir(exist_ok=True)
        repo.source_sha = "abc123"

        output = ("100644 blob " + "a" * 40 + "\tsrc/A.java\0").encode("utf-8")
        completed = subprocess.CompletedProcess(["git"], 0, output, "")
        with mock.patch("brain.index.run_bounded_process", return_value=completed):
            manifest = index_module._git_manifest(repo)
        self.assertEqual(("100644", "a" * 40), manifest["src/A.java"])
        completed.output_truncated = True
        with mock.patch("brain.index.run_bounded_process", return_value=completed), self.assertRaisesRegex(
            RuntimeError, "exceeded",
        ):
            index_module._git_manifest(repo)

    def test_empty_git_tree_never_falls_through_to_untracked_working_tree(self) -> None:
        from brain.index import query_index

        repo = self.settings.repo("trading-service")
        subprocess.run(["git", "init", "-q"], cwd=repo.path, check=True)
        subprocess.run(["git", "config", "user.email", "brain@example.invalid"], cwd=repo.path, check=True)
        subprocess.run(["git", "config", "user.name", "Project Brain Test"], cwd=repo.path, check=True)
        subprocess.run(["git", "commit", "--allow-empty", "-qm", "empty authority"], cwd=repo.path, check=True)
        marker = repo.path / "untracked_authority_escape.py"
        marker.write_text("EMPTY_TREE_MUST_NOT_INDEX_THIS\n", encoding="utf-8")
        repo.source_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo.path, text=True, capture_output=True, check=True,
        ).stdout.strip()
        repo.source_path = None

        state, _ = snapshot_indexes(self.settings, changed_only=False, publish=False)
        self.assertEqual(0, state[repo.name]["files"])
        self.assertEqual([], query_index(
            self.settings, repo, "EMPTY_TREE_MUST_NOT_INDEX_THIS", max_results=5,
            snapshot_sha=repo.source_sha,
        ))

    def test_git_manifest_rejects_non_utf8_paths_without_identity_collapse(self) -> None:
        from brain import index as index_module

        repo = self.settings.repo("trading-service")
        (repo.path / ".git").mkdir(exist_ok=True)
        repo.source_sha = "abc123"
        output = (
            b"100644 blob " + b"a" * 40 + b"\tbad-\xfe.py\0"
            b"100644 blob " + b"b" * 40 + b"\tbad-\xff.py\0"
        )
        completed = subprocess.CompletedProcess(["git"], 0, output, b"")
        with mock.patch("brain.index.run_bounded_process", return_value=completed), self.assertRaisesRegex(
            RuntimeError, "non-UTF-8 path",
        ):
            index_module._git_manifest(repo)

    def test_git_cat_file_failure_never_returns_partial_authoritative_content(self) -> None:
        from brain.index import _git_blob_contents

        repo = self.settings.repo("trading-service")
        completed = subprocess.CompletedProcess(["git"], 1, b"", b"missing")
        with mock.patch("brain.index.run_bounded_process", return_value=completed), self.assertRaisesRegex(
            RuntimeError, "validation failed",
        ):
            list(_git_blob_contents(repo, {"a" * 40}))

    def test_non_git_authoritative_read_failure_rolls_back_the_index_generation(self) -> None:
        from brain.index import query_index

        state, _ = snapshot_indexes(self.settings, publish=False)
        repo = self.settings.repo("trading-service")
        previous_snapshot = str(state[repo.name]["sha"])
        source = repo.scan_path / "src/main/java/demo/TradingEligibilityService.java"
        self.settings.repositories = [repo]
        repo.source_sha = "unreadable-authoritative-source"
        repo.source_path = repo.path
        from brain import index as index_module

        original_read = index_module._read_source_bytes

        def fail_one(path: Path) -> bytes:
            if path == source:
                raise OSError("simulated source failure")
            return original_read(path)

        with mock.patch("brain.index._walk_files", return_value=iter([source])), mock.patch(
            "brain.index._read_source_bytes", side_effect=fail_one,
        ), self.assertRaisesRegex(
            RuntimeError, "Could not read authoritative source",
        ):
            snapshot_indexes(self.settings, changed_only=True, publish=False)
        self.assertTrue(query_index(
            self.settings, repo, "RiskClient", max_results=5, snapshot_sha=previous_snapshot,
        ))

    def test_concurrent_sync_serializes_capacity_accounted_snapshot_exports(self) -> None:
        selected = self.settings.repositories[:2]
        git = core_module.native_command("git") if hasattr(core_module, "native_command") else "git"
        for repo in selected:
            for command in (
                [git, "init", "-q"],
                [git, "config", "user.email", "brain@example.invalid"],
                [git, "config", "user.name", "Project Brain Test"],
                [git, "add", "."],
                [git, "commit", "-qm", "capacity baseline"],
            ):
                subprocess.run(command, cwd=repo.path, check=True)
        active = 0
        maximum = 0
        calls = 0
        guard = threading.Lock()

        def exported(repo, _ref, sha, state_dir, **_kwargs):
            nonlocal active, maximum, calls
            with guard:
                active += 1
                calls += 1
                maximum = max(maximum, active)
            time.sleep(0.05)
            with guard:
                active -= 1
            return state_dir / "snapshots" / repo.name / sha

        with mock.patch("brain.sync._export_snapshot", side_effect=exported), mock.patch(
            "brain.ops.remaining_write_capacity", return_value=1024,
        ) as capacity:
            sync_module.sync_repositories(self.settings, fetch=False)
        self.assertEqual(2, calls)
        self.assertEqual(1, maximum)
        capacity.assert_called_once_with(self.settings)

    def test_uppercase_and_spaced_legacy_repo_names_keep_pinned_snapshot_compatibility(self) -> None:
        for index, name in enumerate(("ServiceA", "Orders Service"), 1):
            project = self.root / f"legacy-name-{index}"
            repository = project / "repo"
            repository.mkdir(parents=True)
            (repository / "source.py").write_text(f"MARKER_{index} = True\n", encoding="utf-8")
            config = project / "brain.toml"
            config.write_text(
                "[project]\nname='legacy'\n[graph]\nenabled=false\n[experience]\nenabled=false\n"
                f"[[repositories]]\nname={json.dumps(name)}\npath='repo'\n",
                encoding="utf-8",
            )
            git = native_command("git")
            for command in (
                [git, "init", "-q"], [git, "config", "user.email", "brain@example.invalid"],
                [git, "config", "user.name", "Project Brain Test"], [git, "add", "."],
                [git, "commit", "-qm", "legacy snapshot"],
            ):
                subprocess.run(command, cwd=repository, check=True)
            settings = load_settings(config)
            sync_module.sync_repositories(settings, fetch=False)
            repo = settings.repo(name)
            self.assertIsNotNone(repo.source_path)
            new_snapshot = repo.source_path
            assert new_snapshot is not None and repo.source_sha is not None
            legacy_name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-") or "repo"
            legacy_parent = settings.state_dir / "snapshots" / legacy_name
            legacy_parent.mkdir(parents=True, exist_ok=True)
            legacy_snapshot = legacy_parent / repo.source_sha
            new_snapshot.replace(legacy_snapshot)
            new_seal = new_snapshot.parent / f".{repo.source_sha}.brain-snapshot.json"
            new_seal.replace(legacy_parent / new_seal.name)
            repo.source_path = legacy_snapshot
            sources_path = settings.state_dir / "sources.json"
            sources = json.loads(sources_path.read_text(encoding="utf-8"))
            sources[name]["snapshot"] = str(legacy_snapshot)
            sources_path.write_text(json.dumps(sources), encoding="utf-8")

            snapshot_indexes(settings, changed_only=False)
            start_session(settings, f"LEGACY-{index}", "Keep the exact legacy source pin.")
            context, _, _ = create_context(settings, f"LEGACY-{index}", f"""CONTEXT_REQUEST:
  objective: Locate MARKER_{index}.
  searches:
    - query: MARKER_{index}
      repos: [{json.dumps(name)}]
  symbols: []
  files: []
  history: []
""")
            self.assertIn(f"MARKER_{index}", context)

    def test_retrieval_hydrates_only_ranked_diverse_candidates(self) -> None:
        self.settings.hydrate_limit = 1
        request = parse_context_request("""CONTEXT_REQUEST:
  objective: Locate representative classes without flooding context.
  searches:
    - query: class
      repos: []
  symbols: []
  files: []
  history: []
""")
        bundle = retrieve_context(self.settings, request)
        self.assertEqual(1, len(bundle.evidence))
        self.assertGreater(len(bundle.additional_candidates), 1)

    def test_context_end_to_end_contains_source_tests_and_unresolved(self) -> None:
        start_session(self.settings, "ABC-1", "Jurisdiction changes should recalculate eligibility immediately.")
        context, path, number = create_context(self.settings, "ABC-1", REQUEST)
        self.assertEqual(1, number)
        self.assertTrue(path.is_file())
        self.assertIn("CustomerChangedListener.java", context)
        self.assertIn("CustomerChangedListenerTest.java", context)
        self.assertIn("TradingEligibilityService.java", context)
        self.assertIn("Static execution relationships", context)
        self.assertIn("## Unresolved", context)
        self.assertIn("## Investigation progress", context)
        self.assertIn("New unique evidence regions:", context)

    def test_concurrent_tickets_keep_request_state_and_artifacts_isolated(self) -> None:
        tickets = ["CONCURRENT-A", "CONCURRENT-B"]
        for ticket in tickets:
            start_session(self.settings, ticket, f"Investigate {ticket}.")
        entered = threading.Barrier(2)
        results: dict[str, tuple[str, Path, int]] = {}
        errors: list[BaseException] = []

        def retrieve(*args: object, **kwargs: object) -> ContextBundle:
            entered.wait(timeout=3)
            return ContextBundle(
                objective="Concurrent isolation check.",
                metrics={"total_ms": 1.0, "candidates": 0},
                trace={
                    "trace_schema_version": 2,
                    "requested_operations": 1,
                    "effective_operations": 1,
                    "physical_backend_operations": 0,
                    "planner": {},
                },
            )

        def create(ticket: str) -> None:
            try:
                results[ticket] = create_context(self.settings, ticket, REQUEST)
            except BaseException as error:  # pragma: no cover - asserted below
                errors.append(error)

        with mock.patch("brain.core.retrieve_context", side_effect=retrieve):
            threads = [threading.Thread(target=create, args=(ticket,)) for ticket in tickets]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)

        self.assertEqual([], errors)
        self.assertEqual(set(tickets), set(results))
        for ticket in tickets:
            _, path, number = results[ticket]
            self.assertEqual(1, number)
            self.assertEqual(self.settings.runs_dir / ticket / "context-001.md", path)
            self.assertTrue(path.is_file())
            state = session_state(self.settings, ticket)
            self.assertEqual(1, state["requests"])
            self.assertEqual(1, len(state["request_history"]))
            trace = state["request_history"][0]["retrieval"]["trace"]
            self.assertIn("context_pack_ms", trace)
            self.assertEqual(trace["wall_ms"], trace["total_ms"])

    def test_missing_requested_file_is_unresolved_without_half_written_round(self) -> None:
        start_session(self.settings, "ABC-MISSING", "Find the implementation even if one guessed file is absent.")
        request = """CONTEXT_REQUEST:
  version: 1
  objective: Find the evaluator and inspect a guessed configuration file.
  searches:
    - query: EligibilityEvaluator
      repos: [trading-service]
  symbols: []
  files:
    - repo: trading-service
      path: src/main/resources/guessed-missing.yml
  history: []
"""

        context, _, number = create_context(self.settings, "ABC-MISSING", request)
        self.assertEqual(1, number)
        self.assertIn("EligibilityEvaluator", context)
        self.assertIn("Requested file `trading-service:src/main/resources/guessed-missing.yml` was not found", context)
        self.assertTrue((self.settings.runs_dir / "ABC-MISSING/request-001.yml").is_file())
        self.assertTrue((self.settings.runs_dir / "ABC-MISSING/context-001.md").is_file())
        self.assertEqual(1, json.loads((self.settings.runs_dir / "ABC-MISSING/session.json").read_text())["requests"])

        start_session(self.settings, "ABC-UNSAFE", "Reject an unsafe direct file path.")
        unsafe = request.replace("src/main/resources/guessed-missing.yml", "../../outside.txt")
        with self.assertRaisesRegex(BrainError, "Unsafe file path"):
            create_context(self.settings, "ABC-UNSAFE", unsafe)
        self.assertFalse((self.settings.runs_dir / "ABC-UNSAFE/request-001.yml").exists())
        self.assertFalse((self.settings.runs_dir / "ABC-UNSAFE/context-001.md").exists())

    def test_ai_reply_routing_duplicate_detection_and_no_progress(self) -> None:
        start_session(self.settings, "ABC-ROUTE", "Investigate eligibility.")
        conversation = response_preview("Which production profile is active?", self.settings, "ABC-ROUTE")
        self.assertEqual("conversation", conversation["kind"])
        final = response_preview(FINAL_SOLUTION, self.settings, "ABC-ROUTE")
        self.assertEqual("final_solution", final["kind"])
        self.assertEqual("conversation", response_preview(
            "Exact source quote:\nFINAL_SOLUTION\nnot a protocol response", self.settings, "ABC-ROUTE",
        )["kind"])
        self.assertEqual("conversation", response_preview(
            "FINAL_SOLUTION\nChange the listener.", self.settings, "ABC-ROUTE",
        )["kind"])
        self.assertEqual("conversation", response_preview(
            "```text\nFINAL_SOLUTION\n```", self.settings, "ABC-ROUTE",
        )["kind"])
        self.assertEqual("conversation", response_preview(
            "FINAL_SOLUTION\nNot provided: ticket interpretation; verified current behavior; execution flow; "
            "root cause; exact repository; suggested production changes; test surface; validation commands; "
            "edge cases; implementation order; remaining assumptions.",
            self.settings, "ABC-ROUTE",
        )["kind"])
        self.assertEqual("conversation", response_preview(
            "FINAL_SOLUTION\n## Ticket interpretation\n\n## Verified current behavior\nNot provided.\n"
            "```markdown\n## Root cause\nFake body\n```",
            self.settings, "ABC-ROUTE",
        )["kind"])
        with self.assertRaisesRegex(BrainError, "contains no repository operations"):
            response_preview(
                "CONTEXT_REQUEST:\n  objective: Inspect the code.\n  searches: []\n  symbols: []\n  files: []\n  history: []",
                self.settings,
                "ABC-ROUTE",
            )

        create_context(self.settings, "ABC-ROUTE", REQUEST)
        duplicate = response_preview(REQUEST, self.settings, "ABC-ROUTE")
        self.assertEqual(1, duplicate["duplicate_of"])
        with self.assertRaisesRegex(BrainError, "already ran as request 001"):
            create_context(self.settings, "ABC-ROUTE", REQUEST)

        empty_request = REQUEST.replace("JURISDICTION_CHANGED", "NOT_PRESENT_ANYWHERE").replace(
            "EligibilityEvaluator", "MissingEvaluator"
        ).replace("recalculate", "missingMethod")
        context, _, _ = create_context(self.settings, "ABC-ROUTE", empty_request)
        self.assertIn("Consecutive requests with no new evidence: 1", context)
        self.assertIn("Do not repeat open-ended retrieval", context)

        for repo in self.settings.repositories:
            repo.source_sha = f"new-{repo.name}"
        start_session(self.settings, "ABC-ROUTE", "Restart against newer snapshots.")
        refreshed = response_preview(REQUEST, self.settings, "ABC-ROUTE")
        self.assertIsNone(refreshed["duplicate_of"])

    def test_complete_ai_reply_uses_the_latest_directive(self) -> None:
        start_session(self.settings, "ABC-LATEST", "Investigate eligibility.")
        create_context(self.settings, "ABC-LATEST", REQUEST)
        newer = REQUEST.replace(
            "Determine why jurisdiction changes do not recalculate eligibility online.",
            "Find the exact cache duration configuration.",
        ).replace("JURISDICTION_CHANGED", "transaction-cache-duration")

        preview = response_preview(REQUEST + "\nThe next request is:\n" + newer, self.settings, "ABC-LATEST")
        self.assertEqual("Find the exact cache duration configuration.", preview["objective"])
        self.assertIsNone(preview["duplicate_of"])
        _, _, number = create_context(self.settings, "ABC-LATEST", REQUEST + "\n" + newer)
        self.assertEqual(2, number)

        final = response_preview(FINAL_SOLUTION, self.settings)
        self.assertEqual("final_solution", final["kind"])

    def test_final_solution_m365_handoff_and_agent_kit(self) -> None:
        start, _ = start_session(self.settings, "ABC-M365", "Prepare an M365 handoff.")
        first, _ = deliver(self.settings, "ABC-M365", start, "m365", copy=False)
        self.assertEqual("ABC-M365-start.md", first[0].name)
        self.assertEqual(self.settings.generated_dir / "handoffs", first[0].parent)
        self.assertTrue((self.settings.runs_dir / "ABC-M365/current-handoff.md").is_file())
        self.assertTrue((self.settings.generated_dir / "handoffs/ABC-M365-current.md").is_file())

        context, _, _ = create_context(self.settings, "ABC-M365", REQUEST)
        request_handoff, _ = deliver(self.settings, "ABC-M365", context, "m365", copy=False)
        self.assertEqual("ABC-M365-context-001.md", request_handoff[0].name)
        self.assertIn("Request: `001`", request_handoff[0].read_text(encoding="utf-8"))

        final_text = FINAL_SOLUTION
        final_path = archive_final_solution(self.settings, "ABC-M365", final_text)
        second, _ = deliver(self.settings, "ABC-M365", final_text, "m365", copy=False)
        self.assertEqual("ABC-M365-final.md", second[0].name)
        self.assertNotEqual(first[0], second[0])
        self.assertEqual(final_text, second[0].read_text(encoding="utf-8"))
        self.assertTrue(final_path.is_file())
        self.assertEqual("ready_to_implement", json.loads((final_path.parent / "session.json").read_text())["status"])

        kit = create_m365_agent_kit(self.settings)
        self.assertLessEqual(len(kit["instructions"]), 8000)
        self.assertIn("The user never needs to remind you", kit["instructions"])
        self.assertIn("Never guess a file path", kit["instructions"])
        self.assertIn("use `paths:` for a filename/path fragment", kit["instructions"])
        self.assertIn("customer-service", kit["knowledge"])
        self.assertIn("Investigate a ticket", kit["suggested_prompts"])
        self.assertTrue(Path(kit["suggested_prompts_path"]).is_file())
        self.assertTrue(Path(kit["setup_path"]).is_file())

        (self.settings.knowledge_dir / "PROJECT_MAP.md").write_text(
            "你好" * (600 * 1024), encoding="utf-8",
        )
        bounded_kit = create_m365_agent_kit(self.settings)
        self.assertLessEqual(
            len(bounded_kit["knowledge"].encode("utf-8")),
            2 * 1024 * 1024,
        )
        self.assertIn("omitted unsafe or excess bytes", bounded_kit["knowledge"])

    def test_start_package_is_bounded_and_restart_failure_rolls_back_all_artifacts(self) -> None:
        with self.assertRaisesRegex(BrainError, "Ticket text exceeds"):
            start_session(self.settings, "TOO-LARGE", "你" * (core_module.MAX_START_TICKET_BYTES // 2))
        self.assertFalse((self.settings.runs_dir / "TOO-LARGE").exists())

        (self.settings.knowledge_dir / "PROJECT_MAP.md").write_text(
            "地图" * (core_module.MAX_START_KNOWLEDGE_ITEM_BYTES), encoding="utf-8",
        )
        content, _ = start_session(self.settings, "ROLLBACK", "original ticket")
        self.assertLessEqual(len(content.encode("utf-8")), core_module.MAX_START_ARTIFACT_BYTES)
        self.assertIn("omitted unsafe or excess bytes", content)
        directory = session_dir(self.settings, "ROLLBACK")
        before = {
            name: (directory / name).read_bytes()
            for name in ("ticket.md", "start.md", "session.json")
        }
        original_write = core_module._atomic_session_text_write

        def fail_start(settings, ticket, path, text):
            if path.name == "start.md":
                raise OSError("injected publication failure")
            return original_write(settings, ticket, path, text)

        with mock.patch("brain.core._atomic_session_text_write", side_effect=fail_start):
            with self.assertRaisesRegex(OSError, "injected publication failure"):
                start_session(self.settings, "ROLLBACK", "replacement ticket")
        self.assertEqual(
            before,
            {name: (directory / name).read_bytes() for name in before},
        )

    def test_implementation_feedback_packages_observed_results_without_running_them(self) -> None:
        start_session(self.settings, "ABC-2", "Review an implementation.")
        content, path, number = create_feedback(
            self.settings,
            "ABC-2",
            notes="Added the jurisdiction branch.",
            test_command="mvn test",
            test_output="Tests run: 4, Failures: 0",
        )
        self.assertEqual(1, number)
        self.assertTrue(path.is_file())
        self.assertIn("Added the jurisdiction branch", content)
        self.assertIn("Tests run: 4, Failures: 0", content)
        self.assertIn("No tracked staged or unstaged changes", content)
        self.assertEqual(1, json.loads((path.parent / "session.json").read_text())["feedbacks"])

    def test_working_tree_diff_stream_is_physically_bounded_with_omission_marker(self) -> None:
        repository = self.settings.repo("customer-service").path
        for command in (
            ["git", "init"],
            ["git", "config", "user.email", "brain@example.invalid"],
            ["git", "config", "user.name", "Project Brain Test"],
            ["git", "add", "."],
            ["git", "commit", "-m", "baseline"],
        ):
            subprocess.run(command, cwd=repository, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        tracked = repository / "src/main/java/demo/CustomerEvent.java"
        tracked.write_text("changed-line\n" * 200_000, encoding="utf-8")
        started = time.monotonic()
        diffs = core_module.working_tree_diffs(self.settings, ["customer-service"])
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(1, len(diffs))
        self.assertLessEqual(
            len(diffs[0].content.encode("utf-8")),
            core_module.MAX_WORKING_TREE_DIFF_TOTAL_BYTES,
        )
        self.assertIn(core_module.WORKING_TREE_DIFF_OMISSION, diffs[0].content)

    def test_external_ticket_evidence_is_local_explicit_and_reused(self) -> None:
        start_session(self.settings, "ABC-DOC", "Use an internal cache standard.")
        document = self.root / "cache-standard.md"
        document.write_text("Transaction cache TTL must be at least 45 seconds.\n", encoding="utf-8")
        content, artifact, number, stored = add_external_evidence(
            self.settings,
            "ABC-DOC",
            document,
            kind="document",
        )
        self.assertEqual(1, number)
        self.assertTrue(artifact.is_file())
        self.assertTrue(stored.is_file())
        self.assertIn("explicitly supplied by the user", content)
        handoff, _ = deliver(self.settings, "ABC-DOC", content, "m365", copy=False)
        self.assertEqual("ABC-DOC-evidence-001.md", handoff[0].name)

        context, _, _ = create_context(self.settings, "ABC-DOC", REQUEST)
        self.assertIn("Transaction cache TTL must be at least 45 seconds", context)
        self.assertIn("user-supplied external evidence", context)

        symlink = self.root / "cache-standard-link.md"
        try:
            symlink.symlink_to(document)
        except OSError as error:
            self.skipTest(f"symlink creation is unavailable: {error}")
        with self.assertRaisesRegex(BrainError, "must not be a symlink"):
            add_external_evidence(self.settings, "ABC-DOC", symlink)

    def test_external_context_rejects_substitution_and_has_global_item_byte_bounds(self) -> None:
        start_session(self.settings, "SEC-EVID", "Bound external evidence.")
        directory = session_dir(self.settings, "SEC-EVID")
        secret = self.root / "outside-evidence.md"
        secret.write_text("OUTSIDE SECRET", encoding="utf-8")
        try:
            (directory / "external-001.md").symlink_to(secret)
        except OSError as error:
            self.skipTest(f"symbolic links unavailable: {error}")
        (directory / "external-002.md").write_bytes(
            b"x" * (core_module.MAX_EXTERNAL_CONTEXT_ITEM_BYTES + 1),
        )
        for number in range(3, 34):
            (directory / f"external-{number:03d}.md").write_text(
                f"bounded-{number}", encoding="utf-8",
            )
        state = session_state(self.settings, "SEC-EVID")
        state["external_evidence"] = 40
        core_module.save_session(self.settings, "SEC-EVID", state)

        evidence = core_module._external_evidence(self.settings, "SEC-EVID")
        rendered = "\n".join(item.content for item in evidence)
        self.assertNotIn("OUTSIDE SECRET", rendered)
        self.assertNotIn("bounded-33", rendered)
        self.assertIn("failed a safety or size check", rendered)
        self.assertLessEqual(len(evidence), core_module.MAX_EXTERNAL_CONTEXT_ITEMS + 1)

    def test_external_evidence_growth_is_rejected_before_session_mutation(self) -> None:
        start_session(self.settings, "SEC-GROW", "Reject a growing external document.")
        document = self.root / "growing.md"
        document.write_text("small", encoding="utf-8")
        with mock.patch(
            "brain.core.read_direct_file_bytes",
            return_value=(b"x" * (core_module.MAX_EXTERNAL_EVIDENCE_SOURCE_BYTES + 1), True),
        ), self.assertRaisesRegex(BrainError, "limited to 20 MB"):
            add_external_evidence(self.settings, "SEC-GROW", document)
        state = session_state(self.settings, "SEC-GROW")
        self.assertEqual(0, int(state.get("external_evidence") or 0))
        self.assertFalse((session_dir(self.settings, "SEC-GROW") / "external-001.md").exists())

    def test_knowledge_search_rejects_symlinks_and_loads_once_per_retrieval(self) -> None:
        secret = self.root / "outside-knowledge.md"
        secret.write_text("KNOWLEDGE_OUTSIDE_SECRET", encoding="utf-8")
        link = self.settings.knowledge_dir / "leak.md"
        try:
            link.symlink_to(secret)
        except OSError as error:
            self.skipTest(f"symbolic links unavailable: {error}")
        oversized = self.settings.knowledge_dir / "oversized.md"
        oversized.write_bytes(b"x" * (core_module.MAX_KNOWLEDGE_ITEM_BYTES + 1))
        self.assertEqual([], core_module.knowledge_hits(self.settings, "KNOWLEDGE_OUTSIDE_SECRET"))
        self.assertEqual([], core_module.knowledge_hits(self.settings, "x" * 200))

        request = parse_context_request("""CONTEXT_REQUEST:
  objective: Find bounded knowledge.
  searches:
    - query: customer-service
      repos: []
    - query: Jurisdiction
      repos: []
  symbols: []
  files: []
  history: []
""")
        with mock.patch("brain.core._knowledge_corpus", wraps=core_module._knowledge_corpus) as loaded:
            retrieve_context(self.settings, request)
        self.assertEqual(1, loaded.call_count)

    def test_checkpoint_knowledge_recovery_rejects_replaced_oversized_file(self) -> None:
        path = self.settings.knowledge_dir / "lineage.md"
        original = "trusted lineage"
        path.write_text(original, encoding="utf-8")
        record = {
            "repo": "knowledge", "path": "lineage.md", "line_start": 1, "line_end": 1,
            "content_hash": hashlib.sha256(original.encode("utf-8")).hexdigest(),
        }
        restored, missed = core_module._restore_checkpoint_evidence(self.settings, [record])
        self.assertEqual(1, len(restored))
        self.assertEqual(0, missed)
        path.write_bytes(b"x" * (core_module.MAX_KNOWLEDGE_ITEM_BYTES + 1))
        restored, missed = core_module._restore_checkpoint_evidence(self.settings, [record])
        self.assertEqual([], restored)
        self.assertEqual(1, missed)

    def test_cli_rejects_oversized_file_and_stdin_before_session_creation(self) -> None:
        oversized = self.root / "oversized-ticket.md"
        oversized.write_bytes(b"x" * (cli_module.MAX_CLI_INPUT_BYTES + 1))
        with redirect_stderr(io.StringIO()):
            self.assertEqual(2, main([
                "-c", str(self.config), "start", "CLI-LARGE", "--no-sync",
                "--ticket-file", str(oversized), "--no-copy",
            ]))
        self.assertFalse((self.settings.runs_dir / "CLI-LARGE").exists())

        args = mock.MagicMock(file=None, clipboard=False)
        oversized_stdin = io.StringIO("你" * (cli_module.MAX_CLI_INPUT_BYTES // 2))
        oversized_stdin.isatty = lambda: False  # type: ignore[method-assign]
        with mock.patch("brain.cli.sys.stdin", oversized_stdin):
            with self.assertRaisesRegex(BrainError, "stdin exceeds"):
                cli_module._request_text(args)

        started = time.monotonic()
        with mock.patch(
            "brain.core._clipboard_command",
            return_value=[sys.executable, "-c", "import sys; sys.stdout.write('x' * 10000000)"],
        ):
            with self.assertRaisesRegex(BrainError, "Clipboard content exceeds"):
                core_module.clipboard_read()
        self.assertLess(time.monotonic() - started, 8)

    def test_chunks_round_trip(self) -> None:
        text = "line\n" * 100
        chunks = chunk_text(text, 73)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(text, "".join(chunks))
        self.assertTrue(all(len(chunk) <= 73 for chunk in chunks))

    def test_cli_search(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["-c", str(self.config), "search", "JURISDICTION_CHANGED", "--fixed"])
        self.assertEqual(0, code)
        self.assertIn("[customer-service]", output.getvalue())
        self.assertIn("[trading-service]", output.getvalue())

    def test_verified_path_search_and_request(self) -> None:
        hits = path_hits(self.settings, "application.properties", ["trading-service"])
        self.assertEqual(["src/main/resources/application.properties"], [item.path for item in hits])
        self.assertEqual(
            ["deploy/templates/configmap.tpl"],
            [item.path for item in path_hits(self.settings, "configmap.tpl", ["trading-service"])],
        )
        start_session(self.settings, "ABC-PATH", "Locate the customer topic configuration.")
        request = """CONTEXT_REQUEST:
  version: 1
  objective: Locate the exact customer topic configuration path.
  searches: []
  paths:
    - query: application.properties
      repos: [trading-service]
  symbols: []
  files: []
  history: []
"""
        context, _, _ = create_context(self.settings, "ABC-PATH", request)
        self.assertIn("src/main/resources/application.properties", context)
        self.assertIn("repository path index", context)
        self.assertIn("## Implementation readiness", context)

    def test_cli_preview_and_status_json(self) -> None:
        request_path = self.root / "request.yml"
        request_path.write_text(REQUEST, encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["-c", str(self.config), "preview", "--file", str(request_path), "--json"])
        self.assertEqual(0, code)
        plan = json.loads(output.getvalue())
        self.assertTrue(plan["valid"])
        self.assertGreater(plan["operation_count"], 1)

        v5 = self.root / "investigation-v5.yml"
        v5.write_text(
            "INVESTIGATION_REQUEST:\n  version: 5\n  mode: root_cause\n"
            "  objective: Trace the production flow.\n",
            encoding="utf-8",
        )
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(0, main(["-c", str(self.config), "preview", "--file", str(v5)]))
        self.assertIn("Valid INVESTIGATION_REQUEST v5", output.getvalue())

        v3 = self.root / "context-v3.yml"
        v3.write_text(
            "CONTEXT_REQUEST:\n  version: 3\n  objective: Trace the production flow.\n",
            encoding="utf-8",
        )
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(0, main(["-c", str(self.config), "preview", "--file", str(v3)]))
        self.assertIn("Valid CONTEXT_REQUEST v3", output.getvalue())

        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["-c", str(self.config), "status", "--json"])
        self.assertEqual(0, code)
        status = json.loads(output.getvalue())
        self.assertEqual("demo", status["project"]["name"])
        self.assertEqual(4, status["summary"]["repositories"])

    def test_generation_trace_candidate_expansion_and_semantic_chunk_contracts(self) -> None:
        self.settings.hydrate_limit = 1
        snapshot_indexes(self.settings)
        self.assertIsNotNone(current_generation(self.settings))
        start_session(self.settings, "ABC-EXPAND", "Inspect deferred evidence.")
        request = """CONTEXT_REQUEST:
  version: 2
  objective: Find classes without hydrating every candidate.
  searches:
    - query: class
      repos: []
  paths: []
  symbols: []
  files: []
  history: []
  expand: []
"""
        _, _, number = create_context(self.settings, "ABC-EXPAND", request)
        self.assertEqual(1, number)
        self.assertTrue((self.settings.runs_dir / "ABC-EXPAND/trace-001.json").is_file())
        state = json.loads((self.settings.runs_dir / "ABC-EXPAND/session.json").read_text(encoding="utf-8"))
        candidate = next(iter(state["candidate_manifest"]))
        expanded = request.replace("expand: []", f"expand: [{candidate}]")
        content, _, number = create_context(self.settings, "ABC-EXPAND", expanded)
        self.assertEqual(2, number)
        self.assertIn(state["candidate_manifest"][candidate]["path"], content)
        chunks = chunk_source("repo", "src/Example.py", "def hello():\n    return 'hello'\n")
        self.assertEqual(chunks, chunk_source("repo", "src/Example.py", "def hello():\n    return 'hello'\n"))

    def test_semantic_cards_exclude_machine_generated_dependency_locks(self) -> None:
        self.assertTrue(_excluded(Path("uv.lock"), b"version = 1\n"))
        self.assertTrue(_excluded(Path("package-lock.json"), b'{"lockfileVersion": 3}'))
        self.assertFalse(_excluded(Path("src/app.py"), b"def retrieve():\n    return True\n"))

    def test_semantic_cards_split_structural_regions_and_cap_pathological_lines(self) -> None:
        content = "\n".join(f"line_{index} = {index}" for index in range(200))
        chunks = chunk_source("repo", "src/generated.py", content)
        self.assertEqual(3, len(chunks))
        self.assertEqual((1, 80), (chunks[0].start_line, chunks[0].end_line))
        self.assertEqual((161, 200), (chunks[-1].start_line, chunks[-1].end_line))
        pathological = chunk_source("repo", "src/config.py", "x = '" + "a" * (SEMANTIC_CARD_CODE_CHARS + 1) + "'")
        self.assertIn("[semantic card code capped]", pathological[0].card)
        self.assertLessEqual(len(pathological[0].card), SEMANTIC_CARD_CODE_CHARS + 500)

    def test_semantic_embedding_batches_bound_card_count_and_total_input(self) -> None:
        chunks = [Chunk(str(index), "blob", "path", 1, 1, "file", "file", "x" * size) for index, size in enumerate((2_500, 2_500, 1_500, 1_500))]
        self.assertEqual([[0, 1, 2, 3]], list(_bounded_embedding_batches(chunks, [0, 1, 2, 3], 8)))
        self.assertEqual([[2, 3]], list(_bounded_embedding_batches(chunks, [2, 3], 2)))

    def test_local_test_model_pack_is_verified_before_semantic_edition(self) -> None:
        pack = self.root / "test-pack"
        pack.mkdir()
        suite = {"embedding": [{"id": "batch-normalized", "texts": ["verified code", "service test"], "dimension": 16, "normalized": True}]}
        suite_path = pack / "conformance.json"
        suite_path.write_text(json.dumps(suite), encoding="utf-8")
        manifest = {
            "pack_id": "test-embedding",
            "capability": "embedding",
            "model_family": "test",
            "upstream_model": "test-only",
            "upstream_revision": "1",
            "license": "MIT",
            "runtime_name": "deterministic-test",
            "runtime_revision": "1",
            "minimum_brain_version": "0.6.1",
            "embedding_dimension": 16,
            "test_only": True,
            "golden_suite": "conformance.json",
            "golden_suite_hash": hashlib.sha256(suite_path.read_bytes()).hexdigest(),
            "artifacts": {"conformance.json": hashlib.sha256(suite_path.read_bytes()).hexdigest()},
        }
        (pack / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        install_pack(self.settings, pack)
        with self.assertRaises(ValueError):
            set_edition(self.settings, "semantic")
        verified = verify_pack(self.settings, "test-embedding")
        self.assertTrue(verified["conformance"]["passed"])
        self.assertEqual("semantic", set_edition(self.settings, "semantic"))
        self.assertEqual("semantic", current_edition(self.settings))
        with mock.patch("brain.semantic._usearch", return_value=None):
            unavailable = capabilities(self.settings)
            self.assertTrue(unavailable["embedding_pack"])
            self.assertFalse(unavailable["embedding"])
            with self.assertRaisesRegex(ValueError, "USearch"):
                set_edition(self.settings, "semantic")
        report = benchmark_pack(self.settings, "test-embedding")
        self.assertTrue(report["batch_consistent"])
        self.assertEqual({"1", "8", "16"}, set(report["embedding_batches"]))
        self.assertTrue(report["conformance"]["passed"])
        self.assertTrue((self.settings.generated_dir / "MODEL_BAKEOFF_REPORT.md").is_file())
        tuning = autotune_pack(self.settings, "test-embedding", samples=1)
        self.assertIn(tuning["recommendations"]["embedding_batch_size"], {1, 8, 16})
        self.assertFalse(tuning["recommendations"]["embedding_resident"])
        self.assertTrue((self.settings.state_dir / "model-tuning.json").is_file())
        output = io.StringIO()
        diagnostics = io.StringIO()
        with redirect_stdout(output), redirect_stderr(diagnostics):
            self.assertEqual(0, main(["-c", str(self.config), "model", "autotune", "test-embedding", "--samples", "1"]))
        self.assertEqual("test-embedding", json.loads(output.getvalue())["pack_id"])
        self.assertIn("running embedding conformance case 1/1", diagnostics.getvalue())

    def test_embedding_batch_parity_allows_only_bounded_runtime_rounding_drift(self) -> None:
        self.assertEqual(7e-3, EMBEDDING_BATCH_PARITY_TOLERANCE)
        self.assertTrue(_same_vectors([[0.0]], [[6.1e-3]], tolerance=EMBEDDING_BATCH_PARITY_TOLERANCE))
        self.assertFalse(_same_vectors([[0.0]], [[7.1e-3]], tolerance=EMBEDDING_BATCH_PARITY_TOLERANCE))

    def test_production_reranker_requires_official_reference_and_candidate_pool_conformance(self) -> None:
        pack = self.root / "production-reranker-conformance-pack"
        pack.mkdir()
        binary, model, tokenizer = pack / "llama-server", pack / "model.gguf", pack / "tokenizer.json"
        binary.write_bytes(b"pinned local runtime")
        model.write_bytes(b"pinned local model")
        tokenizer.write_bytes(b"pinned tokenizer")
        runtime = DeterministicRuntime()
        query = "verified code"
        long_document = ("verified code implementation " * 300)

        def case(case_id: str, documents: list[str], *, truncate: int | None = None) -> dict[str, object]:
            scored_documents = [document[:truncate] for document in documents] if truncate else documents
            scores = runtime.rerank(query, scored_documents)
            payload: dict[str, object] = {
                "id": case_id, "query": query, "documents": documents,
                "expected_order": sorted(range(len(documents)), key=lambda index: (-scores[index], index)),
                "reference_scores": scores, "maximum_score_delta": 0.0,
                "batch_single_parity_indices": sorted({0, len(documents) // 2, len(documents) - 1}),
            }
            if truncate:
                payload["truncate_to_chars"] = truncate
            return payload

        pools = [case(f"candidate-pool-{size}", ["verified code implementation", *[f"unrelated {index}" for index in range(1, size)]]) for size in (10, 20, 40, 80)]
        suite_path = pack / "conformance.json"
        suite_path.write_text(json.dumps({
            "requirements": {
                "long_input_min_chars": 4096,
                "reranker_candidate_pools": [10, 20, 40, 80],
                "reranker_physical_batch_size": 10,
            },
            "reranker": [case("long-public-input", [long_document, "unrelated note"], truncate=4096)],
            "reranker_candidate_pools": pools,
        }), encoding="utf-8")
        digest = hashlib.sha256(suite_path.read_bytes()).hexdigest()
        (pack / "manifest.json").write_text(json.dumps({
            "pack_id": "production-reranker-conformance", "capability": "reranker", "model_family": "Qwen3",
            "upstream_model": "official-source", "upstream_revision": "pinned", "license": "Apache-2.0",
            "runtime_name": "llama.cpp", "runtime_revision": "pinned", "minimum_brain_version": "0.6.3",
            "runtime_compatibility": dict(zip(("os", "architecture"), platform_id().split("-", 1), strict=True)),
            "runtime_binary": "llama-server", "model_file": "model.gguf", "embedding_dimension": 0,
            "reranker_batch_size": 10, "reranker_candidate_pool": 20,
            "verification_request_timeout_seconds": 900,
            "weight_format": "GGUF", "quantization": "Q6_K", "weight_sha256": hashlib.sha256(model.read_bytes()).hexdigest(),
            "tokenizer_file": "tokenizer.json", "tokenizer_sha256": hashlib.sha256(tokenizer.read_bytes()).hexdigest(), "pooling": "rank", "normalization": "none",
            "query_instruction_version": "qwen3-reranker-v1", "document_card_version": "1", "chunk_schema_version": "1", "converter_revision": "llama.cpp@pinned",
            "golden_suite": "conformance.json", "golden_suite_hash": digest,
            "artifacts": {"llama-server": hashlib.sha256(binary.read_bytes()).hexdigest(), "model.gguf": hashlib.sha256(model.read_bytes()).hexdigest(), "tokenizer.json": hashlib.sha256(tokenizer.read_bytes()).hexdigest(), "conformance.json": digest},
        }), encoding="utf-8")
        install_pack(self.settings, pack)
        with mock.patch("brain.models.runtime_for_pack", return_value=runtime) as runtime_factory, mock.patch.object(
            runtime, "rerank", wraps=runtime.rerank,
        ) as rerank:
            verified = verify_pack(self.settings, "production-reranker-conformance")
        self.assertTrue(verified["conformance"]["passed"])
        self.assertEqual(2e-3, RERANKER_BATCH_PARITY_TOLERANCE)
        self.assertEqual(5, len(verified["conformance"]["cases"]))
        self.assertEqual(30, rerank.call_count)
        self.assertEqual(900, runtime_factory.call_args.args[0]["request_timeout_seconds"])
        self.assertTrue(runtime_factory.call_args.kwargs["verification"])

    def test_reranker_batch_single_parity_sampling_is_bounded_and_canonical(self) -> None:
        self.assertEqual(list(range(10)), _reranker_parity_indices(None, count=10, expected_top=0, case=1))
        self.assertEqual([0, 5, 9], _reranker_parity_indices([0, 5, 9], count=10, expected_top=0, case=1))
        self.assertEqual([0, 4, 5, 9], _reranker_parity_indices([0, 4, 5, 9], count=10, expected_top=4, case=1))
        with self.assertRaisesRegex(ValueError, "batch_single_parity_indices"):
            _reranker_parity_indices([0, 9], count=10, expected_top=0, case=1)

    def test_reranker_pack_defaults_bound_physical_batches_before_autotuning(self) -> None:
        runtime = DeterministicRuntime()
        manifest = {"reranker_batch_size": 10, "reranker_candidate_pool": 20}
        self.assertEqual((10, 20), _reranker_tuning(self.settings, "windows-reranker", manifest))
        documents = [f"document {index}" for index in range(25)]
        with mock.patch.object(runtime, "rerank", wraps=runtime.rerank) as rerank:
            self.assertEqual(25, len(_rerank_batched(runtime, "query", documents, 10)))
        self.assertEqual([10, 10, 5], [len(call.args[1]) for call in rerank.call_args_list])

    def test_remote_pack_install_requires_pinned_approved_source(self) -> None:
        with self.assertRaisesRegex(ValueError, "not approved"):
            install_pack_url(self.settings, "https://example.invalid/pack.tar", "a" * 64)
        with self.assertRaisesRegex(ValueError, "--sha256"):
            install_pack_url(self.settings, "https://github.com/example/project/releases/download/v1/pack.tar", "not-a-digest")
        profile = machine_profile(self.settings)
        self.assertNotIn("hostname", profile)
        self.assertIn("logical_cpu_count", profile)

    def test_model_downloads_abort_when_stream_exceeds_declared_or_descriptor_size(self) -> None:
        from brain import models as models_module

        class Response(io.BytesIO):
            def __init__(self, url: str, payload: bytes, length: int):
                super().__init__(payload)
                self._url = url
                self.headers = {"Content-Length": str(length)}

            def geturl(self) -> str:
                return self._url

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        pack_url = "https://github.com/example/project/releases/download/v1/pack.tar"
        with mock.patch(
            "brain.models._open_model_download",
            return_value=Response(pack_url, b"abcde", 4),
        ), self.assertRaisesRegex(ValueError, "exceeded its declared"):
            install_pack_url(self.settings, pack_url, hashlib.sha256(b"abcd").hexdigest())
        self.assertFalse(list(self.settings.state_dir.glob("brain-pack-download-*")))

        descriptor_url = "https://github.com/example/project/releases/download/v1/descriptor.json"
        oversized = b"x" * (models_module.MAX_MODEL_PACK_DESCRIPTOR_BYTES + 1)
        with mock.patch(
            "brain.models._open_model_download",
            return_value=Response(descriptor_url, oversized, 0),
        ), self.assertRaisesRegex(ValueError, "descriptor exceeds"):
            install_release_descriptor(self.settings, descriptor_url, "0" * 64)
        self.assertFalse(list(self.settings.state_dir.glob("brain-pack-release-*")))

    def test_model_archive_headers_and_manifest_reads_are_hard_bounded(self) -> None:
        from brain import models as models_module

        archive_path = self.root / "many-headers.tar"
        with tarfile.open(archive_path, "w") as archive:
            for index in range(4):
                entry = tarfile.TarInfo(f"directory-{index}")
                entry.type = tarfile.DIRTYPE
                archive.addfile(entry)
        with mock.patch("brain.models.MAX_MODEL_PACK_SOURCE_ITEMS", 3), self.assertRaisesRegex(
            ValueError, "item or time limit",
        ):
            models_module._archive_projected_size(archive_path)

        manifest = self.root / "oversized-manifest.json"
        manifest.write_bytes(b"{" + b"x" * models_module.MAX_MODEL_PACK_MANIFEST_BYTES + b"}")
        with self.assertRaisesRegex(ValueError, "manifest exceeds its byte limit"):
            models_module._load_manifest(manifest)

        directory_pack = self.root / "oversized-directory-pack"
        directory_pack.mkdir()
        (directory_pack / "manifest.json").write_text("{}", encoding="utf-8")
        (directory_pack / "weights.gguf").write_bytes(b"too-large")
        with mock.patch("brain.models.MAX_MODEL_PACK_UNPACKED_BYTES", 1), mock.patch(
            "brain.models.shutil.copytree",
        ) as copied, self.assertRaisesRegex(ValueError, "unpacked byte limit"):
            models_module.install_pack(self.settings, directory_pack)
        copied.assert_not_called()

    def test_model_archive_single_member_copy_obeys_shared_deadline_and_cleans_up(self) -> None:
        from brain import models as models_module

        archive_path = self.root / "slow-member-pack.tar"
        payload = b"x" * (2 * 1024 * 1024)
        with tarfile.open(archive_path, "w") as archive:
            entry = tarfile.TarInfo("weights.bin")
            entry.size = len(payload)
            archive.addfile(entry, io.BytesIO(payload))
        clock_calls = 0

        def clock() -> float:
            nonlocal clock_calls
            clock_calls += 1
            return 301.0 if clock_calls >= 5 else 0.0

        with mock.patch("brain.models.time.monotonic", side_effect=clock), self.assertRaisesRegex(
            ValueError, "item or time limit",
        ):
            install_pack(self.settings, archive_path)
        self.assertFalse(list(self.settings.state_dir.glob("brain-pack-*")))
        self.assertFalse((self.settings.state_dir / "models" / "slow-member-pack").exists())

    def test_model_pack_source_mutation_during_copy_never_publishes_or_leaves_staging(self) -> None:
        from brain import models as models_module

        pack = self.root / "mutable-model-pack"
        pack.mkdir()
        (pack / "manifest.json").write_text(json.dumps({
            "pack_id": "mutable-test-pack", "capability": "test", "model_family": "test",
            "upstream_model": "local", "upstream_revision": "1", "license": "MIT",
            "runtime_name": "deterministic-test", "runtime_revision": "1",
            "minimum_brain_version": "1.0.0", "test_only": True,
        }), encoding="utf-8")
        (pack / "weights.bin").write_bytes(b"sealed-before-copy")
        destination = self.settings.state_dir / "models" / "mutable-test-pack"
        staging = destination.with_name(destination.name + ".installing")
        original_copy = models_module._copy_bounded_pack_file
        mutated = False

        def mutate_during_copy(*args: object, **kwargs: object) -> int:
            nonlocal mutated
            if not mutated:
                mutated = True
                (pack / "late-extra.bin").write_bytes(b"must not be copied or published")
            return original_copy(*args, **kwargs)

        with mock.patch(
            "brain.models._copy_bounded_pack_file", side_effect=mutate_during_copy,
        ), self.assertRaisesRegex(ValueError, "source changed"):
            install_pack(self.settings, pack)
        self.assertFalse(destination.exists())
        self.assertFalse(staging.exists())

    def test_exact_verified_pack_lookup_never_scans_unrelated_pack_directories(self) -> None:
        from brain import models as models_module

        directory = models_module._pack_directory(self.settings, "exact-test-pack")
        directory.mkdir(parents=True)
        manifest = {
            "pack_id": "exact-test-pack", "capability": "test", "model_family": "test",
            "upstream_model": "local", "upstream_revision": "1", "license": "MIT",
            "runtime_name": "deterministic-test", "runtime_revision": "1",
            "minimum_brain_version": "1.0.0", "test_only": True, "verified": True,
            "installed_path": str(directory),
        }
        (directory / "installed.json").write_text(json.dumps(manifest), encoding="utf-8")
        root = models_module.model_root(self.settings)
        for number in range(20):
            unrelated = root / f"unrelated-{number:03d}"
            unrelated.mkdir()
            (unrelated / "installed.json").write_text("not json", encoding="utf-8")
        with mock.patch(
            "brain.models.installed_packs",
            side_effect=AssertionError("exact pinned lookup scanned the model root"),
        ):
            resolved = models_module.verified_pack(self.settings, "exact-test-pack", "test")
        self.assertIsNotNone(resolved)
        self.assertEqual("exact-test-pack", resolved["pack_id"])

        with mock.patch("brain.models.MAX_INSTALLED_PACK_DIRECTORIES", 2):
            listed = models_module.installed_packs(self.settings)
        self.assertTrue(any(item.get("listing_truncated") for item in listed))

    def test_model_pack_roots_and_sources_never_follow_symbolic_links(self) -> None:
        from brain.models import installed_packs, remove_pack, verify_pack

        outside = self.root / "outside-models"
        outside.mkdir()
        marker = outside / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        models_root = self.settings.state_dir / "models"
        try:
            models_root.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"symbolic links unavailable: {error}")
        with self.assertRaisesRegex(ValueError, "model pack root escapes"):
            installed_packs(self.settings)
        with self.assertRaisesRegex(ValueError, "model pack root escapes"):
            remove_pack(self.settings, "anything")
        self.assertEqual("keep", marker.read_text(encoding="utf-8"))
        models_root.unlink()

        models_root.mkdir()
        target_pack = self.settings.repo("customer-service").path
        (target_pack / "installed.json").write_text(json.dumps({
            "pack_id": "evil", "capability": "test", "model_family": "test",
            "upstream_model": "test", "upstream_revision": "1", "license": "MIT",
            "runtime_name": "deterministic-test", "runtime_revision": "1",
            "minimum_brain_version": "1.0.0", "embedding_dimension": 4,
            "test_only": True, "installed_path": str(target_pack),
        }), encoding="utf-8")
        before = {
            str(path.relative_to(target_pack)): path.read_bytes()
            for path in target_pack.rglob("*") if path.is_file()
        }
        try:
            (models_root / "evil").symlink_to(target_pack, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"symbolic links unavailable: {error}")
        with mock.patch("brain.models._run_model_conformance") as conformance, self.assertRaisesRegex(
            ValueError, "must not be a symbolic link",
        ):
            verify_pack(self.settings, "evil")
        conformance.assert_not_called()
        self.assertEqual(before, {
            str(path.relative_to(target_pack)): path.read_bytes()
            for path in target_pack.rglob("*") if path.is_file()
        })

        source = self.root / "symlink-pack"
        source.mkdir()
        try:
            (source / "weights.gguf").symlink_to(marker)
        except OSError as error:
            self.skipTest(f"symbolic links unavailable: {error}")
        with self.assertRaisesRegex(ValueError, "must not contain symbolic links"):
            install_pack(self.settings, source)

    def test_gc_fails_closed_for_symlinked_component_roots(self) -> None:
        state = self.root / "gc-state"
        outside = self.root / "gc-outside"
        state.mkdir()
        outside.mkdir()
        for name in ("generations", "snapshots", "semantic-shards", "zoekt"):
            target = outside / name
            target.mkdir()
            (target / "keep.txt").write_text("keep", encoding="utf-8")
            try:
                (state / name).symlink_to(target, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"symbolic links unavailable: {error}")
        isolated = replace(self.settings, state_dir=state)
        report = gc(isolated, dry_run=False, keep_recent=1)
        self.assertTrue(report["reachability_gc_blocked"])
        self.assertEqual([], report["remove"])
        self.assertTrue(all((outside / name / "keep.txt").is_file() for name in ("generations", "snapshots", "semantic-shards", "zoekt")))

        nested_state = self.root / "gc-nested-state"
        nested_state.mkdir()
        for name in ("generations", "snapshots", "semantic-shards", "zoekt"):
            (nested_state / name).mkdir()
        nested_outside = self.root / "gc-nested-outside"
        nested_outside.mkdir()
        (nested_outside / "keep.txt").write_text("keep", encoding="utf-8")
        (nested_state / "snapshots" / "repo").symlink_to(nested_outside, target_is_directory=True)
        nested_report = gc(replace(self.settings, state_dir=nested_state), dry_run=False, keep_recent=1)
        self.assertTrue(nested_report["reachability_gc_blocked"])
        self.assertTrue((nested_outside / "keep.txt").is_file())

    def test_model_download_uses_system_trust_with_hostname_verification(self) -> None:
        context = ssl.create_default_context()
        with mock.patch.dict(os.environ, {"SSL_CERT_FILE": ""}), mock.patch("brain.models.truststore") as truststore_module:
            truststore_module.SSLContext.return_value = context
            actual, source = model_download_ssl_context(self.settings)
        self.assertIs(context, actual)
        self.assertEqual("system trust", source)
        truststore_module.SSLContext.assert_called_once_with(ssl.PROTOCOL_TLS_CLIENT)
        self.assertTrue(actual.check_hostname)
        self.assertEqual(ssl.CERT_REQUIRED, actual.verify_mode)

    def test_model_download_adds_configured_ca_bundle_without_disclosing_its_path(self) -> None:
        bundle = self.root / "enterprise-root.pem"
        bundle.write_text("public test fixture", encoding="utf-8")

        class Context:
            check_hostname = False
            verify_mode = ssl.CERT_NONE

            def __init__(self) -> None:
                self.loaded: list[str] = []

            def load_verify_locations(self, *, cafile: str) -> None:
                self.loaded.append(cafile)

        context = Context()
        self.settings.model_ca_bundle = bundle
        with mock.patch("brain.models.truststore") as truststore_module:
            truststore_module.SSLContext.return_value = context
            _, source = model_download_ssl_context(self.settings)
        self.assertEqual([str(bundle)], context.loaded)
        self.assertIn("configured CA bundle", source)
        self.assertTrue(context.check_hostname)
        self.assertEqual(ssl.CERT_REQUIRED, context.verify_mode)
        environment_context = Context()
        self.settings.model_ca_bundle = None
        with mock.patch.dict(os.environ, {"SSL_CERT_FILE": str(bundle)}), mock.patch("brain.models.truststore") as truststore_module:
            truststore_module.SSLContext.return_value = environment_context
            _, source = model_download_ssl_context(self.settings)
        self.assertEqual([str(bundle)], environment_context.loaded)
        self.assertIn("SSL_CERT_FILE", source)
        with mock.patch("brain.models.model_download_ssl_context", return_value=(context, "system trust + configured CA bundle")):
            report, ok = doctor(self.settings)
        self.assertTrue(ok)
        self.assertIn("Model-download TLS", report)
        self.assertIn("configured CA bundle", report)
        self.assertNotIn(str(bundle), report)

    def test_model_download_rejects_untrusted_certificate_without_insecure_retry(self) -> None:
        from urllib.request import Request

        request = Request("https://github.com/example/project/releases/download/v1/descriptor.json")
        with mock.patch("brain.models.model_download_ssl_context", return_value=(ssl.create_default_context(), "system trust")), mock.patch(
            "brain.models.urlopen", side_effect=URLError(ssl.SSLCertVerificationError(1, "untrusted"))
        ) as opener:
            with self.assertRaisesRegex(ValueError, "certificate verification failed"):
                _open_model_download(self.settings, request, timeout=1)
        self.assertEqual(1, opener.call_count)
        self.assertTrue(opener.call_args.kwargs["context"].check_hostname)
        self.assertEqual(ssl.CERT_REQUIRED, opener.call_args.kwargs["context"].verify_mode)

    def test_remote_model_download_keeps_standard_proxy_eligible_transport(self) -> None:
        from urllib.request import Request

        request = Request("https://github.com/example/project/releases/download/v1/descriptor.json")
        proxy_url = "http://enterprise-proxy.invalid:8080"
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": proxy_url}, clear=False), mock.patch(
            "brain.models.model_download_ssl_context", return_value=(ssl.create_default_context(), "system trust")
        ), mock.patch("brain.models.urlopen", return_value=mock.Mock()) as remote, mock.patch(
            "brain.models._MANAGED_LOOPBACK_OPENER.open"
        ) as direct:
            _open_model_download(self.settings, request, timeout=1)
        remote.assert_called_once()
        direct.assert_not_called()

    def test_pack_owned_loopback_transport_bypasses_proxy_for_semantic_and_precision_calls(self) -> None:
        runtime_calls: list[tuple[str, str | None]] = []
        proxy_calls: list[str] = []

        class RuntimeHandler(http.server.BaseHTTPRequestHandler):
            def _send(self, value: dict[str, object]) -> None:
                encoded = json.dumps(value).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self) -> None:
                runtime_calls.append((self.path, self.headers.get("Authorization")))
                self._send({"ok": True})

            def do_POST(self) -> None:
                runtime_calls.append((self.path, self.headers.get("Authorization")))
                _ = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path == "/v1/embeddings":
                    self._send({"data": [{"embedding": [1.0, 0.0]}]})
                elif self.path == "/rerank":
                    self._send({"results": [{"index": 0, "relevance_score": 0.9}, {"index": 1, "relevance_score": 0.1}]})
                else:
                    self.send_error(404)

            def log_message(self, _format: str, *_args: object) -> None:
                return None

        class ProxyHandler(http.server.BaseHTTPRequestHandler):
            def do_CONNECT(self) -> None:
                proxy_calls.append("CONNECT")
                self.send_error(502)

            def do_GET(self) -> None:
                proxy_calls.append("GET")
                self.send_error(502)

            def do_POST(self) -> None:
                proxy_calls.append("POST")
                self.send_error(502)

            def log_message(self, _format: str, *_args: object) -> None:
                return None

        runtime_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RuntimeHandler)
        proxy_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
        threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (runtime_server, proxy_server)]
        for thread in threads:
            thread.start()
        proxy_url = f"http://127.0.0.1:{proxy_server.server_port}"
        endpoint = f"http://127.0.0.1:{runtime_server.server_port}"
        try:
            with mock.patch.dict(os.environ, {
                "HTTP_PROXY": proxy_url, "HTTPS_PROXY": proxy_url, "NO_PROXY": "", "no_proxy": "",
            }, clear=False), mock.patch("urllib.request.proxy_bypass", side_effect=lambda host: host == "localhost"):
                semantic = LlamaCppRuntime(endpoint, api_key="ephemeral-test-key", direct_loopback=True)
                precision = LlamaCppRuntime(endpoint, api_key="ephemeral-test-key", direct_loopback=True)
                self.assertTrue(semantic.health()["ok"])
                self.assertEqual([[1.0, 0.0]], semantic.embed(["public synthetic semantic card"], dimension=2))
                self.assertEqual([0.9, 0.1], precision.rerank("query", ["relevant", "unrelated"]))
        finally:
            for server in (runtime_server, proxy_server):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=1)
        self.assertEqual([], proxy_calls)
        self.assertEqual(
            [("/health", None), ("/v1/embeddings", "Bearer ephemeral-test-key"), ("/rerank", "Bearer ephemeral-test-key")],
            runtime_calls,
        )

    def test_direct_loopback_transport_is_limited_to_managed_numeric_loopback(self) -> None:
        with self.assertRaisesRegex(ValueError, "pack-owned 127.0.0.1"):
            LlamaCppRuntime("http://localhost:8080", direct_loopback=True)
        with self.assertRaisesRegex(ValueError, "pack-owned 127.0.0.1"):
            LlamaCppRuntime("http://[::1]:8080", direct_loopback=True)

    def test_doctor_reports_safe_pack_runtime_proxy_boundary(self) -> None:
        proxy_url = "http://enterprise-proxy.invalid:8080"
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": proxy_url}, clear=False):
            status = managed_runtime_loopback_status()
            report, ok = doctor(self.settings)
        self.assertTrue(ok)
        self.assertIn("direct no-proxy transport enforced", status)
        self.assertIn("external proxy configuration detected", status)
        self.assertNotIn(proxy_url, status)
        self.assertIn("Pack-owned model runtime", report)
        self.assertIn("direct no-proxy transport enforced", report)
        self.assertNotIn(proxy_url, report)

    def test_local_model_install_never_opens_a_download_connection(self) -> None:
        pack = self.root / "local-only-pack"
        pack.mkdir()
        (pack / "manifest.json").write_text(json.dumps({
            "pack_id": "local-only", "capability": "test", "model_family": "test",
            "upstream_model": "public-fixture", "upstream_revision": "1", "license": "MIT",
            "runtime_name": "deterministic-test", "runtime_revision": "1", "minimum_brain_version": "0.6.1",
            "test_only": True,
        }), encoding="utf-8")
        with mock.patch("brain.models._open_model_download", side_effect=AssertionError("network was used")):
            installed = install_pack(self.settings, pack)
        self.assertEqual("local-only", installed["pack_id"])

    def test_release_descriptor_install_and_verify_assembles_a_pinned_pack(self) -> None:
        model = b"official-weight-part-one-and-two"
        suite = json.dumps({
            "embedding": [{
                "id": "public-synthetic-batch",
                "texts": ["eligibility event handler", "customer eligibility event"],
                "dimension": 4,
                "expected_similarity_order": [1],
            }],
        }, separators=(",", ":")).encode("utf-8")
        manifest = {
            "pack_id": "synthetic-semantic", "capability": "test", "model_family": "test",
            "upstream_model": "public-fixture", "upstream_revision": "1", "license": "Apache-2.0",
            "runtime_name": "deterministic-test", "runtime_revision": "1", "minimum_brain_version": "0.6.1",
            "test_only": True, "embedding_dimension": 4, "model_file": "model.gguf",
            "runtime_compatibility": dict(zip(("os", "architecture"), platform_id().split("-", 1), strict=True)),
            "weight_sha256": hashlib.sha256(model).hexdigest(), "golden_suite": "conformance.json",
            "golden_suite_hash": hashlib.sha256(suite).hexdigest(),
            "artifacts": {"model.gguf": hashlib.sha256(model).hexdigest(), "conformance.json": hashlib.sha256(suite).hexdigest()},
        }
        archive_stream = io.BytesIO()
        runtime = b"#!/bin/sh\nexit 0\n"
        with tarfile.open(fileobj=archive_stream, mode="w:gz") as archive:
            for name, content, mode in (
                ("manifest.json", json.dumps(manifest).encode("utf-8"), 0o644),
                ("conformance.json", suite, 0o644),
                ("llama-server", runtime, 0o755),
            ):
                entry = tarfile.TarInfo(name)
                entry.size = len(content)
                entry.mode = mode
                archive.addfile(entry, io.BytesIO(content))
        metadata = archive_stream.getvalue()
        parts = [model[:11], model[11:]]
        descriptor = {
            "schema": "project-brain-model-pack-v1", "pack_id": "synthetic-semantic",
            "platform": platform_id(),
            "metadata": {"url": "https://github.com/example/project/releases/download/v1/metadata.tar.gz", "sha256": hashlib.sha256(metadata).hexdigest(), "size": len(metadata)},
            "model": {
                "file": "model.gguf", "sha256": hashlib.sha256(model).hexdigest(),
                "parts": [
                    {"url": f"https://github.com/example/project/releases/download/v1/model.part{index}", "sha256": hashlib.sha256(part).hexdigest(), "size": len(part)}
                    for index, part in enumerate(parts)
                ],
            },
        }
        descriptor_bytes = json.dumps(descriptor, separators=(",", ":")).encode("utf-8")
        payloads = {
            "https://github.com/example/project/releases/download/v1/descriptor.json": descriptor_bytes,
            descriptor["metadata"]["url"]: metadata,
            **{part["url"]: content for part, content in zip(descriptor["model"]["parts"], parts, strict=True)},
        }
        redirects = {
            descriptor["metadata"]["url"]: "https://release-assets.githubusercontent.com/project-brain/metadata.tar.gz",
            **{part["url"]: f"https://objects.githubusercontent.com/project-brain/{index}" for index, part in enumerate(descriptor["model"]["parts"])},
        }

        class Response(io.BytesIO):
            def __init__(self, url: str, content: bytes):
                super().__init__(content)
                self.url = url
                self.headers = {"Content-Length": str(len(content))}

            def geturl(self) -> str:
                return redirects.get(self.url, self.url)

            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.close()

        def download(request, timeout=0, context=None):
            self.assertIsNotNone(context)
            self.assertTrue(context.check_hostname)
            return Response(request.full_url, payloads[request.full_url])

        descriptor_url = "https://github.com/example/project/releases/download/v1/descriptor.json"
        capacity_checks: list[int] = []
        with mock.patch("brain.models.urlopen", side_effect=download), mock.patch(
            "brain.ops.ensure_write_capacity",
            side_effect=lambda _settings, projected=0: capacity_checks.append(int(projected)),
        ):
            installed = install_release_descriptor(self.settings, descriptor_url, hashlib.sha256(descriptor_bytes).hexdigest())
        self.assertIn(len(model), capacity_checks)
        self.assertEqual("synthetic-semantic", installed["pack_id"])
        self.assertEqual(model, (self.settings.state_dir / "models" / "synthetic-semantic" / "model.gguf").read_bytes())
        self.assertTrue(os.access(self.settings.state_dir / "models" / "synthetic-semantic" / "llama-server", os.X_OK))
        self.assertTrue(verify_pack(self.settings, "synthetic-semantic")["verified"])
        with mock.patch("brain.models.urlopen", side_effect=download), self.assertRaisesRegex(ValueError, "descriptor SHA-256"):
            install_release_descriptor(self.settings, descriptor_url, "0" * 64)
        bad_descriptor = json.loads(descriptor_bytes)
        bad_descriptor["metadata"]["sha256"] = "0" * 64
        bad_descriptor_bytes = json.dumps(bad_descriptor, separators=(",", ":")).encode("utf-8")
        payloads[descriptor_url] = bad_descriptor_bytes
        with mock.patch("brain.models.urlopen", side_effect=download), self.assertRaisesRegex(ValueError, "release artifact SHA-256"):
            install_release_descriptor(self.settings, descriptor_url, hashlib.sha256(bad_descriptor_bytes).hexdigest())

    def test_cli_official_semantic_alias_uses_the_controlled_catalog(self) -> None:
        output = io.StringIO()
        catalog = {"semantic": {"darwin-arm64": {"pack_id": "qwen3-embedding-4b-q6k-darwin-arm64", "descriptor_url": "https://github.com/example/project/releases/download/v1/descriptor.json", "descriptor_sha256": "a" * 64}}}
        with mock.patch("brain.models.OFFICIAL_PACKS", catalog), mock.patch("brain.models.install_official_pack", return_value={"pack_id": "qwen3-embedding-4b-q6k-darwin-arm64"}) as install:
            with redirect_stdout(output):
                self.assertEqual(0, main(["-c", str(self.config), "model", "install", "semantic"]))
        install.assert_called_once_with(self.settings, "semantic")
        self.assertEqual("qwen3-embedding-4b-q6k-darwin-arm64", json.loads(output.getvalue())["pack_id"])

    def test_model_actions_resolve_official_aliases_for_the_current_platform(self) -> None:
        catalog = {
            "semantic": {"windows-amd64": {"pack_id": "embedding-windows-amd64"}},
            "precision": {"windows-amd64": {"pack_id": "reranker-windows-amd64"}},
        }
        with (
            mock.patch("brain.models.OFFICIAL_PACKS", catalog),
            mock.patch("brain.platforms.platform_id", return_value="windows-amd64"),
            mock.patch("brain.models.verify_pack", return_value={"verified": True}) as verify,
            mock.patch("brain.models.benchmark_pack", return_value={"ok": True}) as benchmark,
            mock.patch("brain.models.autotune_pack", return_value={"ok": True}) as autotune,
            mock.patch("brain.models.remove_pack") as remove,
        ):
            model_operation(self.settings, "verify", "semantic")
            model_operation(self.settings, "benchmark", "precision", samples=2)
            model_operation(self.settings, "autotune", "semantic", samples=2, latency_budget_ms=500)
            removed = model_operation(self.settings, "remove", "precision")
            status = model_status(self.settings)
        verify.assert_called_once_with(self.settings, "embedding-windows-amd64")
        benchmark.assert_called_once_with(self.settings, "reranker-windows-amd64", samples=2)
        autotune.assert_called_once_with(self.settings, "embedding-windows-amd64", samples=2, latency_budget_ms=500)
        remove.assert_called_once_with(self.settings, "reranker-windows-amd64")
        self.assertEqual("reranker-windows-amd64", removed["pack_id"])
        self.assertEqual(
            [{"alias": "precision", "pack_id": "reranker-windows-amd64", "capability": None},
             {"alias": "semantic", "pack_id": "embedding-windows-amd64", "capability": None}],
            status["official"],
        )

    def test_cli_model_install_error_does_not_hardcode_the_semantic_alias(self) -> None:
        output = io.StringIO()
        with redirect_stderr(output):
            self.assertEqual(2, main(["-c", str(self.config), "model", "install"]))
        self.assertIn("official pack alias", output.getvalue())

    def test_model_conformance_rejects_bad_reranker_golden(self) -> None:
        pack = self.root / "bad-reranker-pack"
        pack.mkdir()
        suite_path = pack / "conformance.json"
        suite_path.write_text(json.dumps({"reranker": [{"query": "verified", "documents": ["verified source", "unrelated note"], "expected_order": [1, 0]}]}), encoding="utf-8")
        digest = hashlib.sha256(suite_path.read_bytes()).hexdigest()
        (pack / "manifest.json").write_text(json.dumps({
            "pack_id": "bad-reranker", "capability": "reranker", "model_family": "test",
            "upstream_model": "test-only", "upstream_revision": "1", "license": "MIT",
            "runtime_name": "deterministic-test", "runtime_revision": "1", "minimum_brain_version": "0.6.1",
            "test_only": True, "golden_suite": "conformance.json", "golden_suite_hash": digest,
            "artifacts": {"conformance.json": digest},
        }), encoding="utf-8")
        install_pack(self.settings, pack)
        with self.assertRaisesRegex(ValueError, "reranker conformance failed"):
            verify_pack(self.settings, "bad-reranker")

    def test_reranker_benchmark_and_autotune_use_public_candidate_pools(self) -> None:
        pack = self.root / "reranker-pack"
        pack.mkdir()
        suite_path = pack / "conformance.json"
        suite_path.write_text(json.dumps({
            "reranker": [{
                "query": "verified code", "documents": ["verified code evidence", "unrelated deployment note"],
                "expected_order": [0, 1],
            }],
        }), encoding="utf-8")
        digest = hashlib.sha256(suite_path.read_bytes()).hexdigest()
        (pack / "manifest.json").write_text(json.dumps({
            "pack_id": "test-reranker", "capability": "reranker", "model_family": "test",
            "upstream_model": "test-only", "upstream_revision": "1", "license": "MIT",
            "runtime_name": "deterministic-test", "runtime_revision": "1", "minimum_brain_version": "0.6.1",
            "test_only": True, "golden_suite": "conformance.json", "golden_suite_hash": digest,
            "artifacts": {"conformance.json": digest},
        }), encoding="utf-8")
        install_pack(self.settings, pack)
        report = benchmark_pack(self.settings, "test-reranker", samples=1)
        self.assertEqual({"10", "20", "40", "80"}, set(report["reranker_candidate_pools"]))
        tuning = autotune_pack(self.settings, "test-reranker", samples=1, latency_budget_ms=3_000)
        self.assertIn(tuning["recommendations"]["reranker_candidate_pool"], {10, 20, 40, 80})
        self.assertTrue((self.settings.generated_dir / "RERANKER_BAKEOFF_REPORT.md").is_file())

    def test_production_pack_requires_reference_and_long_input_conformance(self) -> None:
        pack = self.root / "production-conformance-pack"
        pack.mkdir()
        binary = pack / "llama-server"
        binary.write_bytes(b"pinned local runtime")
        model = pack / "model.gguf"
        model.write_bytes(b"pinned local model")
        tokenizer = pack / "tokenizer.json"
        tokenizer.write_bytes(b"pinned tokenizer")
        runtime = DeterministicRuntime(16)
        texts = ["verified code", "verified code implementation", "unrelated " * 100]
        runtime_texts = [text[:500] for text in texts]
        vectors = runtime.embed(runtime_texts, dimension=16)
        order = sorted(range(1, len(texts)), key=lambda index: -sum(left * right for left, right in zip(vectors[0], vectors[index], strict=True)))
        suite_path = pack / "conformance.json"
        suite_path.write_text(json.dumps({
            "requirements": {"long_input_min_chars": 500},
            "embedding": [{
                "texts": texts, "dimension": 16, "normalized": True,
                "truncate_to_chars": 500,
                "reference_vectors": vectors, "minimum_cosine_to_reference": 0.999,
                "expected_similarity_order": order,
            }],
        }), encoding="utf-8")
        digest = hashlib.sha256(suite_path.read_bytes()).hexdigest()
        (pack / "manifest.json").write_text(json.dumps({
            "pack_id": "production-conformance", "capability": "embedding", "model_family": "approved",
            "upstream_model": "approved-local", "upstream_revision": "1", "license": "Apache-2.0",
            "runtime_name": "llama.cpp", "runtime_revision": "1", "minimum_brain_version": "0.6.1",
            "runtime_compatibility": dict(zip(("os", "architecture"), platform_id().split("-", 1), strict=True)),
            "runtime_binary": "llama-server", "model_file": "model.gguf", "embedding_dimension": 16,
            "chunk_schema_version": CHUNK_SCHEMA_VERSION, "document_card_version": CARD_VERSION,
            "weight_format": "GGUF", "quantization": "Q8_0", "weight_sha256": hashlib.sha256(model.read_bytes()).hexdigest(),
            "tokenizer_file": "tokenizer.json", "tokenizer_sha256": hashlib.sha256(tokenizer.read_bytes()).hexdigest(), "pooling": "mean", "normalization": "l2",
            "query_instruction_version": "v1", "converter_revision": "llama.cpp@1",
            "golden_suite": "conformance.json", "golden_suite_hash": digest,
            "artifacts": {"llama-server": hashlib.sha256(binary.read_bytes()).hexdigest(), "model.gguf": hashlib.sha256(model.read_bytes()).hexdigest(), "tokenizer.json": hashlib.sha256(tokenizer.read_bytes()).hexdigest(), "conformance.json": digest},
        }), encoding="utf-8")
        install_pack(self.settings, pack)
        with mock.patch("brain.models.runtime_for_pack", return_value=runtime) as runtime_factory:
            verified = verify_pack(self.settings, "production-conformance")
        self.assertTrue(verified["conformance"]["passed"])
        self.assertEqual(900, runtime_factory.call_args.args[0]["request_timeout_seconds"])
        self.assertTrue(runtime_factory.call_args.kwargs["verification"])
        installed = self.settings.state_dir / "models" / "production-conformance" / "installed.json"
        incompatible = json.loads(installed.read_text(encoding="utf-8"))
        incompatible["chunk_schema_version"] = "obsolete"
        installed.write_text(json.dumps(incompatible), encoding="utf-8")
        available = capabilities(self.settings)
        self.assertFalse(available["embedding"])
        self.assertIn("brain index rebuild --backend semantic", available["installed_packs"][0]["compatibility_error"])
        with self.assertRaisesRegex(ValueError, "brain index rebuild --backend semantic"):
            set_edition(self.settings, "semantic")

    def test_production_pack_rejects_incomplete_conformance_suite(self) -> None:
        pack = self.root / "incomplete-production-conformance-pack"
        pack.mkdir()
        binary = pack / "llama-server"
        binary.write_bytes(b"pinned local runtime")
        model = pack / "model.gguf"
        model.write_bytes(b"pinned local model")
        tokenizer = pack / "tokenizer.json"
        tokenizer.write_bytes(b"pinned tokenizer")
        suite_path = pack / "conformance.json"
        suite_path.write_text(json.dumps({
            "requirements": {"long_input_min_chars": 1},
            "embedding": [{"texts": ["verified", "unrelated"], "dimension": 16}],
        }), encoding="utf-8")
        digest = hashlib.sha256(suite_path.read_bytes()).hexdigest()
        (pack / "manifest.json").write_text(json.dumps({
            "pack_id": "incomplete-production-conformance", "capability": "embedding", "model_family": "approved",
            "upstream_model": "approved-local", "upstream_revision": "1", "license": "Apache-2.0",
            "runtime_name": "llama.cpp", "runtime_revision": "1", "minimum_brain_version": "0.6.1",
            "runtime_compatibility": dict(zip(("os", "architecture"), platform_id().split("-", 1), strict=True)),
            "runtime_binary": "llama-server", "model_file": "model.gguf", "embedding_dimension": 16,
            "chunk_schema_version": CHUNK_SCHEMA_VERSION, "document_card_version": CARD_VERSION,
            "weight_format": "GGUF", "quantization": "Q8_0", "weight_sha256": hashlib.sha256(model.read_bytes()).hexdigest(),
            "tokenizer_file": "tokenizer.json", "tokenizer_sha256": hashlib.sha256(tokenizer.read_bytes()).hexdigest(), "pooling": "mean", "normalization": "l2",
            "query_instruction_version": "v1", "converter_revision": "llama.cpp@1",
            "golden_suite": "conformance.json", "golden_suite_hash": digest,
            "artifacts": {"llama-server": hashlib.sha256(binary.read_bytes()).hexdigest(), "model.gguf": hashlib.sha256(model.read_bytes()).hexdigest(), "tokenizer.json": hashlib.sha256(tokenizer.read_bytes()).hexdigest(), "conformance.json": digest},
        }), encoding="utf-8")
        install_pack(self.settings, pack)
        with mock.patch("brain.models.runtime_for_pack", return_value=DeterministicRuntime(16)), self.assertRaisesRegex(ValueError, "reference_vectors"):
            verify_pack(self.settings, "incomplete-production-conformance")

    @unittest.skipUnless(importlib.util.find_spec("usearch"), "requires optional semantic extra")
    def test_verified_local_pack_builds_persistent_usearch_shards(self) -> None:
        pack = self.root / "usearch-pack"
        pack.mkdir()
        (pack / "manifest.json").write_text(json.dumps({
            "pack_id": "usearch-test-embedding", "capability": "embedding", "model_family": "test",
            "upstream_model": "test-only", "upstream_revision": "1", "license": "MIT",
            "runtime_name": "deterministic-test", "runtime_revision": "1", "minimum_brain_version": "0.6.1",
            "embedding_dimension": 16, "test_only": True,
        }), encoding="utf-8")
        install_pack(self.settings, pack)
        verify_pack(self.settings, "usearch-test-embedding")
        built = build_semantic_index(self.settings)
        self.assertEqual("usearch", built["backend"])
        state = json.loads((self.settings.state_dir / "semantic-index.json").read_text(encoding="utf-8"))
        self.assertTrue(all(Path(shard["path"]).is_file() for shard in state["shards"]))
        self.assertTrue(search_semantic(self.settings, "eligibility", repos={"trading-service"}))
        before = {str(shard["repo"]): str(shard["path"]) for shard in state["shards"]}
        changed = self.root / "trading-service/src/main/java/demo/TradingEligibilityService.java"
        changed.write_text(changed.read_text(encoding="utf-8") + "\n// semantic shard change\n", encoding="utf-8")
        events: list[dict[str, object]] = []
        build_semantic_index(self.settings, progress=events.append)
        state = json.loads((self.settings.state_dir / "semantic-index.json").read_text(encoding="utf-8"))
        after = {str(shard["repo"]): str(shard["path"]) for shard in state["shards"]}
        self.assertEqual(len(self.settings.repositories) - 1, events[-1]["semantic_shards_reused"])
        self.assertEqual(1, events[-1]["semantic_shards_rebuilt"])
        self.assertNotEqual(before["trading-service"], after["trading-service"])
        self.assertTrue(all(before[name] == after[name] for name in before if name != "trading-service"))

    def test_mock_semantic_pipeline_is_snapshot_filtered(self) -> None:
        def embed(cards: list[str]) -> list[list[float]]:
            return [[float("eligibility" in card.lower()), float("risk" in card.lower()), 1.0] for card in cards]

        built = build_semantic_index(self.settings, embed=embed, pack_id="mock-contract")
        self.assertEqual("exact-mock", built["backend"])
        results = search_semantic(self.settings, "eligibility", repos={"trading-service"}, embed=embed)
        self.assertTrue(results)
        self.assertTrue(all(item["repo"] == "trading-service" for item in results))
        self.settings.repo("trading-service").source_sha = "different-snapshot"
        self.assertEqual([], search_semantic(self.settings, "eligibility", repos={"trading-service"}, embed=embed))
        self.settings.repo("trading-service").source_sha = "working-tree"
        state_path = self.settings.state_dir / "semantic-index.json"
        stale_schema = json.loads(state_path.read_text(encoding="utf-8"))
        stale_schema["chunk_schema_version"] = "obsolete"
        state_path.write_text(json.dumps(stale_schema), encoding="utf-8")
        self.assertEqual([], search_semantic(self.settings, "eligibility", repos={"trading-service"}, embed=embed))

    def test_semantic_shards_search_in_parallel_with_deterministic_merge(self) -> None:
        self.settings.semantic_shard_workers = 2
        active = 0
        maximum = 0
        guard = threading.Lock()

        class Match:
            key = 0
            distance = 0.1

        class FakeIndex:
            @classmethod
            def restore(cls, path, view=True):
                return cls()

            def search(self, vector, limit):
                nonlocal active, maximum
                with guard:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.03)
                with guard:
                    active -= 1
                return [Match()]

        class FakeNumpy:
            float32 = "float32"

            @staticmethod
            def asarray(value, dtype=None):
                return value

        shards = []
        shard_root = self.settings.state_dir / "semantic-shards"
        shard_root.mkdir(parents=True, exist_ok=True)
        for index, repo in enumerate(self.settings.repositories):
            path = shard_root / f"semantic-{index}.usearch"
            path.touch()
            shards.append({
                "repo": repo.name,
                "snapshot": repo.source_sha or "working-tree",
                "path": str(path),
                "artifact_ref": path.name,
                "artifact_bytes": path.stat().st_size,
                "artifact_sha256": _shard_sha256(path),
                "entries": [{"path": f"src/{index}.py", "line": 1, "chunk_id": f"chunk-{index}"}],
            })
        (self.settings.state_dir / "semantic-index.json").write_text(json.dumps({
            "chunk_schema_version": CHUNK_SCHEMA_VERSION,
            "card_version": CARD_VERSION,
            "embedding_input_version": SEMANTIC_EMBEDDING_INPUT_VERSION,
            "atlas_card_version": ATLAS_CARD_VERSION,
            "shard_manifest_version": SEMANTIC_SHARD_MANIFEST_VERSION,
            "backend": "usearch",
            "pack_id": "parallel-test",
            "pack_compatibility_identity": _injected_pack_identity("parallel-test"),
            "dimension": 2,
            "stale": False,
            "snapshots": {repo.name: repo.source_sha or "working-tree" for repo in self.settings.repositories},
            "entries": [],
            "shards": shards,
        }), encoding="utf-8")
        trace = RetrievalTrace()
        with mock.patch("brain.semantic._usearch", return_value=(FakeIndex, FakeNumpy)):
            results = search_semantic(self.settings, "eligibility", embed=lambda values: [[1.0, 0.0]], trace=trace)
        self.assertGreater(maximum, 1)
        self.assertLessEqual(maximum, self.settings.semantic_shard_workers)
        self.assertEqual(len(shards), trace.physical_backend_operations)
        self.assertIn("semantic_query_embedding_ms", trace.stage_ms)
        self.assertIn("semantic_shard_search_ms", trace.stage_ms)
        self.assertIn("semantic_total_ms", trace.stage_ms)
        self.assertEqual(sorted(item["repo"] for item in results), [item["repo"] for item in results])

    def test_local_reranker_only_reorders_bounded_nonprotected_candidates(self) -> None:
        protected = SearchHit("trading-service", "src/Eligibility.java", 10, "class Eligibility", "definition", 100, ["symbol"])
        first = SearchHit("trading-service", "README.md", 2, "release note", "code", 50, ["search"])
        second = SearchHit("risk-service", "src/Risk.java", 8, "eligibility risk check", "code", 50, ["search"])
        trace = RetrievalTrace()
        reranked = rerank_candidates(
            self.settings,
            "eligibility",
            [protected, first, second],
            runtime=DeterministicRuntime(),
            limit=1,
            trace=trace,
        )
        self.assertEqual(100, reranked[0].score)
        self.assertEqual(50, reranked[2].score)
        self.assertIn("local reranker", reranked[1].found_by)
        self.assertIn("reranker_inference_ms", trace.stage_ms)

    def test_model_lane_serializes_concurrent_reranking(self) -> None:
        active = 0
        maximum = 0
        guard = threading.Lock()

        class SlowRuntime:
            def rerank(self, query, documents, instruction=""):
                nonlocal active, maximum
                with guard:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.03)
                with guard:
                    active -= 1
                return [float(index) for index, _ in enumerate(documents)]

            def shutdown(self):
                return None

        def run_rerank() -> None:
            hits = [SearchHit("trading-service", "src/A.java", index + 1, f"line {index}") for index in range(3)]
            rerank_candidates(self.settings, "eligibility", hits, runtime=SlowRuntime(), limit=3)

        threads = [threading.Thread(target=run_rerank) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        self.assertEqual(1, maximum)

    def test_brain_owned_model_runtimes_shutdown_on_query_and_index_paths(self) -> None:
        hit = SearchHit("trading-service", "README.md", 2, "eligibility evidence", "code", 50, ["search"])
        reranker = mock.Mock()
        reranker.rerank.return_value = [1.0]
        with mock.patch("brain.models.active_pack", return_value={"pack_id": "reranker"}), mock.patch("brain.models.runtime_for_pack", return_value=reranker):
            rerank_candidates(self.settings, "eligibility", [hit])
        reranker.shutdown.assert_called_once()

        state = {
            "chunk_schema_version": CHUNK_SCHEMA_VERSION, "card_version": CARD_VERSION,
            "embedding_input_version": SEMANTIC_EMBEDDING_INPUT_VERSION,
            "atlas_card_version": ATLAS_CARD_VERSION,
            "shard_manifest_version": SEMANTIC_SHARD_MANIFEST_VERSION,
            "backend": "exact-mock", "pack_id": "embedding", "dimension": 2, "stale": False,
            "pack_compatibility_identity": _injected_pack_identity("embedding"),
            "snapshots": {repo.name: repo.source_sha or "working-tree" for repo in self.settings.repositories},
            "entries": [{"repo": "trading-service", "snapshot": "working-tree", "path": "README.md", "line": 1, "chunk_id": "one", "vector": [1.0, 0.0]}],
            "shards": [],
        }
        (self.settings.state_dir / "semantic-index.json").write_text(json.dumps(state), encoding="utf-8")
        embedding = mock.Mock()
        embedding.embed.return_value = [[1.0, 0.0]]
        embedding_manifest = {
            "pack_id": "embedding", "embedding_dimension": 2,
            "pack_compatibility_identity": _injected_pack_identity("embedding"),
            "test_only": True,
        }
        with mock.patch("brain.semantic.verified_pack", return_value=embedding_manifest), mock.patch("brain.semantic.runtime_for_pack", return_value=embedding):
            self.assertTrue(search_semantic(self.settings, "eligibility"))
        embedding.shutdown.assert_called_once()

        build_runtime = mock.Mock()
        with mock.patch("brain.semantic.active_pack", return_value=embedding_manifest), mock.patch("brain.semantic.runtime_for_pack", return_value=build_runtime), mock.patch("brain.semantic._usearch", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "USearch"):
                build_semantic_index(self.settings)
        build_runtime.shutdown.assert_called_once()

    def test_managed_llama_pack_executes_only_verified_loopback_artifacts(self) -> None:
        pack = self.root / "managed-pack"
        pack.mkdir()
        binary = pack / "llama-server"
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o700)
        model = pack / "model.gguf"
        model.write_bytes(b"local model")
        manifest = {
            "pack_id": "managed-local", "capability": "embedding", "model_family": "test",
            "upstream_model": "local", "upstream_revision": "1", "license": "MIT",
            "runtime_name": "llama.cpp", "runtime_revision": "1", "minimum_brain_version": "0.6.1",
            "installed_path": str(pack), "runtime_binary": "llama-server", "model_file": "model.gguf",
            "artifacts": {"llama-server": hashlib.sha256(binary.read_bytes()).hexdigest(), "model.gguf": hashlib.sha256(model.read_bytes()).hexdigest()},
            "runtime_args": ["--ctx-size", "4096", "-ub", "512"],
            "request_timeout_seconds": 12,
            "test_only": True,
        }
        validate_manifest(manifest)
        with self.assertRaisesRegex(ValueError, "verification_request_timeout_seconds"):
            validate_manifest({**manifest, "verification_request_timeout_seconds": 901})
        runtime = runtime_for_pack(manifest)
        self.assertIsInstance(runtime, ManagedLlamaCppRuntime)
        process = mock.Mock()
        process.poll.return_value = None
        with mock.patch("brain.models.start_managed_process", return_value=process) as popen, mock.patch.object(LlamaCppRuntime, "health", return_value={"ok": True}), mock.patch("brain.models.terminate_process_tree") as terminate:
            runtime.warmup()
            command = popen.call_args.args[0]
            self.assertIn("127.0.0.1", command)
            self.assertIn("--offline", command)
            self.assertIn("--no-webui", command)
            self.assertIn("--embedding", command)
            self.assertEqual("last", command[command.index("--pooling") + 1])
            self.assertIn("--ctx-size", command)
            self.assertIn("4096", command)
            self.assertNotIn("--hf-repo", command)
            self.assertEqual(12.0, runtime.client.timeout_seconds)
            self.assertTrue(runtime.client.direct_loopback)
            runtime.shutdown()
            terminate.assert_called_once_with(process, graceful_timeout=3)

        legacy_embedding = ManagedLlamaCppRuntime({
            **manifest,
            "runtime_args": ["--pooling", "last", "--ctx-size", "4096", "-ub", "512"],
        })
        legacy_process = mock.Mock()
        legacy_process.poll.return_value = None
        with mock.patch("brain.models.start_managed_process", return_value=legacy_process) as popen, mock.patch.object(
            LlamaCppRuntime, "health", return_value={"ok": True}
        ), mock.patch("brain.models.terminate_process_tree"):
            legacy_embedding.warmup()
            command = popen.call_args.args[0]
            self.assertEqual(1, command.count("--pooling"))
            self.assertEqual("last", command[command.index("--pooling") + 1])
            legacy_embedding.shutdown()

        reranker = ManagedLlamaCppRuntime({
            **manifest,
            "capability": "reranker",
            "runtime_args": ["--reranking", "--pooling", "rank", "--ctx-size", "4096", "-ub", "4096"],
        })
        rerank_process = mock.Mock()
        rerank_process.poll.return_value = None
        with mock.patch("brain.models.start_managed_process", return_value=rerank_process) as popen, mock.patch.object(
            LlamaCppRuntime, "health", return_value={"ok": True}
        ), mock.patch("brain.models.terminate_process_tree"):
            reranker.warmup()
            command = popen.call_args.args[0]
            self.assertEqual(1, command.count("--reranking"))
            self.assertNotIn("--rerank", command)
            self.assertNotIn("--embedding", command)
            self.assertEqual(1, command.count("--pooling"))
            self.assertEqual("rank", command[command.index("--pooling") + 1])
            reranker.shutdown()

        for argument in (
            "--host=0.0.0.0", "--api-key=known", "--model=/other/model.gguf",
            "--hf-repo=remote/model", "--port=8080", "--pooling=none", "--reranking", "-m=/other/model.gguf",
        ):
            with self.subTest(argument=argument):
                poisoned = ManagedLlamaCppRuntime({**manifest, "runtime_args": [argument]})
                with mock.patch("brain.models.start_managed_process") as popen:
                    with self.assertRaisesRegex(RuntimeError, "local runtime controls"):
                        poisoned._start()
                popen.assert_not_called()

    def test_managed_runtime_startup_diagnostics_distinguish_start_health_and_transport_failures(self) -> None:
        self.assertEqual(120.0, DEFAULT_RUNTIME_REQUEST_TIMEOUT_SECONDS)
        self.assertEqual(120.0, DEFAULT_RUNTIME_STARTUP_TIMEOUT_SECONDS)
        common = {
            "capability": "embedding", "startup_timeout_seconds": 1,
        }
        with mock.patch("brain.models.start_managed_process") as popen:
            with self.assertRaisesRegex(RuntimeError, "runtime limits must be numeric"):
                ManagedLlamaCppRuntime({**common, "request_timeout_seconds": "invalid"})._start()
        popen.assert_not_called()

        artifacts = Path("/tmp/verified-artifact")
        with mock.patch("brain.models._check_pack_integrity"), mock.patch(
            "brain.models._pack_file", return_value=artifacts
        ), mock.patch("brain.models.os.access", return_value=True), mock.patch(
            "brain.models.start_managed_process", side_effect=OSError("synthetic")
        ):
            with self.assertRaisesRegex(RuntimeError, "failed to start"):
                ManagedLlamaCppRuntime(common)._start()

        process = mock.Mock()
        process.poll.return_value = None
        with mock.patch("brain.models._check_pack_integrity"), mock.patch(
            "brain.models._pack_file", return_value=artifacts
        ), mock.patch("brain.models.os.access", return_value=True), mock.patch(
            "brain.models.start_managed_process", return_value=process
        ), mock.patch.object(LlamaCppRuntime, "health", return_value={"ok": False}), mock.patch(
            "brain.models.time.monotonic", side_effect=[0.0, 0.5, 1.5]
        ), mock.patch("brain.models.time.sleep"), mock.patch.object(ManagedLlamaCppRuntime, "shutdown"):
            with self.assertRaisesRegex(RuntimeError, "alive but health endpoint"):
                ManagedLlamaCppRuntime(common)._start()

        process = mock.Mock()
        process.poll.return_value = None
        with mock.patch("brain.models._check_pack_integrity"), mock.patch(
            "brain.models._pack_file", return_value=artifacts
        ), mock.patch("brain.models.os.access", return_value=True), mock.patch(
            "brain.models.start_managed_process", return_value=process
        ), mock.patch.object(LlamaCppRuntime, "health", side_effect=ConnectionError("synthetic")), mock.patch(
            "brain.models.time.monotonic", side_effect=[0.0, 0.5, 1.5]
        ), mock.patch("brain.models.time.sleep"), mock.patch.object(ManagedLlamaCppRuntime, "shutdown"):
            with self.assertRaisesRegex(RuntimeError, "health transport failed"):
                ManagedLlamaCppRuntime(common)._start()

    def test_managed_llama_runtime_restarts_once_after_a_transport_disconnect(self) -> None:
        runtime = ManagedLlamaCppRuntime({})
        client = mock.Mock()
        client.embed.side_effect = [ConnectionResetError("local server restarted"), [[1.0, 0.0]]]
        with mock.patch.object(runtime, "_start", return_value=client) as start, mock.patch.object(runtime, "shutdown") as shutdown:
            self.assertEqual([[1.0, 0.0]], runtime.embed(["public synthetic card"], dimension=2))
        self.assertEqual(2, start.call_count)
        shutdown.assert_called_once()

        verifier = ManagedLlamaCppRuntime({}, verification=True)
        verifier_client = mock.Mock()
        verifier_client.rerank.side_effect = [
            ConnectionResetError("first transient failure"),
            ConnectionResetError("second transient failure"),
            [1.0],
        ]
        with mock.patch.object(verifier, "_start", return_value=verifier_client) as start, mock.patch.object(
            verifier, "shutdown",
        ) as shutdown:
            self.assertEqual([1.0], verifier.rerank("public query", ["public document"]))
        self.assertEqual(3, start.call_count)
        self.assertEqual(2, shutdown.call_count)

    def test_managed_runtime_hashes_pack_once_at_the_actual_start_boundary(self) -> None:
        manifest = {"runtime_name": "llama.cpp", "capability": "embedding"}
        process = mock.Mock()
        process.poll.return_value = None
        with mock.patch("brain.models._check_pack_integrity") as integrity, mock.patch(
            "brain.models._pack_file", return_value=Path("/tmp/verified-artifact")
        ), mock.patch("brain.models.os.access", return_value=True), mock.patch(
            "brain.models.start_managed_process", return_value=process
        ), mock.patch.object(LlamaCppRuntime, "health", return_value={"ok": True}), mock.patch(
            "brain.models.terminate_process_tree"
        ):
            runtime = runtime_for_pack(manifest)
            integrity.assert_not_called()
            runtime._start()
            runtime.shutdown()
        integrity.assert_called_once_with(manifest)

    def test_managed_llama_runtime_restarts_after_its_request_budget(self) -> None:
        runtime = ManagedLlamaCppRuntime({"capability": "embedding", "max_requests_per_runtime": 2})
        runtime.client = mock.Mock()
        runtime.process = mock.Mock()
        runtime.process.poll.return_value = None
        runtime.request_count = 2
        new_process = mock.Mock()
        new_process.poll.return_value = None
        with mock.patch.object(runtime, "shutdown") as shutdown, mock.patch("brain.models._check_pack_integrity"), mock.patch("brain.models._pack_file", return_value=Path("/tmp/verified-artifact")), mock.patch("brain.models.os.access", return_value=True):
            with mock.patch("brain.models.start_managed_process", return_value=new_process) as popen, mock.patch.object(LlamaCppRuntime, "health", return_value={"ok": True}):
                runtime._start()
        shutdown.assert_called_once()
        popen.assert_called_once()

    def test_production_llama_manifest_cannot_omit_owned_runtime_artifacts(self) -> None:
        manifest = {
            "pack_id": "missing-runtime", "capability": "embedding", "model_family": "test",
            "upstream_model": "local", "upstream_revision": "1", "license": "MIT",
            "runtime_name": "llama.cpp", "runtime_revision": "1", "minimum_brain_version": "0.6.1",
            "artifacts": {"model.gguf": "abc", "conformance.json": "a" * 64}, "model_file": "model.gguf",
            "golden_suite": "conformance.json", "golden_suite_hash": "a" * 64,
        }
        with self.assertRaises(ValueError):
            validate_manifest(manifest)

    def test_production_llama_manifest_cannot_delegate_to_runtime_url(self) -> None:
        manifest = {
            "pack_id": "delegated-runtime", "capability": "embedding", "model_family": "approved",
            "upstream_model": "local", "upstream_revision": "1", "license": "MIT",
            "runtime_name": "llama.cpp", "runtime_revision": "1", "minimum_brain_version": "0.6.1",
            "runtime_url": "http://127.0.0.1:8080", "golden_suite": "conformance.json",
            "golden_suite_hash": "a" * 64, "artifacts": {"conformance.json": "a" * 64},
        }
        with self.assertRaisesRegex(ValueError, "runtime_binary and model_file"):
            validate_manifest(manifest)

    def test_production_model_manifest_requires_checked_golden_suite(self) -> None:
        manifest = {
            "pack_id": "missing-golden", "capability": "embedding", "model_family": "approved",
            "upstream_model": "local", "upstream_revision": "1", "license": "MIT",
            "runtime_name": "llama.cpp", "runtime_revision": "1", "minimum_brain_version": "0.6.1",
            "runtime_url": "http://127.0.0.1:8080", "artifacts": {"model.gguf": "a" * 64},
        }
        with self.assertRaisesRegex(ValueError, "golden_suite"):
            validate_manifest(manifest)

    def test_catalog_migrates_embedding_cache_and_gc_preserves_ticket_snapshot(self) -> None:
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        database = self.settings.state_dir / "catalog.sqlite3"
        connection = sqlite3.connect(database)
        connection.executescript(
            "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "INSERT INTO metadata VALUES ('schema_version', '1');"
            "CREATE TABLE embedding_cache (cache_key TEXT PRIMARY KEY, pack_id TEXT, dimension INTEGER, vector_json TEXT, created_at TEXT);"
        )
        connection.close()
        migrated = catalog_connect(self.settings)
        self.assertIn("last_used_at", {row[1] for row in migrated.execute("PRAGMA table_info(embedding_cache)")})
        migrated.close()

        snapshots = self.settings.state_dir / "snapshots" / "trading-service"
        old, middle, current = (snapshots / name for name in ("old", "middle", "current"))
        for index, path in enumerate((old, middle, current)):
            path.mkdir(parents=True)
            os.utime(path, (100 + index, 100 + index))
        self.settings.runs_dir.mkdir(parents=True, exist_ok=True)
        (self.settings.runs_dir / "ABC-1").mkdir()
        (self.settings.runs_dir / "ABC-1" / "session.json").write_text(json.dumps({"sources": {"trading-service": {"snapshot": str(old)}}}), encoding="utf-8")
        report = gc(self.settings, dry_run=True, keep_recent=1)
        self.assertIn(str(middle), [item["path"] for item in report["remove"]])
        self.assertNotIn(str(old), [item["path"] for item in report["remove"]])
        self.assertNotIn(str(current), [item["path"] for item in report["remove"]])
        (self.settings.runs_dir / "ABC-1" / "session.json").write_text("{", encoding="utf-8")
        blocked = gc(self.settings, dry_run=False, keep_recent=1)
        self.assertTrue(blocked["reachability_gc_blocked"])
        self.assertEqual([], blocked["remove"])
        self.assertTrue(middle.is_dir())
        for corrupt in ({"generation": "garbage"}, {"generation": True}, {"sources": []}, {"sources": {"trading-service": "bad"}}):
            (self.settings.runs_dir / "ABC-1" / "session.json").write_text(
                json.dumps(corrupt), encoding="utf-8",
            )
            blocked = gc(self.settings, dry_run=False, keep_recent=1)
            self.assertTrue(blocked["reachability_gc_blocked"])
            self.assertEqual([], blocked["remove"])
            self.assertTrue(middle.is_dir())

    def test_gc_global_scan_budget_fails_closed_before_any_deletion(self) -> None:
        snapshots = self.settings.state_dir / "snapshots" / "trading-service"
        old = snapshots / "old"
        current = snapshots / "current"
        old.mkdir(parents=True)
        current.mkdir()
        (old / "must-remain.txt").write_text("old", encoding="utf-8")
        (current / "must-remain.txt").write_text("current", encoding="utf-8")
        with mock.patch("brain.ops.MAX_GC_SCAN_ITEMS", 1):
            report = gc(self.settings, dry_run=False, keep_recent=1)
        self.assertTrue(report["reachability_gc_blocked"])
        self.assertEqual([], report["remove"])
        self.assertTrue((old / "must-remain.txt").is_file())
        self.assertTrue((current / "must-remain.txt").is_file())

    def test_gc_catalog_and_membership_loaders_consume_before_materializing(self) -> None:
        from brain.catalog import generations as catalog_generations
        from brain.index import _connect as index_connect, membership_snapshots

        snapshot_indexes(self.settings)
        catalog_visits = 0

        class BudgetExceeded(RuntimeError):
            pass

        def consume_catalog(count: int) -> None:
            nonlocal catalog_visits
            catalog_visits += count
            if catalog_visits > 2:
                raise BudgetExceeded

        with self.assertRaises(BudgetExceeded):
            catalog_generations(self.settings, consume=consume_catalog)
        self.assertEqual(3, catalog_visits)

        connection = index_connect(self.settings)
        try:
            before = int(connection.execute("SELECT COUNT(*) FROM indexed_snapshots").fetchone()[0])
        finally:
            connection.close()
        membership_visits = 0

        def consume_membership(count: int) -> None:
            nonlocal membership_visits
            membership_visits += count
            if membership_visits > 1:
                raise BudgetExceeded

        with self.assertRaises(BudgetExceeded):
            membership_snapshots(self.settings, consume=consume_membership)
        self.assertEqual(2, membership_visits)
        connection = index_connect(self.settings)
        try:
            after = int(connection.execute("SELECT COUNT(*) FROM indexed_snapshots").fetchone()[0])
        finally:
            connection.close()
        self.assertEqual(before, after)

    def test_golden_evaluation_replays_hand_labelled_local_cases(self) -> None:
        snapshot_indexes(self.settings)
        suite = Path(__file__).parent / "fixtures" / "golden_demo.json"
        report = evaluate_golden(self.settings, suite, split="holdout")
        self.assertEqual(1, report["summary"]["evaluated_cases"])
        self.assertEqual(1.0, report["summary"]["file_recall_at_limit"])
        self.assertIn("file_recall_at_5", report["summary"])
        self.assertIn("precision_at_10", report["summary"])
        self.assertIn("semantic_only_useful_hit_rate", report["summary"])
        self.assertIn("total_ms", report["cases"][0])
        self.assertIn("duplicate_ratio", report["cases"][0])
        self.assertTrue((self.settings.state_dir / "golden-eval.json").is_file())

    def test_zoekt_adapter_uses_only_current_snapshot_shard_and_filters_literal_matches(self) -> None:
        from brain.backends import zoekt

        class FakeProcess:
            def __init__(self, output: str, return_code: int = 0) -> None:
                self.stdout = io.BytesIO(output.encode("utf-8"))
                self.final_return_code = return_code
                self.returncode: int | None = None

            def terminate(self) -> None:
                self.returncode = -15

            def kill(self) -> None:
                self.returncode = -9

            def wait(self, timeout: float | None = None) -> int:
                if self.returncode is None:
                    self.returncode = self.final_return_code
                return self.returncode

            def poll(self) -> int | None:
                return self.returncode

        repo = self.settings.repo("trading-service")
        repo.source_sha = "snapshot-1"
        repo.source_path = self.root / "immutable-trading-snapshot"
        repo.source_path.mkdir()
        shard = zoekt.shard_path(self.settings.state_dir, repo.name, repo.source_sha)
        shard.mkdir(parents=True)
        (shard / "snapshot.zoekt").write_bytes(b"valid shard")
        (shard / "brain-shard.json").write_text(json.dumps({
            "manifest_version": zoekt.SHARD_MANIFEST_VERSION,
            "source_sha": repo.source_sha,
            "path_identity": zoekt.SHARD_PATH_IDENTITY,
            "shards": [{"name": "snapshot.zoekt", "size": 11,
                        "sha256": hashlib.sha256(b"valid shard").hexdigest()}],
        }), encoding="utf-8")
        response = json.dumps({"FileName": "src/main/java/demo/EligibilityEvaluator.java", "Score": 5, "LineMatches": [{"LineNumber": 2, "LineStart": 0, "Line": base64.b64encode(b"interface EligibilityEvaluator {}\n").decode()}, {"LineNumber": 3, "LineStart": 0, "Line": base64.b64encode(b"unrelated\n").decode()}]})
        available = zoekt.ZoektStatus(True, "zoekt", "zoekt-index")
        with mock.patch("brain.backends.zoekt.status", return_value=available), mock.patch(
            "brain.backends.zoekt.start_managed_process", return_value=FakeProcess(response),
        ):
            result = zoekt.search(self.settings, repo, "EligibilityEvaluator", fixed=True, max_results=20)
        self.assertIsNotNone(result)
        self.assertEqual([("src/main/java/demo/EligibilityEvaluator.java", 2, "interface EligibilityEvaluator {}", 5.0)], result[0])
        unsafe_response = response.replace(
            "src/main/java/demo/EligibilityEvaluator.java",
            "/old/workspace/src/main/java/demo/EligibilityEvaluator.java",
        )
        with mock.patch("brain.backends.zoekt.status", return_value=available), mock.patch(
            "brain.backends.zoekt.start_managed_process", return_value=FakeProcess(unsafe_response),
        ):
            self.assertIsNone(zoekt.search(self.settings, repo, "EligibilityEvaluator", fixed=True, max_results=20))
        with mock.patch("brain.backends.zoekt.status", return_value=available), mock.patch(
            "brain.backends.zoekt.start_managed_process", return_value=FakeProcess("", 1),
        ):
            self.assertIsNone(zoekt.search(self.settings, repo, "EligibilityEvaluator", fixed=True, max_results=20))
        with mock.patch("brain.backends.zoekt.status", return_value=available), mock.patch(
            "brain.backends.zoekt.start_managed_process", return_value=FakeProcess("not-json\n"),
        ):
            self.assertIsNone(zoekt.search(self.settings, repo, "EligibilityEvaluator", fixed=True, max_results=20))
        (shard / "snapshot.zoekt").write_bytes(b"evil shard!")
        with mock.patch("brain.backends.zoekt.status", return_value=available), mock.patch(
            "brain.backends.zoekt.start_managed_process"
        ) as popen:
            self.assertIsNone(zoekt.search(self.settings, repo, "EligibilityEvaluator", fixed=True, max_results=20))
            popen.assert_not_called()
        (shard / "snapshot.zoekt").unlink()
        with mock.patch("brain.backends.zoekt.status", return_value=available), mock.patch(
            "brain.backends.zoekt.start_managed_process"
        ) as popen:
            self.assertIsNone(zoekt.search(self.settings, repo, "EligibilityEvaluator", fixed=True, max_results=20))
            popen.assert_not_called()
        repo.source_sha = "snapshot-2"
        self.assertIsNone(zoekt.search(self.settings, repo, "EligibilityEvaluator", fixed=True, max_results=20))

    def test_zoekt_never_registers_or_serves_mutable_working_tree_state(self) -> None:
        from brain.backends import zoekt
        from brain.catalog import collect_generation_components

        repo = self.settings.repo("trading-service")
        available = zoekt.ZoektStatus(True, "zoekt", "zoekt-index")
        for source_sha, source_path in ((None, None), ("commit-without-export", None), ("commit-on-working-tree", repo.path)):
            with self.subTest(source_sha=source_sha):
                repo.source_sha = source_sha
                repo.source_path = source_path
                sha = source_sha or "working-tree"
                shard = zoekt.shard_path(self.settings.state_dir, repo.name, sha)
                shard.mkdir(parents=True, exist_ok=True)
                (shard / "stale.zoekt").write_bytes(b"stale shard")
                (shard / "brain-shard.json").write_text(json.dumps({
                    "manifest_version": zoekt.SHARD_MANIFEST_VERSION,
                    "source_sha": sha,
                    "path_identity": zoekt.SHARD_PATH_IDENTITY,
                    "shards": [{"name": "stale.zoekt", "size": 11,
                                "sha256": hashlib.sha256(b"stale shard").hexdigest()}],
                }), encoding="utf-8")
                with mock.patch("brain.backends.zoekt.status", return_value=available), mock.patch(
                    "brain.backends.zoekt.subprocess.run"
                ) as run:
                    self.assertIsNone(zoekt.search(self.settings, repo, "EligibilityEvaluator", fixed=True, max_results=20))
                    built = zoekt.build(self.settings, [repo])
                    run.assert_not_called()
                self.assertEqual("skipped", built[repo.name]["status"])
                components = collect_generation_components(self.settings, {repo.name: {"sha": sha}})
                self.assertEqual("unavailable", components["zoekt"]["status"])

        repo.source_sha = None
        repo.source_path = None
        (repo.path / "src/main/java/demo/EligibilityEvaluator.java").write_text(
            "interface EligibilityEvaluator { void updated(); }\n", encoding="utf-8"
        )
        with mock.patch("brain.backends.zoekt.status", return_value=available):
            hits = search(self.settings, "updated", [repo.name], fixed=True)
        self.assertTrue(any(hit.repo == repo.name and "updated" in hit.text for hit in hits))

    def test_capabilities_counts_usearch_shard_entries(self) -> None:
        (self.settings.state_dir / "semantic-index.json").parent.mkdir(parents=True, exist_ok=True)
        (self.settings.state_dir / "semantic-index.json").write_text(json.dumps({
            "backend": "usearch", "stale": False, "entries": [],
            "shards": [{"entries": [{"chunk_id": "one"}, {"chunk_id": "two"}]}],
        }), encoding="utf-8")
        report = capabilities(self.settings)
        self.assertEqual(2, report["semantic_chunks"])
        self.assertEqual("usearch", report["semantic_backend"])

    def test_trace_metadata_always_identifies_brain_version(self) -> None:
        metadata = trace_metadata(self.settings)
        self.assertRegex(str(metadata["brain_version"]), r"^\d+\.\d+\.\d+")
        self.assertIn("corpus_signature", metadata)

    def test_model_checksum_tampering_is_rejected_and_pack_removal_stales_semantics(self) -> None:
        pack = self.root / "checked-pack"
        pack.mkdir()
        weights = pack / "weights.bin"
        weights.write_bytes(b"checked")
        (pack / "manifest.json").write_text(json.dumps({
            "pack_id": "checked-test", "capability": "embedding", "model_family": "test",
            "upstream_model": "test-only", "upstream_revision": "1", "license": "MIT",
            "runtime_name": "deterministic-test", "runtime_revision": "1", "minimum_brain_version": "0.6.1",
            "embedding_dimension": 16, "test_only": True,
            "artifacts": {"weights.bin": hashlib.sha256(weights.read_bytes()).hexdigest()},
        }), encoding="utf-8")
        install_pack(self.settings, pack)
        verify_pack(self.settings, "checked-test")
        state = {"pack_id": "checked-test", "stale": False}
        (self.settings.state_dir / "semantic-index.json").write_text(json.dumps(state), encoding="utf-8")
        from brain.models import remove_pack

        remove_pack(self.settings, "checked-test")
        self.assertTrue(json.loads((self.settings.state_dir / "semantic-index.json").read_text(encoding="utf-8"))["stale"])

        install_pack(self.settings, pack)
        (self.settings.state_dir / "models" / "checked-test" / "weights.bin").write_bytes(b"tampered")
        with self.assertRaises(ValueError):
            verify_pack(self.settings, "checked-test")

    def test_model_pack_paths_are_portable_and_collision_resistant(self) -> None:
        from brain.models import _pack_directory

        upper = _pack_directory(self.settings, "Pack")
        lower = _pack_directory(self.settings, "pack")
        reserved = _pack_directory(self.settings, "CON.txt")
        oversized = _pack_directory(self.settings, "model-" + "x" * 500)
        self.assertNotEqual(upper, lower)
        self.assertTrue(reserved.name.startswith("id-CON"))
        self.assertLessEqual(len(oversized.name.encode("utf-8")), 128)
        self.assertTrue(all(path.parent == self.settings.state_dir / "models" for path in (upper, lower, reserved, oversized)))

    def test_model_pack_id_is_immutable_but_exact_reinstall_is_idempotent(self) -> None:
        original = self.root / "immutable-pack-original"
        replacement = self.root / "immutable-pack-replacement"
        original.mkdir()
        replacement.mkdir()

        def write_pack(root: Path, weights: bytes, instruction: str) -> None:
            (root / "weights.bin").write_bytes(weights)
            (root / "manifest.json").write_text(json.dumps({
                "pack_id": "immutable-embedding", "capability": "embedding", "model_family": "test",
                "upstream_model": "test-only", "upstream_revision": "1", "license": "MIT",
                "runtime_name": "deterministic-test", "runtime_revision": "1", "minimum_brain_version": "0.6.1",
                "embedding_dimension": 16, "test_only": True, "query_instruction": instruction,
                "artifacts": {"weights.bin": hashlib.sha256(weights).hexdigest()},
            }), encoding="utf-8")

        write_pack(original, b"generation-one-weights", "query-v1")
        write_pack(replacement, b"generation-two-weights", "query-v2")
        installed = install_pack(self.settings, original)
        verified = verify_pack(self.settings, "immutable-embedding")
        same = install_pack(self.settings, original)
        self.assertEqual(verified["checked_artifacts"], same["checked_artifacts"])
        with self.assertRaisesRegex(ValueError, "pack ID .* is immutable"):
            install_pack(self.settings, replacement)
        self.assertEqual(b"generation-one-weights", Path(installed["installed_path"]).joinpath("weights.bin").read_bytes())
        self.assertTrue(verify_pack(self.settings, "immutable-embedding")["verified"])

    def test_model_pack_metadata_failure_leaves_no_poisoned_destination_and_retry_recovers(self) -> None:
        pack = self.root / "recoverable-pack"
        pack.mkdir()
        weights = pack / "weights.bin"
        weights.write_bytes(b"recoverable")
        (pack / "manifest.json").write_text(json.dumps({
            "pack_id": "recoverable-embedding", "capability": "embedding", "model_family": "test",
            "upstream_model": "test-only", "upstream_revision": "1", "license": "MIT",
            "runtime_name": "deterministic-test", "runtime_revision": "1", "minimum_brain_version": "0.6.1",
            "embedding_dimension": 16, "test_only": True,
            "artifacts": {"weights.bin": hashlib.sha256(weights.read_bytes()).hexdigest()},
        }), encoding="utf-8")
        destination = self.settings.state_dir / "models" / "recoverable-embedding"
        from brain import models as models_module

        original_write = models_module.atomic_managed_text_write
        failed = False

        def fail_staged_metadata(root: Path, path: Path, data: str) -> None:
            nonlocal failed
            if path.name == "installed.json" and path.parent.name.endswith(".installing") and not failed:
                failed = True
                raise OSError("simulated staged metadata failure")
            original_write(root, path, data)

        with mock.patch("brain.models.atomic_managed_text_write", side_effect=fail_staged_metadata):
            with self.assertRaisesRegex(OSError, "simulated staged metadata failure"):
                install_pack(self.settings, pack)
        self.assertFalse(destination.exists())
        installed = install_pack(self.settings, pack)
        self.assertEqual("recoverable-embedding", installed["pack_id"])
        self.assertTrue((destination / "installed.json").is_file())

    def test_precision_reranker_failure_leaves_core_candidate_retrieval_usable(self) -> None:
        snapshot_indexes(self.settings)
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        (self.settings.state_dir / "edition.json").write_text(json.dumps({"edition": "precision"}), encoding="utf-8")
        request = {"objective": "Find eligibility", "searches": [{"query": "EligibilityEvaluator", "repos": ["trading-service"]}], "paths": [], "symbols": [], "files": [], "history": [], "expand": []}
        with mock.patch("brain.models.rerank_candidates", side_effect=RuntimeError("timeout")):
            bundle = retrieve_context(self.settings, request)
        self.assertTrue(bundle.evidence)
        self.assertIn("Local reranker failed; used semantic/lexical candidate ranking.", bundle.warnings)

    def test_semantic_runtime_failure_leaves_core_candidate_retrieval_usable(self) -> None:
        snapshot_indexes(self.settings)
        (self.settings.state_dir / "edition.json").write_text(json.dumps({"edition": "semantic"}), encoding="utf-8")
        request = {"objective": "Find eligibility", "searches": [{"query": "EligibilityEvaluator", "repos": ["trading-service"]}], "paths": [], "symbols": [], "files": [], "history": [], "expand": []}
        with mock.patch("brain.semantic.search_semantic", side_effect=RuntimeError("embedding runtime exited")):
            bundle = retrieve_context(self.settings, request)
        self.assertTrue(bundle.evidence)
        self.assertIn("Semantic runtime failed; used Core retrieval only.", bundle.warnings)

    def test_missing_pinned_session_snapshot_is_rejected_before_retrieval(self) -> None:
        start_session(self.settings, "PIN-1", "Keep the source pinned.")
        state = session_state(self.settings, "PIN-1")
        state["sources"]["trading-service"]["snapshot"] = str(self.settings.state_dir / "snapshots" / "trading-service" / "missing")
        (self.settings.runs_dir / "PIN-1" / "session.json").write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(BrainError, "Pinned source snapshot"):
            create_context(self.settings, "PIN-1", REQUEST)

    def test_corrupt_optional_vector_shard_and_catalog_do_not_break_core_fallback(self) -> None:
        shard = self.settings.state_dir / "semantic-shards" / "broken.usearch"
        shard.parent.mkdir(parents=True)
        shard.write_bytes(b"not a vector index")
        (self.settings.state_dir / "semantic-index.json").write_text(json.dumps({
            "chunk_schema_version": CHUNK_SCHEMA_VERSION, "card_version": CARD_VERSION,
            "embedding_input_version": SEMANTIC_EMBEDDING_INPUT_VERSION,
            "atlas_card_version": ATLAS_CARD_VERSION,
            "shard_manifest_version": SEMANTIC_SHARD_MANIFEST_VERSION,
            "backend": "usearch", "pack_id": "mock", "dimension": 3, "stale": False,
            "snapshots": {repo.name: repo.source_sha or "working-tree" for repo in self.settings.repositories},
            "entries": [],
            "shards": [{
                "repo": "trading-service", "snapshot": "working-tree", "path": str(shard),
                "artifact_ref": shard.name, "artifact_bytes": shard.stat().st_size,
                "artifact_sha256": _shard_sha256(shard),
                "entries": [{"path": "src/main/java/demo/EligibilityEvaluator.java", "line": 2, "chunk_id": "chunk"}],
            }],
        }), encoding="utf-8")

        class BrokenIndex:
            @staticmethod
            def restore(*args, **kwargs):
                raise ValueError("corrupt")

        with mock.patch("brain.semantic._usearch", return_value=(BrokenIndex, mock.Mock(asarray=lambda value, dtype: value))):
            self.assertEqual([], search_semantic(self.settings, "eligibility", embed=lambda values: [[1.0, 0.0, 0.0]]))

        (self.settings.state_dir / "catalog.sqlite3").write_bytes(b"corrupt catalog")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            state, _ = snapshot_indexes(self.settings)
            garbage_collector.collect()
        self.assertFalse([item for item in caught if issubclass(item.category, ResourceWarning)])
        self.assertIn("Catalog generation unavailable", str(state["trading-service"].get("warning")))
        self.assertTrue(search(self.settings, "EligibilityEvaluator", ["trading-service"], fixed=True))


class SyntheticFanoutTest(unittest.TestCase):
    def test_fifty_repository_repetitive_request_is_fused_routed_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = ['[project]', 'name="fanout"', '[graph]', 'enabled=false']
            for index in range(50):
                repo = root / f"repo-{index:02d}"
                repo.mkdir()
                if index == 37:
                    (repo / "Needle.java").write_text("class NeedleSymbol {}\n", encoding="utf-8")
                rows.extend(["[[repositories]]", f'name="repo-{index:02d}"', f'path="repo-{index:02d}"'])
            config = root / "brain.toml"
            config.write_text("\n".join(rows) + "\n", encoding="utf-8")
            settings = load_settings(config)
            payload = {
                "CONTEXT_REQUEST": {
                    "version": 1,
                    "objective": "Locate NeedleSymbol and its tests.",
                    "searches": [{"query": "NeedleSymbol", "repos": []} for _ in range(50)],
                    "paths": [],
                    "symbols": [
                        {"name": "NeedleSymbol", "repos": [], "include": ["definition", "callers", "tests"]}
                        for _ in range(10)
                    ],
                    "files": [], "history": [], "expand": [],
                }
            }
            request = parse_context_request(json.dumps(payload))
            bundle = retrieve_context(settings, request)

        self.assertEqual(80, bundle.trace["requested_operations"])
        self.assertEqual(2, bundle.trace["effective_operations"])
        self.assertGreaterEqual(bundle.trace["physical_backend_operations"], 50)
        self.assertLessEqual(bundle.trace["physical_backend_operations"], 56)
        self.assertLessEqual(len(bundle.trace["initial_repo_scope"]), 6)
        self.assertEqual("repo-37", bundle.trace["initial_repo_scope"][0])
        self.assertLessEqual(bundle.trace["candidates_after_prune"], 200)


class ExperienceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for name in ("cache-api", "cache-worker"):
            repo = self.root / name
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "brain@example.invalid"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Project Brain Test"], cwd=repo, check=True)

        api = self.root / "cache-api"
        (api / "src/main/java/demo").mkdir(parents=True)
        (api / "src/test/java/demo").mkdir(parents=True)
        (api / "src/main/java/demo/CachePolicy.java").write_text(
            "final class CachePolicy { int transactionTtlSeconds() { return 30; } }\n", encoding="utf-8"
        )
        (api / "src/test/java/demo/CachePolicyTest.java").write_text(
            "class CachePolicyTest { void transactionExpiryIsConfigured() {} }\n", encoding="utf-8"
        )
        worker = self.root / "cache-worker"
        (worker / "src/main/resources").mkdir(parents=True)
        (worker / "src/main/resources/application.properties").write_text(
            "transaction.cache.ttl=30\nauthorization=Bearer do-not-leak-bearer-value\n", encoding="utf-8"
        )
        (worker / "src/main/resources/credentials.properties").write_text(
            "client.secret=do-not-leak-this-value\n", encoding="utf-8"
        )
        for repo in (api, worker):
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "IPF-101 Extend transaction cache expiry"],
                cwd=repo,
                check=True,
            )

        self.config = self.root / "brain.toml"
        self.config.write_text(
            """[project]
name = "experience-demo"
[graph]
enabled = false
[experience]
enabled = true
commit_limit = 100
similar_cases = 3
patch_chars = 20000
[[repositories]]
name = "cache-api"
path = "cache-api"
[[repositories]]
name = "cache-worker"
path = "cache-worker"
""",
            encoding="utf-8",
        )
        self.settings = load_settings(self.config)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_experience_history_and_query_load_are_globally_bounded(self) -> None:
        self.settings.runs_dir.mkdir(parents=True, exist_ok=True)
        for index in range(20):
            unrelated = self.settings.runs_dir / f"UNRELATED-{index}"
            unrelated.mkdir()
            (unrelated / "ticket.md").write_text("must not be opened", encoding="utf-8")
        selected = self.settings.runs_dir / "IPF-101"
        selected.mkdir()
        (selected / "session.json").write_bytes(
            b'{"ticket":"IPF-101","padding":"'
            + b"x" * (core_module.MAX_SESSION_STATE_BYTES + 1)
            + b'"}'
        )
        (selected / "ticket.md").write_text("oversized state must not be trusted", encoding="utf-8")
        with (
            mock.patch.object(experience_module, "EXPERIENCE_MAX_GLOBAL_COMMITS", 1),
            mock.patch.object(Path, "iterdir", side_effect=AssertionError("unbounded run scan")),
            mock.patch.object(Path, "read_text", side_effect=AssertionError("unbounded text read")),
        ):
            index = build_experience_index(self.settings, changed_only=False)
        self.assertEqual(1, sum(
            len(repository.get("commits") or [])
            for repository in index["repositories"].values()
        ))
        self.assertEqual(1, len(index["cases"][0]["commits"]))

        history = self.settings.state_dir / "ticket-history.json"
        history.write_text(json.dumps({
            "version": experience_module.INDEX_VERSION,
            "repositories": {},
            "cases": [
                {"ticket": "IPF-1", "terms": [], "commits": [], "paths": []},
                {"ticket": "IPF-2", "terms": [], "commits": [], "paths": []},
            ],
        }), encoding="utf-8")
        with mock.patch.object(experience_module, "EXPERIENCE_MAX_CASES", 1):
            self.assertEqual({}, experience_module.load_experience_index(self.settings))
        with mock.patch.object(experience_module, "EXPERIENCE_MAX_ARTIFACT_BYTES", 32):
            self.assertEqual({}, experience_module.load_experience_index(self.settings))

    def test_experience_git_runner_caps_physical_stdout_and_time(self) -> None:
        started = time.monotonic()
        completed = experience_module._run(
            [sys.executable, "-c", "import sys; sys.stdout.write('x' * 10000000)"],
            self.root,
            max_stdout_bytes=4_096,
            timeout=2,
        )
        self.assertNotEqual(0, completed.returncode)
        self.assertLessEqual(len(completed.stdout.encode("utf-8")), 4_096)
        self.assertLess(time.monotonic() - started, 8)

    def test_feedback_evaluates_only_its_bounded_ticket_contexts(self) -> None:
        build_experience_index(self.settings, changed_only=False)
        start_session(self.settings, "IPF-101", "Evaluate only this ticket.")
        state = session_state(self.settings, "IPF-101")
        state["requests"] = 1
        core_module.save_session(self.settings, "IPF-101", state)
        context = session_dir(self.settings, "IPF-101") / "context-001.md"
        context.write_bytes(b"x" * (experience_module.EXPERIENCE_MAX_CONTEXT_BYTES + 1))
        for index in range(20):
            unrelated = self.settings.runs_dir / f"OTHER-{index}"
            unrelated.mkdir()
            (unrelated / "session.json").write_text(
                json.dumps({"ticket": f"OTHER-{index}", "requests": 100}), encoding="utf-8",
            )
        with (
            mock.patch.object(Path, "iterdir", side_effect=AssertionError("global session scan")),
            mock.patch.object(Path, "read_text", side_effect=AssertionError("unbounded context read")),
        ):
            create_feedback(
                self.settings, "IPF-101", notes="bounded evaluation", include_diff=False,
            )

    def test_experience_evaluation_applies_global_context_and_byte_budgets(self) -> None:
        cases = []
        for ticket in ("BUDGET-1", "BUDGET-2"):
            cases.append({
                "ticket": ticket, "paths": ["cache-api:src/Main.java"],
                "repos": ["cache-api"], "test_paths": [],
            })
            directory = session_dir(self.settings, ticket)
            core_module.save_session(self.settings, ticket, {
                "ticket": ticket, "requests": 10, "status": "waiting_for_ai",
            })
            for number in range(1, 11):
                (directory / f"context-{number:03d}.md").write_text(
                    "### 1. cache-api — `src/Main.java:1-1`\n", encoding="utf-8",
                )
        with (
            mock.patch.object(experience_module, "EXPERIENCE_MAX_EVALUATION_CONTEXTS", 3),
            mock.patch.object(experience_module, "EXPERIENCE_MAX_EVALUATION_CONTEXTS_PER_CASE", 2),
            mock.patch.object(experience_module, "EXPERIENCE_MAX_EVALUATION_BYTES", 1_024),
        ):
            report = evaluate_sessions(self.settings, {"cases": cases})
        self.assertEqual(3, report["evaluated_contexts"])
        self.assertGreater(report["skipped_contexts"], 0)
        self.assertGreaterEqual(report["skipped_sessions_at_budget"], 0)

    def test_experience_evaluation_counts_each_context_once(self) -> None:
        ticket = "COUNT-ONCE"
        directory = session_dir(self.settings, ticket)
        core_module.save_session(self.settings, ticket, {
            "ticket": ticket, "requests": 2, "status": "waiting_for_ai",
        })
        for number in (1, 2):
            (directory / f"context-{number:03d}.md").write_text(
                "### 1. cache-api — `src/Main.java:1-1`\n", encoding="utf-8",
            )
        report = evaluate_sessions(self.settings, {"cases": [{
            "ticket": ticket, "paths": ["cache-api:src/Main.java"],
            "repos": ["cache-api"], "test_paths": [],
        }]})
        self.assertEqual(2, report["evaluated_contexts"])

    def test_git_ticket_experience_improves_new_ticket_and_evaluates_retrieval(self) -> None:
        index = build_experience_index(self.settings, changed_only=False)
        self.assertEqual(1, len(index["cases"]))
        case = index["cases"][0]
        self.assertEqual("IPF-101", case["ticket"])
        self.assertEqual({"cache-api", "cache-worker"}, set(case["repos"]))
        self.assertIn("cache-api:src/test/java/demo/CachePolicyTest.java", case["test_paths"])
        self.assertIn("cache-worker:src/main/resources/application.properties", case["config_paths"])

        matches = similar_cases(self.settings, "Increase transaction cache duration")
        self.assertEqual("IPF-101", matches[0]["ticket"])
        self.assertIn("cache", matches[0]["matched_terms"])

        start, _ = start_session(self.settings, "IPF-999", "Increase the transaction cache duration safely.")
        self.assertIn("## Similar ticket history", start)
        self.assertIn("## Historical patch evidence — IPF-101", start)
        self.assertIn("transaction.cache.ttl", start)
        self.assertNotIn("do-not-leak-this-value", start)
        self.assertNotIn("do-not-leak-bearer-value", start)

        self.settings.experience_patch_chars = 0
        safe_start, _ = start_session(self.settings, "IPF-998", "Increase transaction cache duration safely.")
        self.assertIn("## Similar ticket history", safe_start)
        self.assertNotIn("## Historical patch evidence", safe_start)

        start_session(
            self.settings,
            "IPF-101",
            "Reconstruct the completed cache change for the regulatory retention horizon.",
        )
        index = build_experience_index(self.settings, changed_only=True)
        enriched = similar_cases(self.settings, "regulatory retention horizon")
        self.assertEqual("IPF-101", enriched[0]["ticket"])
        self.assertIn("regulatory retention horizon", enriched[0]["ticket_excerpt"])
        (self.root / "knowledge/tickets").mkdir(parents=True)
        (self.root / "knowledge/tickets/IPF-101.md").write_text(
            "Root cause involved a quorum watermark strategy.\n", encoding="utf-8"
        )
        index = build_experience_index(self.settings, changed_only=True)
        self.assertEqual("IPF-101", similar_cases(self.settings, "quorum watermark")[0]["ticket"])
        request = """CONTEXT_REQUEST:
  version: 1
  objective: Locate cache policy, tests, and configuration.
  searches: []
  paths:
    - query: CachePolicy
      repos: [cache-api]
    - query: application.properties
      repos: [cache-worker]
    - query: credentials.properties
      repos: [cache-worker]
  symbols: []
  files: []
  history: []
"""
        context, _, _ = create_context(self.settings, "IPF-101", request)
        self.assertIn("CachePolicyTest.java", context)
        report = evaluate_sessions(self.settings, index)
        evaluation = next(item for item in report["evaluations"] if item["ticket"] == "IPF-101")
        self.assertEqual(1.0, evaluation["repo_recall"])
        self.assertEqual(1.0, evaluation["file_recall"])
        self.assertEqual(1.0, evaluation["test_recall"])
        self.assertEqual(1.0, report["summary"]["file_recall"])
        self.assertTrue((self.root / "generated/EXPERIENCE_REPORT.md").is_file())

    def test_ticket_label_on_merge_commit_uses_first_parent_changes(self) -> None:
        repo = self.root / "cache-api"
        base_branch = subprocess.run(
            ["git", "branch", "--show-current"], cwd=repo, text=True, capture_output=True, check=True
        ).stdout.strip()
        subprocess.run(["git", "checkout", "-q", "-b", "feature/cache"], cwd=repo, check=True)
        source = repo / "src/main/java/demo/MergeOnlyPolicy.java"
        source.write_text("final class MergeOnlyPolicy {}\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "Implement merge-only policy"], cwd=repo, check=True)
        subprocess.run(["git", "checkout", "-q", base_branch], cwd=repo, check=True)
        subprocess.run(
            ["git", "merge", "-q", "--no-ff", "feature/cache", "-m", "Merge feature IPF-202 cache policy"],
            cwd=repo,
            check=True,
        )

        index = build_experience_index(self.settings, changed_only=False)
        case = next(item for item in index["cases"] if item["ticket"] == "IPF-202")
        self.assertIn("cache-api:src/main/java/demo/MergeOnlyPolicy.java", case["paths"])
        rendered = render_similar_cases(self.settings, "IPF-202", include_patches=True)
        self.assertIn("+final class MergeOnlyPolicy", rendered)


class InitTest(unittest.TestCase):
    def test_nested_managed_directory_symlink_is_rejected_before_target_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "target-repository"
            repo.mkdir()
            for key in ("state_dir", "runs_dir", "generated_dir"):
                workspace = root / key
                workspace.mkdir()
                (workspace / "service").mkdir()
                escape = workspace / "escape"
                try:
                    escape.symlink_to(repo, target_is_directory=True)
                except OSError as error:
                    self.skipTest(f"directory symlinks unavailable: {error}")
                config = workspace / "brain.toml"
                config.write_text(
                    "[project]\nname='nested-symlink'\n"
                    f"{key}='escape/{key}'\n"
                    "[graph]\nenabled=false\n"
                    "[[repositories]]\nname='service'\npath='service'\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(BrainError, "escapes its configured location"):
                    load_settings(config)
                self.assertFalse((repo / key).exists())

    def test_refresh_reports_new_repositories_without_mutating_config_and_can_be_opted_out(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "repo-a/.git").mkdir(parents=True)
            config = root / "brain.toml"
            config.write_text(
                '[project]\nname="refresh-discovery"\n[graph]\nenabled=false\n'
                '[[repositories]]\nname="repo-a"\npath="repo-a"\n',
                encoding="utf-8",
            )
            (root / "repo-b/.git").mkdir(parents=True)

            before = config.read_bytes()
            error = io.StringIO()
            with redirect_stderr(error):
                self.assertEqual(2, main(["-c", str(config), "refresh", "--no-fetch"]))
            self.assertIn("require an explicit brain.toml edit", error.getvalue())
            self.assertEqual(before, config.read_bytes())

            (root / "repo-c/.git").mkdir(parents=True)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(0, main(["-c", str(config), "refresh", "--no-fetch", "--no-discover"]))
            self.assertEqual(before, config.read_bytes())

    def test_new_repositories_require_an_explicit_config_edit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "team-a/service"
            second = root / "team-b/service"
            (first / ".git").mkdir(parents=True)
            (second / ".git").mkdir(parents=True)
            (root / "state/ignored/.git").mkdir(parents=True)
            config = root / "brain.toml"
            config.write_text(
                '[project]\nname="discovery"\n[graph]\nenabled=false\n'
                '[[repositories]]\nname="service"\npath="team-a/service"\n'
                'description="Keep this description"\ntags=["existing"]\n',
                encoding="utf-8",
            )

            settings = load_settings(config)
            before = config.read_bytes()
            with self.assertRaisesRegex(BrainError, "automatic config mutation is disabled"):
                discover_and_configure_repositories(settings)
            self.assertEqual(before, config.read_bytes())
            self.assertEqual(["service"], [repo.name for repo in settings.repositories])
            with config.open("a", encoding="utf-8") as output:
                output.write(
                    '\n[[repositories]]\nname="team-b-service"\npath="team-b/service"\n'
                )
            self.assertEqual(
                {"service", "team-b-service"},
                {repo.name for repo in load_settings(config).repositories},
            )

    def test_repository_discovery_never_enters_a_config_mutation_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "repo-a/.git").mkdir(parents=True)
            (root / "repo-b/.git").mkdir(parents=True)
            config = root / "brain.toml"
            config.write_text(
                '[project]\nname="discovery-race"\n[graph]\nenabled=false\n'
                '[[repositories]]\nname="repo-a"\npath="repo-a"\n',
                encoding="utf-8",
            )
            settings = load_settings(config)
            before = config.read_bytes()
            with mock.patch(
                "brain.core.os.write", side_effect=AssertionError("config write boundary reached"),
            ) as wrote, mock.patch(
                "pathlib.Path.replace", side_effect=AssertionError("config replace boundary reached"),
            ) as replaced, self.assertRaisesRegex(BrainError, "automatic config mutation is disabled"):
                discover_and_configure_repositories(settings)
            wrote.assert_not_called()
            replaced.assert_not_called()
            self.assertEqual(before, config.read_bytes())
            self.assertEqual(["repo-a"], [repo.name for repo in settings.repositories])

    def test_repository_discovery_preserves_an_in_place_editor_save_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "repo-a/.git").mkdir(parents=True)
            (root / "repo-b/.git").mkdir(parents=True)
            config = root / "brain.toml"
            config.write_text(
                '[project]\nname="discovery-boundary"\n[graph]\nenabled=false\n'
                '[[repositories]]\nname="repo-a"\npath="repo-a"\n',
                encoding="utf-8",
            )
            settings = load_settings(config)
            user_save = config.read_bytes() + b'# in-place editor save\n'
            config.write_bytes(user_save)
            with self.assertRaisesRegex(BrainError, "automatic config mutation is disabled"):
                discover_and_configure_repositories(settings)
            self.assertEqual(user_save, config.read_bytes())
            self.assertEqual(["repo-a"], [repo.name for repo in settings.repositories])

    def test_repository_discovery_never_follows_a_predictable_config_temp_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "repo-a/.git").mkdir(parents=True)
            (root / "repo-b/.git").mkdir(parents=True)
            config = root / "brain.toml"
            config.write_text(
                '[project]\nname="discovery-symlink"\n[graph]\nenabled=false\n'
                '[[repositories]]\nname="repo-a"\npath="repo-a"\n',
                encoding="utf-8",
            )
            outside = root / "outside.txt"
            outside.write_text("outside marker\n", encoding="utf-8")
            (root / "brain.toml.tmp").symlink_to(outside)

            before = config.read_bytes()
            with self.assertRaisesRegex(BrainError, "automatic config mutation is disabled"):
                discover_and_configure_repositories(load_settings(config))
            self.assertEqual("outside marker\n", outside.read_text(encoding="utf-8"))
            self.assertFalse(config.is_symlink())
            self.assertEqual(before, config.read_bytes())
            self.assertEqual({"repo-a"}, {repo.name for repo in load_settings(config).repositories})

    def test_demo_creates_a_complete_four_repo_investigation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "project-brain-demo"
            output = io.StringIO()
            with redirect_stdout(output):
                with mock.patch("brain.graph.find_backend", return_value=None):
                    code = main(["demo", str(target)])
            self.assertEqual(0, code)
            self.assertTrue((target / "brain.toml").is_file())
            self.assertTrue((target / "ticket.md").is_file())
            self.assertTrue((target / "trading-service/src/main/java/demo/CustomerChangedListener.java").is_file())
            relationships = (target / "generated/PROJECT_RELATIONSHIPS.md").read_text(encoding="utf-8")
            self.assertIn("customer-service → trading-service", relationships)
            self.assertIn("trading-service → risk-service", relationships)
            self.assertIn("brain ui", output.getvalue())
            self.assertFalse(load_settings(target / "brain.toml").graph_enabled)

    def test_init_discovers_all_nested_git_repositories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "payments/customer-service/.git").mkdir(parents=True)
            (root / "trading/risk-service/.git").mkdir(parents=True)
            previous = Path.cwd()
            try:
                os.chdir(root)
                with mock.patch("brain.graph.find_backend", return_value=None):
                    code = main(["init", "--name", "auto-demo"])
            finally:
                os.chdir(previous)
            config = (root / "brain.toml").read_text(encoding="utf-8")
            self.assertEqual(0, code)
            self.assertIn('name = "customer-service"', config)
            self.assertIn('path = "payments/customer-service"', config)
            self.assertIn('name = "risk-service"', config)
            self.assertIn('path = "trading/risk-service"', config)

    def test_init_creates_portable_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo-a"
            repo.mkdir()
            (repo / "service.py").write_text("class Service: pass\n", encoding="utf-8")
            git = native_command("git")
            subprocess.run([git, "init", "-q"], cwd=repo, check=True)
            subprocess.run([git, "config", "user.email", "brain@example.invalid"], cwd=repo, check=True)
            subprocess.run([git, "config", "user.name", "Project Brain"], cwd=repo, check=True)
            subprocess.run([git, "add", "service.py"], cwd=repo, check=True)
            subprocess.run([git, "commit", "-qm", "fixture"], cwd=repo, check=True)
            before = subprocess.run(
                [git, "status", "--porcelain"], cwd=repo, text=True, capture_output=True, check=True,
            ).stdout
            previous = Path.cwd()
            output = io.StringIO()
            try:
                os.chdir(root)
                with redirect_stdout(output):
                    with mock.patch("brain.graph.find_backend", return_value=None):
                        code = main(["init", str(repo), "--name", "portable-demo", "--no-fetch"])
            finally:
                os.chdir(previous)
            self.assertEqual(0, code)
            self.assertTrue((root / "brain.toml").is_file())
            self.assertTrue((root / "knowledge/PROJECT_MAP.md").is_file())
            self.assertFalse((repo / "brain.toml").exists())
            after = subprocess.run(
                [git, "status", "--porcelain"], cwd=repo, text=True, capture_output=True, check=True,
            ).stdout
            self.assertEqual(before, after)
            config = (root / "brain.toml").read_text(encoding="utf-8")
            self.assertIn('path = "repo-a"', config)
            self.assertIn("[experience]", config)
            self.assertIn("patch_chars = 0", config)
            self.assertIn("minimum_free_disk_gb = 5", config)
            self.assertEqual(0, load_settings(root / "brain.toml").experience_patch_chars)
            self.assertEqual(5, load_settings(root / "brain.toml").minimum_free_disk_gb)


class GitSyncTest(unittest.TestCase):
    def _git(self, cwd: Path, *args: str) -> str:
        result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True)
        return result.stdout.strip()

    def test_freshness_detects_selected_local_ref_advancing_after_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "service"
            repository.mkdir()
            self._git(repository, "init", "-b", "main")
            self._git(repository, "config", "user.name", "Test")
            self._git(repository, "config", "user.email", "test@example.invalid")
            source = repository / "service.py"
            source.write_text("VALUE = 'G1'\n", encoding="utf-8")
            self._git(repository, "add", "service.py")
            self._git(repository, "commit", "-m", "G1")
            config = root / "brain.toml"
            config.write_text(
                "[project]\nname='freshness-ref'\n[graph]\nenabled=false\n"
                "[[repositories]]\nname='service'\npath='service'\nbranch='main'\n",
                encoding="utf-8",
            )
            settings = load_settings(config)
            refresh_brain(settings, fetch=False, discover=False)
            published = current_generation(settings)
            self.assertIsNotNone(published)
            source.write_text("VALUE = 'G2'\n", encoding="utf-8")
            self._git(repository, "add", "service.py")
            self._git(repository, "commit", "-m", "G2")

            reloaded = load_settings(config)
            row = freshness(reloaded)["repositories"][0]
            self.assertFalse(row["current"])
            self.assertNotEqual(row["source_sha"], row["index_sha"])
            self.assertNotEqual("Healthy", dashboard_status(reloaded)["health"])

    def test_sync_prefers_develop_and_allows_feature_override_without_touching_local_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            origin = root / "origin.git"
            seed = root / "seed"
            work = root / "service-a"
            self._git(root, "init", "--bare", str(origin))
            seed.mkdir()
            self._git(seed, "init", "-b", "main")
            self._git(seed, "config", "user.name", "Test")
            self._git(seed, "config", "user.email", "test@example.invalid")
            (seed / "value.txt").write_text("old remote\n", encoding="utf-8")
            self._git(seed, "add", "value.txt")
            self._git(seed, "commit", "-m", "initial")
            self._git(seed, "remote", "add", "origin", str(origin))
            self._git(seed, "push", "-u", "origin", "main")
            self._git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
            self._git(root, "clone", str(origin), str(work))

            (work / "value.txt").write_text("local uncommitted\n", encoding="utf-8")
            self._git(seed, "checkout", "-b", "develop")
            (seed / "value.txt").write_text("develop remote\n", encoding="utf-8")
            self._git(seed, "add", "value.txt")
            self._git(seed, "commit", "-m", "develop update")
            self._git(seed, "push", "-u", "origin", "develop")
            self._git(seed, "checkout", "-b", "feature/ABC-123")
            (seed / "value.txt").write_text("feature remote\n", encoding="utf-8")
            self._git(seed, "add", "value.txt")
            self._git(seed, "commit", "-m", "feature update")
            self._git(seed, "push", "-u", "origin", "feature/ABC-123")

            config = root / "brain.toml"
            config.write_text(
                '[project]\nname="sync-test"\n[[repositories]]\nname="service-a"\npath="service-a"\n',
                encoding="utf-8",
            )
            settings = load_settings(config)
            results = sync_repositories(settings)
            self.assertEqual("current", results[0].status)
            self.assertEqual("origin/develop", results[0].ref)
            self.assertEqual("develop remote\n", (settings.repo("service-a").scan_path / "value.txt").read_text(encoding="utf-8"))

            self._git(work, "update-ref", "-d", "refs/remotes/origin/feature/ABC-123")
            results = sync_repositories(settings, branch_overrides={"service-a": "feature/ABC-123"})
            self.assertEqual("origin/feature/ABC-123", results[0].ref)
            self.assertEqual("feature remote\n", (settings.repo("service-a").scan_path / "value.txt").read_text(encoding="utf-8"))

            settings.repo("service-a").branch = "main"
            results = sync_repositories(settings)
            self.assertEqual("origin/main", results[0].ref)
            self.assertEqual("old remote\n", (settings.repo("service-a").scan_path / "value.txt").read_text(encoding="utf-8"))
            self.assertEqual("local uncommitted\n", (work / "value.txt").read_text(encoding="utf-8"))
            self.assertEqual("main", self._git(work, "branch", "--show-current"))

    def test_ssh_authentication_failure_is_not_retried_for_every_repo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = ['[project]\nname="ssh-sync-test"']
            for name in ("service-a", "service-b", "service-c"):
                repo = root / name
                repo.mkdir()
                self._git(repo, "init", "-b", "main")
                self._git(repo, "config", "user.name", "Test")
                self._git(repo, "config", "user.email", "test@example.invalid")
                (repo / "value.txt").write_text(name + "\n", encoding="utf-8")
                self._git(repo, "add", "value.txt")
                self._git(repo, "commit", "-m", "initial")
                self._git(repo, "remote", "add", "origin", f"git@example.test:team/{name}.git")
                self._git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
                self._git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
                rows.append(f'[[repositories]]\nname="{name}"\npath="{name}"')
            config = root / "brain.toml"
            config.write_text("\n".join(rows) + "\n", encoding="utf-8")
            settings = load_settings(config)
            fetch_environments: list[dict[str, str] | None] = []
            original_git = sync_module._git

            def fake_git(repo, *args, binary=False, extra_env=None, timeout=120):
                if args and args[0] == "fetch":
                    fetch_environments.append(extra_env)
                    return subprocess.CompletedProcess(["git", *args], 128, "", "Permission denied (publickey).")
                return original_git(repo, *args, binary=binary, extra_env=extra_env, timeout=timeout)

            with mock.patch("brain.sync._git", side_effect=fake_git):
                results = sync_repositories(settings)

            self.assertEqual(1, len(fetch_environments))
            if os.name == "nt":
                self.assertIsNone(fetch_environments[0])
            else:
                self.assertIn("ControlMaster=auto", fetch_environments[0]["GIT_SSH_COMMAND"])
            self.assertEqual(2, sum("skipped another interactive attempt" in (result.warning or "") for result in results))
            self.assertTrue(all("example.test" not in (result.warning or "") for result in results))

            if os.name == "nt":
                return

            fetch_environments.clear()
            for repo in settings.repositories:
                self._git(repo.path, "config", "core.sshCommand", "ssh -F ~/.ssh/company-config")

            def successful_git(repo, *args, binary=False, extra_env=None, timeout=120):
                if args and args[0] == "fetch":
                    fetch_environments.append(extra_env)
                    return subprocess.CompletedProcess(["git", *args], 0, "", "")
                return original_git(repo, *args, binary=binary, extra_env=extra_env, timeout=timeout)

            with mock.patch("brain.sync.sys.platform", "darwin"):
                with mock.patch("brain.sync._git", side_effect=successful_git):
                    sync_repositories(settings)

            commands = [environment["GIT_SSH_COMMAND"] for environment in fetch_environments]
            self.assertEqual(3, len(commands))
            self.assertEqual(1, sum("BatchMode=yes" not in command for command in commands))
            self.assertEqual(2, sum("BatchMode=yes" in command for command in commands))
            self.assertTrue(all("UseKeychain=no" in command for command in commands))
            self.assertTrue(all("ssh -F ~/.ssh/company-config" in command for command in commands))

            started = time.monotonic()
            timed_out = original_git(
                settings.repositories[0],
                "-c",
                "alias.wait=!sleep 5",
                "wait",
                timeout=0.05,
            )
            self.assertEqual(124, timed_out.returncode)
            self.assertLess(time.monotonic() - started, 1)

    def test_ssh_remote_detection(self) -> None:
        self.assertEqual("git@github.com", _ssh_endpoint("git@github.com:team/project.git"))
        self.assertEqual("git@example.test:2222", _ssh_endpoint("ssh://git@example.test:2222/team/project.git"))
        self.assertIsNone(_ssh_endpoint("https://github.com/team/project.git"))
        self.assertIsNone(_ssh_endpoint("/tmp/origin.git"))


class ReleaseSafetyTest(unittest.TestCase):
    def test_cli_version_matches_distribution_metadata(self) -> None:
        from brain import __version__

        root = Path(__file__).resolve().parents[1]
        metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(metadata["project"]["version"], __version__)

    def test_cross_platform_fixture_exercises_source_and_rejects_parity_drift(self) -> None:
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            "cross_platform_fixture", root / "scripts/cross_platform_fixture.py",
        )
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            first = directory / "parity-macos-arm64.json"
            module.exercise(Path(sys.executable), "macos-arm64", first, source=True)
            report = json.loads(first.read_text(encoding="utf-8"))
            second = directory / "parity-windows-amd64.json"
            module.exercise(Path(sys.executable), "windows-amd64", second, source=True)
            second_report = json.loads(second.read_text(encoding="utf-8"))
            self.assertEqual(report["result"], second_report["result"])
            self.assertTrue(report["result"]["entity_ids"])
            self.assertTrue(report["result"]["routing"]["repositories"])
            self.assertTrue(report["result"]["verified_evidence"])
            self.assertTrue(all(item["content_hash"] for item in report["result"]["verified_evidence"]))
            self.assertTrue(any(flow["steps"] for flow in report["result"]["flows"].values()))
            for platform in sorted(module.PLATFORMS - {"macos-arm64", "windows-amd64"}):
                (directory / f"parity-{platform}.json").write_text(
                    json.dumps({"platform": platform, "result": report["result"]}), encoding="utf-8",
                )
            module.compare(directory)
            drifted = directory / "parity-windows-amd64.json"
            drift = json.loads(drifted.read_text(encoding="utf-8"))
            drift["result"]["verified_evidence"][0]["content_hash"] = "0" * 64
            drifted.write_text(json.dumps(drift), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "deterministic behavior differs"):
                module.compare(directory)

    def test_homebrew_formula_is_rendered_only_from_final_release_checksums(self) -> None:
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("update_homebrew_formula", root / "scripts/update_homebrew_formula.py")
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        spec.loader.exec_module(module)
        values = {
            "project-brain-v9.9.9-macos-arm64.tar.gz": "a" * 64,
            "project-brain-v9.9.9-macos-amd64.tar.gz": "b" * 64,
            "project-brain-v9.9.9-linux-arm64.tar.gz": "c" * 64,
            "project-brain-v9.9.9-linux-amd64.tar.gz": "d" * 64,
        }
        formula = module.render("9.9.9", values)
        self.assertIn("v9.9.9/project-brain-v9.9.9-macos-arm64.tar.gz", formula)
        self.assertIn('sha256 "a" * 64', formula.replace('"' + "a" * 64 + '"', '"a" * 64'))
        self.assertNotIn("bottle do", formula)
        rc_values = {name.replace("v9.9.9", "v9.9.9-rc2"): digest for name, digest in values.items()}
        rc_formula = module.render("9.9.9-rc2", rc_values, release_candidate=True)
        self.assertIn("class ProjectBrainRc < Formula", rc_formula)
        self.assertIn('version "9.9.9-rc2"', rc_formula)
        self.assertIn('conflicts_with "project-brain"', rc_formula)
        self.assertIn("v9.9.9-rc2/project-brain-v9.9.9-rc2-macos-arm64.tar.gz", rc_formula)
        with self.assertRaisesRegex(ValueError, "missing standalone"):
            module.render("9.9.9", {})
        workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn("needs: github-release", workflow)
        self.assertIn(
            "uses: actions/upload-artifact@v7\n"
            "        with:\n"
            "          name: final-release-checksums\n"
            "          path: dist/SHA256SUMS.txt",
            workflow,
        )
        self.assertIn(
            "uses: actions/download-artifact@v8\n"
            "        with:\n"
            "          name: final-release-checksums\n"
            "          path: dist/",
            workflow,
        )
        self.assertIn("cmp published/SHA256SUMS.txt dist/SHA256SUMS.txt", workflow)
        self.assertIn("HOMEBREW_TAP_TOKEN", workflow)
        self.assertIn("Check tap authorization", workflow)
        self.assertIn("steps.authorization.outputs.available == 'true'", workflow)
        self.assertIn("runs-on: macos-14", workflow)
        self.assertIn("brew install --formula \"$FORMULA\"", workflow)
        self.assertIn("brain --version", workflow)
        self.assertIn("brain --help >/dev/null", workflow)
        self.assertIn("brew test \"$FORMULA\"", workflow)
        self.assertNotIn("if: ${{ secrets.HOMEBREW_TAP_TOKEN", workflow)
        self.assertIn("--prerelease", workflow)
        self.assertIn("--draft --prerelease", workflow)
        self.assertIn('gh release download "$GITHUB_REF_NAME" --dir release-verification', workflow)
        self.assertIn("sha256sum -c SHA256SUMS.txt", workflow)
        self.assertIn('gh attestation verify "$asset" --repo "$GH_REPO"', workflow)
        self.assertIn('gh release edit "$GITHUB_REF_NAME" --draft=false --latest', workflow)
        self.assertIn("Formula/project-brain-rc.rb", workflow)
        self.assertIn("--release-candidate", workflow)
        self.assertIn("!contains(github.ref_name, '-')", workflow)

    def test_semantic_pack_workflow_pins_official_inputs_and_never_bundles_core(self) -> None:
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/semantic-pack.yml").read_text(encoding="utf-8")
        self.assertIn("Qwen/Qwen3-Embedding-4B-GGUF/resolve/$QWEN_GGUF_REVISION/Qwen3-Embedding-4B-Q6_K.gguf", workflow)
        self.assertIn("0c04b2b5e9b039dd01fd1e6d757968855fd5e2523bb3e9a4a03fa6454973a1af", workflow)
        self.assertIn("QWEN_TOKENIZER_SHA256", workflow)
        self.assertIn("semantic-pack-v*", workflow)
        self.assertNotIn("release.yml", workflow)
        self.assertIn("--minimum-brain-version 0.6.2", workflow)
        self.assertIn("-DBUILD_SHARED_LIBS=OFF", workflow)
        self.assertIn("-DLLAMA_OPENSSL=OFF", workflow)
        self.assertIn("otool -L", workflow)

    def test_official_catalog_pins_the_cross_machine_qualified_semantic_release(self) -> None:
        self.assertEqual(
            {
                "semantic": {
                    "darwin-arm64": {
                        "pack_id": "qwen3-embedding-4b-q6k-darwin-arm64",
                        "descriptor_url": "https://github.com/superorange0707/project-brain/releases/download/semantic-pack-v1.0.6/qwen3-embedding-4b-q6k-darwin-arm64-descriptor.json",
                        "descriptor_sha256": "cbd09af575fb1b2e036abc17ed3e693e5bab4807af19efd2c1a9b5cd75ae8afc",
                    },
                    "windows-amd64": {
                        "pack_id": "qwen3-embedding-4b-q6k-windows-amd64",
                        "descriptor_url": "https://github.com/superorange0707/project-brain/releases/download/semantic-pack-windows-v1.0.0/qwen3-embedding-4b-q6k-windows-amd64-descriptor.json",
                        "descriptor_sha256": "69ca378fc2a00f01b23ae047ab46a7137c1b952d3c07a478350aaf2e2c6e2a30",
                    },
                },
                "precision": {
                    "darwin-arm64": {
                        "pack_id": "qwen3-reranker-4b-q6k-darwin-arm64",
                        "descriptor_url": "https://github.com/superorange0707/project-brain/releases/download/precision-pack-v1.0.3/qwen3-reranker-4b-q6k-darwin-arm64-descriptor.json",
                        "descriptor_sha256": "f780010c883b9ded459f9e4190a262ee76b6a6e9fc20f9e47ab9a1452b438742",
                    },
                    "windows-amd64": {
                        "pack_id": "qwen3-reranker-4b-q6k-windows-amd64",
                        "descriptor_url": "https://github.com/superorange0707/project-brain/releases/download/precision-pack-windows-v1.0.0/qwen3-reranker-4b-q6k-windows-amd64-descriptor.json",
                        "descriptor_sha256": "524ac460c07b55891029b1de54120c47664969cdc985df713c19957657150d59",
                    },
                },
            },
            OFFICIAL_PACKS,
        )

    def test_semantic_pack_builder_uses_a_strong_cross_machine_reference_threshold(self) -> None:
        builder = (Path(__file__).resolve().parents[1] / "scripts/build_semantic_pack.py").read_text(encoding="utf-8")
        self.assertIn("MINIMUM_REFERENCE_COSINE = 0.995", builder)
        self.assertIn('"minimum_cosine_to_reference": MINIMUM_REFERENCE_COSINE', builder)

    def test_precision_pack_workflow_uses_official_source_conversion_and_local_conformance(self) -> None:
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/precision-pack.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn('tags: ["precision-pack-v*"]', workflow)
        self.assertIn('python-version: "3.12"', workflow)
        self.assertIn("Qwen/Qwen3-Reranker-4B/resolve/$QWEN_REVISION/model-00001-of-00002.safetensors", workflow)
        self.assertIn("cf2e87cbf71fa628961532232e04dd6c19702a0a057f5e2aff95ea1aca4fd488", workflow)
        self.assertIn("78946d22b7f6456ea7a5358dbdf3982de36c5bac1f166a5fd58e18e31db8048a", workflow)
        self.assertIn("d775b8967a46d8beb110d444aa3b8938179e0dd8", workflow)
        self.assertIn("convert_hf_to_gguf.py", workflow)
        self.assertIn("llama-quantize", workflow)
        self.assertIn("Q6_K", workflow)
        self.assertIn("qwen3_reranker_reference.py", workflow)
        self.assertIn("build_precision_pack.py", workflow)
        self.assertIn('MACOSX_DEPLOYMENT_TARGET: "15.0"', workflow)
        self.assertIn('-DCMAKE_OSX_DEPLOYMENT_TARGET="$MACOSX_DEPLOYMENT_TARGET"', workflow)
        self.assertIn("otool -l", workflow)
        self.assertNotIn("vtool -show-build", workflow)
        self.assertIn("model install precision-pack-dist/qwen3-reranker-4b-q6k-darwin-arm64", workflow)
        self.assertIn("model verify qwen3-reranker-4b-q6k-darwin-arm64", workflow)
        self.assertNotIn("release.yml", workflow)
        builder = (root / "scripts/build_precision_pack.py").read_text(encoding="utf-8")
        self.assertIn('RERANK_INSTRUCTION = "Given a web search query, retrieve relevant passages that answer the query"', builder)
        self.assertIn('"reranker_candidate_pools": [10, 20, 40, 80]', builder)
        self.assertIn('"reranker_physical_batch_size": RERANK_CONFORMANCE_DOCUMENT_BATCH_SIZE', builder)
        self.assertIn("timeout=RERANK_CONFORMANCE_REQUEST_TIMEOUT_SECONDS", builder)
        self.assertIn("running Precision conformance case", builder)
        self.assertIn("timed out after {RERANK_CONFORMANCE_REQUEST_TIMEOUT_SECONDS} seconds", builder)
        self.assertIn('"verification_request_timeout_seconds": RERANK_CONFORMANCE_REQUEST_TIMEOUT_SECONDS', builder)
        self.assertIn("log.read_text(encoding=\"utf-8\", errors=\"replace\")[-6000:]", builder)
        self.assertIn("RERANKER_BATCH_PARITY_TOLERANCE = 2e-3", builder)
        self.assertIn('"batch_single_max_delta": parity', builder)
        self.assertIn('"batch_single_parity_indices": _parity_indices', builder)
        self.assertIn("MAXIMUM_REFERENCE_SCORE_DELTA = 0.10", builder)
        self.assertIn("RERANK_PHYSICAL_BATCH_TOKENS = RERANK_CONTEXT_TOKENS", builder)
        self.assertIn('"-ub", str(RERANK_PHYSICAL_BATCH_TOKENS)', builder)
        self.assertIn('"expected_top_index": int(case["expected_top_index"])', builder)
        reference = (root / "scripts/qwen3_reranker_reference.py").read_text(encoding="utf-8")
        self.assertIn("official Qwen", reference)
        self.assertIn("RERANK_CONTEXT_TOKENS", reference)

    def test_darwin_precision_runtime_repack_reuses_pinned_model_and_targets_macos_15(self) -> None:
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/precision-pack-darwin.yml").read_text(encoding="utf-8")
        self.assertIn("precision-pack-vX.Y.Z", workflow)
        self.assertIn("9070626e90b0306237bdf208ce0991cbf3804ee1bbee4ddca28c93df288f7df7", workflow)
        self.assertIn("2fd4a7bbb61400e65bb3849f8d367759232be2206e1bb467b2b3d7ff42e79aeb", workflow)
        self.assertIn('MACOSX_DEPLOYMENT_TARGET: "15.0"', workflow)
        self.assertIn('-DCMAKE_OSX_DEPLOYMENT_TARGET="$MACOSX_DEPLOYMENT_TARGET"', workflow)
        self.assertIn("otool -l", workflow)
        self.assertNotIn("vtool -show-build", workflow)
        self.assertIn("build_precision_pack.py", workflow)
        self.assertNotIn("model verify qwen3-reranker-4b-q6k-darwin-arm64", workflow)
        self.assertIn("runtime_for_pack(verification_manifest, verification=True)", workflow)
        self.assertIn("clean-installed Precision runtime integrity and inference smoke passed", workflow)
        self.assertIn("gh release create", workflow)
        self.assertIn("gh release download", workflow)
        self.assertIn("shasum -a 256 --check SHA256SUMS.txt", workflow)
        self.assertLess(
            workflow.index('gh release edit "$RELEASE_TAG" --draft=false --latest=false'),
            workflow.index('gh api "repos/$GH_REPO/git/ref/tags/$RELEASE_TAG" --jq .object.type'),
        )
        self.assertIn('--json targetCommitish --jq .targetCommitish', workflow)
        self.assertIn("timeout-minutes: 300", workflow)

    def test_standalone_release_builds_source_pinned_zoekt(self) -> None:
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn("timeout-minutes: 180", workflow)
        self.assertIn('python-version: ["3.11", "3.12", "3.13", "3.14"]', workflow)
        self.assertIn(
            "needs: [compatibility, windows-compatibility, v1-release-readiness, build, standalone, standalone-windows, cross-platform-parity]",
            workflow,
        )
        self.assertIn('go-version: "1.24.7"', workflow)
        self.assertIn("github.com/sourcegraph/zoekt/cmd/zoekt@$ZOEKTVERSION", workflow)
        self.assertIn("github.com/sourcegraph/zoekt/cmd/zoekt-index@$ZOEKTVERSION", workflow)
        self.assertIn("v0.0.0-20251202141441-886b229dcd5e", workflow)
        self.assertIn("886b229dcd5e7bec0c9918002b77345d27c84e3c", workflow)
        self.assertIn("scripts/windows/zoekt-windows-amd64.patch", workflow)
        self.assertIn("zoekt-bin/zoekt zoekt-bin/zoekt-index package/", workflow)
        self.assertNotIn("sourcegraph/zoekt/cmd/zoekt@latest", workflow)
        self.assertIn(
            "github-release:\n"
            "    name: Publish GitHub release\n"
            "    needs: [compatibility, windows-compatibility, v1-release-readiness, build, standalone, standalone-windows, cross-platform-parity]\n",
            workflow,
        )
        self.assertIn('gh release create "$GITHUB_REF_NAME" dist/* --verify-tag --notes-file RELEASE_NOTES.md --title "Project Brain $GITHUB_REF_NAME" --draft', workflow)
        self.assertIn("Publish only the verified release", workflow)
        self.assertIn("    steps:\n      - uses: actions/checkout@v7\n        with:\n          persist-credentials: false\n      - uses: actions/download-artifact@v8", workflow)

    def test_repository_contains_no_credential_or_private_path_material(self) -> None:
        root = Path(__file__).resolve().parents[1]
        excluded = {".git", ".venv", "build", "dist", "__pycache__"}
        patterns = {
            "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
            "GitHub token": re.compile(r"gh" + r"[pousr]_[A-Za-z0-9]{20,}"),
            "GitHub fine-grained token": re.compile(r"github" + r"_pat_[A-Za-z0-9_]{20,}"),
            "PyPI token": re.compile(r"pypi" + r"-AgEIcHlwaS[A-Za-z0-9_-]{20,}"),
            "OpenAI-style key": re.compile(r"s" + r"k-[A-Za-z0-9_-]{20,}"),
            "AWS access key": re.compile(r"AKIA[0-9A-Z]{16}"),
            "macOS home path": re.compile(r"/" + r"Users/[A-Za-z0-9._-]+/"),
            "Windows home path": re.compile(r"[A-Za-z]:\\Users\\[^\\]+\\"),
        }
        failures: list[str] = []
        for path in root.rglob("*"):
            if not path.is_file() or any(part in excluded or part.endswith(".egg-info") for part in path.parts):
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for label, pattern in patterns.items():
                if pattern.search(content):
                    failures.append(f"{path.relative_to(root)}: {label}")
        self.assertEqual([], failures)


if __name__ == "__main__":
    unittest.main()

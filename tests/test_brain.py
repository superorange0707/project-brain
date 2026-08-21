from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import os
import re
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from brain.cli import main
from brain.agent import archive_final_solution, create_m365_agent_kit, response_preview
from brain.core import (
    BrainError,
    add_external_evidence,
    chunk_text,
    create_context,
    create_feedback,
    deliver,
    discover_and_configure_repositories,
    generate_map,
    load_settings,
    load_index_state,
    path_hits,
    parse_context_request,
    request_preview,
    request_repair_prompt,
    read_source,
    retrieve_context,
    SearchHit,
    search,
    session_state,
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
from brain.metrics import benchmark_report, machine_profile
from brain.metrics import trace_metadata
from brain.catalog import current_generation
from brain.catalog import connect as catalog_connect
from brain.editions import current_edition, set_edition
from brain.editions import capabilities
from brain.models import EMBEDDING_BATCH_PARITY_TOLERANCE, DeterministicRuntime, LlamaCppRuntime, ManagedLlamaCppRuntime, OFFICIAL_PACKS, _same_vectors, rerank_candidates
from brain.models import autotune_pack, benchmark_pack, install_pack, install_pack_url, install_release_descriptor, runtime_for_pack, validate_manifest, verify_pack
from brain.semantic import CARD_VERSION, CHUNK_SCHEMA_VERSION, Chunk, SEMANTIC_CARD_CODE_CHARS, _bounded_embedding_batches, _excluded, build_semantic_index, chunk_source, search_semantic
from brain.ops import gc
from brain.evaluation import evaluate_golden


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
        relationships = generate_relationship_map(self.settings)
        self.assertIn("customer-service → trading-service", relationships)
        self.assertIn("trading-service → risk-service", relationships)
        self.assertIn("`KAFKA` `customer.updated`", relationships)
        self.assertIn("`HTTP` `GET /risk/restrictions/{}`", relationships)
        self.assertIn("customer-service --KAFKA:customer.updated--> trading-service --HTTP:GET /risk/restrictions/{}--> risk-service", relationships)
        cached = json.loads((self.root / "state/relationships.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(cached["relationships"]), 3)

    def test_each_repo_gets_its_own_search_result_budget(self) -> None:
        self.settings.max_results = 1
        hits = search(self.settings, "class", fixed=True)
        self.assertEqual({"customer-service", "trading-service", "risk-service", "batch-service"}, {hit.repo for hit in hits})

    def test_structural_backend_json_is_used_when_available(self) -> None:
        backend = self.root / "fake-backend"
        backend.write_text(
            '''#!/usr/bin/env python3
import json, sys
if "--version" in sys.argv:
    print("codebase-memory-mcp 0.10.5")
else:
    print(json.dumps({"cols": ["qn", "label", "file", "lines", "in", "out"], "rows": [["demo.EligibilityEvaluator", "Interface", "src/main/java/demo/EligibilityEvaluator.java", "2-2", 0, 1]]}))
''',
            encoding="utf-8",
        )
        backend.chmod(0o755)
        self.settings.graph_enabled = True
        with mock.patch.dict(os.environ, {"PROJECT_BRAIN_GRAPH_BIN": str(backend)}):
            indexed = index_graph(self.settings, changed_only=False)
            hits = graph_symbol_hits(self.settings, "EligibilityEvaluator", ["trading-service"])
        self.assertTrue(all(item.status == "indexed" for item in indexed))
        self.assertEqual("EligibilityEvaluator.java", Path(hits[0].path).name)
        self.assertIn("structural graph", hits[0].found_by[0])

    def test_structural_index_is_deferred_then_built_for_the_relevant_repo(self) -> None:
        backend = self.root / "lazy-backend"
        backend.write_text(
            '''#!/usr/bin/env python3
import json, sys
if "--version" in sys.argv:
    print("codebase-memory-mcp 0.10.5")
else:
    print(json.dumps({"cols": ["qn", "label", "file", "lines"], "rows": [["demo.EligibilityEvaluator", "Interface", "src/main/java/demo/EligibilityEvaluator.java", "2-2"]]}))
''',
            encoding="utf-8",
        )
        backend.chmod(0o755)
        self.settings.graph_enabled = True
        with mock.patch.dict(os.environ, {"PROJECT_BRAIN_GRAPH_BIN": str(backend)}):
            deferred = index_graph(self.settings, defer_lazy=True)
            self.assertEqual("deferred", deferred[0].status)
            self.assertFalse((self.settings.state_dir / "graphs.json").exists())
            hits = symbol_hits(self.settings, "EligibilityEvaluator", ["trading-service"])
        self.assertTrue((self.settings.state_dir / "graphs.json").is_file())
        self.assertTrue(any("structural graph" in " ".join(hit.found_by) for hit in hits))

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
        final = response_preview("FINAL_SOLUTION\nChange the listener.", self.settings, "ABC-ROUTE")
        self.assertEqual("final_solution", final["kind"])
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

        final = response_preview(REQUEST + "\nFINAL_SOLUTION\nChange the cache configuration.", self.settings)
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

        final_text = "FINAL_SOLUTION\n\nChange `CustomerChangedListener.java`."
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
        symlink.symlink_to(document)
        with self.assertRaisesRegex(BrainError, "must not be a symlink"):
            add_external_evidence(self.settings, "ABC-DOC", symlink)

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
        self.assertEqual([[0], [1, 2], [3]], list(_bounded_embedding_batches(chunks, [0, 1, 2, 3], 8)))
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
        with redirect_stdout(output):
            self.assertEqual(0, main(["-c", str(self.config), "model", "autotune", "test-embedding", "--samples", "1"]))
        self.assertEqual("test-embedding", json.loads(output.getvalue())["pack_id"])

    def test_embedding_batch_parity_allows_only_bounded_runtime_rounding_drift(self) -> None:
        self.assertEqual(1e-4, EMBEDDING_BATCH_PARITY_TOLERANCE)
        self.assertTrue(_same_vectors([[0.0]], [[6.2e-5]], tolerance=EMBEDDING_BATCH_PARITY_TOLERANCE))
        self.assertFalse(_same_vectors([[0.0]], [[1.1e-4]], tolerance=EMBEDDING_BATCH_PARITY_TOLERANCE))

    def test_remote_pack_install_requires_pinned_approved_source(self) -> None:
        with self.assertRaisesRegex(ValueError, "not approved"):
            install_pack_url(self.settings, "https://example.invalid/pack.tar", "a" * 64)
        with self.assertRaisesRegex(ValueError, "--sha256"):
            install_pack_url(self.settings, "https://github.com/example/project/releases/download/v1/pack.tar", "not-a-digest")
        profile = machine_profile(self.settings)
        self.assertNotIn("hostname", profile)
        self.assertIn("logical_cpu_count", profile)

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

        class Response(io.BytesIO):
            def __init__(self, url: str, content: bytes):
                super().__init__(content)
                self.url = url
                self.headers = {"Content-Length": str(len(content))}

            def geturl(self) -> str:
                return self.url

            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.close()

        def download(request, timeout=0):
            return Response(request.full_url, payloads[request.full_url])

        descriptor_url = "https://github.com/example/project/releases/download/v1/descriptor.json"
        with mock.patch("brain.models.urlopen", side_effect=download):
            installed = install_release_descriptor(self.settings, descriptor_url, hashlib.sha256(descriptor_bytes).hexdigest())
        self.assertEqual("synthetic-semantic", installed["pack_id"])
        self.assertEqual(model, (self.settings.state_dir / "models" / "synthetic-semantic" / "model.gguf").read_bytes())
        self.assertTrue(os.access(self.settings.state_dir / "models" / "synthetic-semantic" / "llama-server", os.X_OK))
        self.assertTrue(verify_pack(self.settings, "synthetic-semantic")["verified"])

    def test_cli_official_semantic_alias_uses_the_controlled_catalog(self) -> None:
        output = io.StringIO()
        catalog = {"semantic": {"pack_id": "qwen3-embedding-4b-q6k-darwin-arm64", "descriptor_url": "https://github.com/example/project/releases/download/v1/descriptor.json", "descriptor_sha256": "a" * 64}}
        with mock.patch("brain.models.OFFICIAL_PACKS", catalog), mock.patch("brain.models.install_official_pack", return_value={"pack_id": "qwen3-embedding-4b-q6k-darwin-arm64"}) as install:
            with redirect_stdout(output):
                self.assertEqual(0, main(["-c", str(self.config), "model", "install", "semantic"]))
        install.assert_called_once_with(self.settings, "semantic")
        self.assertEqual("qwen3-embedding-4b-q6k-darwin-arm64", json.loads(output.getvalue())["pack_id"])

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
            "runtime_binary": "llama-server", "model_file": "model.gguf", "embedding_dimension": 16,
            "chunk_schema_version": CHUNK_SCHEMA_VERSION, "document_card_version": CARD_VERSION,
            "weight_format": "GGUF", "quantization": "Q8_0", "weight_sha256": hashlib.sha256(model.read_bytes()).hexdigest(),
            "tokenizer_file": "tokenizer.json", "tokenizer_sha256": hashlib.sha256(tokenizer.read_bytes()).hexdigest(), "pooling": "mean", "normalization": "l2",
            "query_instruction_version": "v1", "converter_revision": "llama.cpp@1",
            "golden_suite": "conformance.json", "golden_suite_hash": digest,
            "artifacts": {"llama-server": hashlib.sha256(binary.read_bytes()).hexdigest(), "model.gguf": hashlib.sha256(model.read_bytes()).hexdigest(), "tokenizer.json": hashlib.sha256(tokenizer.read_bytes()).hexdigest(), "conformance.json": digest},
        }), encoding="utf-8")
        install_pack(self.settings, pack)
        with mock.patch("brain.models.runtime_for_pack", return_value=runtime):
            verified = verify_pack(self.settings, "production-conformance")
        self.assertTrue(verified["conformance"]["passed"])
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

    def test_local_reranker_only_reorders_bounded_nonprotected_candidates(self) -> None:
        protected = SearchHit("trading-service", "src/Eligibility.java", 10, "class Eligibility", "definition", 100, ["symbol"])
        first = SearchHit("trading-service", "README.md", 2, "release note", "code", 50, ["search"])
        second = SearchHit("risk-service", "src/Risk.java", 8, "eligibility risk check", "code", 50, ["search"])
        reranked = rerank_candidates(self.settings, "eligibility", [protected, first, second], runtime=DeterministicRuntime(), limit=1)
        self.assertEqual(100, reranked[0].score)
        self.assertEqual(50, reranked[2].score)
        self.assertIn("local reranker", reranked[1].found_by)

    def test_brain_owned_model_runtimes_shutdown_on_query_and_index_paths(self) -> None:
        hit = SearchHit("trading-service", "README.md", 2, "eligibility evidence", "code", 50, ["search"])
        reranker = mock.Mock()
        reranker.rerank.return_value = [1.0]
        with mock.patch("brain.models.active_pack", return_value={"pack_id": "reranker"}), mock.patch("brain.models.runtime_for_pack", return_value=reranker):
            rerank_candidates(self.settings, "eligibility", [hit])
        reranker.shutdown.assert_called_once()

        state = {
            "chunk_schema_version": CHUNK_SCHEMA_VERSION, "card_version": CARD_VERSION,
            "backend": "exact-mock", "pack_id": "embedding", "dimension": 2, "stale": False,
            "entries": [{"repo": "trading-service", "snapshot": "working-tree", "path": "README.md", "line": 1, "chunk_id": "one", "vector": [1.0, 0.0]}],
            "shards": [],
        }
        (self.settings.state_dir / "semantic-index.json").write_text(json.dumps(state), encoding="utf-8")
        embedding = mock.Mock()
        embedding.embed.return_value = [[1.0, 0.0]]
        with mock.patch("brain.semantic.active_pack", return_value={"pack_id": "embedding"}), mock.patch("brain.semantic.runtime_for_pack", return_value=embedding):
            self.assertTrue(search_semantic(self.settings, "eligibility"))
        embedding.shutdown.assert_called_once()

        build_runtime = mock.Mock()
        with mock.patch("brain.semantic.active_pack", return_value={"pack_id": "embedding", "embedding_dimension": 2}), mock.patch("brain.semantic.runtime_for_pack", return_value=build_runtime), mock.patch("brain.semantic._usearch", return_value=None):
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
        runtime = runtime_for_pack(manifest)
        self.assertIsInstance(runtime, ManagedLlamaCppRuntime)
        process = mock.Mock()
        process.poll.return_value = None
        with mock.patch("brain.models.subprocess.Popen", return_value=process) as popen, mock.patch.object(LlamaCppRuntime, "health", return_value={"ok": True}), mock.patch("brain.models.os.killpg") as kill:
            runtime.warmup()
            command = popen.call_args.args[0]
            self.assertIn("127.0.0.1", command)
            self.assertIn("--offline", command)
            self.assertIn("--no-webui", command)
            self.assertIn("--embedding", command)
            self.assertIn("--ctx-size", command)
            self.assertIn("4096", command)
            self.assertNotIn("--hf-repo", command)
            self.assertEqual(12.0, runtime.client.timeout_seconds)
            runtime.shutdown()
            kill.assert_called_once()

    def test_managed_llama_runtime_restarts_once_after_a_transport_disconnect(self) -> None:
        runtime = ManagedLlamaCppRuntime({})
        client = mock.Mock()
        client.embed.side_effect = [ConnectionResetError("local server restarted"), [[1.0, 0.0]]]
        with mock.patch.object(runtime, "_start", return_value=client) as start, mock.patch.object(runtime, "shutdown") as shutdown:
            self.assertEqual([[1.0, 0.0]], runtime.embed(["public synthetic card"], dimension=2))
        self.assertEqual(2, start.call_count)
        shutdown.assert_called_once()

    def test_managed_llama_runtime_restarts_after_its_request_budget(self) -> None:
        runtime = ManagedLlamaCppRuntime({"capability": "embedding", "max_requests_per_runtime": 2})
        runtime.client = mock.Mock()
        runtime.process = mock.Mock()
        runtime.process.poll.return_value = None
        runtime.request_count = 2
        new_process = mock.Mock()
        new_process.poll.return_value = None
        with mock.patch.object(runtime, "shutdown") as shutdown, mock.patch("brain.models._check_pack_integrity"), mock.patch("brain.models._pack_file", return_value=Path("/tmp/verified-artifact")), mock.patch("brain.models.os.access", return_value=True):
            with mock.patch("brain.models.subprocess.Popen", return_value=new_process) as popen, mock.patch.object(LlamaCppRuntime, "health", return_value={"ok": True}):
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

        repo = self.settings.repo("trading-service")
        repo.source_sha = "snapshot-1"
        shard = zoekt.shard_path(self.settings.state_dir, repo.name, repo.source_sha)
        shard.mkdir(parents=True)
        (shard / "brain-shard.json").write_text(json.dumps({"source_sha": repo.source_sha}), encoding="utf-8")
        response = json.dumps({"FileName": "src/main/java/demo/EligibilityEvaluator.java", "Score": 5, "LineMatches": [{"LineNumber": 2, "LineStart": 0, "Line": base64.b64encode(b"interface EligibilityEvaluator {}\n").decode()}, {"LineNumber": 3, "LineStart": 0, "Line": base64.b64encode(b"unrelated\n").decode()}]})
        available = zoekt.ZoektStatus(True, "zoekt", "zoekt-index")
        with mock.patch("brain.backends.zoekt.status", return_value=available), mock.patch("brain.backends.zoekt.subprocess.run", return_value=subprocess.CompletedProcess([], 0, response, "")):
            result = zoekt.search(self.settings, repo, "EligibilityEvaluator", fixed=True, max_results=20)
        self.assertIsNotNone(result)
        self.assertEqual([("src/main/java/demo/EligibilityEvaluator.java", 2, "interface EligibilityEvaluator {}", 5.0)], result[0])
        with mock.patch("brain.backends.zoekt.status", return_value=available), mock.patch("brain.backends.zoekt.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "corrupt shard")):
            self.assertIsNone(zoekt.search(self.settings, repo, "EligibilityEvaluator", fixed=True, max_results=20))
        repo.source_sha = "snapshot-2"
        self.assertIsNone(zoekt.search(self.settings, repo, "EligibilityEvaluator", fixed=True, max_results=20))

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
            "backend": "usearch", "pack_id": "mock", "dimension": 3, "stale": False,
            "shards": [{"repo": "trading-service", "snapshot": "working-tree", "path": str(shard), "entries": [{"path": "src/main/java/demo/EligibilityEvaluator.java", "line": 2, "chunk_id": "chunk"}]}],
        }), encoding="utf-8")

        class BrokenIndex:
            @staticmethod
            def restore(*args, **kwargs):
                raise ValueError("corrupt")

        with mock.patch("brain.semantic._usearch", return_value=(BrokenIndex, mock.Mock(asarray=lambda value, dtype: value))):
            self.assertEqual([], search_semantic(self.settings, "eligibility", embed=lambda values: [[1.0, 0.0, 0.0]]))

        (self.settings.state_dir / "catalog.sqlite3").write_bytes(b"corrupt catalog")
        state, _ = snapshot_indexes(self.settings)
        self.assertIn("Catalog generation unavailable", str(state["trading-service"].get("warning")))
        self.assertTrue(search(self.settings, "EligibilityEvaluator", ["trading-service"], fixed=True))


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
    def test_refresh_discovers_new_repositories_and_can_be_opted_out(self) -> None:
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

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(0, main(["-c", str(config), "refresh", "--no-fetch"]))
            self.assertIn("Discovered and configured: repo-b", output.getvalue())
            self.assertIn('name = "repo-b"', config.read_text(encoding="utf-8"))

            (root / "repo-c/.git").mkdir(parents=True)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(0, main(["-c", str(config), "refresh", "--no-fetch", "--no-discover"]))
            self.assertNotIn('name = "repo-c"', config.read_text(encoding="utf-8"))

    def test_new_repositories_are_appended_without_rewriting_existing_config(self) -> None:
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
            additions = discover_and_configure_repositories(settings)
            self.assertEqual(["team-b-service"], [repo.name for repo in additions])
            self.assertEqual([], discover_and_configure_repositories(settings))

            content = config.read_text(encoding="utf-8")
            self.assertEqual(2, content.count("[[repositories]]"))
            self.assertIn('description="Keep this description"', content)
            self.assertIn('name = "team-b-service"', content)
            self.assertNotIn("ignored", content)
            self.assertEqual(
                {"service", "team-b-service"},
                {repo.name for repo in load_settings(config).repositories},
            )

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
            self.assertTrue((repo / "brain.toml").is_file())
            self.assertTrue((repo / "knowledge/PROJECT_MAP.md").is_file())
            config = (repo / "brain.toml").read_text(encoding="utf-8")
            self.assertIn('path = "."', config)
            self.assertIn("[experience]", config)
            self.assertIn("patch_chars = 0", config)
            self.assertIn("minimum_free_disk_gb = 5", config)
            self.assertEqual(0, load_settings(repo / "brain.toml").experience_patch_chars)
            self.assertEqual(5, load_settings(repo / "brain.toml").minimum_free_disk_gb)


class GitSyncTest(unittest.TestCase):
    def _git(self, cwd: Path, *args: str) -> str:
        result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True)
        return result.stdout.strip()

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
            self.assertIn("ControlMaster=auto", fetch_environments[0]["GIT_SSH_COMMAND"])
            self.assertEqual(2, sum("skipped another interactive attempt" in (result.warning or "") for result in results))
            self.assertTrue(all("example.test" not in (result.warning or "") for result in results))

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
        with self.assertRaisesRegex(ValueError, "missing standalone"):
            module.render("9.9.9", {})
        workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn("needs: github-release", workflow)
        self.assertIn("cmp published/SHA256SUMS.txt dist/SHA256SUMS.txt", workflow)
        self.assertIn("HOMEBREW_TAP_TOKEN", workflow)
        self.assertIn("Check tap authorization", workflow)
        self.assertIn("steps.authorization.outputs.available == 'true'", workflow)
        self.assertNotIn("if: ${{ secrets.HOMEBREW_TAP_TOKEN", workflow)

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
                    "pack_id": "qwen3-embedding-4b-q6k-darwin-arm64",
                    "descriptor_url": "https://github.com/superorange0707/project-brain/releases/download/semantic-pack-v1.0.6/qwen3-embedding-4b-q6k-darwin-arm64-descriptor.json",
                    "descriptor_sha256": "cbd09af575fb1b2e036abc17ed3e693e5bab4807af19efd2c1a9b5cd75ae8afc",
                }
            },
            OFFICIAL_PACKS,
        )

    def test_semantic_pack_builder_uses_a_strong_cross_machine_reference_threshold(self) -> None:
        builder = (Path(__file__).resolve().parents[1] / "scripts/build_semantic_pack.py").read_text(encoding="utf-8")
        self.assertIn("MINIMUM_REFERENCE_COSINE = 0.995", builder)
        self.assertIn('"minimum_cosine_to_reference": MINIMUM_REFERENCE_COSINE', builder)

    def test_standalone_release_builds_source_pinned_zoekt(self) -> None:
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn('go-version: "1.24.7"', workflow)
        self.assertIn("github.com/sourcegraph/zoekt/cmd/zoekt@$ZOEKTVERSION", workflow)
        self.assertIn("github.com/sourcegraph/zoekt/cmd/zoekt-index@$ZOEKTVERSION", workflow)
        self.assertIn("v0.0.0-20251202141441-886b229dcd5e", workflow)
        self.assertIn("zoekt-bin/zoekt zoekt-bin/zoekt-index package/", workflow)
        self.assertNotIn("sourcegraph/zoekt/cmd/zoekt@latest", workflow)
        self.assertIn(
            "github-release:\n"
            "    name: Publish GitHub release\n"
            "    needs: [build, standalone]\n",
            workflow,
        )
        self.assertIn('gh release create "$GITHUB_REF_NAME" dist/* --verify-tag --notes-file RELEASE_NOTES.md', workflow)
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

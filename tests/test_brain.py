from __future__ import annotations

import io
import json
import os
import re
import subprocess
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
    search,
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
            self.assertEqual(0, load_settings(repo / "brain.toml").experience_patch_chars)


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

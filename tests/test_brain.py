from __future__ import annotations

import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from brain.cli import main
from brain.core import (
    BrainError,
    chunk_text,
    create_context,
    create_feedback,
    generate_map,
    load_settings,
    load_index_state,
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


class InitTest(unittest.TestCase):
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
            self.assertIn('path = "."', (repo / "brain.toml").read_text(encoding="utf-8"))


class GitSyncTest(unittest.TestCase):
    def _git(self, cwd: Path, *args: str) -> str:
        result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True)
        return result.stdout.strip()

    def test_sync_reads_latest_remote_commit_without_touching_local_changes(self) -> None:
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
            (seed / "value.txt").write_text("new remote\n", encoding="utf-8")
            self._git(seed, "add", "value.txt")
            self._git(seed, "commit", "-m", "remote update")
            self._git(seed, "push", "origin", "main")

            config = root / "brain.toml"
            config.write_text(
                '[project]\nname="sync-test"\n[[repositories]]\nname="service-a"\npath="service-a"\n',
                encoding="utf-8",
            )
            settings = load_settings(config)
            results = sync_repositories(settings)
            self.assertEqual("current", results[0].status)
            self.assertEqual("new remote\n", (settings.repo("service-a").scan_path / "value.txt").read_text(encoding="utf-8"))
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

            def fake_git(repo, *args, binary=False, extra_env=None):
                if args and args[0] == "fetch":
                    fetch_environments.append(extra_env)
                    return subprocess.CompletedProcess(["git", *args], 128, "", "Permission denied (publickey).")
                return original_git(repo, *args, binary=binary, extra_env=extra_env)

            with mock.patch("brain.sync._git", side_effect=fake_git):
                results = sync_repositories(settings)

            self.assertEqual(1, len(fetch_environments))
            self.assertIn("ControlMaster=auto", fetch_environments[0]["GIT_SSH_COMMAND"])
            self.assertEqual(2, sum("skipped another password prompt" in (result.warning or "") for result in results))

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

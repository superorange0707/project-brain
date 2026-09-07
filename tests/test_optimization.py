from __future__ import annotations

import tempfile
import time
import unittest
import json
import re
import shutil
import subprocess
from pathlib import Path
from unittest import mock

from brain.core import ContextBundle, Repository, SearchHit, load_settings, pack_context, snapshot_indexes
from brain.catalog import current_generation_ref
from brain.retrieval.ranker import fuse_and_rank


class OptimizationTests(unittest.TestCase):
    def test_execution_flow_batches_validation_without_losing_later_seeds(self) -> None:
        import sqlite3
        from brain.core import Evidence
        from brain.investigation import _execution_flow, MAX_FLOW_DB_QUERIES, MAX_FLOW_SEEDS

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "repo").mkdir()
            config = root / "brain.toml"
            config.write_text("[project]\nname='flow'\n[graph]\nenabled=false\n[[repositories]]\nname='repo'\npath='repo'\n")
            sources = {}
            for i in range(MAX_FLOW_SEEDS):
                path = f"flow{i:02}.py"
                sources[path] = (
                    f"def entry_{i}():\n    return middle_{i}()\n"
                    f"def middle_{i}():\n    return final_{i}()\n"
                    f"def final_{i}():\n    return {i}\n"
                )
                (root / "repo" / path).write_text(sources[path])
            settings = load_settings(config)
            snapshot_indexes(settings)
            generation = current_generation_ref(settings)
            connection = sqlite3.connect(settings.state_dir / "catalog.sqlite3")
            try:
                seeds = [str(row[0]) for row in connection.execute(
                    "SELECT e.entity_id FROM generation_entities g JOIN atlas_entities e ON e.entity_id=g.entity_id "
                    "WHERE g.generation=? AND e.simple_name LIKE 'entry_%' ORDER BY e.path",
                    (generation.generation,),
                )]
            finally:
                connection.close()
            self.assertEqual(MAX_FLOW_SEEDS, len(seeds))
            bundle = ContextBundle("Trace every anchored entry", atlas_generation=generation, evidence=[
                Evidence("repo", path, 1, 6, content, "code", 100, verification_content=content)
                for path, content in sources.items()
            ])
            flow = _execution_flow(settings, generation, seeds, bundle)
            self.assertLessEqual(flow["database_operations"], MAX_FLOW_DB_QUERIES)
            self.assertLessEqual(flow["database_operations"], 15)
            self.assertEqual(2 * MAX_FLOW_SEEDS, len(flow["steps"]))
            self.assertTrue(all(step["state"] == "verified" for step in flow["steps"]))
            self.assertEqual([0] * MAX_FLOW_SEEDS + [1] * MAX_FLOW_SEEDS, [step["depth"] for step in flow["steps"]])
            self.assertEqual(set(seeds), {step["source_id"] for step in flow["steps"] if step["depth"] == 0})
            self.assertEqual(flow, _execution_flow(settings, generation, seeds, bundle))
            without_source = _execution_flow(
                settings, generation, seeds, ContextBundle("metadata only", atlas_generation=generation),
            )
            self.assertTrue(all(step["state"] == "candidate" for step in without_source["steps"]))
            with mock.patch("brain.investigation.MAX_FLOW_DB_QUERIES", 4):
                bounded = _execution_flow(settings, generation, seeds, bundle)
            self.assertEqual(0, bounded["database_operations"])
            self.assertEqual([], bounded["steps"])
            connection = sqlite3.connect(settings.state_dir / "catalog.sqlite3")
            try:
                connection.execute(
                    "UPDATE atlas_edges SET target_id='poisoned-target' WHERE edge_id=?",
                    (flow["steps"][-1]["identity"],),
                )
                connection.commit()
            finally:
                connection.close()
            poisoned = _execution_flow(settings, generation, seeds, bundle)
            self.assertEqual("degraded", poisoned["status"])
            self.assertEqual([], poisoned["steps"])
            self.assertIn("content identity", poisoned["reason"])

    def test_unresolved_external_call_does_not_discard_the_verified_internal_flow(self) -> None:
        import sqlite3
        from brain.core import Evidence
        from brain.investigation import _execution_flow

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "repo").mkdir()
            content = "def entry():\n    logger.info('trace')\n    return service()\ndef service():\n    return 1\n"
            (root / "repo/main.py").write_text(content)
            config = root / "brain.toml"
            config.write_text("[project]\nname='external-call'\n[graph]\nenabled=false\n[[repositories]]\nname='repo'\npath='repo'\n")
            settings = load_settings(config)
            snapshot_indexes(settings)
            generation = current_generation_ref(settings)
            connection = sqlite3.connect(settings.state_dir / "catalog.sqlite3")
            try:
                seed = str(connection.execute(
                    "SELECT e.entity_id FROM generation_entities g JOIN atlas_entities e ON e.entity_id=g.entity_id "
                    "WHERE g.generation=? AND e.simple_name='entry'", (generation.generation,),
                ).fetchone()[0])
            finally:
                connection.close()
            bundle = ContextBundle("Trace through logging", atlas_generation=generation, evidence=[
                Evidence("repo", "main.py", 1, 5, content, "code", 100, verification_content=content),
            ])
            flow = _execution_flow(settings, generation, [seed], bundle)
            self.assertEqual("ready", flow["status"])
            steps = {step["target"]: step for step in flow["steps"]}
            self.assertEqual("verified", steps["service"]["state"])
            self.assertEqual("candidate", steps["info"]["state"])
            self.assertEqual("atlas_candidate", steps["info"]["evidence_authority"])
            self.assertFalse(any(step["source_id"] == steps["info"]["target_id"] for step in flow["steps"]))

    def test_full_storage_inventory_streams_more_than_half_a_million_entries(self) -> None:
        import stat
        from types import SimpleNamespace
        from brain.ops import _directory_bytes

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            visits = []
            info = SimpleNamespace(st_mode=stat.S_IFREG, st_size=7)
            entry = SimpleNamespace(stat=lambda **kwargs: info)

            def entries():
                for i in range(500_010):
                    if i % 100_000 == 0:
                        visits.append(i)
                    yield entry

            with mock.patch("brain.ops.os.scandir") as scan:
                scan.return_value.__enter__.return_value = entries()
                self.assertEqual(500_010 * 7, _directory_bytes(root))
            self.assertEqual(6, len(visits))

    def test_storage_quota_depth_and_incomplete_probe_never_authorize_writes(self) -> None:
        from dataclasses import replace
        from brain.ops import _directory_bytes, ensure_write_capacity, StateCapacityError

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "repo").mkdir()
            config = root / "brain.toml"
            config.write_text("[project]\nname='inventory'\n[[repositories]]\nname='repo'\npath='repo'\n")
            settings = load_settings(config)
            nested = settings.state_dir / "nested/deeper"
            nested.mkdir(parents=True)
            (nested / "payload").write_bytes(b"x" * 1024)
            with self.assertRaisesRegex(StateCapacityError, "quota"):
                ensure_write_capacity(replace(settings, max_state_gb=0.0000001))
            with self.assertRaisesRegex(StateCapacityError, "Quick storage check"):
                _directory_bytes(settings.state_dir, scan_seconds=0)
            with mock.patch.object(Path, "lstat", side_effect=PermissionError("denied")), self.assertRaises(PermissionError):
                _directory_bytes(settings.state_dir)
            with mock.patch("brain.ops.MAX_INVENTORY_DEPTH", 1), self.assertRaisesRegex(StateCapacityError, "directory-depth"):
                _directory_bytes(settings.state_dir)
            self.assertEqual(1024, _directory_bytes(settings.state_dir, stop_after=100))

    def test_gc_payload_inventory_does_not_spend_reachability_item_budget(self) -> None:
        from brain.ops import _GcScanBudget, _gc_path_bytes

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for i in range(50):
                (root / str(i)).write_bytes(b"abc")
            budget = _GcScanBudget(remaining_items=2)
            self.assertEqual(150, _gc_path_bytes(None, root, budget))
            self.assertEqual(1, budget.remaining_items)
            self.assertEqual(150, budget.accounted_bytes)

    def test_golden_mapping_uses_the_same_request_validation_as_the_ui(self) -> None:
        from brain.core import BrainError
        from brain.evaluation import _request

        with self.assertRaisesRegex(BrainError, "symbols\\[0\\].name"):
            _request({"objective": "Find a method", "symbols": [{"query": "invalid field"}]})
        result = _request({"objective": "Find a method", "symbols": [{"name": "validMethod"}]})
        self.assertEqual(["definition"], result["symbols"][0]["include"])

    def test_public_cross_repository_quality_and_hydrated_evidence_are_measured_separately(self) -> None:
        from brain.demo import create_demo
        from brain.evaluation import evaluate_golden
        from brain.ops import refresh_brain

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = load_settings(create_demo(root))
            refresh_brain(settings, fetch=False, discover=False)
            suite = root / "quality.json"
            suite.write_text(json.dumps({"name": "public-workspace-quality", "cases": [
                {"id": "unscoped-event-consumer", "request": {
                    "objective": "Locate the customer.updated Kafka consumer and its regression tests.",
                    "searches": [{"query": "KafkaListener"}, {"query": "recalculate"}],
                }, "expect": {"required_files": [
                    "trading-service:src/main/java/demo/CustomerChangedListener.java",
                    "trading-service:src/test/java/demo/CustomerChangedListenerTest.java",
                ]}},
                {"id": "cross-repository-contract", "request": {
                    "objective": "Find the risk Feign caller and the REST implementation.",
                    "symbols": [{"name": "RiskClient"}, {"name": "RiskController"}],
                }, "expect": {"required_files": [
                    "trading-service:src/main/java/demo/RiskClient.java",
                    "risk-service:src/main/java/demo/RiskController.java",
                ]}},
                {"id": "configuration-evidence", "request": {
                    "objective": "Find topic and service configuration, not a similarly named class.",
                    "searches": [{"query": "topics.customer"}],
                }, "expect": {"required_files": [
                    "trading-service:src/main/resources/application.properties",
                ]}},
            ]}))
            report = evaluate_golden(settings, suite)
            self.assertEqual(3, report["summary"]["evaluated_cases"])
            for case in report["cases"]:
                self.assertEqual(1.0, case["hydrated_file_recall_at_limit"], case)
            settings.hydrate_limit = 1
            limited = evaluate_golden(settings, suite)
            self.assertGreater(limited["summary"]["candidate_file_recall_at_limit"], limited["summary"]["hydrated_file_recall_at_limit"])

    def test_packaged_ui_javascript_boots_without_optional_browser_storage(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is unavailable for the optional packaged JavaScript smoke test")
        html = (Path(__file__).parents[1] / "brain/ui.html").read_text(encoding="utf-8")
        script = html.split("<script>", 1)[1].split("</script>", 1)[0]
        ids = re.findall(r'\bid="([^"]+)"', html)
        self.assertEqual(len(ids), len(set(ids)), "duplicate DOM IDs break workspace actions")
        result = subprocess.run([node, "-e", r'''
const vm = require("node:vm");
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
// Test UI assertions, not toast animation latency or the runner's Node startup.
const timers = new Map();
const watchdog = setTimeout(() => {console.error("UI assertions did not complete"); process.exit(1);}, 10000);
const elements = Object.fromEntries(input.ids.map(id => [id, {
  dataset: {}, listeners: {}, textContent: "", hidden: false, style: {},
  addEventListener(name, fn) { this.listeners[name] = fn; },
  querySelectorAll() { return []; },
  classList: { add() {}, remove() {}, toggle() {} }
}]));
const ctx = { URLSearchParams, location: {search:""},
  window: { matchMedia() {return {matches:true};} },
  localStorage: { getItem() {throw Error("disabled");}, setItem() {throw Error("disabled");} },
  document: {documentElement:{dataset:{}}, querySelectorAll() {return [];},
    getElementById(id) {if (!elements[id]) throw Error("Missing DOM node: " + id); return elements[id];}},
  fetch: async () => {throw Error("connection lost");},
  setTimeout(fn, milliseconds) {
    if (milliseconds !== 2600) throw Error("unexpected unmocked UI timer: " + milliseconds);
    const id = Symbol(); timers.set(id, fn); return id;
  },
  clearTimeout(id) { timers.delete(id); }
};
vm.createContext(ctx);
vm.runInContext(input.script, ctx, {timeout:5000});
if (ctx.document.documentElement.dataset.theme !== "light") throw Error("system theme ignored");
elements["theme-button"].listeners.click();
if (ctx.document.documentElement.dataset.theme !== "dark") throw Error("theme toggle failed");
ctx.setView("request");
if (elements["page-title"].textContent !== "Continue with AI") throw Error("navigation failed");
ctx.setJob({name:"model-verify", phase:"Verifying", status:"running"});
if (elements["activity-button"].dataset.go !== "models") throw Error("model progress opens the wrong view");
const progress = {semantic_cards_total:30150, cached_embeddings_reused:75, shard_vectors_reused:2000,
  new_embeddings_completed:8161, embedding_elapsed_ms:3264400, elapsed_ms:3300000,
  semantic_shards_reused:32, semantic_shards_rebuilt:1, remaining_embeddings_known:0};
ctx.renderRefreshProgress({name:"refresh", status:"running", progress});
if (!elements["refresh-counts"].innerHTML.includes("Shards reused 32")) throw Error("shard reuse is hidden");
if (!elements["refresh-counts"].innerHTML.includes("Recovered old vectors 2,000")) throw Error("durable reuse is hidden");
if (elements["refresh-counts"].innerHTML.includes("Estimated remaining embedding time")) throw Error("ETA guessed before reuse checks");
if (elements["refresh-progress-fill"].style.width !== "34%") throw Error("durable vectors omitted from progress");
progress.remaining_embeddings_known = 1;
progress.remaining_embeddings = 25;
ctx.renderRefreshProgress({name:"refresh", status:"running", progress});
if (!elements["refresh-counts"].innerHTML.includes("Estimated remaining embedding time 00:10")) throw Error("ETA must use model-only rate");
ctx.renderBrain({health:"Freshness unverified", core:{ready:true}, semantic:{aligned:true}, effective:"Precision active"});
if (!elements["brain-status"].innerHTML.includes("Published generation ready")) throw Error("Git probe misreported as broken index");
if (!elements["recovery-guidance"].textContent.includes("remains usable")) throw Error("unverified Git triggers misleading rebuild advice");
ctx.state.ticket = "TICKET-A";
ctx.state.preview = {valid: true};
ctx.state.deliveries["view-request"] = {content:"A private evidence", total:1};
ctx.selectTicket("TICKET-B");
if (ctx.state.preview || ctx.state.deliveries["view-request"].content) throw Error("old ticket context leaked");
if (!elements["run-request"].disabled || elements["review-ticket"].value !== "TICKET-B") throw Error("ticket state not synchronized");
(async function () {
  await ctx.api("/api/status").then(() => {throw Error("connection failure hidden");}, error => {
    if (!error.message.includes("may still be running")) throw error;
  });
  const paused = {valid:true, kind:"context_request", operation_count:1, actions:[],
    objective:"Additional evidence", continuation:{required:true, reason:"Automatic allowance reached",
      next_wave:5, generation:1, physical_operations_per_wave:32, context_bytes_per_wave:48000, token:"a".repeat(64)}};
  ctx.state.ticket = "TICKET-B";
  elements["request-ticket"].value = "TICKET-B";
  elements["request-text"].value = "new focused request";
  ctx.renderPreview(paused);
  if (elements["run-request"].textContent !== "Continue gathering evidence") throw Error("continuation action hidden");
  if (!elements["request-message"].innerHTML.includes("no refresh or reset")) throw Error("wrong continuation guidance");
  let calls = [];
  ctx.api = async (path, options) => {calls.push({path, body:JSON.parse(options.body)}); return {id:"job"};};
  ctx.waitForJob = async () => ({kind:"context_request"});
  ctx.loadStatus = async () => {};
  ctx.window.confirm = () => false;
  await elements["run-request"].listeners.click.call(elements["run-request"]);
  if (calls.length) throw Error("cancelled continuation ran retrieval");
  ctx.window.confirm = message => {
    if (!message.includes("wave 5") || !message.includes("generation 1") || !message.includes("32 backend")) throw Error("approval is not scoped");
    return true;
  };
  await elements["run-request"].listeners.click.call(elements["run-request"]);
  if (calls.length !== 1 || calls[0].body.continue_investigation !== true || calls[0].body.continuation_token !== paused.continuation.token) throw Error("approval not sent");
  if (ctx.state.preview || !elements["run-request"].disabled) throw Error("approval remains armed after use");
  await elements["run-request"].listeners.click.call(elements["run-request"]);
  if (calls.length !== 1) throw Error("approval reused without preview");
  ctx.renderPreview(paused);
  elements["request-text"].listeners.input();
  if (ctx.state.preview) throw Error("changed request retains approval");
  ctx.report({message:"Investigation paused", recovery:{action:"continue_investigation", message:"Continue with AI"}});
  if (elements["error-recovery"].dataset.go !== "request" || elements["page-title"].textContent !== "Continue with AI") throw Error("wave pause routed to health/refresh");
  for (const fn of timers.values()) fn();
  timers.clear();
})().then(() => {
  clearTimeout(watchdog);
  console.log("UI smoke assertions complete");
}).catch(error => {clearTimeout(watchdog); console.error(error); process.exitCode = 1;});
'''], input=json.dumps({"script": script, "ids": ids}), capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("UI smoke assertions complete", result.stdout)

    def test_rrf_uses_per_channel_candidate_rank_without_mutating_inputs(self) -> None:
        hits = [
            SearchHit("r", "first.py", 1, "", score=100, found_by=["lexical"]),
            SearchHit("r", "second.py", 1, "", score=100, found_by=["semantic"]),
            SearchHit("r", "second.py", 1, "", score=90, found_by=["lexical"]),
        ]
        ranked = fuse_and_rank(hits)
        self.assertEqual("second.py", ranked[0].path)
        self.assertEqual(round(100 + 100 * (1 / 61 + 1 / 62), 3), ranked[0].score)
        self.assertEqual([100, 100, 90], [hit.score for hit in hits])
        self.assertEqual(ranked, fuse_and_rank(list(reversed(hits))))
        self.assertEqual(ranked, fuse_and_rank(hits))

    def test_java_entity_identity_and_lines_match_reference_brace_scanning(self) -> None:
        from brain.atlas import _java_entities
        from bisect import bisect_left as actual_bisect

        content = "class Outer {\n" + "\n".join(
            f"public void method{i}() {{ if (true) {{ run(); }} }}" for i in range(1000)
        ) + "\nclass Inner { void child() { run(); } }\n}\n"
        lines = [i for i, char in enumerate(content) if char == "\n"]
        lookups = []

        def reference(values, position):
            self.assertEqual(lines, values)
            lookups.append(position)
            self.assertEqual(content.count("\n", 0, position), actual_bisect(values, position))
            return actual_bisect(values, position)

        with mock.patch("brain.atlas.bisect_left", side_effect=reference):
            entities = _java_entities("repo", "Outer.java", "blob", "module", content, content, time.monotonic() + 10)
        self.assertEqual(1003, len(entities))
        self.assertGreater(len(lookups), 1000)
        for item in entities:
            from brain.atlas import _valid_entity_content_identity
            self.assertTrue(_valid_entity_content_identity(item))
        child = next(item for item in entities if item["simple_name"] == "child")
        inner = next(item for item in entities if item["simple_name"] == "Inner")
        self.assertEqual(inner["entity_id"], child["parent_entity_id"])
        self.assertEqual((1002, 1002), (child["line_start"], child["line_end"]))

    def test_pinned_context_pack_does_not_probe_current_head_for_every_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "service").mkdir()
            (root / "service/main.py").write_text("def main(): return 1\n")
            config = root / "brain.toml"
            config.write_text("[project]\nname='pack'\n[graph]\nenabled=false\n[[repositories]]\nname='service'\npath='service'\n")
            settings = load_settings(config)
            snapshot_indexes(settings)
            generation = current_generation_ref(settings)
            with mock.patch("brain.core.git_head", side_effect=AssertionError("live HEAD is not pinned evidence")):
                text = pack_context(settings, "PACK-1", 1, ContextBundle("Inspect", atlas_generation=generation))
            self.assertIn(generation.snapshots["service"][:12], text)
            self.assertIn("not probed (pinned)", text)

    def test_freshness_100_repositories_share_one_probe_time_budget(self) -> None:
        from brain.ops import freshness

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "brain.toml"
            (root / "initial").mkdir()
            config.write_text("[project]\nname='bounded'\n[graph]\nenabled=false\n[[repositories]]\nname='initial'\npath='initial'\n")
            settings = load_settings(config)
            repositories = []
            for i in range(100):
                path = root / f"repo{i}"
                (path / ".git").mkdir(parents=True)
                repositories.append(Repository(name=f"repo{i}", path=path))
            settings.repositories = repositories
            with mock.patch("brain.ops.MAX_FRESHNESS_PROBE_SECONDS", 0), mock.patch("brain.core.git_head") as probe:
                result = freshness(settings)
            self.assertEqual(100, len(result["repositories"]))
            probe.assert_not_called()
            self.assertTrue(all(not row["current"] and row["source_sha"] is None for row in result["repositories"]))


if __name__ == "__main__":
    unittest.main()

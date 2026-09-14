"""Execute the actual inline UI handlers with deferred retrieval responses."""
from pathlib import Path
import shutil
import subprocess
import unittest


UI_HTML = Path(__file__).resolve().parents[1] / "brain" / "ui.html"


@unittest.skipUnless(shutil.which("node"), "UI JavaScript regression requires Node.js")
class UiInteractionTest(unittest.TestCase):
    def test_pending_retrieval_preserves_edits_and_prevents_double_submit(self):
        result = subprocess.run(
            [shutil.which("node"), "-e", r'''
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const html = fs.readFileSync(process.argv[1], "utf8");
function section(start, end) {
  const offset = html.indexOf(start);
  assert.ok(offset >= 0, start);
  const stop = html.indexOf(end, offset + start.length);
  assert.ok(stop > offset, end);
  return html.slice(offset, stop);
}
const plan = {valid:true, kind:"context_request", operation_count:1, actions:[]};
function setup() {
  const elements = new Map();
  const document = {getElementById(id) {
    if (!elements.has(id)) elements.set(id, {
      value:"", dataset:{}, disabled:false, checked:false, textContent:"", innerHTML:"",
      listeners:{}, addEventListener(event, callback) { this.listeners[event] = callback; },
      focus() { document.activeElement = this; }
    });
    return elements.get(id);
  }};
  let finish;
  const pending = new Promise(resolve => { finish = resolve; });
  let calls = 0;
  const context = {
    document, state:{ticket:"A", preview:plan, requestRevision:0, retrieving:false},
    esc:String, showToast() {}, report(error) { throw error; },
    busy(button, active) { button.disabled = active; },
    runRetrieval() { calls++; return pending; },
    api() { throw new Error("Unexpected request for empty ticket"); }
  };
  vm.createContext(context);
  vm.runInContext(section('var state =', 'var titles ='), context);
  Object.assign(context.state, {ticket:"A", preview:plan});
  if (html.includes('function canRunRequest()')) {
    vm.runInContext(section('function canRunRequest()', 'function renderPreview(plan)'), context);
  }
  vm.runInContext(section('function renderPreview(plan)', 'document.getElementById("preview-request").addEventListener'), context);
  vm.runInContext(section('document.getElementById("request-text").addEventListener', 'document.getElementById("auto-refresh-mode").addEventListener'), context);
  vm.runInContext(section('document.getElementById("run-request").addEventListener', 'document.getElementById("checkpoint-continuation").addEventListener'), context);
  vm.runInContext(section('document.getElementById("start-ticket").addEventListener', 'function renderPreview(plan)'), context);
  const input = document.getElementById("request-text");
  const button = document.getElementById("run-request");
  document.getElementById("request-ticket").value = "A";
  input.value = "submitted request";
  const edit = value => { input.value = value; input.listeners.input.call(input); };
  return {context, document, input, button, edit, finish, calls:() => calls,
    run:() => button.listeners.click.call(button)};
}
(async () => {
  // A late completion must not erase a newly typed reply, even if its text is identical.
  for (const draft of ["new reply", "submitted request"]) {
    const app = setup();
    const running = app.run();
    app.edit(draft);
    app.context.renderPreview(plan);
    assert.equal(app.button.disabled, true, "Classify must not unlock a pending submission");
    await app.run();
    assert.equal(app.calls(), 1, "A second click must not submit another job");
    app.finish(true); await running;
    assert.equal(app.input.value, draft, "A completed job erased a newer input event");
    assert.equal(app.button.disabled, false, "The newly classified reply should become actionable");
  }
  // Unclassified edits remain disabled, while an untouched consumed reply is cleared.
  for (const edited of [false, true]) {
    const app = setup(); const running = app.run();
    if (edited) app.edit("unclassified draft");
    app.finish(true); await running;
    assert.equal(app.input.value, edited ? "unclassified draft" : "");
    assert.equal(app.button.disabled, true);
  }
  const switched = setup(); const running = switched.run();
  switched.context.state.ticket = "B";
  switched.input.value = "other ticket draft";
  switched.context.state.preview = null;
  switched.finish(true); await running;
  assert.equal(switched.input.value, "other ticket draft");
  const failed = setup(); const failedRun = failed.run();
  failed.finish(false); await failedRun;
  assert.equal(failed.input.value, "submitted request");
  assert.equal(failed.button.disabled, false, "An uncommitted failure must allow retry");
  for (const value of ["", "   "]) {
    const app = setup(); const identifier = app.document.getElementById("ticket-id");
    identifier.value = value;
    await app.document.getElementById("start-ticket").listeners.click.call({});
    assert.equal(app.document.activeElement, identifier);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
''', str(UI_HTML)],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

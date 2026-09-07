#!/usr/bin/env python3
"""Opt-in patch releases may reuse unchanged, previously qualified model packs."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import tomllib


FILES = ("brain/models.py", "brain/platforms.py", "brain/locks.py")
CONTRACTS = {
    "brain/core.py": {"Settings", "simple_yaml_load", "_bounded_utf8_text", "_atomic_generated_text_write"},
    "brain/ops.py": {"ensure_write_capacity"},
    "brain/catalog.py": {"connect"},
    "brain/semantic.py": {
        "CARD_VERSION", "ATLAS_CARD_VERSION", "CHUNK_SCHEMA_VERSION", "SEMANTIC_EMBEDDING_INPUT_VERSION",
        "SEMANTIC_MAX_CARD_INPUT_BYTES", "SEMANTIC_MAX_REQUEST_BODY_BYTES", "_card_input_bytes", "_bounded_semantic_card",
    },
}


def fingerprint(sources: dict[str, str]) -> str:
    selected = {path: sources[path] for path in FILES}
    for path, names in CONTRACTS.items():
        definitions = {}
        for node in ast.parse(sources[path]).body:
            name = getattr(node, "name", None)
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                name = getattr(node.targets[0], "id", None)
            if name in names:
                definitions[name] = ast.dump(node, include_attributes=False)
        if set(definitions) != names:
            raise ValueError(f"Model qualification contract is missing in {path}")
        selected[path] = definitions
    project = tomllib.loads(sources["pyproject.toml"])
    selected["dependencies"] = {
        "python": project["project"]["requires-python"],
        "dependencies": project["project"]["dependencies"],
        "optional": project["project"].get("optional-dependencies", {}),
        "build": project["build-system"],
    }
    return hashlib.sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"v\d+\.\d+\.\d+", args.baseline):
        raise SystemExit("Qualification baseline must be a stable version tag")

    def run(*command):
        return subprocess.check_output(command, text=True).strip()

    subprocess.run(["git", "merge-base", "--is-ancestor", args.baseline, "HEAD"], check=True)
    repo = "superorange0707/project-brain"
    release = json.loads(run("gh", "release", "view", args.baseline, "--repo", repo,
                             "--json", "isDraft,isPrerelease,tagName"))
    if release["isDraft"] or release["isPrerelease"] or release["tagName"] != args.baseline:
        raise SystemExit("Qualification baseline is not a published stable release")
    baseline_sha = run("git", "rev-parse", args.baseline + "^{commit}")
    runs = json.loads(run("gh", "api", "--method", "GET",
        f"repos/{repo}/actions/workflows/release.yml/runs", "-f", "branch=" + args.baseline,
        "-f", "event=push", "-f", "status=success"))["workflow_runs"]
    qualified = next((item for item in runs if item["head_sha"] == baseline_sha and item["conclusion"] == "success"), None)
    if qualified is None:
        raise SystemExit("No successful full release qualification exists for the exact baseline commit")
    paths = {*FILES, *CONTRACTS, "pyproject.toml"}
    old = fingerprint({path: run("git", "show", f"{args.baseline}:{path}") for path in paths})
    new = fingerprint({path: run("git", "show", f"HEAD:{path}") for path in paths})
    if old != new:
        raise SystemExit("Model runtime, transport, locking, input schemas or dependencies changed; fresh qualification is required")
    print(json.dumps({"qualification": "reused", "baseline": args.baseline, "commit": baseline_sha,
                      "fingerprint": new, "qualified_run": qualified["html_url"]}, indent=2))


if __name__ == "__main__":
    main()

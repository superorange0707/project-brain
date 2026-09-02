#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path


PLATFORMS = {"macos-arm64", "macos-amd64", "linux-arm64", "linux-amd64", "windows-amd64"}


def _run(
    command: list[str], cwd: Path, *, timeout: int = 240,
    extra_environment: dict[str, str] | None = None,
) -> str:
    result = subprocess.run(
        command, cwd=cwd, text=True, capture_output=True, timeout=timeout,
        env={
            **os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1",
            **(extra_environment or {}),
        },
    )
    if result.returncode:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout:\n{result.stdout[-4000:]}\nstderr:\n{result.stderr[-4000:]}"
        )
    return result.stdout


def _git_repository(path: Path, files: dict[str, bytes]) -> None:
    path.mkdir(parents=True)
    for relative, content in files.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    _run(["git", "init", "-q", "-b", "main"], path)
    _run(["git", "config", "core.autocrlf", "false"], path)
    _run(["git", "config", "user.name", "Project Brain parity"], path)
    _run(["git", "config", "user.email", "brain@example.invalid"], path)
    _run(["git", "add", "."], path)
    _run(
        ["git", "commit", "-qm", "deterministic fixture"], path,
        extra_environment={
            "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
        },
    )


def _brain_prefix(executable: Path, source: bool) -> list[str]:
    return [str(executable), "-m", "brain.cli"] if source else [str(executable)]


def _projection(workspace: Path) -> dict[str, object]:
    state = json.loads((workspace / ".runs" / "PARITY" / "session.json").read_text(encoding="utf-8"))
    runtime = state["investigation_runtime"]
    history = state.get("request_history") or []
    retrieval = history[-1].get("retrieval") if history and isinstance(history[-1], dict) else {}
    trace = retrieval.get("trace") if isinstance(retrieval, dict) else {}
    route = trace.get("atlas_route") if isinstance(trace, dict) else {}
    route = route if isinstance(route, dict) else {}
    flows: dict[str, object] = {}
    for name in ("execution_flow", "integration_flow"):
        flow = runtime[name]
        flows[name] = {
            # A flow container ID is ticket-local. The ordered, source-derived
            # steps are the portable behavior that must agree across platforms.
            "steps": [
                {key: item.get(key) for key in ("edge_type", "source_id", "target_id", "repo", "path", "line")
                 if item.get(key) is not None}
                for item in flow.get("steps") or [] if isinstance(item, dict)
            ],
        }
    verified_evidence = sorted(
        (
            {
                "evidence_id": item.get("public_id"),
                "identity": item.get("evidence_id"),
                "repo": item.get("repo"),
                "path": item.get("path"),
                "line_start": item.get("line_start"),
                "line_end": item.get("line_end"),
                "content_hash": item.get("content_hash"),
            }
            for item in state.get("evidence_records") or []
            if isinstance(item, dict) and item.get("public_id") and item.get("content_hash")
        ),
        key=lambda item: (str(item["evidence_id"]), str(item["repo"]), str(item["path"])),
    )
    entity_ids = sorted(str(value) for value in state.get("atlas_entity_ids") or [] if value)
    return {
        "entity_ids": entity_ids,
        "routing": {
            "repositories": sorted(str(value) for value in route.get("repositories") or []),
            "modules": sorted(str(value) for value in route.get("modules") or []),
            "entity_ids": entity_ids,
        },
        "verified_evidence": verified_evidence,
        "flows": flows,
    }


def exercise(executable: Path, platform: str, output: Path, *, source: bool = False) -> None:
    if platform not in PLATFORMS:
        raise ValueError(f"unsupported parity platform: {platform}")
    prefix = _brain_prefix(executable.resolve(), source)
    brain_environment = {"PYTHONPATH": str(Path(__file__).resolve().parents[1])} if source else None
    with tempfile.TemporaryDirectory(prefix="project-brain-cross-platform-") as temporary:
        workspace = Path(temporary)
        api = workspace / "order-api"
        worker = workspace / "order-worker"
        _git_repository(api, {
            "src/main/java/example/OrderController.java": b"""package example;
import org.springframework.web.bind.annotation.*;
import org.springframework.cloud.openfeign.FeignClient;
@RestController @RequestMapping(\"/orders\")
class OrderController { private final WorkerClient client; @GetMapping(\"/{id}\") String get(String id) { return client.fetch(id); } }
@FeignClient(name=\"order-worker\") interface WorkerClient { @GetMapping(\"/internal/orders/{id}\") String fetch(String id); }
""",
            "src/main/java/example/OrderPublisher.java": b"""package example;
class OrderPublisher { void publish(KafkaTemplate<String,String> kafka) { kafka.send(\"order.created\", \"created\"); } }
""",
            "src/test/java/example/OrderControllerTest.java": b"class OrderControllerTest { void getsOrder() {} }\n",
            "src/main/resources/lf.properties": b"worker.url=http://worker\nfeature.orders=true\n",
        })
        _git_repository(worker, {
            "src/main/java/example/WorkerController.java": b"""package example;
import org.springframework.web.bind.annotation.*;
@RestController class WorkerController { @GetMapping(\"/internal/orders/{id}\") String fetch(String id) { return id; } }
""",
            "src/main/java/example/OrderListener.java": b"""package example;\r
class OrderListener { @KafkaListener(topics=\"order.created\") void receive(String event) {} }\r
""",
            "src/test/java/example/OrderListenerTest.java": b"class OrderListenerTest { void consumesOrder() {} }\n",
        })
        before = {repo.name: _run(["git", "status", "--porcelain"], repo) for repo in (api, worker)}
        _run(
            [*prefix, "init", str(api), str(worker), "--name", "cross-platform-parity", "--no-fetch"],
            workspace, extra_environment=brain_environment,
        )
        config = workspace / "brain.toml"
        if not config.is_file() or (api / "brain.toml").exists() or (worker / "brain.toml").exists():
            raise RuntimeError("Brain workspace escaped its parent or modified a target repository")
        _run(
            [*prefix, "-c", str(config), "refresh", "--no-fetch", "--no-discover"],
            workspace, extra_environment=brain_environment,
        )
        after = {repo.name: _run(["git", "status", "--porcelain"], repo) for repo in (api, worker)}
        if before != after or any(after.values()):
            raise RuntimeError(f"target repository changed during init/refresh: {after}")
        if b"\r\n" not in (worker / "src/main/java/example/OrderListener.java").read_bytes():
            raise RuntimeError("CRLF Git fixture was normalized by the platform")
        if b"\r\n" in (api / "src/main/resources/lf.properties").read_bytes():
            raise RuntimeError("LF Git fixture was normalized by the platform")

        _run(
            [*prefix, "-c", str(config), "start", "PARITY", "--text", "Trace the order flow", "--no-sync", "--no-copy", "--json"],
            workspace, extra_environment=brain_environment,
        )
        request = workspace / "request.json"
        request.write_text(json.dumps({"INVESTIGATION_REQUEST": {
            "version": 5, "mode": "flow_trace", "objective": "Trace GET /orders/{id} through order.created",
            "runtime_facts": ["production request reaches OrderController"], "hypotheses": [],
            "required": ["production entry point", "tests", "cross-repository integration flow"],
            "resolve": ["OrderController", "order.created"],
            "anchors": [
                {"kind": "endpoint", "value": "/orders/{id}"},
                {"kind": "topic", "value": "order.created"},
                {"kind": "symbol", "value": "OrderController"},
            ],
            "base_context_id": None, "checkpoint": True, "wave": 1,
        }}, sort_keys=True), encoding="utf-8")
        _run(
            [*prefix, "-c", str(config), "ctx", "PARITY", "--file", str(request), "--no-copy", "--json"],
            workspace, extra_environment=brain_environment,
        )
        result = _projection(workspace)
        if (
            not result["entity_ids"] or not result["routing"]["repositories"] or not result["verified_evidence"]
            or not any((flow or {}).get("steps") for flow in result["flows"].values())
        ):
            raise RuntimeError("native parity fixture did not produce routed entities, verified evidence, and flow steps")
        result["newline_fixtures"] = {
            "lf": hashlib.sha256((api / "src/main/resources/lf.properties").read_bytes()).hexdigest(),
            "crlf": hashlib.sha256((worker / "src/main/java/example/OrderListener.java").read_bytes()).hexdigest(),
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"platform": platform, "result": result}, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def compare(directory: Path) -> None:
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.rglob("*.json"))]
    platforms = {str(report.get("platform")) for report in reports}
    if platforms != PLATFORMS:
        raise RuntimeError(f"cross-platform reports are incomplete: {sorted(platforms)}")
    baseline = reports[0]["result"]
    mismatches = [str(report["platform"]) for report in reports[1:] if report.get("result") != baseline]
    if mismatches:
        raise RuntimeError(f"cross-platform deterministic behavior differs: {', '.join(mismatches)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--brain", type=Path)
    parser.add_argument("--source", action="store_true")
    parser.add_argument("--platform", choices=sorted(PLATFORMS))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compare", type=Path)
    args = parser.parse_args()
    if args.compare:
        compare(args.compare)
    elif args.brain and args.platform and args.output:
        exercise(args.brain, args.platform, args.output, source=args.source)
    else:
        parser.error("use --compare DIR or --brain PATH --platform NAME --output PATH")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

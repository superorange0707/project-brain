from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from ..platforms import logical_path, start_managed_process, terminate_process_tree, trusted_path_executable

IGNORED_DIRS = (".git", ".idea", ".venv", "node_modules", "target", "build", "dist", "vendor", "generated")
SENSITIVE_GLOBS = (".env", ".envrc", "credentials", "credentials.json", "service-account.json", "id_rsa", "id_ed25519", "*.key", "*.pem", "*.p12", "*.pfx", "*.jks")
MAX_RIPGREP_LINE_BYTES = 1024 * 1024
MAX_RIPGREP_OUTPUT_BYTES = 8 * 1024 * 1024


def search(root: Path, pattern: str, *, fixed: bool, max_results: int, timeout_seconds: float = 10.0, reserve: Callable[[], bool] | None = None) -> tuple[list[tuple[str, int, str]], dict[str, int | float | bool]] | None:
    """Stream `rg` output and terminate its process group as soon as the budget is met."""
    executable = trusted_path_executable("rg")
    if not executable or not root.is_dir():
        return None
    if reserve is not None and not reserve():
        return None
    command = [str(executable), "--line-number", "--with-filename", "--path-separator", "/", "--color", "never", "--no-messages"]
    command.extend(f"--glob=!{directory}/**" for directory in IGNORED_DIRS)
    command.extend(f"--glob=!{pattern}" for pattern in SENSITIVE_GLOBS)
    if fixed:
        command.append("--fixed-strings")
    command.extend(["--", pattern, "."])
    started = time.perf_counter()
    process: subprocess.Popen[bytes] | None = None
    results: list[tuple[str, int, str]] = []
    output_bytes = 0
    timed_out = False
    output: queue.Queue[bytes | None] = queue.Queue(maxsize=8)
    stopped = threading.Event()
    reader: threading.Thread | None = None
    tree_reaped = False
    try:
        process = start_managed_process(
            command,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert process.stdout is not None

        def read_output() -> None:
            def deliver(value: bytes | None) -> bool:
                while not stopped.is_set():
                    try:
                        output.put(value, timeout=.05)
                        return True
                    except queue.Full:
                        continue
                return False

            try:
                while not stopped.is_set():
                    raw = process.stdout.readline(MAX_RIPGREP_LINE_BYTES + 1)
                    if not deliver(raw or None) or not raw:
                        return
            except (OSError, ValueError):
                try:
                    output.put_nowait(None)
                except queue.Full:
                    pass

        reader = threading.Thread(target=read_output, name="brain-ripgrep-output", daemon=True)
        reader.start()
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                raw_bytes = output.get(timeout=remaining)
            except queue.Empty:
                timed_out = True
                break
            if raw_bytes is None:
                break
            output_bytes += len(raw_bytes)
            if len(raw_bytes) > MAX_RIPGREP_LINE_BYTES or output_bytes > MAX_RIPGREP_OUTPUT_BYTES:
                timed_out = True
                break
            raw = raw_bytes.decode("utf-8", errors="replace")
            try:
                path, line, text = raw.rstrip("\n").split(":", 2)
                results.append((logical_path(path), int(line), text))
            except ValueError:
                continue
            if len(results) >= max_results:
                break
        stopped.set()
        if timed_out or len(results) >= max_results:
            terminate_process_tree(process, graceful_timeout=0 if timed_out else 1)
            tree_reaped = True
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            terminate_process_tree(process, graceful_timeout=0)
            tree_reaped = True
        else:
            # Close the retained Windows Job Object and reap descendants that
            # outlived a successful leader before returning the query result.
            if not tree_reaped:
                terminate_process_tree(process, graceful_timeout=0)
                tree_reaped = True
    except OSError:
        return None
    finally:
        stopped.set()
        if process and process.stdout:
            process.stdout.close()
        if reader is not None:
            reader.join(timeout=1)
    return results, {
        "subprocesses": 1,
        "bytes_scanned": output_bytes,
        "raw_hits": len(results),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "timed_out": timed_out,
        "terminated_on_budget": len(results) >= max_results,
    }

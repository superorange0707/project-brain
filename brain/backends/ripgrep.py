from __future__ import annotations

import os
import signal
import shutil
import subprocess
import time
from pathlib import Path

IGNORED_DIRS = (".git", ".idea", ".venv", "node_modules", "target", "build", "dist", "vendor", "generated")
SENSITIVE_GLOBS = (".env", ".envrc", "credentials", "credentials.json", "service-account.json", "id_rsa", "id_ed25519", "*.key", "*.pem", "*.p12", "*.pfx", "*.jks")


def search(root: Path, pattern: str, *, fixed: bool, max_results: int, timeout_seconds: float = 10.0) -> tuple[list[tuple[str, int, str]], dict[str, int | float | bool]] | None:
    """Stream `rg` output and terminate its process group as soon as the budget is met."""
    executable = shutil.which("rg")
    if not executable or not root.is_dir():
        return None
    command = [executable, "--line-number", "--with-filename", "--color", "never", "--no-messages"]
    command.extend(f"--glob=!{directory}/**" for directory in IGNORED_DIRS)
    command.extend(f"--glob=!{pattern}" for pattern in SENSITIVE_GLOBS)
    if fixed:
        command.append("--fixed-strings")
    command.extend(["--", pattern, "."])
    started = time.perf_counter()
    process: subprocess.Popen[str] | None = None
    results: list[tuple[str, int, str]] = []
    output_bytes = 0
    timed_out = False
    try:
        process = subprocess.Popen(
            command,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=os.name != "nt",
        )
        assert process.stdout is not None
        for raw in process.stdout:
            output_bytes += len(raw.encode("utf-8", errors="replace"))
            if time.perf_counter() - started > timeout_seconds:
                timed_out = True
                break
            try:
                path, line, text = raw.rstrip("\n").split(":", 2)
                results.append((path.removeprefix("./"), int(line), text))
            except ValueError:
                continue
            if len(results) >= max_results:
                break
        if timed_out or len(results) >= max_results:
            if os.name == "nt":
                process.terminate()
            else:
                os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    except OSError:
        return None
    finally:
        if process and process.stdout:
            process.stdout.close()
    return results, {
        "subprocesses": 1,
        "bytes_scanned": output_bytes,
        "raw_hits": len(results),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "timed_out": timed_out,
        "terminated_on_budget": len(results) >= max_results,
    }

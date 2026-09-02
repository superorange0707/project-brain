from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import shutil
import socket
import ssl
import stat
import subprocess
import tarfile
import tempfile
import time
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener, urlopen
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol

from .locks import model_lane
from .platforms import (
    atomic_managed_text_write,
    filesystem_component,
    normalize_platform_id,
    platform_id,
    read_managed_text,
    start_managed_process,
    terminate_process_tree,
)

if TYPE_CHECKING:
    from .core import Settings

try:
    import truststore
except ImportError:  # pragma: no cover - package releases declare this dependency
    truststore = None

REQUIRED_MANIFEST_FIELDS = {
    "pack_id", "capability", "model_family", "upstream_model", "upstream_revision", "license",
    "runtime_name", "runtime_revision", "minimum_brain_version",
}
PRODUCTION_PROVENANCE_FIELDS = {
    "weight_format", "quantization", "weight_sha256", "tokenizer_file", "tokenizer_sha256", "pooling", "normalization",
    "query_instruction_version", "document_card_version", "chunk_schema_version", "embedding_dimension", "converter_revision",
}


def valid_embedding_vector(value: object, *, dimension: int | None = None) -> list[float] | None:
    if not isinstance(value, list) or not value or (dimension is not None and len(value) != dimension):
        return None
    try:
        vector = [float(number) for number in value]
    except (TypeError, ValueError, OverflowError):
        return None
    if any(not math.isfinite(number) for number in vector) or not any(number != 0.0 for number in vector):
        return None
    return vector
DEFAULT_RERANK_POOL = 40
MAX_RERANK_POOL = 80
DEFAULT_EMBEDDING_BATCH_SIZE = 16
DEFAULT_BENCHMARK_SAMPLES = 3
DEFAULT_MODEL_LATENCY_BUDGET_MS = 3_000
DEFAULT_RUNTIME_MAX_REQUESTS = 64
SUPPORTED_NATIVE_PLATFORMS = {
    "darwin-arm64", "darwin-amd64", "linux-arm64", "linux-amd64", "windows-amd64",
}
# Native llama.cpp backends can differ slightly between a batch request and
# equivalent single-item requests. This permits only bounded floating-point
# reduction drift; reference-vector cosine and ranking gates remain independent
# conformance requirements.
EMBEDDING_BATCH_PARITY_TOLERANCE = 2e-3
# A production reranker is evaluated once as a bounded batch and once as the
# equivalent one-document requests. Native llama.cpp backends differ slightly
# in floating-point reduction order (Windows CPU measured 0.00111086); ranking
# and the independent official-reference score gate remain mandatory.
RERANKER_BATCH_PARITY_TOLERANCE = 2e-3

# Each entry is added only after its separately versioned model-pack release
# passes final-release checksum verification and a clean installation check.
# Never resolve an unpinned "latest" release at install time.
OFFICIAL_PACKS: dict[str, dict[str, dict[str, str]]] = {
    "semantic": {
        "darwin-arm64": {
            "pack_id": "qwen3-embedding-4b-q6k-darwin-arm64",
            "descriptor_url": (
                "https://github.com/superorange0707/project-brain/releases/download/"
                "semantic-pack-v1.0.6/qwen3-embedding-4b-q6k-darwin-arm64-descriptor.json"
            ),
            "descriptor_sha256": "cbd09af575fb1b2e036abc17ed3e693e5bab4807af19efd2c1a9b5cd75ae8afc",
        },
        "windows-amd64": {
            "pack_id": "qwen3-embedding-4b-q6k-windows-amd64",
            "descriptor_url": (
                "https://github.com/superorange0707/project-brain/releases/download/"
                "semantic-pack-windows-v1.0.0/qwen3-embedding-4b-q6k-windows-amd64-descriptor.json"
            ),
            "descriptor_sha256": "69ca378fc2a00f01b23ae047ab46a7137c1b952d3c07a478350aaf2e2c6e2a30",
        },
    },
    "precision": {
        "darwin-arm64": {
            "pack_id": "qwen3-reranker-4b-q6k-darwin-arm64",
            "descriptor_url": (
                "https://github.com/superorange0707/project-brain/releases/download/"
                "precision-pack-v1.0.2/qwen3-reranker-4b-q6k-darwin-arm64-descriptor.json"
            ),
            "descriptor_sha256": "9070626e90b0306237bdf208ce0991cbf3804ee1bbee4ddca28c93df288f7df7",
        },
        "windows-amd64": {
            "pack_id": "qwen3-reranker-4b-q6k-windows-amd64",
            "descriptor_url": (
                "https://github.com/superorange0707/project-brain/releases/download/"
                "precision-pack-windows-v1.0.0/qwen3-reranker-4b-q6k-windows-amd64-descriptor.json"
            ),
            "descriptor_sha256": "524ac460c07b55891029b1de54120c47664969cdc985df713c19957657150d59",
        },
    },
}
MODEL_PACK_DESCRIPTOR_SCHEMA = "project-brain-model-pack-v1"
MAX_MODEL_PACK_DESCRIPTOR_BYTES = 1024 * 1024
MAX_MODEL_PACK_SOURCE_ITEMS = 10_000
MAX_MODEL_PACK_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_MODEL_PACK_SUITE_BYTES = 8 * 1024 * 1024
MAX_MODEL_PACK_UNPACKED_BYTES = 128 * 1024 * 1024 * 1024
MAX_MODEL_PACK_ARCHIVE_SECONDS = 300.0
MAX_INSTALLED_PACK_DIRECTORIES = 1_024
MAX_INSTALLED_PACK_SCAN_SECONDS = 5.0
MAX_INSTALLED_PACK_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_MODEL_RUNTIME_REQUEST_BYTES = 16 * 1024 * 1024
MAX_MODEL_RUNTIME_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_EMBEDDING_DIMENSION = 16_384
MODEL_DOWNLOAD_TLS_ERROR = (
    "model download TLS certificate verification failed; certificate and hostname verification remain enabled. "
    "Run 'brain doctor' and ensure the enterprise root is trusted by the operating system or configure models.ca_bundle."
)

# A pack-owned llama.cpp process is deliberately bound to this exact address.
# Its requests must not inherit an enterprise proxy route: some proxy policies
# bypass ``localhost`` but not its numeric loopback spelling.  This opener is
# used only by ManagedLlamaCppRuntime after it has verified the pack and
# created the process; external model downloads continue to use ``urlopen``.
_MANAGED_LOOPBACK_OPENER = build_opener(ProxyHandler({}))


def _json_request_bytes(body: dict[str, object]) -> bytes:
    """Serialize a local-runtime request once so size checks match the POST."""
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def embedding_request_bytes(texts: list[str], *, instruction: str = "", input_suffix: str = "", dimension: int | None = None) -> int:
    """Return the exact UTF-8 JSON request-body size for an embedding batch."""
    payload: dict[str, object] = {"input": [instruction + text + input_suffix for text in texts]}
    if dimension:
        payload["dimensions"] = dimension
    return len(_json_request_bytes(payload))


def _version(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split(".") if part.isdigit())


class ModelRuntime(Protocol):
    def embed(self, texts: list[str], instruction: str = "", dimension: int | None = None) -> list[list[float]]: ...
    def rerank(self, query: str, documents: list[str], instruction: str = "") -> list[float]: ...
    def warmup(self) -> None: ...
    def health(self) -> dict[str, object]: ...
    def shutdown(self) -> None: ...


@dataclass
class DeterministicRuntime:
    """Test-only runtime used for conformance tests; never selected without an explicit test pack."""

    default_dimension: int = 64

    def embed(self, texts: list[str], instruction: str = "", dimension: int | None = None) -> list[list[float]]:
        size = dimension or self.default_dimension
        vectors: list[list[float]] = []
        for text in texts:
            values = [0.0] * size
            for token in (instruction + "\n" + text).lower().split():
                digest = hashlib.sha256(token.encode("utf-8")).digest()
                values[int.from_bytes(digest[:4], "big") % size] += 1.0 if digest[4] & 1 else -1.0
            length = math.sqrt(sum(value * value for value in values)) or 1.0
            vectors.append([round(value / length, 7) for value in values])
        return vectors

    def rerank(self, query: str, documents: list[str], instruction: str = "") -> list[float]:
        query_tokens = set((instruction + " " + query).lower().split())
        return [float(len(query_tokens & set(document.lower().split()))) for document in documents]

    def warmup(self) -> None:
        self.embed(["warmup"])

    def health(self) -> dict[str, object]:
        return {"ok": True, "runtime": "deterministic-test", "localhost_only": True}

    def shutdown(self) -> None:
        return None


@dataclass
class LlamaCppRuntime:
    """Client for an already-started local-only pinned llama.cpp runtime."""

    endpoint: str
    timeout_seconds: float = 30.0
    api_key: str | None = None
    input_suffix: str = ""
    direct_loopback: bool = False

    def __post_init__(self) -> None:
        parsed = urlparse(self.endpoint)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("model runtime must bind only loopback HTTP or a Unix socket")
        if self.direct_loopback and parsed.hostname != "127.0.0.1":
            raise ValueError("direct model runtime transport is restricted to a pack-owned 127.0.0.1 endpoint")
        self.endpoint = self.endpoint.rstrip("/")

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _open(self, request: Request):
        if self.direct_loopback:
            return _MANAGED_LOOPBACK_OPENER.open(request, timeout=self.timeout_seconds)
        return urlopen(request, timeout=self.timeout_seconds)

    def _post(self, path: str, body: dict[str, object]) -> dict[str, object]:
        payload = _json_request_bytes(body)
        if len(payload) > MAX_MODEL_RUNTIME_REQUEST_BYTES:
            raise RuntimeError("local runtime request exceeds its byte limit")
        request = Request(self.endpoint + path, data=payload, method="POST", headers=self._headers())
        with self._open(request) as response:
            raw = response.read(MAX_MODEL_RUNTIME_RESPONSE_BYTES + 1)
        if len(raw) > MAX_MODEL_RUNTIME_RESPONSE_BYTES:
            raise RuntimeError("local runtime response exceeds its byte limit")
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise RuntimeError("local runtime returned an invalid response")
        return value

    def embed(self, texts: list[str], instruction: str = "", dimension: int | None = None) -> list[list[float]]:
        payload: dict[str, object] = {"input": [instruction + text + self.input_suffix for text in texts]}
        if dimension:
            payload["dimensions"] = dimension
        value = self._post("/v1/embeddings", payload)
        rows = value.get("data") or []
        vectors = [list(item["embedding"]) for item in rows if isinstance(item, dict) and isinstance(item.get("embedding"), list)]
        normalized = [valid_embedding_vector(vector, dimension=dimension) for vector in vectors]
        if len(vectors) != len(texts) or any(vector is None for vector in normalized):
            raise RuntimeError("local embedding runtime returned an incomplete vector batch")
        return [vector for vector in normalized if vector is not None]

    def rerank(self, query: str, documents: list[str], instruction: str = "") -> list[float]:
        value = self._post("/rerank", {"query": instruction + query if instruction else query, "documents": documents})
        rows = value.get("results") or []
        indexed = {
            int(item["index"]): float(item["relevance_score"])
            for item in rows
            if isinstance(item, dict) and isinstance(item.get("index"), int) and item.get("relevance_score") is not None
        }
        scores = [indexed[index] for index in range(len(documents))] if len(indexed) == len(documents) else []
        if len(scores) != len(documents) or any(not math.isfinite(score) for score in scores):
            raise RuntimeError("local reranker runtime returned incomplete or invalid scores")
        return scores

    def warmup(self) -> None:
        self.health()

    def health(self) -> dict[str, object]:
        request = Request(self.endpoint + "/health")
        with self._open(request) as response:
            return {"ok": response.status < 400, "runtime": "llama.cpp", "localhost_only": True}

    def shutdown(self) -> None:
        """The runtime is externally owned; Project Brain never kills an unknown process."""
        return None


def _pack_file(manifest: dict[str, Any], value: object, field: str) -> Path:
    root = Path(str(manifest.get("installed_path") or "")).resolve()
    if not root.is_dir():
        raise RuntimeError("verified model pack has no installed local directory")
    candidate = (root / str(value or "")).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise RuntimeError(f"model pack {field} is missing or escapes its pack directory")
    return candidate


def _check_pack_integrity(manifest: dict[str, Any]) -> None:
    """Check every declared artifact before executing a pack-owned binary."""
    artifacts = manifest.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        raise RuntimeError("model pack artifacts are invalid")
    for name, expected in artifacts.items():
        target = _pack_file(manifest, name, "artifact")
        if _sha256(target) != str(expected).lower():
            raise RuntimeError(f"model pack artifact changed after verification: {name}")


class ManagedLlamaCppRuntime:
    """A short-lived, pack-owned local llama.cpp server with no network route."""

    def __init__(self, manifest: dict[str, Any]):
        self.manifest = manifest
        self.process: subprocess.Popen[bytes] | None = None
        self.client: LlamaCppRuntime | None = None
        self.request_count = 0

    def _start(self) -> LlamaCppRuntime:
        try:
            max_requests = max(1, int(self.manifest.get("max_requests_per_runtime") or DEFAULT_RUNTIME_MAX_REQUESTS))
            request_timeout_seconds = min(300.0, max(5.0, float(self.manifest.get("request_timeout_seconds") or 30.0)))
            startup_timeout_seconds = min(120.0, max(1.0, float(self.manifest.get("startup_timeout_seconds") or 30.0)))
        except (TypeError, ValueError) as error:
            raise RuntimeError("model pack runtime limits must be numeric") from error
        if self.client is not None and self.process is not None and self.process.poll() is None and self.request_count < max_requests:
            return self.client
        if self.client is not None:
            self.shutdown()
        _check_pack_integrity(self.manifest)
        binary = _pack_file(self.manifest, self.manifest.get("runtime_binary"), "runtime_binary")
        model = _pack_file(self.manifest, self.manifest.get("model_file"), "model_file")
        if not os.access(binary, os.X_OK):
            raise RuntimeError("model pack runtime_binary is not executable")
        capability = str(self.manifest.get("capability"))
        if capability not in {"embedding", "reranker"}:
            raise RuntimeError("managed llama.cpp pack must provide embedding or reranker capability")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            port = int(listener.getsockname()[1])
        key = secrets.token_urlsafe(24)
        command = [
            str(binary), "--model", str(model), "--host", "127.0.0.1", "--port", str(port),
            "--api-key", key, "--offline", "--no-webui",
        ]
        if capability == "embedding":
            command.extend(["--embedding", "--pooling", "last"])
        else:
            command.extend(["--embedding", "--pooling", "rank", "--reranking"])
        runtime_args = self.manifest.get("runtime_args") or []
        if not isinstance(runtime_args, list) or not all(isinstance(value, str) and value for value in runtime_args):
            raise RuntimeError("model pack runtime_args must be a list of non-empty strings")
        # Published pre-v1 Darwin packs carried the same capability flags that
        # Brain now owns. Accept only exact, behavior-preserving duplicates;
        # conflicting values and every other protected override still fail.
        desired_pooling = "last" if capability == "embedding" else "rank"
        compatible_args: list[str] = []
        index = 0
        while index < len(runtime_args):
            value = runtime_args[index]
            if value == "--pooling" and index + 1 < len(runtime_args) and runtime_args[index + 1] == desired_pooling:
                index += 2
                continue
            if value == f"--pooling={desired_pooling}":
                index += 1
                continue
            if capability == "reranker" and value == "--reranking":
                index += 1
                continue
            compatible_args.append(value)
            index += 1
        protected = {
            "--model", "-m", "--host", "--port", "--api-key", "--hf-repo", "--hf-file",
            "--offline", "--no-webui", "--embedding", "--rerank", "--reranking", "--pooling",
        }
        if any(
            value in protected or any(value.startswith(flag + "=") for flag in protected)
            for value in compatible_args
        ):
            raise RuntimeError("model pack runtime_args may not override Project Brain local runtime controls")
        command.extend(compatible_args)
        try:
            self.process = start_managed_process(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            raise RuntimeError("pack-owned llama.cpp runtime failed to start") from error
        self.client = LlamaCppRuntime(
            f"http://127.0.0.1:{port}",
            api_key=key,
            timeout_seconds=request_timeout_seconds,
            input_suffix=str(self.manifest.get("input_suffix") or ""),
            direct_loopback=True,
        )
        deadline = time.monotonic() + startup_timeout_seconds
        health_transport_failed = False
        health_unavailable = False
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.shutdown()
                raise RuntimeError("pack-owned llama.cpp runtime exited before becoming healthy")
            try:
                if self.client.health().get("ok"):
                    return self.client
            except OSError:
                health_transport_failed = True
            else:
                health_unavailable = True
            time.sleep(0.05)
        self.shutdown()
        if health_unavailable:
            raise RuntimeError("pack-owned llama.cpp runtime is alive but health endpoint did not become ready before startup timeout")
        if health_transport_failed:
            raise RuntimeError("pack-owned llama.cpp runtime health transport failed before startup timeout")
        raise RuntimeError("pack-owned llama.cpp runtime start timed out before a health check")

    def embed(self, texts: list[str], instruction: str = "", dimension: int | None = None) -> list[list[float]]:
        for attempt in range(2):
            try:
                value = self._start().embed(texts, instruction, dimension)
                self.request_count += 1
                return value
            except OSError:
                self.shutdown()
                if attempt:
                    raise
        raise AssertionError("unreachable")

    def rerank(self, query: str, documents: list[str], instruction: str = "") -> list[float]:
        for attempt in range(2):
            try:
                value = self._start().rerank(query, documents, instruction)
                self.request_count += 1
                return value
            except OSError:
                self.shutdown()
                if attempt:
                    raise
        raise AssertionError("unreachable")

    def warmup(self) -> None:
        self._start().health()

    def health(self) -> dict[str, object]:
        return self._start().health()

    def shutdown(self) -> None:
        process, self.process, self.client = self.process, None, None
        self.request_count = 0
        if process is None:
            return
        terminate_process_tree(process, graceful_timeout=3)


def model_root(settings: Settings) -> Path:
    configured = settings.state_dir / "models"
    root = configured.resolve()
    if configured.is_symlink() or root.parent != settings.state_dir.resolve():
        raise ValueError("model pack root escapes managed state")
    return root


def _tuning_path(settings: Settings) -> Path:
    return settings.state_dir / "model-tuning.json"


def _model_tuning(settings: Settings, pack_id: str) -> dict[str, Any]:
    """Read only a tuning result made for this exact locally verified pack."""
    try:
        result = json.loads(read_managed_text(
            settings.state_dir, _tuning_path(settings), max_bytes=16 * 1024 * 1024,
        ))
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return result if isinstance(result, dict) and result.get("pack_id") == pack_id else {}


def embedding_batch_size(settings: Settings, pack_id: str, default: int = DEFAULT_EMBEDDING_BATCH_SIZE) -> int:
    recommended = _model_tuning(settings, pack_id).get("recommendations", {}).get("embedding_batch_size", default)
    try:
        return max(1, min(int(recommended), 256))
    except (TypeError, ValueError):
        return default


def _reranker_tuning(settings: Settings, pack_id: str, manifest: dict[str, Any] | None = None) -> tuple[int, int]:
    recommendations = _model_tuning(settings, pack_id).get("recommendations", {})
    manifest = manifest or {}
    try:
        default_batch = max(1, min(int(manifest.get("reranker_batch_size", DEFAULT_RERANK_POOL)), MAX_RERANK_POOL))
    except (TypeError, ValueError):
        default_batch = DEFAULT_RERANK_POOL
    try:
        default_pool = max(1, min(int(manifest.get("reranker_candidate_pool", DEFAULT_RERANK_POOL)), MAX_RERANK_POOL))
    except (TypeError, ValueError):
        default_pool = DEFAULT_RERANK_POOL
    try:
        batch_size = max(1, min(int(recommendations.get("reranker_batch_size", default_batch)), MAX_RERANK_POOL))
    except (TypeError, ValueError):
        batch_size = default_batch
    try:
        candidate_pool = max(1, min(int(recommendations.get("reranker_candidate_pool", default_pool)), MAX_RERANK_POOL))
    except (TypeError, ValueError):
        candidate_pool = default_pool
    return batch_size, candidate_pool


def _rerank_batched(
    runtime: ModelRuntime,
    query: str,
    documents: list[str],
    batch_size: int,
    instruction: str = "",
) -> list[float]:
    size = max(1, min(int(batch_size), MAX_RERANK_POOL))
    return [
        score
        for start in range(0, len(documents), size)
        for score in runtime.rerank(query, documents[start:start + size], instruction)
    ]


def _safe_pack_id(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in "._-" else "-" for char in value).strip(".-")
    if not safe:
        raise ValueError("pack_id is empty or unsafe")
    return safe


def _pack_directory(settings: Settings, pack_id: str) -> Path:
    root = model_root(settings)
    logical = _safe_pack_id(pack_id)
    legacy = root / logical
    canonical = root / filesystem_component(pack_id)
    if legacy != canonical and not legacy.is_symlink() and (legacy / "installed.json").is_file():
        try:
            if str(_load_manifest(legacy / "installed.json").get("pack_id") or "") == pack_id:
                return legacy
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    selected = canonical
    if selected.is_symlink():
        raise ValueError("model pack directory must not be a symbolic link")
    if selected.exists() and not selected.is_dir():
        raise ValueError("model pack path must be a directory")
    return selected


def _direct_pack_identity(settings: Settings, pack_id: str) -> tuple[Path, tuple[int, int]]:
    directory = _pack_directory(settings, pack_id)
    try:
        metadata = directory.lstat()
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("model pack directory must be a direct managed directory")
        if directory.resolve().parent != model_root(settings).resolve():
            raise ValueError("model pack directory escapes managed state")
    except OSError as error:
        raise ValueError(f"model pack is not installed: {pack_id}") from error
    return directory, (metadata.st_dev, metadata.st_ino)


def _load_manifest(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("model pack manifest must be a direct regular file")
    with path.open("rb") as source:
        raw = source.read(MAX_MODEL_PACK_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MODEL_PACK_MANIFEST_BYTES:
        raise ValueError("model pack manifest exceeds its byte limit")
    text = raw.decode("utf-8")
    if path.suffix == ".json":
        value = json.loads(text)
    else:
        from .core import simple_yaml_load

        value = simple_yaml_load(text)
    if not isinstance(value, dict):
        raise ValueError("model pack manifest must be a mapping")
    return {str(key): value for key, value in value.items()}


def validate_manifest(manifest: dict[str, Any]) -> None:
    missing = sorted(field for field in REQUIRED_MANIFEST_FIELDS if not str(manifest.get(field) or "").strip())
    if missing:
        raise ValueError("model pack manifest is missing: " + ", ".join(missing))
    if manifest["capability"] not in {"embedding", "reranker", "test"}:
        raise ValueError("model pack capability must be embedding, reranker, or test")
    if manifest["capability"] == "reranker":
        for field in ("reranker_batch_size", "reranker_candidate_pool"):
            value = manifest.get(field)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_RERANK_POOL
            ):
                raise ValueError(f"{field} must be between 1 and {MAX_RERANK_POOL}")
    _safe_pack_id(str(manifest["pack_id"]))
    if str(manifest["runtime_name"]) not in {"llama.cpp", "deterministic-test"}:
        raise ValueError("runtime_name must be a pinned local runtime")
    if str(manifest["runtime_name"]) == "deterministic-test" and manifest["capability"] != "test" and not manifest.get("test_only"):
        raise ValueError("deterministic-test runtime is allowed only for a test-only pack")
    dimension = manifest.get("embedding_dimension")
    minimum_dimension = 0 if manifest["capability"] == "reranker" else 1
    if dimension is not None and (
        isinstance(dimension, bool)
        or not isinstance(dimension, int)
        or not minimum_dimension <= dimension <= MAX_EMBEDDING_DIMENSION
    ):
        raise ValueError(f"embedding_dimension must be between 1 and {MAX_EMBEDDING_DIMENSION}")
    if manifest["capability"] != "test" and not manifest.get("test_only") and not manifest.get("artifacts"):
        raise ValueError("production model packs must declare checksummed artifacts")
    if manifest["capability"] != "test" and not manifest.get("test_only"):
        artifacts = manifest.get("artifacts") or {}
        suite = str(manifest.get("golden_suite") or "")
        suite_hash = str(manifest.get("golden_suite_hash") or "").lower()
        if not suite or len(suite_hash) != 64 or any(char not in "0123456789abcdef" for char in suite_hash):
            raise ValueError("production model packs require a checksummed golden_suite")
        if not isinstance(artifacts, dict) or str(artifacts.get(suite) or "").lower() != suite_hash:
            raise ValueError("golden_suite must be included in checksummed artifacts")
    if manifest["runtime_name"] == "llama.cpp" and not manifest.get("test_only"):
        artifacts = manifest.get("artifacts") or {}
        if not isinstance(artifacts, dict):
            raise ValueError("production llama.cpp packs must map artifacts to SHA-256 values")
        if manifest.get("runtime_url"):
            raise ValueError("production llama.cpp packs must own a checksummed runtime_binary and model_file, not runtime_url")
        required = {str(manifest.get("runtime_binary") or ""), str(manifest.get("model_file") or "")}
        if "" in required or not required <= set(str(name) for name in artifacts):
            raise ValueError("managed llama.cpp packs require checksummed runtime_binary and model_file artifacts")
    if manifest["capability"] != "test" and not manifest.get("test_only"):
        compatibility = manifest.get("runtime_compatibility")
        if compatibility is None:
            pack_id = str(manifest.get("pack_id") or "")
            declared = next(
                (candidate for candidate in SUPPORTED_NATIVE_PLATFORMS if pack_id.endswith("-" + candidate)),
                "",
            )
            if not declared:
                raise ValueError("production model packs require runtime_compatibility")
        else:
            if not isinstance(compatibility, dict):
                raise ValueError("runtime_compatibility must be a mapping")
            runtime_os = str(compatibility.get("os") or "")
            runtime_architecture = str(compatibility.get("architecture") or "")
            if not runtime_os or not runtime_architecture:
                raise ValueError("runtime_compatibility requires os and architecture")
            declared = normalize_platform_id(f"{runtime_os}-{runtime_architecture}")
        if declared not in SUPPORTED_NATIVE_PLATFORMS:
            raise ValueError(f"model pack runtime platform is unsupported: {declared}")
        if declared != platform_id():
            raise ValueError(f"model pack runtime is for {declared}, not {platform_id()}")
        missing_provenance = sorted(
            field
            for field in PRODUCTION_PROVENANCE_FIELDS
            if (
                field == "embedding_dimension" and (
                    isinstance(manifest.get(field), bool)
                    or not isinstance(manifest.get(field), int)
                    or not (0 if manifest["capability"] == "reranker" else 1)
                    <= int(manifest[field]) <= MAX_EMBEDDING_DIMENSION
                )
            )
            or (field != "embedding_dimension" and not str(manifest.get(field) or "").strip())
        )
        if missing_provenance:
            raise ValueError("production model pack is missing provenance fields: " + ", ".join(missing_provenance))
        weight_sha256 = str(manifest["weight_sha256"]).lower()
        tokenizer_sha256 = str(manifest["tokenizer_sha256"]).lower()
        if not _valid_sha256(weight_sha256) or not _valid_sha256(tokenizer_sha256):
            raise ValueError("production model pack weight_sha256 and tokenizer_sha256 must be SHA-256 values")
        if manifest["runtime_name"] == "llama.cpp" and str((manifest.get("artifacts") or {}).get(manifest.get("model_file")) or "").lower() != weight_sha256:
            raise ValueError("production model pack weight_sha256 must match the checksummed model_file artifact")
        if str((manifest.get("artifacts") or {}).get(manifest.get("tokenizer_file")) or "").lower() != tokenizer_sha256:
            raise ValueError("production model pack tokenizer_sha256 must match a checksummed tokenizer_file artifact")
    if manifest["capability"] == "embedding" and not manifest.get("test_only"):
        from .semantic import CARD_VERSION, CHUNK_SCHEMA_VERSION

        chunk_schema = str(manifest.get("chunk_schema_version") or "")
        card_version = str(manifest.get("document_card_version") or "")
        if chunk_schema != CHUNK_SCHEMA_VERSION or card_version != CARD_VERSION:
            raise ValueError(
                "embedding pack semantic schema is incompatible; install a pack built for "
                f"chunk schema {CHUNK_SCHEMA_VERSION} and card version {CARD_VERSION}, then run "
                "`brain index rebuild --backend semantic`"
            )
    from . import __version__

    if _version(str(manifest["minimum_brain_version"])) > _version(__version__):
        raise ValueError(f"model pack requires Project Brain {manifest['minimum_brain_version']} or newer")


def pack_compatibility_error(manifest: dict[str, Any]) -> str | None:
    """Return a safe operator-facing incompatibility reason for an installed pack."""
    try:
        validate_manifest(manifest)
    except ValueError as error:
        return str(error)
    return None


def _manifest_file(source: Path) -> Path:
    if source.is_dir():
        for name in ("manifest.json", "manifest.yaml", "manifest.yml"):
            if (source / name).is_file():
                return source / name
    if source.is_file() and source.name.startswith("manifest."):
        return source
    raise ValueError("model pack needs manifest.json, manifest.yaml, or manifest.yml")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _pack_definition(manifest: dict[str, Any]) -> str:
    """Canonical immutable pack definition; verification state is installation-local."""
    ignored = {"installed_path", "verified", "checked_artifacts", "conformance"}
    return json.dumps(
        {key: value for key, value in manifest.items() if key not in ignored},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )


def pack_compatibility_identity(manifest: dict[str, Any]) -> str:
    """Bind vector compatibility to immutable weights, tokenizer, and model inputs."""
    explicit = str(manifest.get("pack_compatibility_identity") or "")
    if manifest.get("test_only") is True and re.fullmatch(r"sha256:[0-9a-f]{64}", explicit):
        return explicit
    return "sha256:" + hashlib.sha256(_pack_definition(manifest).encode("utf-8")).hexdigest()


def _pack_artifacts_valid(manifest: dict[str, Any], root: Path) -> bool:
    candidate = dict(manifest)
    candidate["installed_path"] = str(root)
    try:
        _check_pack_integrity(candidate)
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _model_suite(manifest: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    """Load an optional local conformance suite only after its artifact hash is checked."""
    name = str(manifest.get("golden_suite") or "")
    if not name:
        return None
    expected = str(manifest.get("golden_suite_hash") or "").lower()
    path = _pack_file(manifest, name, "golden_suite")
    if not expected or _sha256(path) != expected:
        raise ValueError("golden_suite checksum mismatch")
    try:
        if path.is_symlink() or path.stat().st_size > MAX_MODEL_PACK_SUITE_BYTES:
            raise ValueError("golden_suite exceeds its byte limit")
        with path.open("rb") as source:
            raw = source.read(MAX_MODEL_PACK_SUITE_BYTES + 1)
        if len(raw) > MAX_MODEL_PACK_SUITE_BYTES:
            raise ValueError("golden_suite exceeds its byte limit")
        suite = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("golden_suite must be valid local JSON") from error
    if not isinstance(suite, dict):
        raise ValueError("golden_suite must be a JSON object")
    return suite, _sha256(path)


def _same_vectors(actual: list[list[float]], expected: list[list[float]], tolerance: float = 1e-5) -> bool:
    return len(actual) == len(expected) and all(
        len(left) == len(right) and all(abs(a - b) <= tolerance for a, b in zip(left, right, strict=True))
        for left, right in zip(actual, expected, strict=True)
    )


def _cosine(left: list[float], right: list[float]) -> float:
    denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(sum(value * value for value in right))
    return sum(a * b for a, b in zip(left, right, strict=True)) / denominator if denominator else 0.0


def _reference_vectors(value: object, *, count: int, dimension: int, case: int) -> list[list[float]]:
    if not isinstance(value, list) or len(value) != count:
        raise ValueError(f"golden_suite embedding case {case} needs one reference vector per text")
    try:
        vectors = [[float(number) for number in vector] for vector in value]
    except (TypeError, ValueError) as error:
        raise ValueError(f"golden_suite embedding case {case} has invalid reference vectors") from error
    if any(len(vector) != dimension or not any(vector) or any(not math.isfinite(number) for number in vector) for vector in vectors):
        raise ValueError(f"golden_suite embedding case {case} has invalid reference vectors")
    return vectors


def _reference_scores(value: object, *, count: int, case: int) -> list[float]:
    """Parse independently produced official-reference reranker scores."""
    if not isinstance(value, list) or len(value) != count:
        raise ValueError(f"golden_suite reranker case {case} needs one reference score per document")
    try:
        scores = [float(number) for number in value]
    except (TypeError, ValueError) as error:
        raise ValueError(f"golden_suite reranker case {case} has invalid reference scores") from error
    if not scores or any(not math.isfinite(score) for score in scores):
        raise ValueError(f"golden_suite reranker case {case} has invalid reference scores")
    return scores


def _reranker_parity_indices(value: object, *, count: int, expected_top: int, case: int) -> list[int]:
    """Select bounded, deterministic batch/single probes for large pools."""
    if value is None:  # Preserve conformance behavior for already-published packs.
        return list(range(count))
    if not isinstance(value, list) or not value or any(type(index) is not int for index in value):
        raise ValueError(f"golden_suite reranker case {case} has invalid batch_single_parity_indices")
    indices = [int(index) for index in value]
    expected = sorted({0, count // 2, count - 1, expected_top})
    if indices != expected:
        raise ValueError(f"golden_suite reranker case {case} has invalid batch_single_parity_indices")
    return indices


def _run_model_conformance(manifest: dict[str, Any]) -> dict[str, Any] | None:
    loaded = _model_suite(manifest)
    if loaded is None:
        return None
    suite, suite_hash = loaded
    capability = str(manifest["capability"])
    cases = suite.get("embedding" if capability in {"embedding", "test"} else "reranker")
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"golden_suite has no {capability} conformance cases")
    strict = not bool(manifest.get("test_only"))
    requirements = suite.get("requirements") if strict else {}
    if strict and not isinstance(requirements, dict):
        raise ValueError("production golden_suite requires a requirements object")
    try:
        long_input_min_chars = int(requirements.get("long_input_min_chars") or 0) if isinstance(requirements, dict) else 0
    except (TypeError, ValueError) as error:
        raise ValueError("production golden_suite has invalid requirements.long_input_min_chars") from error
    if strict and long_input_min_chars < 1:
        raise ValueError("production golden_suite requires requirements.long_input_min_chars")
    candidate_pool_sizes: list[int] = []
    candidate_pool_cases: list[dict[str, Any]] = []
    reranker_physical_batch_size: int | None = None
    if capability == "reranker" and strict:
        raw_sizes = requirements.get("reranker_candidate_pools")
        if not isinstance(raw_sizes, list) or not raw_sizes:
            raise ValueError("production reranker golden_suite requires requirements.reranker_candidate_pools")
        try:
            candidate_pool_sizes = [int(size) for size in raw_sizes]
        except (TypeError, ValueError) as error:
            raise ValueError("production reranker golden_suite has invalid requirements.reranker_candidate_pools") from error
        if candidate_pool_sizes != sorted(set(candidate_pool_sizes)) or any(size < 2 for size in candidate_pool_sizes):
            raise ValueError("production reranker golden_suite has invalid requirements.reranker_candidate_pools")
        raw_cases = suite.get("reranker_candidate_pools")
        if not isinstance(raw_cases, list):
            raise ValueError("production reranker golden_suite requires reranker_candidate_pools cases")
        by_size: dict[int, dict[str, Any]] = {}
        for raw_case in raw_cases:
            if not isinstance(raw_case, dict):
                raise ValueError("production reranker candidate-pool case must be an object")
            documents = raw_case.get("documents")
            if not isinstance(documents, list):
                raise ValueError("production reranker candidate-pool case has invalid documents")
            by_size[len(documents)] = raw_case
        if set(by_size) != set(candidate_pool_sizes):
            raise ValueError("production reranker candidate-pool cases do not match required pool sizes")
        candidate_pool_cases = [by_size[size] for size in candidate_pool_sizes]
        raw_batch_size = requirements.get("reranker_physical_batch_size")
        if raw_batch_size is not None and (
            isinstance(raw_batch_size, bool)
            or not isinstance(raw_batch_size, int)
            or not 2 <= raw_batch_size <= MAX_RERANK_POOL
        ):
            raise ValueError("production reranker golden_suite has invalid requirements.reranker_physical_batch_size")
        reranker_physical_batch_size = raw_batch_size
    observed_input_chars = 0
    ranking_exercised = False
    runtime = runtime_for_pack(manifest)
    passed: list[str] = []
    try:
        runtime.warmup()
        all_cases = list(cases) + candidate_pool_cases if capability == "reranker" else cases
        for number, case in enumerate(all_cases, start=1):
            if not isinstance(case, dict):
                raise ValueError(f"golden_suite case {number} must be an object")
            if capability in {"embedding", "test"}:
                texts = case.get("texts")
                dimension = case.get("dimension") or manifest.get("embedding_dimension")
                if not isinstance(texts, list) or not texts or not all(isinstance(text, str) for text in texts) or not isinstance(dimension, int) or dimension < 1:
                    raise ValueError(f"golden_suite embedding case {number} is invalid")
                truncate_to_chars = case.get("truncate_to_chars")
                if truncate_to_chars is not None and (not isinstance(truncate_to_chars, int) or truncate_to_chars < 1):
                    raise ValueError(f"golden_suite embedding case {number} has invalid truncate_to_chars")
                runtime_texts = [text[:truncate_to_chars] for text in texts] if truncate_to_chars is not None else texts
                instruction = str(case.get("instruction") or manifest.get("document_instruction") or "")
                batch = runtime.embed(runtime_texts, instruction=instruction, dimension=dimension)
                individual = [runtime.embed([text], instruction=instruction, dimension=dimension)[0] for text in runtime_texts]
                if (
                    len(batch) != len(runtime_texts)
                    or len(individual) != len(runtime_texts)
                    or any(len(vector) != dimension or any(not math.isfinite(value) for value in vector) for vector in [*batch, *individual])
                ):
                    raise ValueError(f"embedding conformance failed at case {number}")
                if not _same_vectors(batch, individual, tolerance=EMBEDDING_BATCH_PARITY_TOLERANCE):
                    maximum_delta = max(
                        abs(left - right)
                        for batch_vector, individual_vector in zip(batch, individual, strict=True)
                        for left, right in zip(batch_vector, individual_vector, strict=True)
                    )
                    raise ValueError(
                        f"embedding conformance failed at case {number}: maximum batch/single delta "
                        f"{maximum_delta:.9g} exceeds {EMBEDDING_BATCH_PARITY_TOLERANCE:.9g}"
                    )
                if case.get("normalized") and any(abs(math.sqrt(sum(value * value for value in vector)) - 1.0) > 1e-3 for vector in batch):
                    raise ValueError(f"embedding normalization conformance failed at case {number}")
                references = case.get("reference_vectors")
                if strict and references is None:
                    raise ValueError(f"production embedding case {number} requires reference_vectors")
                if references is not None:
                    reference_vectors = _reference_vectors(references, count=len(texts), dimension=dimension, case=number)
                    if strict and "minimum_cosine_to_reference" not in case:
                        raise ValueError(f"production embedding case {number} requires minimum_cosine_to_reference")
                    try:
                        minimum = float(case.get("minimum_cosine_to_reference") or 0.0)
                    except (TypeError, ValueError) as error:
                        raise ValueError(f"golden_suite embedding case {number} has invalid minimum_cosine_to_reference") from error
                    if not -1.0 <= minimum <= 1.0 or any(_cosine(vector, reference) < minimum for vector, reference in zip(batch, reference_vectors, strict=True)):
                        raise ValueError(f"embedding reference-vector conformance failed at case {number}")
                expected_order = case.get("expected_similarity_order")
                if strict and len(texts) > 1 and expected_order is None:
                    raise ValueError(f"production embedding case {number} requires expected_similarity_order")
                if expected_order is not None:
                    if not isinstance(expected_order, list) or sorted(expected_order) != list(range(1, len(texts))):
                        raise ValueError(f"golden_suite embedding case {number} has invalid expected_similarity_order")
                    order = sorted(range(1, len(texts)), key=lambda index: (-_cosine(batch[0], batch[index]), index))
                    if order != expected_order:
                        raise ValueError(f"embedding similarity-order conformance failed at case {number}")
                    ranking_exercised = ranking_exercised or len(texts) > 1
                observed_input_chars = max(observed_input_chars, *(len(text) for text in texts))
            else:
                query, documents, expected = case.get("query"), case.get("documents"), case.get("expected_order")
                expected_top = case.get("expected_top_index")
                if not isinstance(query, str) or not isinstance(documents, list) or not documents or not all(isinstance(document, str) for document in documents) or not isinstance(expected, list) or sorted(expected) != list(range(len(documents))):
                    raise ValueError(f"golden_suite reranker case {number} is invalid")
                if expected_top is None:
                    expected_top = expected[0]
                if not isinstance(expected_top, int) or expected_top < 0 or expected_top >= len(documents):
                    raise ValueError(f"golden_suite reranker case {number} has invalid expected_top_index")
                truncate_to_chars = case.get("truncate_to_chars")
                if truncate_to_chars is not None and (not isinstance(truncate_to_chars, int) or truncate_to_chars < 1):
                    raise ValueError(f"golden_suite reranker case {number} has invalid truncate_to_chars")
                runtime_documents = [document[:truncate_to_chars] for document in documents] if truncate_to_chars is not None else documents
                scores = _rerank_batched(
                    runtime, query, runtime_documents,
                    reranker_physical_batch_size or len(runtime_documents),
                )
                if len(scores) != len(documents) or any(not math.isfinite(float(score)) for score in scores):
                    raise ValueError(f"reranker conformance failed at case {number}")
                parity_indices = _reranker_parity_indices(
                    case.get("batch_single_parity_indices"),
                    count=len(documents), expected_top=expected_top, case=number,
                )
                single_scores = [runtime.rerank(query, [runtime_documents[index]])[0] for index in parity_indices]
                order = sorted(range(len(scores)), key=lambda index: (-float(scores[index]), index))
                references = case.get("reference_scores")
                if strict and references is None:
                    raise ValueError(f"production reranker case {number} requires reference_scores")
                if references is not None:
                    reference_scores = _reference_scores(references, count=len(documents), case=number)
                    if strict and "maximum_score_delta" not in case:
                        raise ValueError(f"production reranker case {number} requires maximum_score_delta")
                    try:
                        maximum_delta = float(case.get("maximum_score_delta") or 0.0)
                    except (TypeError, ValueError) as error:
                        raise ValueError(f"golden_suite reranker case {number} has invalid maximum_score_delta") from error
                    if maximum_delta < 0.0 or maximum_delta > 1.0 or any(abs(score - reference) > maximum_delta for score, reference in zip(scores, reference_scores, strict=True)):
                        raise ValueError(f"reranker reference-score conformance failed at case {number}")
                    reference_order = sorted(range(len(reference_scores)), key=lambda index: (-reference_scores[index], index))
                    if reference_order[0] != expected_top:
                        raise ValueError(f"reranker official-reference top-result conformance failed at case {number}")
                parity_scores = [scores[index] for index in parity_indices]
                if not _same_vectors([[float(score)] for score in parity_scores], [[float(score)] for score in single_scores], tolerance=RERANKER_BATCH_PARITY_TOLERANCE) or order[0] != expected_top:
                    raise ValueError(f"reranker conformance failed at case {number}")
                ranking_exercised = ranking_exercised or len(documents) > 1
                observed_input_chars = max(observed_input_chars, len(query), *(len(document) for document in documents))
            passed.append(str(case.get("id") or number))
    finally:
        runtime.shutdown()
    if strict and observed_input_chars < long_input_min_chars:
        raise ValueError("golden_suite does not exercise its declared long input")
    if strict and not ranking_exercised:
        raise ValueError("golden_suite does not exercise ranking parity")
    return {"passed": True, "suite_hash": suite_hash, "cases": passed}


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value.lower())


def model_download_ssl_context(settings: Settings) -> tuple[ssl.SSLContext, str]:
    """Return a verified context backed by OS trust, plus a safe diagnostic label.

    `truststore` uses the operating system certificate store on macOS and
    Windows and the platform OpenSSL store on Linux. An administrator-supplied
    PEM bundle is additive, never a replacement for TLS or hostname verification.
    """
    try:
        context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT) if truststore is not None else None
    except (OSError, ssl.SSLError, ValueError):
        context = None
    if context is None:
        context = ssl.create_default_context()
        source = "Python platform trust fallback"
    else:
        source = "system trust"
    configured_bundle = settings.model_ca_bundle
    environment_bundle = os.environ.get("SSL_CERT_FILE")
    bundle = configured_bundle or (Path(environment_bundle).expanduser() if environment_bundle else None)
    if bundle is not None:
        if not bundle.is_file():
            label = "models.ca_bundle" if configured_bundle else "SSL_CERT_FILE"
            raise ValueError(f"{label} does not name a readable CA bundle")
        try:
            context.load_verify_locations(cafile=str(bundle))
        except (OSError, ssl.SSLError) as error:
            label = "models.ca_bundle" if configured_bundle else "SSL_CERT_FILE"
            raise ValueError(f"{label} CA bundle could not be loaded") from error
        source += " + configured CA bundle" if configured_bundle else " + SSL_CERT_FILE"
    # Be explicit about both guarantees even when the underlying constructor
    # already sets them, so a future context implementation cannot relax them.
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context, source


def model_download_trust_status(settings: Settings) -> tuple[str, bool]:
    """Provide doctor with a useful trust diagnostic without exposing CA paths."""
    try:
        _, source = model_download_ssl_context(settings)
    except (OSError, ssl.SSLError, ValueError) as error:
        return f"ERROR — {error}; TLS downloads will fail closed", False
    return f"OK — {source}; certificate and hostname verification enabled", True


def managed_runtime_loopback_status() -> str:
    """Report the fixed local-runtime proxy boundary without exposing proxy values."""
    proxy_configured = any(
        bool(os.environ.get(name))
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
    )
    suffix = "; external proxy configuration detected" if proxy_configured else ""
    return "OK — direct no-proxy transport enforced for pack-owned 127.0.0.1 runtime" + suffix


def _open_model_download(settings: Settings, request: Request, timeout: float):
    """Open a one-time model-pack download with verified platform trust."""
    context, _ = model_download_ssl_context(settings)
    try:
        return urlopen(request, timeout=timeout, context=context)
    except ssl.SSLCertVerificationError as error:
        raise ValueError(MODEL_DOWNLOAD_TLS_ERROR) from error
    except URLError as error:
        if isinstance(error.reason, ssl.SSLCertVerificationError):
            raise ValueError(MODEL_DOWNLOAD_TLS_ERROR) from error
        raise


def _approved_pack_url(settings: Settings, source_url: str, *, final: bool = False) -> None:
    parsed = urlparse(source_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("model pack URL must be a credential-free HTTPS URL")
    configured = {str(value).lower().lstrip(".") for value in settings.model_install_hosts}
    configured_match = any(host == value or host.endswith("." + value) for value in configured)
    github_asset = host in {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
    if not configured_match and not github_asset:
        raise ValueError("model pack URL host is not approved; add it to models.approved_install_hosts")
    if host == "github.com" and not final and not any(
        marker in parsed.path for marker in ("/releases/download/", "/releases/latest/download/")
    ):
        raise ValueError("GitHub model packs must use an official release artifact URL")


def install_pack_url(settings: Settings, source_url: str, expected_sha256: str) -> dict[str, Any]:
    """Stage a pinned approved release once, then use the normal offline installer.

    This is intentionally an installation-time action, not a model-runtime
    dependency.  It accepts only HTTPS from a configured organization host or a
    GitHub Release path and rejects the bytes unless the caller supplied SHA-256
    matches exactly.
    """
    _approved_pack_url(settings, source_url)
    expected_sha256 = expected_sha256.lower().strip()
    if not _valid_sha256(expected_sha256):
        raise ValueError("remote model pack installation requires a 64-character --sha256")
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    staged: Path | None = None
    try:
        request = Request(source_url, headers={"User-Agent": "Project-Brain-model-pack/1"})
        with _open_model_download(settings, request, timeout=60) as response:
            _approved_pack_url(settings, response.geturl(), final=True)
            try:
                projected_bytes = int(response.headers.get("Content-Length") or 0)
            except ValueError as error:
                raise ValueError("model pack download has an invalid Content-Length") from error
            if projected_bytes < 1:
                raise ValueError("model pack download must provide Content-Length for disk preflight")
            from .ops import ensure_write_capacity

            ensure_write_capacity(settings, projected_bytes)
            handle = tempfile.NamedTemporaryFile(prefix="brain-pack-download-", suffix=".tar", dir=settings.state_dir, delete=False)
            staged = Path(handle.name)
            digest = hashlib.sha256()
            downloaded = 0
            with handle:
                while chunk := response.read(1024 * 1024):
                    downloaded += len(chunk)
                    if downloaded > projected_bytes:
                        raise ValueError("model pack download exceeded its declared Content-Length")
                    handle.write(chunk)
                    digest.update(chunk)
            if downloaded != projected_bytes:
                raise ValueError("model pack download size does not match its declared Content-Length")
        if digest.hexdigest() != expected_sha256:
            raise ValueError("downloaded model pack SHA-256 does not match --sha256")
        return install_pack(settings, staged)
    finally:
        if staged:
            staged.unlink(missing_ok=True)


def official_packs() -> list[dict[str, str]]:
    """List source-pinned Project Brain controlled pack descriptors."""
    current_platform = platform_id()
    return [
        {"alias": alias, **{key: value for key, value in pack.items() if key != "descriptor_sha256"}}
        for alias, platforms in sorted(OFFICIAL_PACKS.items())
        for pack in [platforms.get(current_platform)]
        if isinstance(pack, dict)
    ]


def _descriptor_error(message: str) -> ValueError:
    return ValueError(f"invalid Project Brain model-pack descriptor: {message}")


def _descriptor_artifact(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _descriptor_error(f"{field} must be an object")
    url = value.get("url")
    digest = str(value.get("sha256") or "").lower()
    size = value.get("size")
    if not isinstance(url, str) or not _valid_sha256(digest):
        raise _descriptor_error(f"{field} requires HTTPS url and SHA-256")
    try:
        size = int(size)
    except (TypeError, ValueError) as error:
        raise _descriptor_error(f"{field} requires a positive byte size") from error
    if size < 1:
        raise _descriptor_error(f"{field} requires a positive byte size")
    return {"url": url, "sha256": digest, "size": size}


def _download_verified_artifact(
    settings: Settings,
    artifact: dict[str, Any],
    destination: Path,
    *,
    append: bool = False,
) -> None:
    """Download one declared release asset with a size and digest gate."""
    source_url = str(artifact["url"])
    _approved_pack_url(settings, source_url)
    request = Request(source_url, headers={"User-Agent": "Project-Brain-model-pack/1"})
    with _open_model_download(settings, request, timeout=120) as response:
        _approved_pack_url(settings, response.geturl(), final=True)
        try:
            content_length = int(response.headers.get("Content-Length") or 0)
        except ValueError as error:
            raise ValueError("model pack download has an invalid Content-Length") from error
        if content_length != int(artifact["size"]):
            raise ValueError("model pack download size does not match its pinned descriptor")
        digest = hashlib.sha256()
        downloaded = 0
        with destination.open("ab" if append else "wb") as handle:
            while chunk := response.read(1024 * 1024):
                downloaded += len(chunk)
                if downloaded > int(artifact["size"]):
                    raise ValueError("model pack release artifact exceeded its pinned size")
                handle.write(chunk)
                digest.update(chunk)
    if downloaded != int(artifact["size"]):
        raise ValueError("model pack release artifact size does not match its pinned descriptor")
    if digest.hexdigest() != str(artifact["sha256"]):
        raise ValueError("model pack release artifact SHA-256 does not match its pinned descriptor")


def _archive_projected_size(source: Path, *, deadline: float | None = None) -> int:
    total = 0
    items = 0
    deadline = deadline or time.monotonic() + MAX_MODEL_PACK_ARCHIVE_SECONDS
    with tarfile.open(source, "r:*") as archive:
        for member in archive:
            items += 1
            if items > MAX_MODEL_PACK_SOURCE_ITEMS or time.monotonic() >= deadline:
                raise ValueError("model pack archive exceeds its item or time limit")
            if member.isfile():
                total += member.size
                if total > MAX_MODEL_PACK_UNPACKED_BYTES:
                    raise ValueError("model pack archive exceeds its unpacked byte limit")
    return total


_PackSourceProjection = tuple[int, tuple[int, int], dict[str, tuple[str, int, int, int, int]]]


def _directory_source_projection(
    source: Path, *, deadline: float | None = None,
) -> _PackSourceProjection:
    """Seal one bounded unpacked source tree before managed-state copying."""
    root = source.lstat()
    if source.is_symlink() or not stat.S_ISDIR(root.st_mode):
        raise ValueError("model pack source must be a direct directory")
    total = 0
    items = 0
    deadline = deadline or time.monotonic() + MAX_MODEL_PACK_ARCHIVE_SECONDS
    projection: dict[str, tuple[str, int, int, int, int]] = {}
    for item in source.rglob("*"):
        items += 1
        if items > MAX_MODEL_PACK_SOURCE_ITEMS or time.monotonic() >= deadline:
            raise ValueError("model pack source exceeds its item or time limit")
        metadata = item.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("model pack source must not contain symbolic links")
        relative = item.relative_to(source).as_posix()
        if stat.S_ISDIR(metadata.st_mode):
            kind = "directory"
            size = 0
        elif stat.S_ISREG(metadata.st_mode):
            kind = "file"
            size = int(metadata.st_size)
            total += size
            if total > MAX_MODEL_PACK_UNPACKED_BYTES:
                raise ValueError("model pack source exceeds its unpacked byte limit")
        else:
            raise ValueError("model pack source must contain only regular files and directories")
        projection[relative] = (
            kind, size, int(metadata.st_dev), int(metadata.st_ino), int(metadata.st_mtime_ns),
        )
    return total, (int(root.st_dev), int(root.st_ino)), projection


def _directory_projected_size(source: Path) -> int:
    """Apply the archive's hard source bounds to an unpacked local pack."""
    return _directory_source_projection(source)[0]


def _copy_bounded_pack_file(
    source: Path,
    destination: Path,
    expected: tuple[str, int, int, int, int],
    *,
    deadline: float,
    remaining_bytes: int,
) -> int:
    """Copy one sealed regular file without exceeding the reserved bytes."""
    _, expected_size, expected_device, expected_inode, expected_mtime = expected
    descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    copied = 0
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (expected_device, expected_inode)
            or opened.st_size != expected_size
            or opened.st_mtime_ns != expected_mtime
        ):
            raise ValueError("model pack source changed during installation")
        with os.fdopen(descriptor, "rb", closefd=False) as input_file, destination.open("xb") as output_file:
            while True:
                if time.monotonic() >= deadline:
                    raise ValueError("model pack source exceeds its item or time limit")
                chunk = input_file.read(min(1024 * 1024, expected_size - copied + 1))
                if not chunk:
                    break
                copied += len(chunk)
                if copied > expected_size or copied > remaining_bytes:
                    raise ValueError("model pack source changed or exceeded its unpacked byte limit")
                output_file.write(chunk)
        after = os.fstat(descriptor)
        current = source.lstat()
        if (
            copied != expected_size
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (expected_device, expected_inode, expected_size, expected_mtime)
            or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
            != (expected_device, expected_inode, expected_size, expected_mtime)
        ):
            raise ValueError("model pack source changed during installation")
        destination.chmod(opened.st_mode & 0o777)
        return copied
    finally:
        os.close(descriptor)


def _copy_bounded_pack_source(
    source: Path,
    destination: Path,
    sealed: _PackSourceProjection,
    *,
    deadline: float | None = None,
) -> None:
    """Copy exactly one sealed source tree under hard physical-operation bounds."""
    projected_bytes, root_identity, projection = sealed
    current_root = source.lstat()
    if (
        source.is_symlink()
        or not stat.S_ISDIR(current_root.st_mode)
        or (current_root.st_dev, current_root.st_ino) != root_identity
        or _directory_source_projection(source, deadline=deadline) != sealed
    ):
        raise ValueError("model pack source changed during installation")
    destination.mkdir()
    deadline = deadline or time.monotonic() + MAX_MODEL_PACK_ARCHIVE_SECONDS
    copied_bytes = 0
    for relative, expected in sorted(
        projection.items(), key=lambda item: (item[0].count("/"), item[0]),
    ):
        if time.monotonic() >= deadline:
            raise ValueError("model pack source exceeds its item or time limit")
        target = destination / relative
        if expected[0] == "directory":
            target.mkdir()
            continue
        copied_bytes += _copy_bounded_pack_file(
            source / relative,
            target,
            expected,
            deadline=deadline,
            remaining_bytes=projected_bytes - copied_bytes,
        )
    if (
        copied_bytes != projected_bytes
        or _directory_source_projection(source, deadline=deadline) != sealed
    ):
        raise ValueError("model pack source changed during installation")


def _extract_pack_archive(
    source: Path, destination: Path, *, deadline: float | None = None,
) -> None:
    total = 0
    written_total = 0
    items = 0
    deadline = deadline or time.monotonic() + MAX_MODEL_PACK_ARCHIVE_SECONDS
    with tarfile.open(source, "r:*") as archive:
        for member in archive:
            items += 1
            if items > MAX_MODEL_PACK_SOURCE_ITEMS or time.monotonic() >= deadline:
                raise ValueError("model pack archive exceeds its item or time limit")
            if member.isfile():
                total += member.size
                if total > MAX_MODEL_PACK_UNPACKED_BYTES:
                    raise ValueError("model pack archive exceeds its unpacked byte limit")
            target = (destination / member.name).resolve()
            if not target.is_relative_to(destination) or member.issym() or member.islnk():
                raise ValueError("model pack contains an unsafe path")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ValueError("model pack contains an unreadable file")
                with target.open("wb") as handle:
                    member_bytes = 0
                    while True:
                        if time.monotonic() >= deadline:
                            raise ValueError("model pack archive exceeds its item or time limit")
                        chunk = extracted.read(min(1024 * 1024, member.size - member_bytes + 1))
                        if not chunk:
                            break
                        member_bytes += len(chunk)
                        written_total += len(chunk)
                        if member_bytes > member.size or written_total > total:
                            raise ValueError("model pack archive member exceeded its declared size")
                        handle.write(chunk)
                if member_bytes != member.size:
                    raise ValueError("model pack archive member is incomplete")
                target.chmod(member.mode & 0o777)


def install_release_descriptor(settings: Settings, descriptor_url: str, expected_sha256: str) -> dict[str, Any]:
    """Install a multipart Project Brain release pack without a runtime network dependency."""
    expected_sha256 = expected_sha256.lower().strip()
    if not _valid_sha256(expected_sha256):
        raise ValueError("model-pack descriptor requires a 64-character SHA-256")
    _approved_pack_url(settings, descriptor_url)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="brain-pack-release-", dir=settings.state_dir))
    try:
        descriptor_path = temporary / "descriptor.json"
        request = Request(descriptor_url, headers={"User-Agent": "Project-Brain-model-pack/1"})
        with _open_model_download(settings, request, timeout=60) as response:
            _approved_pack_url(settings, response.geturl(), final=True)
            try:
                descriptor_length = int(response.headers.get("Content-Length") or 0)
            except ValueError as error:
                raise _descriptor_error("descriptor has an invalid Content-Length") from error
            if descriptor_length > MAX_MODEL_PACK_DESCRIPTOR_BYTES:
                raise _descriptor_error("descriptor exceeds its byte limit")
            digest = hashlib.sha256()
            downloaded = 0
            with descriptor_path.open("wb") as handle:
                while chunk := response.read(1024 * 1024):
                    downloaded += len(chunk)
                    if downloaded > MAX_MODEL_PACK_DESCRIPTOR_BYTES:
                        raise _descriptor_error("descriptor exceeds its byte limit")
                    handle.write(chunk)
                    digest.update(chunk)
            if descriptor_length and downloaded != descriptor_length:
                raise _descriptor_error("descriptor size does not match Content-Length")
        if digest.hexdigest() != expected_sha256:
            raise ValueError("model-pack descriptor SHA-256 does not match the pinned Core catalog")
        try:
            descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise _descriptor_error("descriptor is not JSON") from error
        if not isinstance(descriptor, dict) or descriptor.get("schema") != MODEL_PACK_DESCRIPTOR_SCHEMA:
            raise _descriptor_error(f"schema must be {MODEL_PACK_DESCRIPTOR_SCHEMA}")
        pack_id = _safe_pack_id(str(descriptor.get("pack_id") or ""))
        metadata = _descriptor_artifact(descriptor.get("metadata"), "metadata")
        model = descriptor.get("model")
        if not isinstance(model, dict):
            raise _descriptor_error("model must be an object")
        model_file = str(model.get("file") or "")
        if Path(model_file).name != model_file or not model_file:
            raise _descriptor_error("model.file must be a safe basename")
        model_sha256 = str(model.get("sha256") or "").lower()
        if not _valid_sha256(model_sha256):
            raise _descriptor_error("model requires SHA-256")
        parts = [_descriptor_artifact(part, "model.parts") for part in model.get("parts") or []]
        if not parts:
            raise _descriptor_error("model requires one or more parts")
        platform_name = normalize_platform_id(str(descriptor.get("platform") or ""))
        local_platform = platform_id()
        if not platform_name:
            raise _descriptor_error("platform is required")
        if platform_name not in SUPPORTED_NATIVE_PLATFORMS:
            raise _descriptor_error("platform is unsupported")
        if platform_name != local_platform:
            raise ValueError(f"model pack {pack_id} is for {platform_name}, not {local_platform}")
        from .ops import ensure_write_capacity

        ensure_write_capacity(settings, int(metadata["size"]) + sum(int(part["size"]) for part in parts))
        archive = temporary / "metadata.tar"
        _download_verified_artifact(settings, metadata, archive)
        ensure_write_capacity(settings, _archive_projected_size(archive))
        contents = temporary / "contents"
        contents.mkdir()
        _extract_pack_archive(archive, contents)
        assembled = contents / model_file
        # The archive and unpacked metadata now both occupy managed state.
        # Reserve the complete remaining model payload before appending the
        # first part so the temporary peak cannot exceed the workspace guard.
        ensure_write_capacity(settings, sum(int(part["size"]) for part in parts))
        for part in parts:
            _download_verified_artifact(settings, part, assembled, append=assembled.exists())
        if _sha256(assembled) != model_sha256:
            raise ValueError("assembled model SHA-256 does not match the pinned descriptor")
        manifest_path = _manifest_file(contents)
        manifest = _load_manifest(manifest_path)
        if _safe_pack_id(str(manifest.get("pack_id") or "")) != pack_id:
            raise _descriptor_error("pack_id does not match embedded manifest")
        compatibility = manifest.get("runtime_compatibility") or {}
        if isinstance(compatibility, dict) and compatibility.get("os") and compatibility.get("architecture"):
            manifest_platform = normalize_platform_id(
                f"{compatibility['os']}-{compatibility['architecture']}"
            )
        else:
            manifest_platform = next(
                (candidate for candidate in SUPPORTED_NATIVE_PLATFORMS if pack_id.endswith("-" + candidate)),
                "",
            )
        if not manifest_platform or manifest_platform != platform_name:
            raise _descriptor_error("platform does not match embedded manifest")
        if str(manifest.get("model_file") or "") != model_file or str(manifest.get("weight_sha256") or "").lower() != model_sha256:
            raise _descriptor_error("embedded manifest does not pin the assembled model")
        return install_pack(settings, contents)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def install_official_pack(settings: Settings, alias: str) -> dict[str, Any]:
    alias = alias.lower().strip()
    platforms = OFFICIAL_PACKS.get(alias)
    if platforms is None:
        raise ValueError(f"no Project Brain-controlled release pack is available for {alias}")
    pack = platforms.get(platform_id())
    if pack is None:
        raise ValueError(f"no Project Brain-controlled {alias} release pack is available for {platform_id()}")
    return install_release_descriptor(settings, str(pack["descriptor_url"]), str(pack["descriptor_sha256"]))


def install_pack(settings: Settings, source: Path) -> dict[str, Any]:
    """Install an already-local pack. Runtime never performs a network download."""
    supplied = source.expanduser()
    if supplied.is_symlink():
        raise ValueError("model pack source must not be a symbolic link")
    source = supplied.resolve()
    if not source.exists():
        raise ValueError(f"model pack does not exist: {source}")
    install_deadline = time.monotonic() + MAX_MODEL_PACK_ARCHIVE_SECONDS
    source_is_archive = source.is_file() and tarfile.is_tarfile(source)
    source_projection: _PackSourceProjection | None = None
    if source.is_dir():
        source_projection = _directory_source_projection(source, deadline=install_deadline)
        projected_bytes = source_projection[0]
    elif source_is_archive:
        projected_bytes = _archive_projected_size(source, deadline=install_deadline)
    else:
        source_projection = _directory_source_projection(source.parent, deadline=install_deadline)
        projected_bytes = source_projection[0]
    from .ops import ensure_write_capacity

    ensure_write_capacity(settings, projected_bytes * (2 if source_is_archive else 1))
    temporary: Path | None = None
    staging: Path | None = None
    try:
        if source_is_archive:
            temporary = Path(tempfile.mkdtemp(prefix="brain-pack-", dir=settings.state_dir))
            _extract_pack_archive(source, temporary, deadline=install_deadline)
            source = temporary
            source_projection = _directory_source_projection(source, deadline=install_deadline)
        copy_source = source if source.is_dir() else source.parent
        if source_projection is None:
            raise ValueError("model pack source projection is unavailable")
        manifest_path = _manifest_file(source)
        manifest = _load_manifest(manifest_path)
        validate_manifest(manifest)
        destination = _pack_directory(settings, str(manifest["pack_id"]))
        staging = destination.with_name(destination.name + ".installing")
        previous = destination.with_name(destination.name + ".previous")
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(previous, ignore_errors=True)
        staging.parent.mkdir(parents=True, exist_ok=True)
        _copy_bounded_pack_source(
            copy_source, staging, source_projection, deadline=install_deadline,
        )
        os.chmod(staging, 0o700)
        if not _pack_artifacts_valid(manifest, staging):
            raise ValueError("model pack contains an artifact that does not match its declared SHA-256")
        manifest["installed_path"] = str(destination)
        installed_path = staging / "installed.json"
        atomic_managed_text_write(staging, installed_path, json.dumps(manifest, indent=2) + "\n")
        staged_manifest = _load_manifest(installed_path)
        validate_manifest(staged_manifest)
        if destination.exists():
            try:
                installed = _load_manifest(destination / "installed.json")
                validate_manifest(installed)
            except (OSError, json.JSONDecodeError, ValueError):
                # A managed directory without valid publication metadata is an
                # incomplete prior install, not an immutable pack definition.
                destination.replace(previous)
                installed = None
            if installed is None:
                pass
            elif _pack_definition(installed) != _pack_definition(manifest):
                raise ValueError(
                    f"model pack ID {manifest['pack_id']} is immutable; publish changed weights or instructions under a new pack_id"
                )
            elif _pack_artifacts_valid(installed, destination):
                shutil.rmtree(staging, ignore_errors=True)
                return installed
            else:
                destination.replace(previous)
        try:
            staging.replace(destination)
        except Exception:
            if previous.exists():
                previous.replace(destination)
            raise
        finally:
            shutil.rmtree(previous, ignore_errors=True)
        semantic_state = settings.state_dir / "semantic-index.json"
        if semantic_state.is_file() and manifest["capability"] in {"embedding", "test"}:
            try:
                state = json.loads(read_managed_text(
                    settings.state_dir, semantic_state, max_bytes=64 * 1024 * 1024,
                ))
                if not isinstance(state, dict):
                    raise ValueError("Semantic state must be an object")
                state["stale"] = True
                state["stale_reason"] = f"embedding pack changed to {manifest['pack_id']}"
                atomic_managed_text_write(
                    settings.state_dir, semantic_state, json.dumps(state, separators=(",", ":")),
                )
            except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                pass
        from .catalog import connect

        connection = connect(settings)
        try:
            from datetime import UTC, datetime

            connection.execute(
                "INSERT OR REPLACE INTO model_packs(pack_id, capability, manifest_json, installed_at) VALUES (?, ?, ?, ?)",
                (manifest["pack_id"], manifest["capability"], json.dumps(manifest), datetime.now(UTC).isoformat()),
            )
            connection.commit()
        finally:
            connection.close()
        return manifest
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        if temporary:
            shutil.rmtree(temporary, ignore_errors=True)


def installed_packs(settings: Settings) -> list[dict[str, Any]]:
    root = model_root(settings)
    packs: list[dict[str, Any]] = []
    if not root.is_dir():
        return packs
    directories: list[Path] = []
    deadline = time.monotonic() + MAX_INSTALLED_PACK_SCAN_SECONDS
    scanned = 0
    for candidate in root.iterdir():
        scanned += 1
        if scanned > MAX_INSTALLED_PACK_DIRECTORIES or time.monotonic() >= deadline:
            packs.append({"pack_id": "listing-truncated", "invalid": True, "listing_truncated": True})
            return packs
        if not candidate.is_symlink() and candidate.is_dir():
            directories.append(candidate)
    manifest_bytes = 0
    for directory in sorted(directories):
        path = directory / "installed.json"
        try:
            manifest_bytes += path.lstat().st_size
            if manifest_bytes > MAX_INSTALLED_PACK_MANIFEST_BYTES or time.monotonic() >= deadline:
                packs.append({"pack_id": "listing-truncated", "invalid": True, "listing_truncated": True})
                return packs
            value = _load_manifest(path)
            pack_id = str(value.get("pack_id") or "")
            installed_path = Path(str(value.get("installed_path") or ""))
            if (
                not installed_path.is_absolute()
                or installed_path.resolve() != directory.resolve()
                or _pack_directory(settings, pack_id).resolve() != directory.resolve()
            ):
                raise ValueError("installed model pack path identity is invalid")
            packs.append(value)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            packs.append({"pack_id": directory.name, "invalid": True})
    return packs


def active_pack(settings: Settings, capability: str) -> dict[str, Any] | None:
    """Choose one audited local pack deterministically; never silently use a test runtime."""
    candidates = [
        pack for pack in installed_packs(settings)
        if pack.get("capability") == capability
        and pack.get("verified")
        and not pack.get("invalid")
        and pack_compatibility_error(pack) is None
    ]
    return min(candidates, key=lambda pack: str(pack["pack_id"])) if candidates else None


def verified_pack(settings: Settings, pack_id: str, capability: str) -> dict[str, Any] | None:
    """Resolve the exact verified pack retained by a pinned serving component."""
    try:
        directory, identity = _direct_pack_identity(settings, pack_id)
        value = json.loads(read_managed_text(
            settings.state_dir, directory / "installed.json",
            max_bytes=MAX_MODEL_PACK_MANIFEST_BYTES,
        ))
        if not isinstance(value, dict):
            return None
        pack = {str(key): item for key, item in value.items()}
        validate_manifest(pack)
        installed_path = Path(str(pack.get("installed_path") or ""))
        if (
            str(pack.get("pack_id") or "") != pack_id
            or pack.get("capability") != capability
            or not pack.get("verified")
            or installed_path.is_symlink()
            or not installed_path.is_absolute()
            or installed_path.resolve() != directory.resolve()
            or _direct_pack_identity(settings, pack_id) != (directory, identity)
            or pack_compatibility_error(pack) is not None
        ):
            return None
        return pack
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None


def verify_pack(settings: Settings, pack_id: str) -> dict[str, Any]:
    directory, identity = _direct_pack_identity(settings, pack_id)
    path = directory / "installed.json"
    manifest = _load_manifest(path)
    validate_manifest(manifest)
    installed_path = Path(str(manifest.get("installed_path") or ""))
    if (
        not installed_path.is_absolute()
        or installed_path.is_symlink()
        or installed_path.resolve() != directory.resolve()
    ):
        raise ValueError("installed model pack path identity is invalid")
    checked: list[str] = []
    artifacts = manifest.get("artifacts") or {}
    if artifacts and not isinstance(artifacts, dict):
        raise ValueError("artifacts must map file names to SHA-256 values")
    for name, expected in sorted(artifacts.items()):
        target = _pack_file(manifest, name, "artifact")
        if _sha256(target) != str(expected).lower():
            raise ValueError(f"checksum mismatch for {name}")
        checked.append(str(name))
    if _direct_pack_identity(settings, pack_id) != (directory, identity):
        raise ValueError("model pack directory changed during verification")
    conformance = _run_model_conformance(manifest)
    manifest["verified"] = True
    manifest["checked_artifacts"] = checked
    if conformance is not None:
        manifest["conformance"] = conformance
    atomic_managed_text_write(path.parent, path, json.dumps(manifest, indent=2) + "\n")
    from .catalog import connect
    from datetime import UTC, datetime

    connection = connect(settings)
    try:
        connection.execute(
            "UPDATE model_packs SET manifest_json=?, verified_at=? WHERE pack_id=?",
            (json.dumps(manifest), datetime.now(UTC).isoformat(), manifest["pack_id"]),
        )
        connection.commit()
    finally:
        connection.close()
    return manifest


def runtime_for_pack(manifest: dict[str, Any]) -> ModelRuntime:
    if manifest.get("runtime_name") == "deterministic-test":
        return DeterministicRuntime(int(manifest.get("embedding_dimension") or 64))
    if manifest.get("runtime_name") == "llama.cpp":
        if manifest.get("runtime_url"):
            _check_pack_integrity(manifest)
            if not manifest.get("test_only"):
                raise RuntimeError("production llama.cpp packs must use their checksummed local runtime")
            key_name = str(manifest.get("runtime_api_key_env") or "")
            if key_name and (not key_name.isidentifier() or not key_name.isupper()):
                raise RuntimeError("runtime_api_key_env must be an uppercase environment-variable name")
            return LlamaCppRuntime(str(manifest["runtime_url"]), api_key=os.environ.get(key_name) if key_name else None)
        return ManagedLlamaCppRuntime(manifest)
    raise RuntimeError("unsupported local model runtime")


def rerank_candidates(
    settings: Settings,
    query: str,
    hits: list[Any],
    *,
    runtime: ModelRuntime | None = None,
    limit: int = DEFAULT_RERANK_POOL,
    trace: Any | None = None,
) -> list[Any]:
    """Apply a local reranker to a bounded candidate shortlist only.

    This function intentionally receives the already-found candidate snippets,
    never full source files, and only changes a candidate score.  Direct file
    requests and symbol definitions are protected from a learned score so a
    Precision profile cannot discard explicitly requested evidence.
    """
    if not query or not hits:
        return hits
    lane_started = time.perf_counter()
    lane = model_lane(settings)
    lane.__enter__()
    if trace is not None:
        trace.add_stage("model_lane_wait_ms", (time.perf_counter() - lane_started) * 1000)
    owns_runtime = runtime is None
    pack_id = ""
    manifest: dict[str, Any] | None = None
    try:
        if runtime is None:
            manifest = active_pack(settings, "reranker")
            if manifest is None:
                raise RuntimeError("Precision edition requires a verified local reranker pack")
            pack_id = str(manifest["pack_id"])
            runtime_started = time.perf_counter()
            runtime = runtime_for_pack(manifest)
            if trace is not None:
                trace.add_stage("reranker_runtime_start_ms", (time.perf_counter() - runtime_started) * 1000)
        batch_size, recommended_pool = _reranker_tuning(settings, pack_id, manifest) if pack_id else (DEFAULT_RERANK_POOL, DEFAULT_RERANK_POOL)
        limit = max(1, min(limit, recommended_pool, MAX_RERANK_POOL))
        protected = ["requested" in str(hit.kind).lower() or "definition" in str(hit.kind).lower() for hit in hits]
        positions: list[int] = []
        documents: list[str] = []
        seen: set[tuple[str, str, int]] = set()
        for index, hit in enumerate(hits):
            key = (str(hit.repo), str(hit.path), int(hit.line))
            if protected[index] or key in seen or len(positions) >= limit:
                continue
            seen.add(key)
            positions.append(index)
            snippet = str(hit.text).strip().replace("\x00", " ")[:1_200]
            extension = Path(str(hit.path)).suffix.lower().lstrip(".") or "text"
            documents.append(f"Repository: {hit.repo}\nPath: {hit.path}\nLanguage: {extension}\nKind: {hit.kind}\nSnippet: {snippet}")
        if not documents:
            return hits
        # Production conformance compares a batch with one-document calls before
        # a pack becomes usable.  Splitting a calibrated shortlist is therefore
        # a memory bound, not a change to its relevance semantics.
        inference_started = time.perf_counter()
        scores = _rerank_batched(runtime, query, documents, batch_size)
        if trace is not None:
            trace.add_stage("reranker_inference_ms", (time.perf_counter() - inference_started) * 1000)
        if len(scores) != len(positions) or any(not math.isfinite(float(score)) for score in scores):
            raise RuntimeError("local reranker returned incomplete or invalid candidate scores")
        low, high = min(scores), max(scores)
        for position, score in zip(positions, scores, strict=True):
            # A bounded tie-breaker leaves lexical/definition/request features and
            # downstream diversity limits in charge of the final evidence set.
            normalized = 1.0 if high == low else (float(score) - low) / (high - low)
            hits[position].score = round(float(hits[position].score) + 20 * normalized, 3)
            hits[position].found_by = sorted(set(hits[position].found_by + ["local reranker"] ))
        return hits
    finally:
        if owns_runtime and runtime is not None:
            runtime.shutdown()
        lane.__exit__(None, None, None)


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1)]


def _measure(operation: Callable[[], Any], samples: int) -> dict[str, float | int]:
    durations: list[float] = []
    for _ in range(samples):
        started = time.perf_counter()
        operation()
        durations.append((time.perf_counter() - started) * 1_000)
    return {
        "samples": samples,
        "p50_ms": round(_percentile(durations, 0.50), 3),
        "p95_ms": round(_percentile(durations, 0.95), 3),
        "max_ms": round(max(durations), 3),
    }


def _synthetic_cards(count: int) -> list[str]:
    """Public, source-free candidate cards matching the bounded reranker shape."""
    return [
        "\n".join([
            "Repository: synthetic-repository",
            f"Path: src/service/EligibilityService{number}.java",
            "Language: java",
            "Kind: method",
            "Snippet: public EligibilityResult recalculate(Customer customer) {",
            "  return policyEvaluator.evaluate(customer.getJurisdiction());",
            "}",
        ])
        for number in range(count)
    ]


def _measurement_memory() -> dict[str, float]:
    """Best-effort process peaks; child-process memory needs OS tooling to split."""
    try:
        import resource

        scale = 1 if os.uname().sysname == "Darwin" else 1_024
        return {
            "process_peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale / 1_000_000, 3),
            "children_peak_rss_mb": round(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * scale / 1_000_000, 3),
        }
    except (AttributeError, ImportError, OSError):
        return {}


def benchmark_pack(settings: Settings, pack_id: str, *, samples: int = DEFAULT_BENCHMARK_SAMPLES) -> dict[str, Any]:
    """Measure a verified pack locally with public synthetic inputs only."""
    samples = max(1, min(int(samples), 10))
    manifest = verify_pack(settings, pack_id)
    runtime = runtime_for_pack(manifest)
    try:
        if manifest["capability"] in {"embedding", "test"}:
            dimension = int(manifest.get("embedding_dimension") or 64)
            probe = ["Project Brain retrieves verified code evidence.", "A service test verifies a request."]
            vectors = runtime.embed(probe, dimension=dimension)
            batches: dict[str, dict[str, float | int]] = {}
            for batch_size in (1, 8, 16):
                timing = _measure(lambda size=batch_size: runtime.embed(_synthetic_cards(size), dimension=dimension), samples)
                timing["texts_per_second"] = round(batch_size * 1_000 / max(float(timing["p50_ms"]), 0.001), 3)
                batches[str(batch_size)] = timing
            report = {
                "pack_id": pack_id,
                "capability": manifest["capability"],
                "dimension": len(vectors[0]),
                "batch_consistent": vectors == runtime.embed(probe, dimension=len(vectors[0])),
                "embedding_batches": batches,
                "conformance": manifest.get("conformance"),
                "health": runtime.health(),
                **_measurement_memory(),
            }
            filename = "MODEL_BAKEOFF_REPORT.md"
        else:
            batch_size, _ = _reranker_tuning(settings, pack_id, manifest)
            scores = _rerank_batched(
                runtime, "verified code evidence",
                ["verified code evidence", "unrelated deployment note"], batch_size,
            )
            pools = {
                str(size): _measure(
                    lambda count=size: _rerank_batched(
                        runtime,
                        "Why did eligibility stop recalculating after a jurisdiction change?",
                        _synthetic_cards(count), batch_size,
                    ),
                    samples,
                )
                for size in (10, 20, 40, 80)
            }
            report = {
                "pack_id": pack_id,
                "capability": "reranker",
                "ordering_ok": scores[0] > scores[1],
                "scores": scores,
                "reranker_candidate_pools": pools,
                "conformance": manifest.get("conformance"),
                "health": runtime.health(),
                **_measurement_memory(),
            }
            filename = "RERANKER_BAKEOFF_REPORT.md"
        lines = [f"# {filename.removesuffix('.md').replace('_', ' ').title()}", "", "This local synthetic benchmark and conformance report is not a holdout-quality claim.", "", *[f"- {key}: `{value}`" for key, value in report.items()]]
        from .core import _atomic_generated_text_write

        _atomic_generated_text_write(
            settings, settings.generated_dir / filename, "\n".join(lines) + "\n",
        )
        from .metrics import record_metric

        record_metric(settings, "model_benchmark", pack_id=pack_id, capability=report["capability"], samples=samples)
        return report
    finally:
        runtime.shutdown()


def autotune_pack(
    settings: Settings,
    pack_id: str,
    *,
    samples: int = DEFAULT_BENCHMARK_SAMPLES,
    latency_budget_ms: int = DEFAULT_MODEL_LATENCY_BUDGET_MS,
) -> dict[str, Any]:
    """Persist conservative recommendations derived from this machine's real run."""
    latency_budget_ms = max(1, int(latency_budget_ms))
    report = benchmark_pack(settings, pack_id, samples=samples)
    recommendations: dict[str, int | bool] = {
        # Managed runtimes are intentionally short-lived today.  These values
        # document the applied lifecycle rather than pretending a resident model
        # exists when it does not.
        "embedding_resident": False,
        "reranker_idle_unload_seconds": 0,
        "query_worker_count": 1,
    }
    if report["capability"] in {"embedding", "test"}:
        batches = report["embedding_batches"]
        selected = max(
            batches,
            key=lambda size: (float(batches[size]["texts_per_second"]), int(size)),
        )
        recommendations["embedding_batch_size"] = int(selected)
    else:
        pools = report["reranker_candidate_pools"]
        eligible = [int(size) for size, result in pools.items() if float(result["p95_ms"]) <= latency_budget_ms]
        candidate_pool = max(eligible) if eligible else min(int(size) for size in pools)
        recommendations["reranker_candidate_pool"] = candidate_pool
        recommendations["reranker_batch_size"] = candidate_pool
    from .metrics import write_machine_profile

    tuning = {
        "schema_version": 1,
        "pack_id": pack_id,
        "capability": report["capability"],
        "created_at": time.time(),
        "latency_budget_ms": latency_budget_ms,
        "recommendations": recommendations,
        "machine": write_machine_profile(settings),
        "benchmark": report,
    }
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    target = _tuning_path(settings)
    atomic_managed_text_write(settings.state_dir, target, json.dumps(tuning, indent=2) + "\n")
    return {**tuning, "profile_path": str(target)}


def remove_pack(settings: Settings, pack_id: str) -> None:
    target = _pack_directory(settings, pack_id)
    if target.is_symlink() or not target.is_dir():
        raise ValueError(f"model pack is not installed: {pack_id}")
    from .catalog import connect

    connection = connect(settings)
    try:
        referenced = any(
            str((json.loads(details) if details else {}).get("pack_id") or "") == pack_id
            for (details,) in connection.execute(
                "SELECT details_json FROM generation_components WHERE component='semantic' AND status IN ('ready','degraded')"
            )
        )
    except (TypeError, json.JSONDecodeError):
        referenced = True
    finally:
        connection.close()
    if referenced:
        raise ValueError(
            f"model pack {pack_id} is retained by an Atlas generation; remove it only after reachability GC reclaims those generations"
        )
    try:
        capability = str(_load_manifest(target / "installed.json").get("capability") or "")
    except (OSError, ValueError, json.JSONDecodeError):
        capability = ""
    shutil.rmtree(target)
    connection = connect(settings)
    try:
        connection.execute("DELETE FROM model_packs WHERE pack_id=?", (pack_id,))
        connection.commit()
    finally:
        connection.close()
    if capability in {"embedding", "test"}:
        semantic_state = settings.state_dir / "semantic-index.json"
        try:
            state = json.loads(read_managed_text(
                settings.state_dir, semantic_state, max_bytes=64 * 1024 * 1024,
            ))
            if not isinstance(state, dict):
                raise ValueError("Semantic state must be an object")
            state["stale"] = True
            state["stale_reason"] = f"embedding pack {pack_id} was removed"
            atomic_managed_text_write(
                settings.state_dir, semantic_state, json.dumps(state, separators=(",", ":")),
            )
        except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            pass

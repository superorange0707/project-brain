from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import signal
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time
from platform import machine, system
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol

if TYPE_CHECKING:
    from .core import Settings

REQUIRED_MANIFEST_FIELDS = {
    "pack_id", "capability", "model_family", "upstream_model", "upstream_revision", "license",
    "runtime_name", "runtime_revision", "minimum_brain_version",
}
PRODUCTION_PROVENANCE_FIELDS = {
    "weight_format", "quantization", "weight_sha256", "tokenizer_file", "tokenizer_sha256", "pooling", "normalization",
    "query_instruction_version", "document_card_version", "chunk_schema_version", "embedding_dimension", "converter_revision",
}
DEFAULT_RERANK_POOL = 40
MAX_RERANK_POOL = 80
DEFAULT_EMBEDDING_BATCH_SIZE = 16
DEFAULT_BENCHMARK_SAMPLES = 3
DEFAULT_MODEL_LATENCY_BUDGET_MS = 3_000
DEFAULT_RUNTIME_MAX_REQUESTS = 64
# The local llama.cpp Metal backend can differ very slightly between a batch
# request and equivalent single-item requests. This permits only observed
# floating-point reduction drift; reference-vector cosine and ranking gates
# remain independent conformance requirements.
EMBEDDING_BATCH_PARITY_TOLERANCE = 1e-4
# A production reranker is evaluated once as a bounded batch and once as the
# equivalent one-document requests.  llama.cpp's local backends may differ by
# a tiny floating-point reduction, but a larger change is a conformance error.
RERANKER_BATCH_PARITY_TOLERANCE = 1e-4

# Each entry is added only after its separately versioned model-pack release
# passes final-release checksum verification and a clean installation check.
# Never resolve an unpinned "latest" release at install time.
OFFICIAL_PACKS: dict[str, dict[str, str]] = {
    "semantic": {
        "pack_id": "qwen3-embedding-4b-q6k-darwin-arm64",
        "descriptor_url": (
            "https://github.com/superorange0707/project-brain/releases/download/"
            "semantic-pack-v1.0.6/qwen3-embedding-4b-q6k-darwin-arm64-descriptor.json"
        ),
        "descriptor_sha256": "cbd09af575fb1b2e036abc17ed3e693e5bab4807af19efd2c1a9b5cd75ae8afc",
    },
}
MODEL_PACK_DESCRIPTOR_SCHEMA = "project-brain-model-pack-v1"


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

    def __post_init__(self) -> None:
        parsed = urlparse(self.endpoint)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("model runtime must bind only loopback HTTP or a Unix socket")
        self.endpoint = self.endpoint.rstrip("/")

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _post(self, path: str, body: dict[str, object]) -> dict[str, object]:
        request = Request(self.endpoint + path, data=json.dumps(body).encode("utf-8"), method="POST", headers=self._headers())
        with urlopen(request, timeout=self.timeout_seconds) as response:
            value = json.loads(response.read().decode("utf-8"))
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
        if len(vectors) != len(texts) or any(not vector for vector in vectors):
            raise RuntimeError("local embedding runtime returned an incomplete vector batch")
        return [[float(number) for number in vector] for vector in vectors]

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
        with urlopen(request, timeout=self.timeout_seconds) as response:
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
        max_requests = max(1, int(self.manifest.get("max_requests_per_runtime") or DEFAULT_RUNTIME_MAX_REQUESTS))
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
            command.append("--embedding")
        else:
            command.extend(["--embedding", "--pooling", "rank", "--rerank"])
        runtime_args = self.manifest.get("runtime_args") or []
        if not isinstance(runtime_args, list) or not all(isinstance(value, str) and value for value in runtime_args):
            raise RuntimeError("model pack runtime_args must be a list of non-empty strings")
        protected = {"--model", "-m", "--host", "--port", "--api-key", "--hf-repo", "--hf-file"}
        if any(value in protected for value in runtime_args):
            raise RuntimeError("model pack runtime_args may not override Project Brain local runtime controls")
        command.extend(runtime_args)
        self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            request_timeout_seconds = min(300.0, max(5.0, float(self.manifest.get("request_timeout_seconds") or 30.0)))
        except (TypeError, ValueError) as error:
            raise RuntimeError("model pack request_timeout_seconds must be numeric") from error
        self.client = LlamaCppRuntime(
            f"http://127.0.0.1:{port}",
            api_key=key,
            timeout_seconds=request_timeout_seconds,
            input_suffix=str(self.manifest.get("input_suffix") or ""),
        )
        deadline = time.monotonic() + min(120.0, max(1.0, float(self.manifest.get("startup_timeout_seconds") or 30)))
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.shutdown()
                raise RuntimeError("pack-owned llama.cpp runtime exited before becoming healthy")
            try:
                if self.client.health().get("ok"):
                    return self.client
            except OSError:
                pass
            time.sleep(0.05)
        self.shutdown()
        raise RuntimeError("pack-owned llama.cpp runtime did not become healthy before timeout")

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
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass


def model_root(settings: Settings) -> Path:
    return settings.state_dir / "models"


def _tuning_path(settings: Settings) -> Path:
    return settings.state_dir / "model-tuning.json"


def _model_tuning(settings: Settings, pack_id: str) -> dict[str, Any]:
    """Read only a tuning result made for this exact locally verified pack."""
    try:
        result = json.loads(_tuning_path(settings).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return result if isinstance(result, dict) and result.get("pack_id") == pack_id else {}


def embedding_batch_size(settings: Settings, pack_id: str, default: int = DEFAULT_EMBEDDING_BATCH_SIZE) -> int:
    recommended = _model_tuning(settings, pack_id).get("recommendations", {}).get("embedding_batch_size", default)
    try:
        return max(1, min(int(recommended), 256))
    except (TypeError, ValueError):
        return default


def _reranker_tuning(settings: Settings, pack_id: str) -> tuple[int, int]:
    recommendations = _model_tuning(settings, pack_id).get("recommendations", {})
    try:
        batch_size = max(1, min(int(recommendations.get("reranker_batch_size", DEFAULT_RERANK_POOL)), MAX_RERANK_POOL))
    except (TypeError, ValueError):
        batch_size = DEFAULT_RERANK_POOL
    try:
        candidate_pool = max(1, min(int(recommendations.get("reranker_candidate_pool", DEFAULT_RERANK_POOL)), MAX_RERANK_POOL))
    except (TypeError, ValueError):
        candidate_pool = DEFAULT_RERANK_POOL
    return batch_size, candidate_pool


def _safe_pack_id(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in "._-" else "-" for char in value).strip(".-")
    if not safe:
        raise ValueError("pack_id is empty or unsafe")
    return safe


def _load_manifest(path: Path) -> dict[str, Any]:
    if path.suffix == ".json":
        value = json.loads(path.read_text(encoding="utf-8"))
    else:
        from .core import simple_yaml_load

        value = simple_yaml_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("model pack manifest must be a mapping")
    return {str(key): value for key, value in value.items()}


def validate_manifest(manifest: dict[str, Any]) -> None:
    missing = sorted(field for field in REQUIRED_MANIFEST_FIELDS if not str(manifest.get(field) or "").strip())
    if missing:
        raise ValueError("model pack manifest is missing: " + ", ".join(missing))
    if manifest["capability"] not in {"embedding", "reranker", "test"}:
        raise ValueError("model pack capability must be embedding, reranker, or test")
    _safe_pack_id(str(manifest["pack_id"]))
    if str(manifest["runtime_name"]) not in {"llama.cpp", "deterministic-test"}:
        raise ValueError("runtime_name must be a pinned local runtime")
    if str(manifest["runtime_name"]) == "deterministic-test" and manifest["capability"] != "test" and not manifest.get("test_only"):
        raise ValueError("deterministic-test runtime is allowed only for a test-only pack")
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
        missing_provenance = sorted(
            field
            for field in PRODUCTION_PROVENANCE_FIELDS
            if (field == "embedding_dimension" and (not isinstance(manifest.get(field), int) or int(manifest[field]) < 0))
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
        suite = json.loads(path.read_text(encoding="utf-8"))
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
                if (not _same_vectors(batch, individual, tolerance=EMBEDDING_BATCH_PARITY_TOLERANCE) or any(len(vector) != dimension or any(not math.isfinite(value) for value in vector) for vector in batch)):
                    raise ValueError(f"embedding conformance failed at case {number}")
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
                scores = runtime.rerank(query, runtime_documents)
                single_scores = [runtime.rerank(query, [document])[0] for document in runtime_documents]
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
                if len(scores) != len(documents) or not _same_vectors([[float(score)] for score in scores], [[float(score)] for score in single_scores], tolerance=RERANKER_BATCH_PARITY_TOLERANCE) or any(not math.isfinite(float(score)) for score in scores) or order[0] != expected_top:
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
        with urlopen(request, timeout=60) as response:
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
            with handle:
                while chunk := response.read(1024 * 1024):
                    handle.write(chunk)
                    digest.update(chunk)
        if _sha256(staged) != expected_sha256:
            raise ValueError("downloaded model pack SHA-256 does not match --sha256")
        return install_pack(settings, staged)
    finally:
        if staged:
            staged.unlink(missing_ok=True)


def official_packs() -> list[dict[str, str]]:
    """List source-pinned Project Brain controlled pack descriptors."""
    return [
        {"alias": alias, **{key: value for key, value in pack.items() if key != "descriptor_sha256"}}
        for alias, pack in sorted(OFFICIAL_PACKS.items())
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
    with urlopen(request, timeout=120) as response:
        _approved_pack_url(settings, response.geturl(), final=True)
        try:
            content_length = int(response.headers.get("Content-Length") or 0)
        except ValueError as error:
            raise ValueError("model pack download has an invalid Content-Length") from error
        if content_length != int(artifact["size"]):
            raise ValueError("model pack download size does not match its pinned descriptor")
        digest = hashlib.sha256()
        with destination.open("ab" if append else "wb") as handle:
            while chunk := response.read(1024 * 1024):
                handle.write(chunk)
                digest.update(chunk)
    if digest.hexdigest() != str(artifact["sha256"]):
        raise ValueError("model pack release artifact SHA-256 does not match its pinned descriptor")


def _extract_pack_archive(source: Path, destination: Path) -> None:
    with tarfile.open(source, "r:*") as archive:
        for member in archive.getmembers():
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
                    shutil.copyfileobj(extracted, handle)
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
        with urlopen(request, timeout=60) as response:
            _approved_pack_url(settings, response.geturl(), final=True)
            digest = hashlib.sha256()
            with descriptor_path.open("wb") as handle:
                while chunk := response.read(1024 * 1024):
                    handle.write(chunk)
                    digest.update(chunk)
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
        platform_name = str(descriptor.get("platform") or "")
        local_platform = f"{system().lower()}-{machine().lower()}"
        supported_platforms = {"darwin-arm64", "darwin-aarch64"}
        if platform_name and platform_name not in supported_platforms:
            raise _descriptor_error("platform is unsupported")
        if platform_name and local_platform not in supported_platforms:
            raise ValueError(f"model pack {pack_id} is for {platform_name}, not {local_platform}")
        from .ops import ensure_write_capacity

        ensure_write_capacity(settings, int(metadata["size"]) + sum(int(part["size"]) for part in parts))
        archive = temporary / "metadata.tar"
        _download_verified_artifact(settings, metadata, archive)
        contents = temporary / "contents"
        contents.mkdir()
        _extract_pack_archive(archive, contents)
        assembled = contents / model_file
        for part in parts:
            _download_verified_artifact(settings, part, assembled, append=assembled.exists())
        if _sha256(assembled) != model_sha256:
            raise ValueError("assembled model SHA-256 does not match the pinned descriptor")
        manifest_path = _manifest_file(contents)
        manifest = _load_manifest(manifest_path)
        if _safe_pack_id(str(manifest.get("pack_id") or "")) != pack_id:
            raise _descriptor_error("pack_id does not match embedded manifest")
        if str(manifest.get("model_file") or "") != model_file or str(manifest.get("weight_sha256") or "").lower() != model_sha256:
            raise _descriptor_error("embedded manifest does not pin the assembled model")
        return install_pack(settings, contents)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def install_official_pack(settings: Settings, alias: str) -> dict[str, Any]:
    alias = alias.lower().strip()
    pack = OFFICIAL_PACKS.get(alias)
    if pack is None:
        raise ValueError(f"no Project Brain-controlled release pack is available for {alias}")
    return install_release_descriptor(settings, str(pack["descriptor_url"]), str(pack["descriptor_sha256"]))


def install_pack(settings: Settings, source: Path) -> dict[str, Any]:
    """Install an already-local pack. Runtime never performs a network download."""
    source = source.expanduser().resolve()
    if not source.exists():
        raise ValueError(f"model pack does not exist: {source}")
    if source.is_dir():
        projected_bytes = sum(item.stat().st_size for item in source.rglob("*") if item.is_file())
    elif tarfile.is_tarfile(source):
        with tarfile.open(source, "r:*") as archive:
            projected_bytes = sum(member.size for member in archive.getmembers() if member.isfile())
    else:
        projected_bytes = source.stat().st_size
    from .ops import ensure_write_capacity

    ensure_write_capacity(settings, projected_bytes)
    temporary: Path | None = None
    try:
        if source.is_file() and tarfile.is_tarfile(source):
            temporary = Path(tempfile.mkdtemp(prefix="brain-pack-", dir=settings.state_dir))
            _extract_pack_archive(source, temporary)
            source = temporary
        manifest_path = _manifest_file(source)
        manifest = _load_manifest(manifest_path)
        validate_manifest(manifest)
        destination = model_root(settings) / _safe_pack_id(str(manifest["pack_id"]))
        staging = destination.with_name(destination.name + ".installing")
        previous = destination.with_name(destination.name + ".previous")
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(previous, ignore_errors=True)
        staging.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source if source.is_dir() else source.parent, staging, dirs_exist_ok=False)
        os.chmod(staging, 0o700)
        if destination.exists():
            destination.replace(previous)
        try:
            staging.replace(destination)
        except Exception:
            if previous.exists():
                previous.replace(destination)
            raise
        finally:
            shutil.rmtree(previous, ignore_errors=True)
        manifest["installed_path"] = str(destination)
        (destination / "installed.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        semantic_state = settings.state_dir / "semantic-index.json"
        if semantic_state.is_file() and manifest["capability"] in {"embedding", "test"}:
            try:
                state = json.loads(semantic_state.read_text(encoding="utf-8"))
                state["stale"] = True
                state["stale_reason"] = f"embedding pack changed to {manifest['pack_id']}"
                semantic_state.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
            except (OSError, json.JSONDecodeError):
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
        if temporary:
            shutil.rmtree(temporary, ignore_errors=True)


def installed_packs(settings: Settings) -> list[dict[str, Any]]:
    root = model_root(settings)
    packs: list[dict[str, Any]] = []
    if not root.is_dir():
        return packs
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        path = directory / "installed.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                packs.append(value)
        except (OSError, json.JSONDecodeError):
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


def verify_pack(settings: Settings, pack_id: str) -> dict[str, Any]:
    path = model_root(settings) / _safe_pack_id(pack_id) / "installed.json"
    manifest = _load_manifest(path)
    validate_manifest(manifest)
    checked: list[str] = []
    artifacts = manifest.get("artifacts") or {}
    if artifacts and not isinstance(artifacts, dict):
        raise ValueError("artifacts must map file names to SHA-256 values")
    for name, expected in sorted(artifacts.items()):
        target = _pack_file(manifest, name, "artifact")
        if _sha256(target) != str(expected).lower():
            raise ValueError(f"checksum mismatch for {name}")
        checked.append(str(name))
    conformance = _run_model_conformance(manifest)
    manifest["verified"] = True
    manifest["checked_artifacts"] = checked
    if conformance is not None:
        manifest["conformance"] = conformance
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
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
        _check_pack_integrity(manifest)
        if manifest.get("runtime_url"):
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
) -> list[Any]:
    """Apply a local reranker to a bounded candidate shortlist only.

    This function intentionally receives the already-found candidate snippets,
    never full source files, and only changes a candidate score.  Direct file
    requests and symbol definitions are protected from a learned score so a
    Precision profile cannot discard explicitly requested evidence.
    """
    if not query or not hits:
        return hits
    owns_runtime = runtime is None
    pack_id = ""
    if runtime is None:
        manifest = active_pack(settings, "reranker")
        if manifest is None:
            raise RuntimeError("Precision edition requires a verified local reranker pack")
        pack_id = str(manifest["pack_id"])
        runtime = runtime_for_pack(manifest)
    try:
        batch_size, recommended_pool = _reranker_tuning(settings, pack_id) if pack_id else (DEFAULT_RERANK_POOL, DEFAULT_RERANK_POOL)
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
        scores = [
            score
            for start in range(0, len(documents), batch_size)
            for score in runtime.rerank(query, documents[start:start + batch_size])
        ]
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
        if owns_runtime:
            runtime.shutdown()


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
            scores = runtime.rerank("verified code evidence", ["verified code evidence", "unrelated deployment note"])
            pools = {
                str(size): _measure(lambda count=size: runtime.rerank("Why did eligibility stop recalculating after a jurisdiction change?", _synthetic_cards(count)), samples)
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
        settings.generated_dir.mkdir(parents=True, exist_ok=True)
        lines = [f"# {filename.removesuffix('.md').replace('_', ' ').title()}", "", "This local synthetic benchmark and conformance report is not a holdout-quality claim.", "", *[f"- {key}: `{value}`" for key, value in report.items()]]
        (settings.generated_dir / filename).write_text("\n".join(lines) + "\n", encoding="utf-8")
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
    temporary = target.with_suffix(".writing")
    temporary.write_text(json.dumps(tuning, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return {**tuning, "profile_path": str(target)}


def remove_pack(settings: Settings, pack_id: str) -> None:
    target = model_root(settings) / _safe_pack_id(pack_id)
    if not target.is_dir():
        raise ValueError(f"model pack is not installed: {pack_id}")
    try:
        capability = str(_load_manifest(target / "installed.json").get("capability") or "")
    except (OSError, ValueError, json.JSONDecodeError):
        capability = ""
    shutil.rmtree(target)
    from .catalog import connect

    connection = connect(settings)
    try:
        connection.execute("DELETE FROM model_packs WHERE pack_id=?", (pack_id,))
        connection.commit()
    finally:
        connection.close()
    if capability in {"embedding", "test"}:
        semantic_state = settings.state_dir / "semantic-index.json"
        try:
            state = json.loads(semantic_state.read_text(encoding="utf-8"))
            state["stale"] = True
            state["stale_reason"] = f"embedding pack {pack_id} was removed"
            semantic_state.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            pass

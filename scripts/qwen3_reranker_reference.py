#!/usr/bin/env python3
"""Generate public/synthetic official-Qwen reranker reference scores locally.

This follows Qwen's published Transformers reranker input contract.  It never
uploads inputs and its output is intended only for the release builder's local
conformance comparison.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from build_precision_pack import RERANK_CONTEXT_TOKENS, RERANK_INSTRUCTION, candidate_pool_cases, runtime_documents, standard_cases


PREFIX = "<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the Instruct provided. Note that the answer can only be \"yes\" or \"no\".<|im_end|>\n<|im_start|>user\n"
SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
OFFICIAL_REFERENCE = "https://github.com/QwenLM/Qwen3-Embedding/blob/main/examples/qwen3_reranker_transformers.py"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--upstream-revision", required=True)
    parser.add_argument("--batch-size", type=int, default=4)
    args = parser.parse_args()
    if not args.model_dir.is_dir():
        raise SystemExit(f"official model directory does not exist: {args.model_dir}")
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite reference output: {args.output}")
    if args.batch_size < 1:
        raise SystemExit("--batch-size must be positive")

    import torch
    import transformers
    from transformers import AutoModelForCausalLM, AutoTokenizer

    device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
    dtype = torch.float16 if device.type == "mps" else torch.float32
    tokenizer = AutoTokenizer.from_pretrained(args.model_dir, local_files_only=True, trust_remote_code=True, padding_side="left")
    tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        args.model_dir, local_files_only=True, trust_remote_code=True, torch_dtype=dtype, low_cpu_mem_usage=True,
    ).to(device).eval()
    false_id = tokenizer.convert_tokens_to_ids("no")
    true_id = tokenizer.convert_tokens_to_ids("yes")
    if not isinstance(false_id, int) or not isinstance(true_id, int) or false_id < 0 or true_id < 0:
        raise RuntimeError("official Qwen tokenizer does not expose yes/no reranker labels")
    prefix_tokens = tokenizer.encode(PREFIX, add_special_tokens=False)
    suffix_tokens = tokenizer.encode(SUFFIX, add_special_tokens=False)

    def scores(case: dict[str, object]) -> list[float]:
        pairs = [
            f"<Instruct>: {RERANK_INSTRUCTION}\n<Query>: {case['query']}\n<Document>: {document}"
            for document in runtime_documents(case)
        ]
        values: list[float] = []
        for start in range(0, len(pairs), args.batch_size):
            encoded = tokenizer(
                pairs[start:start + args.batch_size], padding=False, truncation="longest_first",
                return_attention_mask=False, max_length=RERANK_CONTEXT_TOKENS - len(prefix_tokens) - len(suffix_tokens),
            )
            encoded["input_ids"] = [prefix_tokens + row + suffix_tokens for row in encoded["input_ids"]]
            inputs = tokenizer.pad(encoded, padding=True, return_tensors="pt", max_length=RERANK_CONTEXT_TOKENS)
            inputs = {key: value.to(device) for key, value in inputs.items()}
            with torch.inference_mode():
                logits = model(**inputs).logits[:, -1, :]
                labels = torch.stack([logits[:, false_id], logits[:, true_id]], dim=1)
                values.extend(torch.nn.functional.log_softmax(labels, dim=1)[:, 1].exp().float().cpu().tolist())
        return [float(value) for value in values]

    payload = {
        "reference": OFFICIAL_REFERENCE,
        "upstream_model": "Qwen/Qwen3-Reranker-4B",
        "upstream_revision": args.upstream_revision,
        "instruction": RERANK_INSTRUCTION,
        "max_length": RERANK_CONTEXT_TOKENS,
        "device": device.type,
        "torch": torch.__version__,
        "transformers": transformers.__version__,
        "public_synthetic_only": True,
        "reranker": [{"id": str(case["id"]), "scores": scores(case)} for case in standard_cases()],
        "candidate_pools": [{"id": str(case["id"]), "scores": scores(case)} for case in candidate_pool_cases()],
    }
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

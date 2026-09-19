#!/usr/bin/env python3
"""
Cross-encoder reranking for rag-stack retrieval.

Hybrid search ranks chunks by vector similarity and keyword overlap, each
scored without looking at the question and chunk together. A cross-encoder
reads the question and one chunk as a pair and scores how well the chunk
answers it -- slower, so it only re-scores the RAG_TOP_K fused candidates
and keeps the best RAG_TOP_K_RERANKER.

Runs on the GPU where available (Apple Silicon: PyTorch's MPS backend, in
float16), otherwise CPU. Settings, from .env:

  RERANKER_MODEL       HuggingFace cross-encoder (default BAAI/bge-reranker-v2-m3)
  RERANKER_DEVICE      auto | mps | cuda | cpu
  RERANKER_BATCH_SIZE  question/chunk pairs scored per forward pass
  RERANKER_MAX_LENGTH  token limit per pair (question + chunk)

Run `uv run rerank.py --download` to fetch the model ahead of first use.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parents[2]

# Shared with docling-serve and embed.py; must be set before transformers loads.
os.environ.setdefault("HF_HOME", str(REPO_ROOT / "data" / "hf-cache"))
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")

DEVICES = ("auto", "mps", "cuda", "cpu")


def load_env() -> dict:
    return {**dotenv_values(REPO_ROOT / ".env"), **os.environ}


@dataclass(frozen=True)
class RerankConfig:
    model: str
    device: str
    batch_size: int
    max_length: int

    @classmethod
    def from_env(cls, env: dict | None = None) -> "RerankConfig":
        env = load_env() if env is None else env
        device = (env.get("RERANKER_DEVICE") or "auto").strip().lower()
        if device not in DEVICES:
            raise ValueError(f"RERANKER_DEVICE must be one of {', '.join(DEVICES)}, got {device!r}")
        cfg = cls(
            model=env.get("RERANKER_MODEL") or "BAAI/bge-reranker-v2-m3",
            device=device,
            batch_size=int(env.get("RERANKER_BATCH_SIZE") or 16),
            max_length=int(env.get("RERANKER_MAX_LENGTH") or 1024),
        )
        if cfg.batch_size < 1 or cfg.max_length < 16:
            raise ValueError("RERANKER_BATCH_SIZE must be >= 1 and RERANKER_MAX_LENGTH >= 16")
        return cfg


def pick_device(requested: str) -> str:
    import torch

    if requested == "auto":
        if torch.backends.mps.is_available():
            return "mps"
        return "cuda" if torch.cuda.is_available() else "cpu"
    if requested == "mps" and not torch.backends.mps.is_available():
        raise RuntimeError("RERANKER_DEVICE=mps, but PyTorch reports no MPS (Apple GPU) device")
    if requested == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("RERANKER_DEVICE=cuda, but PyTorch reports no CUDA device")
    return requested


def _load_pretrained(loader, name: str, **kwargs):
    """Cached copy first, so normal runs never touch the network."""
    try:
        return loader.from_pretrained(name, local_files_only=True, **kwargs)
    except OSError:
        return loader.from_pretrained(name, **kwargs)


class Reranker:
    """Loads the model once; score() is cheap to call repeatedly."""

    def __init__(self, cfg: RerankConfig | None = None):
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        self.cfg = cfg or RerankConfig.from_env()
        self.device = pick_device(self.cfg.device)
        # float16 halves memory and roughly doubles GPU throughput; the scores
        # only need to order a handful of candidates. CPU stays in float32.
        self.dtype = torch.float32 if self.device == "cpu" else torch.float16
        t0 = time.monotonic()
        self.tokenizer = _load_pretrained(AutoTokenizer, self.cfg.model)
        self.model = _load_pretrained(AutoModelForSequenceClassification, self.cfg.model, dtype=self.dtype)
        self.model.to(self.device).eval()
        self.load_seconds = time.monotonic() - t0
        self._torch = torch
        # One forward pass on the GPU at a time: concurrent requests would
        # only contend for it and multiply peak memory.
        self._lock = threading.Lock()

    def score(self, question: str, passages: list[str]) -> list[float]:
        """Relevance of each passage to the question, 0..1 (sigmoid of the logit)."""
        torch = self._torch
        scores: list[float] = []
        with self._lock, torch.inference_mode():
            for start in range(0, len(passages), self.cfg.batch_size):
                batch = passages[start:start + self.cfg.batch_size]
                inputs = self.tokenizer(
                    [question] * len(batch), batch,
                    padding=True,
                    # Cut the passage, never the question, if a pair is too long.
                    truncation="only_second",
                    max_length=self.cfg.max_length,
                    return_tensors="pt",
                ).to(self.device)
                logits = self.model(**inputs).logits.view(-1).float()
                scores.extend(torch.sigmoid(logits).tolist())
        return scores


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--download", action="store_true", help="Download the model into data/hf-cache and exit")
    args = parser.parse_args()
    if not args.download:
        parser.print_help()
        return 0
    cfg = RerankConfig.from_env()
    from huggingface_hub import snapshot_download

    print(f"Downloading {cfg.model} into {os.environ['HF_HOME']} (about 2.3 GB for bge-reranker-v2-m3)...")
    # Weights as safetensors plus tokenizer/config files only; skips any
    # duplicate formats (pytorch .bin, ONNX) a model repo may also carry.
    snapshot_download(cfg.model, allow_patterns=["*.json", "*.safetensors", "*.model", "*.txt"])
    reranker = Reranker(cfg)
    [score] = reranker.score("test", ["test passage"])
    print(f"Ready: {cfg.model} on {reranker.device} ({str(reranker.dtype).removeprefix('torch.')}), "
          f"loaded in {reranker.load_seconds:.1f}s, smoke-test score {score:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

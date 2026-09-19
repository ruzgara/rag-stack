#!/usr/bin/env python3
"""
Retrieval over the rag-stack index: hybrid search, then reranking.

1. Hybrid search: the question is embedded with the same Ollama model the
   index was built with, then Qdrant runs two retrievers in one query --
   dense vectors and BM25 (sparse vectors Qdrant computes itself) -- and
   fuses their rankings into RAG_TOP_K candidates.
2. Reranking (rerank.py): a cross-encoder on the GPU re-scores those
   candidates against the question; those scoring at least
   RAG_RERANK_MIN_SCORE are kept, best first.
3. Section expansion: when two or more kept chunks share a heading (say
   "Investors" and "Lead manager", both under "The parties to a typical bond
   issue"), the whole section under it is fetched and returned as one
   result, in document order -- as long as it fits RAG_SECTION_MAX_TOKENS.
   The deepest shared heading wins, so a chapter is never pulled in when a
   subsection will do. Matching stays at chunk level, where it's precise;
   what's returned is the complete section.
4. At most RAG_TOP_K_RERANKER results are returned (a section counts as one).

Every tuning knob is read from .env:

  RAG_SEARCH_MODE       hybrid | dense | bm25
  RAG_PREFETCH_K        candidates each retriever contributes before fusion
  RAG_TOP_K             results after fusion (the reranker's input)
  RAG_FUSION            rrf (reciprocal rank fusion) | dbsf (distribution-based score fusion)
  RAG_RRF_K             RRF smoothing constant; larger flattens the rank weighting
  RAG_DENSE_WEIGHT      RRF weight of the dense ranking
  RAG_BM25_WEIGHT       RRF weight of the BM25 ranking
  RAG_RERANK            true | false -- false returns the top fused results instead
  RAG_TOP_K_RERANKER    at most this many results returned
  RAG_RERANK_MIN_SCORE  drop reranked chunks scoring below this (0..1; 0 = keep all)
  RAG_EXPAND_SECTIONS   true | false -- return whole sections (step 3)
  RAG_SECTION_MAX_TOKENS  largest section returned whole, in (estimated) tokens
  RERANKER_*            model and device, see rerank.py

Import `Retriever` from here in other services and call retrieve(); search()
alone is the fused, un-reranked list. Run this file for a CLI.

Run with: uv run retrieval.py "question" [--fused] [--json] [-v/--verbose]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx
from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parents[2]

SEARCH_MODES = ("hybrid", "dense", "bm25")
FUSIONS = ("rrf", "dbsf")

# Same retry pattern as embed.py: a 503 means Ollama's queue is full, and
# connection errors/timeouts usually mean the model is still loading.
EMBED_RETRY_BACKOFF_S = (2, 5, 10)
RETRYABLE_STATUS = {429, 502, 503, 504}

# Chunks fetched per section lookup; a section with more is too big anyway.
SECTION_FETCH_LIMIT = 64


def estimate_tokens(text: str) -> int:
    # Rough (≈4 characters per token for English); only used for budgets.
    return len(text) // 4 + 1


def load_env() -> dict:
    return {**dotenv_values(REPO_ROOT / ".env"), **os.environ}


def _bool(env: dict, key: str, default: str) -> bool:
    value = (env.get(key) or default).strip().lower()
    if value not in ("true", "false"):
        raise ValueError(f"{key} must be true or false, got {env.get(key)!r}")
    return value == "true"


def _float(env: dict, key: str, default: str) -> float:
    try:
        return float(env.get(key) or default)
    except ValueError:
        raise ValueError(f"{key} must be a number, got {env.get(key)!r}") from None


def _int(env: dict, key: str, default: str) -> int:
    try:
        value = int(env.get(key) or default)
    except ValueError:
        raise ValueError(f"{key} must be an integer, got {env.get(key)!r}") from None
    if value < 1:
        raise ValueError(f"{key} must be at least 1, got {value}")
    return value


@dataclass(frozen=True)
class SearchConfig:
    ollama_url: str
    qdrant_url: str
    collection: str
    embed_model: str
    mode: str
    prefetch_k: int
    top_k: int
    fusion: str
    rrf_k: int
    dense_weight: float
    bm25_weight: float
    rerank: bool
    top_k_reranker: int
    rerank_min_score: float
    expand_sections: bool = True
    section_max_tokens: int = 2000

    @classmethod
    def from_env(cls, env: dict | None = None) -> "SearchConfig":
        env = load_env() if env is None else env
        if not env.get("EMBED_MODEL"):
            raise ValueError("EMBED_MODEL is not set in .env")
        # Ollama treats an untagged name as "<name>:latest"; embed.py stores the
        # normalized name on every point, so compare like with like.
        model = env["EMBED_MODEL"] if ":" in env["EMBED_MODEL"] else f"{env['EMBED_MODEL']}:latest"
        cfg = cls(
            ollama_url=f"http://127.0.0.1:{env.get('OLLAMA_PORT', '11434')}",
            qdrant_url=f"http://127.0.0.1:{env.get('QDRANT_PORT', '6333')}",
            collection=env.get("QDRANT_COLLECTION") or "ragstack_chunks",
            embed_model=model,
            mode=(env.get("RAG_SEARCH_MODE") or "hybrid").strip().lower(),
            prefetch_k=_int(env, "RAG_PREFETCH_K", "40"),
            top_k=_int(env, "RAG_TOP_K", "20"),
            fusion=(env.get("RAG_FUSION") or "rrf").strip().lower(),
            rrf_k=_int(env, "RAG_RRF_K", "60"),
            dense_weight=_float(env, "RAG_DENSE_WEIGHT", "1.0"),
            bm25_weight=_float(env, "RAG_BM25_WEIGHT", "1.0"),
            rerank=_bool(env, "RAG_RERANK", "true"),
            top_k_reranker=_int(env, "RAG_TOP_K_RERANKER", "10"),
            rerank_min_score=_float(env, "RAG_RERANK_MIN_SCORE", "0.1"),
            expand_sections=_bool(env, "RAG_EXPAND_SECTIONS", "true"),
            section_max_tokens=_int(env, "RAG_SECTION_MAX_TOKENS", "2000"),
        )
        if cfg.mode not in SEARCH_MODES:
            raise ValueError(f"RAG_SEARCH_MODE must be one of {', '.join(SEARCH_MODES)}, got {cfg.mode!r}")
        if cfg.fusion not in FUSIONS:
            raise ValueError(f"RAG_FUSION must be one of {', '.join(FUSIONS)}, got {cfg.fusion!r}")
        if cfg.dense_weight < 0 or cfg.bm25_weight < 0 or cfg.dense_weight + cfg.bm25_weight == 0:
            raise ValueError("RAG_DENSE_WEIGHT and RAG_BM25_WEIGHT must be >= 0 and not both 0")
        if not 0 <= cfg.rerank_min_score < 1:
            raise ValueError(f"RAG_RERANK_MIN_SCORE must be in [0, 1), got {cfg.rerank_min_score}")
        return cfg


@dataclass
class Hit:
    """One retrieved chunk, with what's needed to cite it."""
    rank: int          # final position (after reranking, when it ran)
    score: float       # rerank score (0..1) when reranked, else the fusion score
    fused_rank: int    # position in the hybrid-search list
    fused_score: float
    point_id: str
    doc_sha: str
    filename: str
    chunk_index: int
    pages: list[int]
    headings: list[str]
    text: str          # the chunk as it appears in the document
    context_text: str  # heading path + text: what was embedded and BM25-indexed
    rerank_score: float | None = None
    # A whole section (step 3) stands for several chunks: all their point ids,
    # in document order, and how many of them the search actually matched.
    point_ids: list[str] = field(default_factory=list)
    matched: int = 1

    @property
    def ids(self) -> list[str]:
        return self.point_ids or [self.point_id]


class Retriever:
    def __init__(self, cfg: SearchConfig | None = None, client: httpx.Client | None = None):
        self.cfg = cfg or SearchConfig.from_env()
        self._client = client or httpx.Client()
        self._owns_client = client is None
        self._checked = False
        self._reranker = None
        self._reranker_lock = threading.Lock()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "Retriever":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -------------------------------------------------------------- helpers --

    def _qdrant(self, method: str, path: str, body: dict | None = None) -> dict:
        resp = self._client.request(method, f"{self.cfg.qdrant_url}{path}", json=body, timeout=60)
        if resp.status_code == 404 and "doesn't exist" in resp.text:
            raise RuntimeError(f"Qdrant collection {self.cfg.collection!r} doesn't exist. Run `./ragctl embed` first.")
        if resp.is_error:
            raise RuntimeError(f"qdrant {method} {path} failed ({resp.status_code}): {resp.text[:500]}")
        return resp.json()["result"]

    def _check_index(self) -> None:
        """Query vectors must come from the model the index was built with;
        a mismatch doesn't error, it just returns nonsense."""
        if self._checked:
            return
        sample = self._qdrant("POST", f"/collections/{self.cfg.collection}/points/scroll",
                              {"limit": 1, "with_payload": ["embed_model"]})["points"]
        if sample and sample[0]["payload"].get("embed_model") != self.cfg.embed_model:
            raise RuntimeError(
                f"The index was built with {sample[0]['payload'].get('embed_model')}, but EMBED_MODEL is "
                f"{self.cfg.embed_model}. Run `./ragctl embed --recreate` or restore EMBED_MODEL."
            )
        self._checked = True

    def embed_query(self, text: str) -> list[float]:
        body = {"model": self.cfg.embed_model, "input": [text], "truncate": True, "keep_alive": "10m"}
        for attempt, backoff in enumerate((*EMBED_RETRY_BACKOFF_S, None), start=1):
            try:
                resp = self._client.post(f"{self.cfg.ollama_url}/api/embed", json=body, timeout=120)
                if resp.status_code not in RETRYABLE_STATUS:
                    if resp.is_error:
                        raise RuntimeError(f"ollama embed failed ({resp.status_code}): {resp.text[:500]}")
                    return resp.json()["embeddings"][0]
                reason = f"HTTP {resp.status_code}"
            except httpx.TransportError as exc:
                reason = f"{type(exc).__name__}: {exc}"
            if backoff is None:
                raise RuntimeError(f"ollama embed failed after {attempt} attempts ({reason})")
            time.sleep(backoff)
        raise AssertionError("unreachable")

    def _fusion_query(self) -> dict:
        if self.cfg.fusion == "dbsf":
            return {"fusion": "dbsf"}
        # Weighted RRF: score = sum over retrievers of weight / (k + rank).
        return {"rrf": {"k": self.cfg.rrf_k, "weights": [self.cfg.dense_weight, self.cfg.bm25_weight]}}

    def build_query(self, question: str, vector: list[float] | None, mode: str | None = None, limit: int | None = None) -> dict:
        mode = mode or self.cfg.mode
        limit = limit or self.cfg.top_k
        dense = {"query": vector, "using": "dense"}
        bm25 = {"query": {"text": question, "model": "qdrant/bm25"}, "using": "bm25"}
        common = {"limit": limit, "with_payload": True}
        if mode == "dense":
            return {**dense, **common}
        if mode == "bm25":
            return {**bm25, **common}
        return {
            "prefetch": [{**dense, "limit": self.cfg.prefetch_k}, {**bm25, "limit": self.cfg.prefetch_k}],
            "query": self._fusion_query(),
            **common,
        }

    # --------------------------------------------------------------- search --

    def search(self, question: str, mode: str | None = None, limit: int | None = None) -> list[Hit]:
        """Top chunks for a question. `mode` and `limit` default to .env."""
        question = question.strip()
        if not question:
            return []
        self._check_index()
        mode = mode or self.cfg.mode
        vector = None if mode == "bm25" else self.embed_query(question)
        points = self._qdrant("POST", f"/collections/{self.cfg.collection}/points/query",
                              self.build_query(question, vector, mode, limit))["points"]
        return [
            Hit(
                rank=rank,
                score=p["score"],
                fused_rank=rank,
                fused_score=p["score"],
                point_id=str(p["id"]),
                doc_sha=p["payload"]["doc_sha"],
                filename=p["payload"]["filename"],
                chunk_index=p["payload"]["chunk_index"],
                pages=p["payload"].get("pages") or [],
                headings=p["payload"].get("headings") or [],
                text=p["payload"]["text"],
                context_text=p["payload"].get("context_text") or p["payload"]["text"],
            )
            for rank, p in enumerate(points, start=1)
        ]

    @property
    def reranker(self):
        """Loaded on first use: importing torch and moving the model to the
        GPU takes a few seconds, which search-only callers shouldn't pay."""
        with self._reranker_lock:  # concurrent first requests must not load it twice
            if self._reranker is None:
                from rerank import Reranker, RerankConfig

                self._reranker = Reranker(RerankConfig.from_env())
        return self._reranker

    def retrieve(self, question: str, limit: int | None = None, exclude: set[str] | None = None) -> list[Hit]:
        """The final results for a question: hybrid search, reranked and
        thresholded when RAG_RERANK=true, grouped into whole sections when
        RAG_EXPAND_SECTIONS=true, then at most `limit` (default
        RAG_TOP_K_RERANKER), best first. Chunks whose point_id is in
        `exclude` are skipped, so asking again with the same question
        returns the next-best unseen ones."""
        limit = limit or self.cfg.top_k_reranker
        exclude = exclude or set()
        hits = [h for h in self.search(question) if h.point_id not in exclude]
        if self.cfg.rerank and hits:
            # Score the text as it was indexed (heading path + chunk), so the
            # reranker sees the same context the retrievers did.
            scores = self.reranker.score(question, [h.context_text for h in hits])
            for h, s in zip(hits, scores):
                h.rerank_score = s
                h.score = s
            hits.sort(key=lambda h: h.rerank_score, reverse=True)
            hits = [h for h in hits if h.rerank_score >= self.cfg.rerank_min_score]
        if self.cfg.expand_sections:
            hits = self.expand_sections(hits, exclude)
        kept = hits[:limit]
        for rank, h in enumerate(kept, start=1):
            h.rank = rank
        return kept

    # ------------------------------------------------------------- sections --

    def _section_chunks(self, doc_sha: str, prefix: tuple[str, ...]) -> list[dict] | None:
        """Every chunk of a document whose heading path starts with prefix,
        in document order; None if there are too many to be worth fetching."""
        body = {
            "filter": {"must": [
                {"key": "doc_sha", "match": {"value": doc_sha}},
                # Narrows the scan; the full prefix is checked below.
                {"key": "headings", "match": {"value": prefix[-1]}},
            ]},
            "limit": SECTION_FETCH_LIMIT + 1,
            "with_payload": ["chunk_index", "headings", "pages", "text"],
        }
        points = self._qdrant("POST", f"/collections/{self.cfg.collection}/points/scroll", body)["points"]
        if len(points) > SECTION_FETCH_LIMIT:
            return None
        chunks = [p for p in points if tuple((p["payload"].get("headings") or [])[:len(prefix)]) == prefix]
        return sorted(chunks, key=lambda p: p["payload"]["chunk_index"])

    def _section_hit(self, prefix: tuple[str, ...], chunks: list[dict], members: list[Hit]) -> Hit:
        """One Hit for a whole section: its chunks in order, each subsection's
        heading kept as a line where it starts."""
        parts, prev = [], ()
        for c in chunks:
            extra = tuple(c["payload"].get("headings") or [])[len(prefix):]
            # Headings that begin here: the part of this chunk's path below the
            # section that differs from the previous chunk's.
            same = 0
            while same < min(len(extra), len(prev)) and extra[same] == prev[same]:
                same += 1
            parts.append("\n".join([*extra[same:], c["payload"]["text"].strip()]))
            prev = extra
        text = "\n\n".join(parts)
        best = max(members, key=lambda h: h.score)
        ids = [str(c["id"]) for c in chunks]
        return Hit(
            rank=best.rank, score=best.score, fused_rank=min(h.fused_rank for h in members),
            fused_score=max(h.fused_score for h in members), point_id=str(chunks[0]["id"]),
            doc_sha=best.doc_sha, filename=best.filename, chunk_index=chunks[0]["payload"]["chunk_index"],
            pages=sorted({p for c in chunks for p in c["payload"].get("pages") or []}),
            headings=list(prefix), text=text, context_text="\n".join([*prefix, text]),
            rerank_score=best.rerank_score, point_ids=ids, matched=sum(h.matched for h in members),
        )

    def expand_sections(self, hits: list[Hit], exclude: set[str] | None = None) -> list[Hit]:
        """Replace results that share a heading with the whole section under
        the deepest such heading, where it fits RAG_SECTION_MAX_TOKENS. The
        section takes the place of its best-ranked member. Repeats until
        nothing more can be grouped, so a section already built is absorbed
        by an enclosing one that fits, rather than returned inside it twice."""
        exclude = exclude or set()
        out = list(hits)
        too_big: set[tuple[str, tuple[str, ...]]] = set()
        while True:
            counts = Counter((h.doc_sha, tuple(h.headings[:n])) for h in out
                             for n in range(1, len(h.headings) + 1))
            # Deepest shared heading first; skip any enclosing one already too big.
            candidates = sorted((k for k, c in counts.items() if c >= 2), key=lambda k: -len(k[1]))
            candidates = [(sha, p) for sha, p in candidates
                          if not any(sha == s2 and p2[:len(p)] == p for s2, p2 in too_big)]
            for sha, prefix in candidates:
                chunks = self._section_chunks(sha, prefix)
                text_tokens = sum(estimate_tokens(c["payload"]["text"]) for c in chunks or [])
                if not chunks or text_tokens > self.cfg.section_max_tokens:
                    too_big.add((sha, prefix))
                    continue
                if any(str(c["id"]) in exclude for c in chunks):
                    # Part of it was already handed out; returning it whole would repeat text.
                    too_big.add((sha, prefix))
                    continue
                # Everything under the heading: single chunks and sections built
                # in an earlier pass alike.
                members = [h for h in out if h.doc_sha == sha and tuple(h.headings[:len(prefix)]) == prefix]
                section = self._section_hit(prefix, chunks, members)
                first = min(out.index(h) for h in members)
                out = [h for h in out if h not in members]
                out.insert(first, section)
                break
            else:
                return out


# ---------------------------------------------------------------------- CLI --

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("question", help="The question to search for")
    parser.add_argument("--fused", action="store_true", help="Show the hybrid-search list (RAG_TOP_K) without reranking")
    parser.add_argument("--json", action="store_true", help="Print hits as JSON instead of text")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Show the search settings, and each hit's rank in the dense and BM25 lists (hybrid mode)")
    args = parser.parse_args()

    try:
        cfg = SearchConfig.from_env()
        if cfg.rerank:
            from rerank import RerankConfig

            RerankConfig.from_env()  # validate now rather than when the model loads
    except ValueError as exc:
        sys.exit(f"Bad search settings in .env: {exc}")

    with Retriever(cfg) as retriever:
        try:
            reranking = cfg.rerank and not args.fused
            if reranking:
                retriever.reranker  # load now, so the timing below is the query alone
            t0 = time.monotonic()
            hits = retriever.search(args.question) if args.fused else retriever.retrieve(args.question)
            dt = time.monotonic() - t0
        except (RuntimeError, httpx.HTTPError) as exc:
            sys.exit(str(exc))

        if args.json:
            print(json.dumps([asdict(h) for h in hits], indent=2, ensure_ascii=False))
            return 0

        if args.verbose:
            weights = f", weights dense={cfg.dense_weight} bm25={cfg.bm25_weight}, k={cfg.rrf_k}" if cfg.fusion == "rrf" else ""
            print(f"collection={cfg.collection} mode={cfg.mode} prefetch_k={cfg.prefetch_k} top_k={cfg.top_k} "
                  f"fusion={cfg.fusion}{weights}")
            if reranking:
                r = retriever.reranker
                print(f"rerank: {r.cfg.model} on {r.device} ({str(r.dtype).removeprefix('torch.')}, "
                      f"loaded in {r.load_seconds:.1f}s), keep top {cfg.top_k_reranker}"
                      + (f" scoring >= {cfg.rerank_min_score}" if cfg.rerank_min_score else ""))
            elif not cfg.rerank:
                print(f"rerank: off (RAG_RERANK=false), showing the top {cfg.top_k_reranker} fused results")
        # Where each hit sits in the two underlying rankings shows which
        # retriever found it -- the main thing to look at when tuning.
        sub_ranks: dict[str, dict[str, int]] = {}
        if args.verbose and cfg.mode == "hybrid":
            for mode in ("dense", "bm25"):
                sub = retriever.search(args.question, mode=mode, limit=cfg.prefetch_k)
                sub_ranks[mode] = {h.point_id: h.rank for h in sub}

        print(f"{len(hits)} results in {dt * 1000:.0f} ms for: {args.question}")
        for h in hits:
            pages = ",".join(map(str, h.pages)) or "?"
            section = f"  section: {len(h.ids)} chunks, {h.matched} matched" if len(h.ids) > 1 else ""
            print(f"\n[{h.rank}] score={h.score:.4f}  {h.filename}  (pages {pages}){section}")
            if args.verbose and h.rerank_score is not None:
                print(f"    rerank {h.rerank_score:.3f}, was #{h.fused_rank} after fusion (fused score {h.fused_score:.4f})")
            if sub_ranks:
                found = ", ".join(f"{m} #{sub_ranks[m][h.point_id]}" if h.point_id in sub_ranks[m] else f"{m} -"
                                  for m in ("dense", "bm25"))
                print(f"    found by: {found}")
            if h.headings:
                print(f"    headings: {' > '.join(h.headings)}")
            text = " ".join(h.text.split())
            print(f"    {text[:400]}{'…' if len(text) > 400 else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

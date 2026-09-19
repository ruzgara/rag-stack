#!/usr/bin/env python3
"""
Chunk, embed and index converted documents for rag-stack.

Per document recorded as processed in data/manifest.jsonl:
  1. Load its DoclingDocument JSON (data/docling/<sha>.json, written by
     ingest.py).
  2. HybridChunker: structure-aware chunks, sized with the embed model's own
     tokenizer. Each chunk is contextualized by prepending its heading path.
  3. Embed the contextualized text via Ollama's /api/embed, one batch per
     request and one request at a time, so Ollama's queue never sees a burst.
  4. Upsert into Qdrant with a dense vector plus a BM25 sparse vector that
     Qdrant computes server-side, ready for hybrid (RRF) search.

A document is skipped when Qdrant already holds all of its chunks under the
current fingerprint (embed model + tokenizer + chunk size + chunker version +
the content of its docling JSON, so a re-conversion is re-embedded too).
Qdrant itself, not a separate ledger, is the record of what's embedded.

Run with: uv run embed.py [--doc SHA] [--recreate] [--show-chunks [--full]] [-v/--verbose]

Searching the index lives in services/query (`./ragctl search`).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx
from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
MANIFEST = DATA_DIR / "manifest.jsonl"

# Share the tokenizer download cache with docling-serve; must be set before
# transformers is imported.
os.environ.setdefault("HF_HOME", str(DATA_DIR / "hf-cache"))
# transformers is only used for tokenizers; silence its "PyTorch was not found" notice.
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")

# Bump when chunking or payload logic changes, so every document is re-embedded.
CHUNKER_VERSION = 1

# Namespace for deterministic point IDs: uuid5(NS, "<sha>:<fingerprint>:<index>").
POINT_ID_NS = uuid.UUID("5b0f5e8e-6c2a-4f0e-9d3b-2a7c1e4f8a10")

# Waits between retries of a failed embed request. A 503 means Ollama's queue
# is full (see OLLAMA_MAX_QUEUE); connection errors and timeouts usually mean
# the model is still loading. Both tend to clear on their own.
EMBED_RETRY_BACKOFF_S = (5, 15, 30)
RETRYABLE_STATUS = {429, 502, 503, 504}

VERBOSE = False


def vprint(*args, **kwargs) -> None:
    if VERBOSE:
        kwargs.setdefault("flush", True)  # stdout is block-buffered when piped/backgrounded
        print(*args, **kwargs)


def load_env() -> dict:
    env = {**dotenv_values(REPO_ROOT / ".env"), **os.environ}
    return env


@dataclass
class Config:
    ollama_url: str
    qdrant_url: str
    collection: str
    embed_model: str
    tokenizer: str
    max_tokens: int
    batch_size: int

    @classmethod
    def from_env(cls, env: dict) -> "Config":
        missing = [k for k in ("EMBED_MODEL", "EMBED_TOKENIZER") if not env.get(k)]
        if missing:
            sys.exit(f"Missing required .env values: {', '.join(missing)}.")
        # Ollama treats an untagged name as "<name>:latest"; normalize so that
        # "bge-m3" and "bge-m3:latest" count as the same model in the index.
        embed_model = env["EMBED_MODEL"] if ":" in env["EMBED_MODEL"] else f"{env['EMBED_MODEL']}:latest"
        return cls(
            ollama_url=f"http://127.0.0.1:{env.get('OLLAMA_PORT', '11434')}",
            qdrant_url=f"http://127.0.0.1:{env.get('QDRANT_PORT', '6333')}",
            collection=env.get("QDRANT_COLLECTION", "ragstack_chunks"),
            embed_model=embed_model,
            tokenizer=env["EMBED_TOKENIZER"],
            max_tokens=int(env.get("CHUNK_MAX_TOKENS", "512")),
            batch_size=int(env.get("EMBED_BATCH_SIZE", "32")),
        )

    @property
    def fingerprint(self) -> str:
        key = json.dumps([self.embed_model, self.tokenizer, self.max_tokens, CHUNKER_VERSION])
        return hashlib.sha256(key.encode()).hexdigest()[:16]


@dataclass
class Doc:
    sha256: str
    filename: str
    docling_json: Path


def doc_fingerprint(cfg: Config, doc: Doc) -> str:
    """Settings fingerprint plus the document's converted content: re-running
    ingest on a file changes its JSON, which must replace its old chunks."""
    content = hashlib.sha256(doc.docling_json.read_bytes()).hexdigest()
    return hashlib.sha256(f"{cfg.fingerprint}:{content}".encode()).hexdigest()[:16]


@dataclass
class Chunk:
    index: int
    text: str
    context_text: str
    headings: list[str]
    pages: list[int]


# ------------------------------------------------------------------ manifest --

def select_docs(sha_prefix: str | None) -> tuple[list[Doc], list[str]]:
    """Latest processed manifest row per sha256. Returns (docs with docling
    JSON, filenames of processed docs that predate JSON output)."""
    latest: dict[str, dict] = {}
    if MANIFEST.exists():
        with MANIFEST.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("status") == "processed":
                    latest[rec["sha256"]] = rec  # later rows win

    docs, no_json = [], []
    for sha, rec in latest.items():
        if sha_prefix and not sha.startswith(sha_prefix):
            continue
        json_rel = rec.get("docling_json")
        if json_rel and (REPO_ROOT / json_rel).exists():
            docs.append(Doc(sha256=sha, filename=rec["filename"], docling_json=REPO_ROOT / json_rel))
        else:
            no_json.append(rec["filename"])
    docs.sort(key=lambda d: d.filename)
    return docs, sorted(no_json)


# -------------------------------------------------------------------- ollama --

def embed_batch(client: httpx.Client, cfg: Config, texts: list[str]) -> list[list[float]]:
    body = {
        "model": cfg.embed_model,
        "input": texts,
        # Error out instead of silently cutting a chunk that's too long for
        # the model -- that would mean EMBED_TOKENIZER doesn't match EMBED_MODEL.
        "truncate": False,
        "keep_alive": "10m",
    }
    for attempt, backoff in enumerate((*EMBED_RETRY_BACKOFF_S, None), start=1):
        try:
            resp = client.post(f"{cfg.ollama_url}/api/embed", json=body, timeout=300)
            if resp.status_code not in RETRYABLE_STATUS:
                if resp.is_error:
                    raise RuntimeError(f"ollama embed failed ({resp.status_code}): {resp.text[:500]}")
                embeddings = resp.json()["embeddings"]
                if len(embeddings) != len(texts):
                    raise RuntimeError(f"ollama returned {len(embeddings)} embeddings for {len(texts)} inputs")
                return embeddings
            reason = f"HTTP {resp.status_code}: {resp.text[:200]}"
        except httpx.TransportError as exc:
            reason = f"{type(exc).__name__}: {exc}"
        if backoff is None:
            raise RuntimeError(f"ollama embed failed after {attempt} attempts ({reason})")
        vprint(f"  ollama: attempt {attempt} failed ({reason}), retrying in {backoff}s")
        time.sleep(backoff)
    raise AssertionError("unreachable")


# -------------------------------------------------------------------- qdrant --

def qdrant(client: httpx.Client, cfg: Config, method: str, path: str, body: dict | None = None) -> dict:
    resp = client.request(method, f"{cfg.qdrant_url}{path}", json=body, timeout=120)
    if resp.is_error:
        raise RuntimeError(f"qdrant {method} {path} failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()["result"]


def ensure_collection(client: httpx.Client, cfg: Config, dim: int, recreate: bool) -> None:
    c = cfg.collection
    exists = client.get(f"{cfg.qdrant_url}/collections/{c}", timeout=30).status_code == 200

    if exists and recreate:
        vprint(f"  qdrant: --recreate, deleting collection {c}")
        qdrant(client, cfg, "DELETE", f"/collections/{c}")
        exists = False

    if not exists:
        qdrant(client, cfg, "PUT", f"/collections/{c}", {
            "vectors": {"dense": {"size": dim, "distance": "Cosine"}},
            # Qdrant computes BM25 term weights itself (model "qdrant/bm25");
            # the IDF modifier makes it true BM25 rather than raw term counts.
            "sparse_vectors": {"bm25": {"modifier": "idf"}},
        })
        for field in ("doc_sha", "fingerprint"):
            qdrant(client, cfg, "PUT", f"/collections/{c}/index?wait=true",
                   {"field_name": field, "field_schema": "keyword"})
        print(f"Created Qdrant collection {c} (dense dim={dim}, sparse bm25).")
        return

    info = qdrant(client, cfg, "GET", f"/collections/{c}")
    have_dim = info["config"]["params"]["vectors"]["dense"]["size"]
    if have_dim != dim:
        sys.exit(
            f"Collection {c} holds {have_dim}-dim vectors, but {cfg.embed_model} produces {dim}. "
            "The embed model changed; run `./ragctl embed --recreate` to rebuild the index."
        )
    sample = qdrant(client, cfg, "POST", f"/collections/{c}/points/scroll",
                    {"limit": 1, "with_payload": ["embed_model"]})["points"]
    if sample and sample[0]["payload"].get("embed_model") != cfg.embed_model:
        sys.exit(
            f"Collection {c} was built with {sample[0]['payload'].get('embed_model')}, "
            f"not {cfg.embed_model}. Vectors from two models can't share an index; "
            "run `./ragctl embed --recreate` to rebuild it."
        )


def doc_filter(sha: str, fingerprint: str | None = None, exclude_fingerprint: bool = False) -> dict:
    """Points of one document; optionally only those with (or without) a fingerprint."""
    flt: dict = {"must": [{"key": "doc_sha", "match": {"value": sha}}]}
    if fingerprint is not None:
        cond = {"key": "fingerprint", "match": {"value": fingerprint}}
        if exclude_fingerprint:
            flt["must_not"] = [cond]
        else:
            flt["must"].append(cond)
    return flt


def count_points(client: httpx.Client, cfg: Config, flt: dict) -> int:
    return qdrant(client, cfg, "POST", f"/collections/{cfg.collection}/points/count",
                  {"filter": flt, "exact": True})["count"]


def is_up_to_date(client: httpx.Client, cfg: Config, sha: str, fp: str) -> bool:
    """True when every point for this doc carries the current fingerprint and
    none are missing (an interrupted run leaves fewer than chunk_count)."""
    current = doc_filter(sha, fp)
    have = count_points(client, cfg, current)
    if not have or have != count_points(client, cfg, doc_filter(sha)):
        return False
    sample = qdrant(client, cfg, "POST", f"/collections/{cfg.collection}/points/scroll",
                    {"filter": current, "limit": 1, "with_payload": ["chunk_count"]})["points"]
    return bool(sample) and sample[0]["payload"].get("chunk_count") == have


# ------------------------------------------------------------------ chunking --

def make_chunker(cfg: Config):
    from docling_core.transforms.chunker.hybrid_chunker import HybridChunker
    from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer

    vprint(f"  chunker: loading tokenizer {cfg.tokenizer} (max_tokens={cfg.max_tokens})")
    try:
        # Use the cached copy without touching the network once it's been downloaded.
        tokenizer = HuggingFaceTokenizer.from_pretrained(
            model_name=cfg.tokenizer, max_tokens=cfg.max_tokens, local_files_only=True)
    except OSError:
        tokenizer = HuggingFaceTokenizer.from_pretrained(model_name=cfg.tokenizer, max_tokens=cfg.max_tokens)
    return HybridChunker(tokenizer=tokenizer, merge_peers=True)


def chunk_doc(chunker, doc: Doc) -> list[Chunk]:
    from docling_core.types.doc import DoclingDocument

    dl_doc = DoclingDocument.model_validate_json(doc.docling_json.read_text(encoding="utf-8"))
    chunks = []
    for raw in chunker.chunk(dl_doc=dl_doc):
        # contextualize() prepends the heading path to the chunk text; that's
        # what gets embedded and BM25-indexed. HybridChunker counts tokens on
        # this contextualized form, so headings can't push a chunk over max_tokens.
        context_text = chunker.contextualize(chunk=raw)
        if not context_text.strip():
            continue
        pages = sorted({prov.page_no for item in raw.meta.doc_items for prov in item.prov})
        chunks.append(Chunk(
            index=len(chunks),
            text=raw.text,
            context_text=context_text,
            headings=list(raw.meta.headings or []),
            pages=pages,
        ))
    return chunks


# ------------------------------------------------------------------ pipeline --

def embed_doc(client: httpx.Client, cfg: Config, chunker, doc: Doc, fp: str) -> int:
    t0 = time.monotonic()
    chunks = chunk_doc(chunker, doc)
    if not chunks:
        raise RuntimeError("chunker produced no chunks")
    vprint(f"  chunker: {len(chunks)} chunks in {time.monotonic() - t0:.1f}s")

    for start in range(0, len(chunks), cfg.batch_size):
        batch = chunks[start:start + cfg.batch_size]
        t1 = time.monotonic()
        vectors = embed_batch(client, cfg, [c.context_text for c in batch])
        points = [
            {
                "id": str(uuid.uuid5(POINT_ID_NS, f"{doc.sha256}:{fp}:{c.index}")),
                "vector": {
                    "dense": vec,
                    "bm25": {"text": c.context_text, "model": "qdrant/bm25"},
                },
                "payload": {
                    "doc_sha": doc.sha256,
                    "filename": doc.filename,
                    "chunk_index": c.index,
                    "chunk_count": len(chunks),
                    "text": c.text,
                    "context_text": c.context_text,
                    "headings": c.headings,
                    "pages": c.pages,
                    "embed_model": cfg.embed_model,
                    "fingerprint": fp,
                },
            }
            for c, vec in zip(batch, vectors)
        ]
        qdrant(client, cfg, "PUT", f"/collections/{cfg.collection}/points?wait=true", {"points": points})
        vprint(f"  batch {start // cfg.batch_size + 1}: {len(batch)} chunks embedded + upserted in {time.monotonic() - t1:.1f}s")

    # New points are in; only now drop any from an older fingerprint, so the
    # document stays searchable throughout a re-embed.
    stale = doc_filter(doc.sha256, fp, exclude_fingerprint=True)
    if count_points(client, cfg, stale):
        qdrant(client, cfg, "POST", f"/collections/{cfg.collection}/points/delete?wait=true", {"filter": stale})
        vprint("  qdrant: removed points from an older fingerprint")
    return len(chunks)


def show_chunks(cfg: Config, docs: list[Doc], full: bool) -> None:
    """Print each document's chunks exactly as they would be embedded, plus
    size stats. Runs the real chunker offline; needs neither Ollama nor Qdrant."""
    chunker = make_chunker(cfg)
    for doc in docs:
        chunks = chunk_doc(chunker, doc)
        tokens = [chunker.tokenizer.count_tokens(text=c.context_text) for c in chunks]
        print(f"\n{'=' * 80}\n{doc.filename}\nsha256={doc.sha256}  chunks={len(chunks)}\n{'=' * 80}")
        for c, n in zip(chunks, tokens):
            pages = ",".join(map(str, c.pages)) or "?"
            print(f"\n--- chunk {c.index}  tokens={n}/{cfg.max_tokens}  pages {pages}")
            print(f"    headings: {' > '.join(c.headings) if c.headings else '(none)'}")
            text = c.context_text
            if not full and len(text) > 600:
                text = f"{text[:600]}… [{len(text) - 600} more chars, --full to show]"
            print("    " + text.replace("\n", "\n    "))
        if tokens:
            no_heading = sum(1 for c in chunks if not c.headings)
            depths = {len(c.headings) for c in chunks if c.headings}
            print(
                f"\nSummary: {len(chunks)} chunks, tokens min={min(tokens)} "
                f"avg={sum(tokens) // len(tokens)} max={max(tokens)} (limit {cfg.max_tokens}), "
                f"{sum(1 for n in tokens if n < cfg.max_tokens // 4)} under a quarter of the limit, "
                f"{no_heading} without headings, heading depths seen: {sorted(depths) or 'none'}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--doc", dest="sha_prefix", default=None, help="Only embed the document whose sha256 starts with this")
    parser.add_argument("--recreate", action="store_true", help="Delete and rebuild the Qdrant collection (needed after changing EMBED_MODEL)")
    parser.add_argument("--show-chunks", action="store_true", help="Print how documents are chunked (as embedded, with token counts) and exit; needs neither Ollama nor Qdrant")
    parser.add_argument("--full", action="store_true", help="With --show-chunks, print whole chunks instead of truncating at 600 chars")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print per-stage detail: chunk counts, batch timings, retries")
    args = parser.parse_args()

    global VERBOSE
    VERBOSE = args.verbose
    try:
        sys.stdout.reconfigure(line_buffering=True)  # stdout is block-buffered when piped/backgrounded otherwise
    except Exception:  # noqa: BLE001 - cosmetic, never worth failing the run over
        pass

    cfg = Config.from_env(load_env())

    if args.show_chunks:
        if args.recreate:
            sys.exit("--show-chunks can't be combined with --recreate.")
        docs, _ = select_docs(args.sha_prefix)
        if not docs:
            sys.exit("No documents with docling JSON match. Run `./ragctl ingest` (or --reprocess) first.")
        show_chunks(cfg, docs, args.full)
        return 0

    with httpx.Client() as client:
        # Fail fast if the local services aren't reachable.
        for name, url in (("Ollama", f"{cfg.ollama_url}/api/version"), ("Qdrant", f"{cfg.qdrant_url}/readyz")):
            try:
                client.get(url, timeout=10).raise_for_status()
            except Exception as exc:
                sys.exit(f"{name} is not reachable at {url} ({exc}). Run `./ragctl status` first.")

        # The vector size comes from the model itself, never from config.
        dim = len(embed_batch(client, cfg, ["dimension probe"])[0])
        vprint(f"  ollama: {cfg.embed_model} produces {dim}-dim vectors")
        ensure_collection(client, cfg, dim, args.recreate)

        docs, no_json = select_docs(args.sha_prefix)
        if no_json:
            print(f"{len(no_json)} processed document(s) have no docling JSON yet and were skipped. "
                  "Run `./ragctl ingest --reprocess` to convert them again.")
            for name in no_json:
                vprint(f"  no json: {name}")
        if not docs:
            print("No documents to embed.")
            return 0

        chunker = None  # loading the tokenizer is slow; only do it if something needs embedding
        ok, bad, skipped = 0, 0, 0
        total = len(docs)
        for i, doc in enumerate(docs, start=1):
            fp = doc_fingerprint(cfg, doc)
            if is_up_to_date(client, cfg, doc.sha256, fp):
                skipped += 1
                print(f"[{i}/{total}] skip (up to date): {doc.filename}")
                continue
            print(f"[{i}/{total}] embedding: {doc.filename}")
            t0 = time.monotonic()
            try:
                if chunker is None:
                    chunker = make_chunker(cfg)
                n = embed_doc(client, cfg, chunker, doc, fp)
                ok += 1
                print(f"  -> ok, chunks={n}, {time.monotonic() - t0:.1f}s total")
            except Exception as exc:  # noqa: BLE001 - report and continue with the next doc
                bad += 1
                print(f"  -> FAILED ({time.monotonic() - t0:.1f}s): {exc}")

    print(f"\nDone. {ok} embedded, {skipped} up to date, {bad} failed. Collection: {cfg.collection}")
    return 1 if bad and not ok else 0


if __name__ == "__main__":
    raise SystemExit(main())

# rag-stack

A self-contained, local document-ingestion and indexing pipeline for macOS
on Apple Silicon, meant to grow into a full local RAG (retrieval-augmented
generation) system. One repo, one command (`./ragctl`), configured as if it
were running on a bigger machine — it's meant to be installed as-is on a
more powerful Mac later, not migrated.

## Status

**v2 is mid-migration.** Open WebUI has been removed — it previously
provided chunking, embedding, Qdrant writes, hybrid search, reranking,
retrieval and the chat UI itself. The **indexing half** has been rebuilt:
documents are OCR'd and parsed, chunked along Docling's document structure,
embedded with Ollama and written to Qdrant with both dense and BM25 vectors,
and **retrieval** over them works: hybrid search plus GPU reranking
(`./ragctl search`). There is still **no chat interface and no answer
generation** — those components don't exist yet.

**You provide the models.** What's wired up and pinned to specific
versions: OCR, document parsing, the Ollama-backed VLM OCR fallback, and
the embedding/indexing pipeline.

## Architecture

```
   ingest/ (native): data/inbox → page triage → born-digital: docling
          (embedded text) | scanned: rotation fix → docling + ocrmac
          → quality check → [VLM fallback] → data/docling/<sha>.json
                    │                        + data/markdown/<sha>.md
                    ▼
   docling-serve (native, on-device GPU) ── VLM fallback ──► Ollama.app
                                                    (native, MLX)
   embed/ (native): data/docling/<sha>.json → HybridChunker   ▲
          (heading path prepended) → batches ─── /api/embed ───┘
                    │
                    ▼
            ┌──────────── Docker Desktop ────────────────────┐
            │ Qdrant :6333 — collection ragstack_chunks:      │
            │   dense (Ollama embeddings) + bm25 (sparse,     │
            │   computed by Qdrant) → hybrid search via RRF   │
            └──────────────────────────────────────────────────┘
```

| Layer | Choice | Runs where |
|---|---|---|
| Model server | Ollama | native macOS, MLX/Metal |
| VLM OCR fallback | you provide (e.g. Qwen-VL) | Ollama |
| OCR | ocrmac (Apple Vision) via docling; tesseract only for page orientation | native macOS |
| Document parsing / VLM pipeline | Docling (docling-serve) | native macOS |
| Chunking | Docling HybridChunker (docling-core) | native macOS batch job |
| Embeddings | you provide (e.g. bge-m3) | Ollama |
| Vector DB + BM25 | Qdrant (dense + server-side BM25 sparse) | Docker |
| Chat UI / query service / reranking | not yet built | — |

Ollama and Docling run **natively** (not in Docker) so they can use Apple's
MLX/Metal acceleration. Qdrant runs in Docker for easy, reproducible
packaging (`compose.yaml` still lists the old `open-webui` service and
`ragstack_openwebui` volume, but `ragctl` only ever starts Qdrant).
Everything the stack installs lives inside this repo, in Docker
volumes prefixed `ragstack_`, or in LaunchAgents labelled `com.ragstack.*`
— nothing is scattered across the system, so it's easy to audit or remove.

## Requirements

- macOS on Apple Silicon (arm64).
- [Homebrew](https://brew.sh).
- [Docker Desktop](https://www.docker.com/products/docker-desktop/), running,
  with at least 8GB of RAM allocated (Settings → Resources). On a bigger
  machine, raise this — 16GB is a good target on a Mac Studio.
- [Ollama.app](https://ollama.com/download), running.
- Your VLM and embedding models already pulled with `ollama pull`.

## Install

```bash
cd rag-stack
cp .env.example .env
```

Edit `.env`:
- `VLM_MODEL`, `EMBED_MODEL` — exact names as they appear in `ollama list`.
  `CHAT_MODEL_EVERYDAY` and `CHAT_MODEL_HARD` are still in `.env.example`
  but nothing consumes them yet — they're held for the future chat
  components.
- `EMBED_TOKENIZER` — the HuggingFace tokenizer for your embedding model
  (`BAAI/bge-m3` for `bge-m3`), used to size chunks in tokens.
- Leave everything else at its default unless you know you want to change it.

```bash
./ragctl install
```

This checks prerequisites and disk space, installs Homebrew + Python
dependencies (all inside this repo), sets Ollama tuning via `launchctl`,
installs LaunchAgents for docling-serve (and, optionally, auto-ingestion),
and starts Qdrant.

Then run:

```bash
./ragctl health
```

to confirm every service is reachable.

## Using it

**Batch ingestion** is the only path in. Drop files into `data/inbox/`
and run:

```bash
./ragctl ingest
./ragctl embed
```

`ingest` also accepts `--force-vlm`, `--file PATH` to process a single
file, `--reprocess` (below) and `-v`/`--verbose`.

There's nothing to configure per file. Each PDF is first triaged page by
page (instant, nothing is rendered):

- **Born-digital** (visible text, not covered by a page-sized image): Docling
  converts the embedded text directly — no OCR, exact text, ~0.1 s/page. If
  that text fails the quality check (e.g. fonts with no usable character
  map), the file is OCR'd instead.
- **Scanned** (any page that's a page-sized image — with no text, an old
  invisible OCR layer, or a few real lines over the scan): tesseract's
  orientation detection finds sideways or upside-down pages and marks them
  rotated (lossless — the PDF's page rotation flag, nothing is
  rasterized), then Docling OCRs every page with **ocrmac** (Apple
  Vision), replacing any old OCR layer. ~0.5–0.8 s/page. Without the
  rotation step, a table printed sideways comes out transposed.

A document is OCR'd as a whole if any of its pages needs it. Images are
OCR'd with ocrmac too; other formats (docx, html…) are converted directly.
Then comes a quality check and, only if that fails (or you pass
`--force-vlm`), Docling's VLM pipeline using your configured `VLM_MODEL`
as a last resort. Up to `DOCLING_LOC_NUM_WORKERS` files (default 2) are
converted at once — ocrmac gets ~1.65× more throughput with two in
parallel, so large batches cost little more than born-digital ones would
suggest. Docling's output is written
twice, both named by the source file's sha256: `data/docling/<sha>.json`
(the full DoclingDocument — structure, headings, page numbers — which is
what gets chunked) and `data/markdown/<sha>.md` (for reading). Originals
move to `data/processed/` or `data/failed/`, and every run is logged to
`data/manifest.jsonl` so re-running `ingest` never reprocesses a file twice.

`ingest --reprocess` re-converts everything already in `data/processed/`
without moving the originals. Use it after a pipeline change, or for
documents processed before the JSON output existed — `embed` lists any it
had to skip. Then run `./ragctl embed` to re-index them.

**Embedding** (`./ragctl embed`) takes every processed document from the
manifest and:
1. Chunks its DoclingDocument JSON with Docling's `HybridChunker`, which
   splits along the document's structure and sizes chunks with
   `EMBED_TOKENIZER` (at most `CHUNK_MAX_TOKENS`, heading included).
2. Prepends each chunk's heading path, so a chunk under "Mitigation" in a
   risk chapter isn't indistinguishable from one in a currency chapter.
   The raw text, headings and page numbers are stored alongside.
3. Embeds in batches of `EMBED_BATCH_SIZE`, one request per batch and one
   batch at a time — so Ollama's request queue never sees a burst — with
   retries on 503s and timeouts.
4. Writes to Qdrant with a dense vector plus a BM25 sparse vector that
   Qdrant computes itself.

It's idempotent: Qdrant is the record of what's embedded, and a document is
skipped when all its chunks are there under the current settings. Changing
`CHUNK_MAX_TOKENS` or `EMBED_TOKENIZER` re-embeds everything on the next
run. The vector size is read from the model, never configured; switching
`EMBED_MODEL` makes `embed` stop and ask for `./ragctl embed --recreate`,
so vectors from two models never mix in one index.

**Searching** (`./ragctl search "your question"`) runs the retrieval
module in `services/query/retrieval.py` — the same code the future query
service will import (`Retriever(...).search(question)` returns ranked hits
with filename, pages, headings and text). It embeds the question with
`EMBED_MODEL`, has Qdrant run the dense and BM25 retrievers in one query,
and fuses their rankings. Every setting lives in `.env`:

| Setting | Default | Meaning |
|---|---|---|
| `RAG_SEARCH_MODE` | `hybrid` | `hybrid`, or `dense` / `bm25` alone to compare what each finds |
| `RAG_PREFETCH_K` | `40` | candidates each retriever contributes before fusion |
| `RAG_TOP_K` | `20` | results returned after fusion (the reranker's input, later) |
| `RAG_FUSION` | `rrf` | `rrf` (rank-based) or `dbsf` (normalized scores summed) |
| `RAG_RRF_K` | `60` | RRF: score = Σ weight / (k + rank); larger k flattens rank differences |
| `RAG_DENSE_WEIGHT`, `RAG_BM25_WEIGHT` | `1.0` | RRF weight of each retriever |
| `RAG_RERANK` | `true` | rerank the fused candidates; `false` returns the top fused results |
| `RAG_TOP_K_RERANKER` | `6` | chunks returned in the end |
| `RAG_RERANK_MIN_SCORE` | `0` | drop reranked chunks scoring below this (0–1); 0 keeps all |
| `RERANKER_MODEL` | `BAAI/bge-reranker-v2-m3` | HuggingFace cross-encoder |
| `RERANKER_DEVICE` | `auto` | `auto` (Apple GPU via MPS if available, else CUDA, else CPU), or force one |
| `RERANKER_BATCH_SIZE`, `RERANKER_MAX_LENGTH` | `16`, `1024` | pairs per forward pass; token limit per question+chunk pair |

**Reranking** (`services/query/rerank.py`): hybrid search scores the question
and each chunk separately, so a cross-encoder then reads each question/chunk
*pair* and scores how well the chunk answers it. It only re-scores the
`RAG_TOP_K` fused candidates, keeping `RAG_TOP_K_RERANKER`. It runs on the
Mac's GPU through PyTorch's MPS backend in float16. Measured here: ~1.2 s
to rerank 20 chunks on the GPU against ~2.8 s on the CPU, with float16
giving the same order as float32. The model (~2.3 GB) is downloaded into
`data/hf-cache` by `ragctl install`, or on first use; loading it takes ~1 s,
so a one-off `ragctl search` takes a few seconds while a long-running
service pays that once. Scores are 0–1 and comparable across questions,
which is what makes `RAG_RERANK_MIN_SCORE` usable.

`-v` prints the settings, the device the reranker landed on, and for each
hit its rerank score, where it was after fusion, and its rank in the dense
and the BM25 lists (`found by: dense #11, bm25 #3`) — the main things to
look at when tuning. `--fused` shows the un-reranked hybrid list, `--json`
prints hits for scripting.

To inspect the chunking itself, `./ragctl embed --show-chunks` prints every
chunk exactly as it would be embedded (heading prepended), with its token
count, pages and headings, then a per-document summary: token min/avg/max,
how many chunks are tiny or have no heading, and which heading depths
occurred. It runs offline — no Ollama or Qdrant needed. Narrow it with
`--doc <sha prefix>`, add `--full` to stop truncating long chunks, and pipe
it to `less` for big documents.

Set `INGEST_AUTORUN=true` in `.env` and run `./ragctl reload` to have
ingest and embed run automatically whenever a file lands in `data/inbox/`.

## Operating

```bash
./ragctl status              # what's running and reachable
./ragctl health               # checks Ollama (+ model checks + an embedding
                               # smoke test), docling-serve, and Qdrant
./ragctl logs [docling|ollama|ingest|qdrant]
./ragctl models check         # confirm .env model names exist in Ollama
./ragctl search "question"    # hybrid search + reranking (-v, --fused, --json)
./ragctl up / down / restart
./ragctl reload               # re-apply .env changes to running services
./ragctl clean --yes          # wipe ingest data, outputs and the index
```

`ragctl clean --yes` wipes `data/inbox`, `work`, `processed`, `failed`,
`markdown`, `docling` and `manifest.jsonl`, and deletes the Qdrant
collection — it resets the pipeline to a clean slate. Model caches (`data/hf-cache`, `data/docling-artifacts`) are
left alone since they're expensive to redownload and aren't "your data."
For a full teardown, use `ragctl uninstall` instead.

## Moving to a bigger machine (e.g. Mac Studio)

This repo is written to be installed fresh, not migrated:

```bash
git clone <this repo> && cd rag-stack
cp .env.example .env   # fill in your (bigger) models
./ragctl install
```

Raise `OLLAMA_MAX_LOADED_MODELS`, `OLLAMA_CONTEXT_LENGTH`,
`OLLAMA_NUM_PARALLEL`, `DOCLING_NUM_THREADS`, `DOCLING_LOC_NUM_WORKERS` in
`.env` to match the new hardware, and raise Docker Desktop's memory limit
(Settings → Resources) to something like 16GB. Set `BIND_ADDR=0.0.0.0` if
other machines on the network should reach Qdrant (and, once it exists,
the future chat/retrieval service).

## Uninstall

```bash
./ragctl uninstall             # removes containers, volumes, LaunchAgents, local Python envs
./ragctl uninstall --purge-data  # also deletes data/ (inbox, processed docs, logs, manifest)
```

Ollama.app itself is left installed and running — this stack only tunes its
environment variables via `launchctl setenv`, which clears on the next
reboot (or run `launchctl unsetenv <VAR>` to clear it immediately).

## Troubleshooting

- **docling-serve won't start** — check `./ragctl logs docling`.
  `UVICORN_WORKERS` must stay at `1` (set by the LaunchAgent) — more than
  one worker breaks its task tracking.
- **A model "not found" in `ollama list`** — run `./ragctl models check`, fix
  the name in `.env`, then `./ragctl reload`.
- **Ingest quality gate keeps triggering the VLM fallback unnecessarily** —
  raise `MIN_CHARS_PER_PAGE` in `.env`, or check that `OCR_LANG` lists
  your documents' languages as Apple locale codes (`en-US`, `de-DE`…) —
  tesseract-style codes like `eng` make every OCR job fail.
- **A scanned PDF's text looks plausible but is subtly wrong** (misread
  characters, e.g. f↔t or j↔i) — the quality gate only catches *obviously*
  broken text (too short, full of junk characters), not wrong-but-readable
  text. ocrmac is the best OCR option measured for this stack, but no OCR
  is perfect; `ingest --file PATH --force-vlm` re-runs one document through
  the VLM instead.
- **A sideways page wasn't rotated** — orientation detection skips pages it
  isn't confident about (mostly near-blank ones). Check with `ingest -v`,
  which lists the pages it rotated.
- **Versions**: `compose.yaml` pins `qdrant/qdrant` to a specific tag,
  checked at the time this repo was written — that's the only image that
  currently matters for this stack. If a pull fails because the tag was
  retired, check Qdrant's release page and bump the tag in `compose.yaml`.

## Future potential updates

- Explore docling-hierarcical-pdf to fix heading detection
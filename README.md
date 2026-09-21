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
**retrieval** over them works (hybrid search plus GPU reranking), and a
**query service** answers questions from them with citations over an HTTP
API, storing chat history — natively and OpenAI-compatibly. It has
**accounts**: users with passwords, groups, and libraries owned and shared
rather than granted, manageable from `ragctl user` or the admin console at
`/admin`. Several
sessions can use it at once, with turns queueing for the GPU. There is now
a **chat UI** (`ui/`, Svelte + Vite, served at `/`): sign-in, conversations,
streaming markdown answers with citations beside the sources they cite, and
per-library filtering.

**You provide the models.** What's wired up and pinned to specific
versions: OCR, document parsing, the Ollama-backed VLM OCR fallback, and
the embedding/indexing pipeline.

## Architecture

```
   ingest/ (native): data/inbox → page triage → born-digital: docling
          (embedded text) | scanned: rotation fix → docling + ocrmac
          → quality check → [VLM fallback] → heading levels
          → data/docling/<sha>.json
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
                    ▲
   query/ (native, :8088): HTTP API (native + OpenAI-compatible)
          sign in (session cookie) → library ownership/access check → admission queue
          question → [rewrite] → hybrid search → rerank (Apple GPU)
          → chat model via Ollama (+ optional search tool rounds)
          → streamed answer with [n] citations
          accounts, libraries and history all in data/chat.db
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
| Reranker | bge-reranker-v2-m3 (cross-encoder) | native macOS, Apple GPU (MPS) |
| Query service (chat + search API, history in SQLite) | FastAPI, `services/query` | native macOS, LaunchAgent |
| Chat models | you provide (Everyday / Hard presets) | Ollama |
| Accounts | email + password (argon2id via `pwdlib`), signed session cookies, ownership + sharing on libraries | `services/query`, same SQLite file |
| Chat UI | Svelte 5 + Vite + TS, `ui/`, built to a committed `ui/dist` | static files served by the query service at `/` |

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
- Node.js + [pnpm](https://pnpm.io/installation) — **only if you're changing
  the web UI.** `ui/dist` is committed to git, so a normal install and
  `./ragctl up` never need Node; `./ragctl ui build` does.

## Install

```bash
cd rag-stack
cp .env.example .env
```

Edit `.env`:
- `VLM_MODEL`, `EMBED_MODEL`, `CHAT_MODEL_EVERYDAY`, `CHAT_MODEL_HARD` —
  exact names as they appear in `ollama list`.
- `EMBED_TOKENIZER` — the HuggingFace tokenizer for your embedding model
  (`BAAI/bge-m3` for `bge-m3`), used to size chunks in tokens.
- Leave everything else at its default unless you know you want to change it.

```bash
./ragctl install
```

This checks prerequisites, installs Homebrew + Python
dependencies (all inside this repo), sets Ollama tuning via `launchctl`,
installs LaunchAgents for docling-serve (and, optionally, auto-ingestion),
and starts Qdrant.

Then run:

```bash
./ragctl health
```

to confirm every service is reachable. Then create the account you'll use —
nothing else works until one exists, and there is no self-registration:

```bash
./ragctl user add you@example.com --name "Your Name" --admin
```

It prompts for the password, at the terminal — piping into the prompt is
refused, because that echoes the password and stores whatever the pipe
delivered. To script it, `--password-stdin` reads one line deliberately. If
you already had conversations from before accounts existed, this first admin
adopts them.

Then open **http://127.0.0.1:8088** and sign in — that's the chat UI. The
admin console is at `/admin`, the API docs at `/docs`.

## Using it

**Batch ingestion** is the only path in. Drop files into `data/inbox/`
and run:

```bash
./ragctl ingest
./ragctl embed
```

`ingest` also accepts `--force-vlm`, `--file PATH` to process a single
file, `--reprocess` and `--fix-headings` (below) and `-v`/`--verbose`.

There's nothing to configure per file. Each PDF is first triaged page by
page (instant, nothing is rendered) into one of four labels:

- **digital**: visible text, not covered by a page-sized image.
- **scan**: a page-sized image with no text layer of its own, or with a few
  real lines stamped over it (a signature block, a page number).
- **stale-ocr**: a page-sized image carrying someone else's invisible OCR
  text layer underneath.
- **empty**: neither text nor a page-sized image.

Any `stale-ocr` page sends the whole file through OCR with `force_ocr` on,
because that old text layer has to be overwritten, not trusted — and
docling-serve takes one option set for the whole file, so there's no way to
force just that page and leave the rest alone. A document with a minority of
stale-ocr pages still pays for full OCR on every page; splitting the PDF and
merging the resulting DoclingDocuments would fix that, but isn't done here.

Otherwise, Docling converts the embedded text directly — no OCR, exact text,
~0.1 s/page — and any `scan` pages just have their own images OCR'd with
**ocrmac** (Apple Vision, `do_ocr=true, force_ocr=false`), leaving pages that
have real text alone. This is what saves a book with a scanned cover or an
inserted plate: OCR'ing the whole thing to catch one page would read a `£`
as `€` and a `Baa2` rating as `Baaz`, and rebuild table cells that the
embedded text already states exactly. If the embedded text fails the quality
check (e.g. fonts with no usable character map), the file drops to full OCR
instead.

The force-OCR route (any stale-ocr page, or embedded text that failed the
quality check) OCRs every page with ocrmac, replacing any old OCR layer,
~0.5–0.8 s/page.

Sideways or upside-down pages are marked rotated (lossless — the PDF's page
rotation flag, nothing is rasterized) before conversion either way, but the
angle is measured differently per page: a `digital` page states its angle
exactly in its text objects' matrices, for free. A `scan` or `stale-ocr`
page has no text of its own to measure, so tesseract's orientation
detection runs on a render of just that page. This means a 129-page book
with a single scanned cover renders and OSDs one page, not 129, while a
150-page scan with no text layer still gets OSD everywhere it needs it.
Without the rotation step, a table printed sideways comes out transposed.

Images are OCR'd with ocrmac too; other formats (docx, html…) are converted
directly. Control characters are stripped from the result: symbol fonts map
their bullets to code points like 0x07, which mean nothing to a reader or to
the embedding model.
Then comes a quality check and, only if that fails (or you pass
`--force-vlm`), Docling's VLM pipeline using your configured `VLM_MODEL`
as a last resort.

**Heading levels.** Docling gives every PDF heading level 1, so on its own
a chunk's heading path is just its nearest heading, and a heading with no
text of its own ("The parties to a typical bond issue", followed straight
away by "Investors") never reaches the index. For PDFs, ingest rebuilds the
levels from the best evidence available (`services/ingest/headings.py`):

1. **Bookmarks** (the PDF outline), when they match the headings.
2. **Numbering** ("7", "7.3", "7.3.1"), when most headings are numbered.
3. **Style**, measured from the rendered page: letter height, stroke width
   (bold) and slant (italic), plus ALL CAPS. This works on scans, which
   carry no font information. Styles rank "Chapter N" > very large > caps
   > bold > upright > italic.

It also cleans up what docling mislabels as headings: running page headers
and logos repeated in the page margin are dropped, figure/table captions
and sentence-length "headings" become text, and headings split over two
lines ("Chapter 4" + "Determination of a" + "Discount Rate") are joined. If
the styles don't form a few clear groups, the document keeps flat levels.
It costs a few seconds per book, no model calls; `-v` prints what it
decided, and `INGEST_HEADING_LEVELS=false` turns it off. Known limit:
headings docling doesn't detect at all can't be recovered.

Up to `DOCLING_LOC_NUM_WORKERS` files (default 2) are
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

`ingest --fix-headings` redoes only the heading levels for every processed
PDF, from its stored JSON: no OCR, seconds per document. Follow it with
`./ragctl embed`, which re-indexes the documents whose JSON changed.

**Embedding** (`./ragctl embed`) takes every processed document from the
manifest and:
1. Chunks its DoclingDocument JSON with Docling's `HybridChunker`, which
   splits along the document's structure and sizes chunks with
   `EMBED_TOKENIZER` (at most `CHUNK_MAX_TOKENS`, heading included).
2. Prepends each chunk's heading path, so a chunk under "Mitigation" in a
   risk chapter isn't indistinguishable from one in a currency chapter.
   The raw text, headings and page numbers are stored alongside.
   Short sibling sections under one parent (a run of one-paragraph
   subsections, say) are merged into shared chunks while they fit in
   `CHUNK_MAX_TOKENS`, so a list-like section comes back in a few chunks
   instead of many small ones (`CHUNK_MERGE_SIBLINGS`).
3. Embeds in batches of `EMBED_BATCH_SIZE`, one request per batch and one
   batch at a time — so Ollama's request queue never sees a burst — with
   retries on 503s and timeouts.
4. Writes to Qdrant with a dense vector plus a BM25 sparse vector that
   Qdrant computes itself.

It's idempotent: Qdrant is the record of what's embedded, and a document is
skipped when all its chunks are there under the current settings. Changing
`CHUNK_MAX_TOKENS`, `CHUNK_MERGE_SIBLINGS` or `EMBED_TOKENIZER` re-embeds everything on the next
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
| `RAG_RERANK_MIN_SCORE` | `0.1` | drop reranked chunks scoring below this (0–1); 0 keeps all |
| `RAG_TOP_K_RERANKER` | `10` | at most this many results (a whole section counts as one) |
| `RAG_EXPAND_SECTIONS` | `true` | return whole sections when several of their chunks match (below) |
| `RAG_SECTION_MAX_TOKENS` | `2000` | largest section returned whole |
| `RERANKER_MODEL` | `BAAI/bge-reranker-v2-m3` | HuggingFace cross-encoder |
| `RERANKER_DEVICE` | `auto` | `auto` (Apple GPU via MPS if available, else CUDA, else CPU), or force one |
| `RERANKER_BATCH_SIZE`, `RERANKER_MAX_LENGTH` | `16`, `1024` | pairs per forward pass; token limit per question+chunk pair |

**Reranking** (`services/query/rerank.py`): hybrid search scores the question
and each chunk separately, so a cross-encoder then reads each question/chunk
*pair* and scores how well the chunk answers it. It only re-scores the
`RAG_TOP_K` fused candidates, keeping those scoring at least
`RAG_RERANK_MIN_SCORE`. It runs on the
Mac's GPU through PyTorch's MPS backend in float16. Measured here: ~1.2 s
to rerank 20 chunks on the GPU against ~2.8 s on the CPU, with float16
giving the same order as float32. The model (~2.3 GB) is downloaded into
`data/hf-cache` by `ragctl install`, or on first use; loading it takes ~1 s,
so a one-off `ragctl search` takes a few seconds while a long-running
service pays that once. Scores are 0–1 and comparable across questions,
which is what makes `RAG_RERANK_MIN_SCORE` usable. Measured on this
library: off-topic questions ("how do I bake sourdough bread?") and the
tails of real ones score ≤ 0.08, while useful passages score ≥ 0.1 even for
vague questions, hence the default of 0.1. How many passages clear it
depends on the question: 1 for "what is a mini-perm loan?", 18 of 20 for
"what are the risks of a concession agreement?".

**Whole sections.** Search matches individual chunks, but a list-like
section ("The parties to a typical bond issue": investors, issuers, lead
manager, trustee… each a short subsection) is spread over several of
them. When two or more relevant chunks share a heading, retrieval fetches
every chunk under the deepest such heading and returns the section as one
result, in document order with its subheadings — if it fits in
`RAG_SECTION_MAX_TOKENS`, so a whole chapter is never pulled in. Chunks
already handed out in the conversation turn aren't repeated.

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

## Chatting: the query service

`ragctl up` starts the query service (`services/query/api.py`) on
`http://127.0.0.1:$QUERY_PORT` (default 8088). Interactive API docs are at
`/docs`. Every endpoint but `/api/health` and `/api/auth/login` needs a
signed-in caller (see "Accounts, groups and libraries"). Scripts and API
clients authenticate as a person with a personal token
(`Authorization: Bearer rsk_...`), or as a shared service account with
`QUERY_API_KEY`. It still binds to 127.0.0.1: the
accounts are real, but passwords over a LAN want TLS in front first.

**Each answer:**
1. **Rewrite** — one call to the chat model decides whether the message
   needs a search at all ("hello", "thanks", "make that shorter" or
   "translate that" are answered without one) and, for a follow-up, turns it
   into a standalone search query ("and for Level 1?" → "RSF factor for
   Level 1 assets").
2. **Automatic search** — hybrid search + reranking; every passage above
   `RAG_RERANK_MIN_SCORE` (whole sections where several of their chunks
   match), best first, until `CHAT_AUTO_SOURCE_TOKENS` is used. A narrow
   question gets one or two passages, a broad one many, and an off-topic
   one none. `CHAT_INITIAL_SOURCES` only caps the count. Numbered
   [1]..[n].
3. **Answer** — streamed from the preset's chat model, citing passages as
   [n]. If the preset has tools on, the model may call a `search_documents`
   tool for more passages (up to `CHAT_MAX_TOOL_ROUNDS`, within
   `CHAT_MAX_SOURCE_TOKENS`). Passages it already has are never returned
   again, and numbering continues, so earlier citations stay valid.
4. **Citations** are resolved to their passages; numbers that don't exist
   are reported as `invalid_citations`.

The instructions the model gets are in `services/query/prompts/` —
`system.md` always, `tools.md` when tools are on. Edit them freely; they're
read on every question.

**Presets.** `everyday` and `hard` map to `CHAT_MODEL_EVERYDAY` /
`CHAT_MODEL_HARD`, each with its own `CHAT_TOOLS_*` and `CHAT_THINK_*`. By
default Hard has thinking on: measured with qwen3.5:9b, the model only
calls the search tool reliably when thinking (4 of 4 runs, against 1 of 12
without), at the cost of 5–10 s of reasoning and an extra model pass —
a question needing one extra search took ~35 s to its first word. Everyday
answers in a few seconds but rarely searches beyond the automatic search.

**Native API** (`/api`, for a UI):

| Endpoint | |
|---|---|
| `POST /api/workspaces` · `GET /api/workspaces` | create (`{name, description?}`), list your own workspaces |
| `GET` · `PATCH` · `DELETE /api/workspaces/{id}` | open, rename / change `description` / change `library_ids` (every chat in the workspace, old ones included, searches these), delete (also deletes the chats in it) |
| `GET /api/workspaces/{id}/conversations` | chats in this workspace |
| `POST /api/conversations` · `GET /api/conversations` | create (`{preset?, title?, library_ids?, workspace_id?}`; `library_ids` only outside a workspace, 422 otherwise), list |
| `GET` · `PATCH` · `DELETE /api/conversations/{id}` | open (messages with their sources), rename / change preset / change `library_ids`, delete |
| `POST /api/conversations/{id}/messages` | `{content, preset?}` → server-sent events: `user`, `rewrite`, `searching`, `sources`, `thinking`, `token`, `done` (message id, cited numbers, all sources, timings), `title` (after the first answer), `error` |
| `POST /api/search` | `{query, limit?, fused?, library_ids?}` → ranked chunks |
| `GET /api/documents` | processed documents you can read (admins: all), your files waiting in the inbox (admins: everyone's), upload-ingest status |
| `POST /api/upload` | multipart `files` + `library_id` (required) → saved into `data/inbox`, then ingest + embed run in the background (or the inbox watcher does it, with `INGEST_AUTORUN=true`) |
| `GET /api/uploads` · `DELETE /api/uploads/{id}` | your uploads (each `waiting`, `processed` or `failed`); delete one, which purges the document from every library, the index and the disk -- unless another user uploaded the same file too, when only yours goes (`{purged: true/false}`). Deleted uploads still count against the quota |
| `GET /api/quota` | what's left of your upload allowance, and how busy the machine is |
| `GET /api/libraries` | libraries the caller can read, each with `role` (`owner`/`write`/`read`), `owner_name`, and `source` (`owner`/`admin`/`user`/`group` — why the caller can see it) |
| `POST /api/libraries` | any signed-in user creates a library (`{name, description?}`) and becomes its owner -- full access right away, no separate grant |
| `GET /api/libraries/{id}` | a library's own documents, for anyone who can read it |
| `PATCH` · `DELETE /api/libraries/{id}` | rename / change `description`, or delete -- the owner or an admin (`can_manage`) |
| `PUT` · `DELETE /api/libraries/{id}/documents/{sha}` | assign / unassign an already-indexed document -- needs write on the library, and adding also needs the caller to already be able to read that document elsewhere (otherwise a writer could pull any indexed document into their own library and read it from there) |
| `GET /api/libraries/{id}/access` | `{owner: {id, name} \| null, access: [{subject_type, subject_id, name, role}]}` -- the owner or an admin |
| `PUT /api/libraries/{id}/access` | `{subject_type, subject_id, role}` (`read`/`write`) -- upsert; owner or admin |
| `DELETE /api/libraries/{id}/access/{subject_type}/{subject_id}` | revoke -- owner or admin |
| `POST /api/auth/login` · `POST /api/auth/logout` | `{email, password}` → sets the session cookie; clear it |
| `GET /api/auth/me` | `{user, auth_mode}` — what a UI renders itself from; `user.is_admin` is the only global permission fact, everything else is per library |
| `POST /api/auth/password` | `{current_password, new_password}`, which signs your other sessions out |
| `GET` · `POST /api/tokens` · `DELETE /api/tokens/{id}` | your personal API tokens: list them (never the tokens themselves), make one (`{name, expires_in_days?}` → the token, shown only this once; needs a signed-in session, not another token), revoke one |
| `/api/admin/users` · `/api/admin/groups` | users, groups and memberships (admin-only: `users.is_admin`); what the admin console calls. Creates and manages members only -- any change to an admin account is a 403 (`ragctl user`) |
| `GET /api/admin/documents` | every indexed document on the machine, unfiltered, for assigning into libraries (admin-only) |
| `GET /api/health` | Ollama, the Qdrant collection, the database, reranker device, presets, current load |

History is stored in `data/chat.db` (SQLite): the UI sends only the new
message, and the service loads earlier turns, sending the model the most
recent ones within `CHAT_HISTORY_MAX_TOKENS`. Each answer stores a copy of
the passages it was given, so its citations still resolve after documents
are re-ingested. An answer keeps generating and is stored even if the
client disconnects; reopening the conversation shows it. Every row carries a
`user_id` and every query is scoped by it, so one person's conversations are
never visible to another.

**OpenAI-compatible API** (`/v1`): `GET /v1/models` lists `everyday` and
`hard`; `POST /v1/chat/completions` runs the same pipeline, streamed or
not, so any OpenAI client works:

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8088/v1", api_key="rsk_...")  # a personal token, or QUERY_API_KEY
client.chat.completions.create(model="everyday", messages=[{"role": "user", "content": "What is a mini-perm loan?"}])
```

The protocol is stateless (clients send the whole conversation) and has no
field for citations, so nothing is stored and the cited passages are
appended to the answer as a "Sources" list. A client's system prompt is
added to the service's, not substituted for it; client-supplied `tools`
are rejected.

## Operating

```bash
./ragctl status              # what's running and reachable
./ragctl health               # checks Ollama (+ model checks + an embedding
                               # smoke test), docling-serve, Qdrant and the query service
./ragctl logs [docling|ollama|ingest|query|qdrant]
./ragctl models check         # confirm .env model names exist in Ollama
./ragctl search "question"    # hybrid search + reranking (-v, --fused, --json)
./ragctl user list            # accounts and groups (see below)
./ragctl ui <install|dev|build|check|test>  # the web UI -- needs Node only for this
./ragctl up / down / restart
./ragctl reload               # re-apply .env changes to running services
./ragctl clean --yes          # wipe everything: ingest data, the index, chat history, accounts
```

`ragctl clean --yes` wipes `data/inbox`, `work`, `processed`, `failed`,
`markdown`, `docling` and `manifest.jsonl`, deletes the Qdrant collection,
and clears `data/chat.db` entirely — conversations, uploads, users, groups,
libraries and access. It's a full reset: after running it, sign-in needs a fresh
`ragctl user add you@example.com --name "Your Name" --admin`. Model caches (`data/hf-cache`,
`data/docling-artifacts`) are left alone since they're expensive to
redownload and aren't "your data."
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
- **A born-digital book was OCR'd anyway** — check `route` in
  `data/manifest.jsonl`: `ocrmac` on a document whose pages are digital
  means one page triaged as `stale-ocr` (an old invisible OCR layer under a
  scan), or the quality gate rejected the embedded text. `ingest -v` prints
  the page triage.
- **Everything returns 401** — nobody is signed in. `POST /api/auth/login`,
  or check that an account exists at all with `ragctl user list`.
- **A 403 on a library** — every signed-in account can chat; a 403 there
  means the caller can't read, write or manage that particular library.
  `ragctl user library share <library> <email> read` (or `write`) fixes it,
  or add them to a group that has it, or make them an admin.
- **Locked out after wrong passwords** — eight failures per email or address
  lock logins for five minutes; wait, or reset the password with
  `ragctl user passwd them@example.com`.
- **A forgotten password** — there is no email reset: an admin sets a new one
  with `ragctl user passwd`. That also signs that account out everywhere.
- **Everyone was signed out at once** — `data/session.key` was deleted or
  `QUERY_SESSION_SECRET` changed, which invalidates every cookie. Harmless;
  people sign in again.
- **Versions**: `compose.yaml` pins `qdrant/qdrant` to a specific tag,
  checked at the time this repo was written — that's the only image that
  currently matters for this stack. If a pull fails because the tag was
  retired, check Qdrant's release page and bump the tag in `compose.yaml`.

## Accounts, groups and libraries

Everyone who uses the service has an account: an email, a name, a password,
and (unless they're an admin) whatever libraries they own or were given
access to. There is no self-registration — an admin creates accounts, from
the command line or the admin console at `/admin`. Admin accounts are the
exception: making, demoting, renaming, re-passwording, disabling or deleting
one is only ever done at the terminal (`ragctl user ...`), never over HTTP —
the API and admin console refuse any change to an admin account, the
caller's own included, and signing in first through a proxy or with the
service key never makes anyone an admin. So a stolen admin session can
manage members, but can't mint admins or lock the real ones out. An admin's
own self-service (`POST /api/auth/password`, their tokens) works as it does
for anyone. There are no partial admins and no permission to ask questions
at all: every account can chat, and `users.is_admin` is the only global
privilege there is.

```
ragctl user add you@example.com --name "Your Name" --admin     # the first admin (prompts for a password)
ragctl user add alice@example.com --name "Alice"                # a colleague
ragctl user group add Analysts
ragctl user member add Analysts alice@example.com
ragctl user passwd alice@example.com         # reset a password (signs them out)
ragctl user rename alice@example.com --name "Alice Smith"
ragctl user admin alice@example.com [--revoke]   # promote or demote -- terminal only
ragctl user list                            # who exists, and their groups
```

**Ownership, not grants.** A library's owner has full control of it --
rename, delete, add or remove documents, and decide who else can reach it --
with no separate row saying so. Everyone else gets at most one row per
library, naming a role:

```
owner                -> whoever created it, or an admin (every library, always)
a user's own row      -> read or write, directly
any of its groups'    -> read or write, via membership
nothing                -> no access
```

There's no deny and nothing to override: if a role reaches a user two ways
(their own row and a group's, say), the higher one wins. `library.write`
implies `library.read`; `owner` implies both plus managing the library
itself.

**Signing in.** `POST /api/auth/login` sets a signed, httpOnly session cookie;
`GET /api/auth/me` returns the caller (`user`, including `is_admin`) and the
auth mode -- what a library-level permission looks like is per library
(`GET /api/libraries`' `role`), not a map here. Passwords are hashed with
argon2id by `pwdlib`, and the cookie is signed by Starlette's session
middleware — no scheme is invented here. There is no session table: the
cookie carries the user id and a `session_epoch`, both checked against the
row on every request, so disabling a user or changing a password takes effect
at once.

**Personal API tokens** are how a script or an OpenAI-compatible client acts
as a particular person, with that person's libraries, queue share and upload
quota. Make one with `POST /api/tokens` while signed in, or
`ragctl user token add alice@example.com --name "laptop script"`. It's shown
once; only a sha256 of it is stored. A token has no permissions of its own:
every request re-reads its user, so disabling, demoting or deleting them
applies to their tokens at once. Changing a password doesn't revoke tokens --
revoke them explicitly (`DELETE /api/tokens/{id}`, which works with the token
itself, or `ragctl user token revoke`). A token can't make more tokens, so a
leaked one can't mint replacements. `QUERY_API_KEY` still works too, as one
shared service account with its own row rather than a way past anything.

**SSO later.** Identity arrives through one seam, so `QUERY_AUTH_MODE=proxy`
switches the service to trusting an authenticating reverse proxy's header
(`tailscale serve`, `oauth2-proxy`) instead of passwords, without touching
authorization or storage. That is only sound when the service can be reached
*only* through that proxy — otherwise anyone on the network can name themselves
in a header — so the header is ignored entirely in password mode.

**Upgrading an existing install.** `chat.db` migrates itself. Rows written
before accounts existed are adopted by the first admin you create, so old
conversations stay where you can see them.

## Libraries

A library is a named collection of documents, owned by whoever created it. A
conversation chooses which libraries to search by attaching one or more of
the ones it can read.

```
ragctl user library add "Project finance" --owner alice@example.com   # name, optionally --description
ragctl user library documents                     # every indexed document and its sha256
ragctl user library add-doc "Project finance" infra-finance.pdf   # filename match, or a full sha256
ragctl user library remove-doc "Project finance" infra-finance.pdf
ragctl user library list
ragctl user library share "Project finance" alice@example.com read   # or write; a user or a group
ragctl user library unshare "Project finance" alice@example.com
ragctl user library delete "Project finance"      # its access list and chat attachments go too
ragctl user token add alice@example.com --name "laptop script" --expires-days 90   # prints it once
ragctl user token list alice@example.com
ragctl user token revoke alice@example.com <token id>
```

`GET /api/libraries` lists the libraries the caller can read, each with its
`role` and why they can see it (`source`), for a UI to offer when starting or
editing a chat (`POST/PATCH /api/conversations` take `library_ids`). Any
signed-in user can start their own library with `POST /api/libraries` -- they
become its owner, full access right away, no separate grant step. Every
account also starts with one: a library called "Default Library", attached to
a workspace called "<name>'s Workspace", both created with the account. The owner
(or an admin) manages sharing through `GET/PUT /api/libraries/{id}/access`
and `DELETE /api/libraries/{id}/access/{subject_type}/{subject_id}`.

**No re-embedding.** The document → library mapping lives in SQLite; Qdrant
is filtered on `doc_sha`, which every point already carries. Assigning a
document to a library at `/api/upload` time (its `library_id` field, required,
needing write access -- a document in no library would be readable by admins
only, not even by whoever uploaded it) works before ingest has even run: the sha is computed from
the uploaded bytes right there, identically to how ingest will hash the same
file, so the mapping is already in place once embedding makes the document
searchable. Assigning one directly (`PUT /api/libraries/{id}/documents/{sha}`)
needs write on the target library *and* that the caller can already read that
document somewhere -- otherwise a writer could pull any indexed document on
the machine into their own library and read it from there.

**What a search actually sees**, resolved fresh on every question rather than
trusted from when a conversation attached its libraries — losing read access
takes effect on the next question, not the next attach:

- An **admin** searches everything, always. A document not in any library is
  reachable only by an admin — assigning it is what makes it reachable by
  anyone else, so a fresh install with unassigned documents isn't silently
  wide open.
- A **conversation with libraries attached** searches the intersection of
  those and the ones the asker can currently read.
- A **conversation with nothing attached** (the default for a new chat, and
  for `/api/search` and the OpenAI-compatible endpoint, which have no
  conversation to attach to) searches every library the asker can read, so a
  fresh chat isn't silently mute until someone picks something.
- Someone who can read **no** library gets no results, not an unfiltered
  search — an empty selection and no selection are different states.

**Deleting an upload** (`DELETE /api/uploads/{id}`) takes the document back
completely: out of every library — including any a reader copied it into
from one it was shared through — out of Qdrant, and off the disk (the
original, its markdown and docling JSON). The manifest gets a `deleted` row,
which ingest, embed and the query service all read as cancelling the
`processed` rows before it, so the same file can be uploaded again later.
The one exception is a document another user uploaded too, independently:
only your upload goes then, and the document leaves your own libraries but
stays in theirs. Old chat answers that cited it keep their copy of the
passages, as they do across a re-ingest.

**Deleting a user** (`ragctl user delete`, or the admin console for a member)
removes everything about them — their conversations, workspaces, tokens,
group memberships and the access they'd been given — except their libraries
and documents. Those become unowned, keep their sharing, and are attached to
the **Admin Workspace**: a workspace every admin sees and nobody else does,
created with the database and impossible to rename or delete. Each admin's
chats inside it are still their own. The deleted user's upload records pass
to the admins, so their documents still count as held and can't be purged by
someone else deleting an identical upload.

The `uploads` table gained a `doc_sha` column for this; as with any schema
change here (see `db.py`), drop `data/chat.db` in a dev install to recreate it.

## Several sessions at once

The backend assumes more than one session at a time:

- **Turns queue.** The bottleneck is the GPU, not the web server: the reranker
  is one model behind a lock and Ollama holds one set of weights, so answering
  ten questions at once only makes all ten slow. `QUERY_MAX_CONCURRENT` turns
  run, `QUERY_MAX_PER_USER` of them per user, and the rest wait in line and
  receive a `queued` event with their position. Past `QUERY_MAX_QUEUED` waiting
  turns, or `QUERY_QUEUE_WAIT` seconds of waiting, the turn is refused rather
  than left hanging. `/api/search` and the OpenAI-compatible endpoint queue in
  the same line — they reach the same GPU.
- **`QUERY_MAX_PER_USER` is per account**, so one person can't fill the
  machine while colleagues wait.
- **The line is fair between users**, not first come, first served: a freed
  slot goes to whoever has the least running or already waiting, so ten
  questions sent at once by one person interleave with everyone else's
  rather than going first. One user can hold at most
  `QUERY_MAX_QUEUED_PER_USER` places in line, so nobody can fill the queue
  and have everyone else turned away.
- **One turn per conversation.** Two sends to the same conversation are
  serialised, and the question is stored once its turn is admitted, not when
  the request arrives, so two tabs can't read each other's half-written
  history.
- **Ingest quotas.** Converting a document is the most expensive thing here,
  so `/api/upload` is capped per user over a rolling window
  (`INGEST_MAX_FILE_MB`, `INGEST_MAX_FILES_PER_WINDOW`,
  `INGEST_MAX_MB_PER_WINDOW`, `INGEST_QUOTA_WINDOW_HOURS`). A file that
  doesn't fit is rejected by name with the reason and the rest still go
  through; `GET /api/quota` says what's left. **Admins have no quota at all**
  — not the window and not the per-file cap — since filling the library is
  their job; their uploads are still recorded, so who added what stays known.
  Files copied into `data/inbox` by hand aren't counted either: the quota is
  on the API, not on you.
- **Uploads don't collide.** The inbox name is claimed with a hard link, which
  fails rather than overwriting, so two sessions uploading `report.pdf` at the
  same moment get `report.pdf` and `report (1).pdf`.
- **SQLite is fine for this.** WAL is on, every row already carries a
  `user_id`, and `chat.db` migrates itself forward (`PRAGMA user_version`).
  Postgres becomes necessary when a second process writes — in practice, the
  day you run uvicorn with more than one worker.

What's deliberately *not* here yet: TLS. Bind to the LAN only behind a TLS
terminator, and set `QUERY_HTTPS_ONLY=true` so the session cookie never
crosses plain http.

## Tests

Unit tests live next to each service (`services/<name>/tests/`) and run in
that service's own environment, separately from `ragctl`:

```bash
./run-tests.sh                  # every Python service
./run-tests.sh query            # one service
./run-tests.sh query -k chat    # extra arguments go to pytest
./run-tests.sh ui               # the web UI's Vitest suite (needs Node)
cd services/ingest && uv run pytest   # or directly, per service
```

The UI's tests (`ui/src/tests/`) cover the SSE frame parser (chunked at
several byte sizes, to catch boundary bugs), the streaming-markdown fence
tracker, citation parsing and linkification, conversation-to-turn pairing,
and the permissions-map shapes -- all pure TypeScript, no browser needed.
`./ragctl ui check` runs `svelte-check`.

Most need no running services, network, `.env` or `data/`, and take a few
seconds. Ollama and Qdrant are faked (`httpx.MockTransport`), and the
heading tests generate PDFs on the fly in the standard fonts (Helvetica-Bold,
Times-Italic…), so style measurement runs on real renders. Covered: heading
levels and cleanup, page triage, the ingest pipeline around docling, sibling
chunk merging, embed retries, section expansion and thresholds in
retrieval, the chat pipeline (rewrite, source budget, tool rounds,
citations), chat history storage, the OpenAI adapter's helpers, the
knowledge-base listing tool, the admission queue and ingest quotas, and
accounts — library ownership and sharing, the highest role winning across a
user's own access and its groups', session lifecycle, login throttling, and
that a proxy identity header is ignored unless the service is in proxy mode.

**The OCR route end to end** is covered by the tests marked `integration` in
`services/ingest/tests/test_ocr_integration.py`. They convert one real
scanned page — page 5 of the Basel III case study, turned 90°, in
`tests/fixtures/` — through tesseract's orientation detection, ocrmac and
docling, and check the rotation was applied, the text is readable, the RSF
table keeps its 8×2 shape with each factor's assets in its own row (a
sideways page merges the 15% and 50% rows, which is how "Level 2B" ends up
with the wrong factor), the caption and section heading survive, and the
running header and footer stay out of the text. They need docling-serve
(`./ragctl up`), take a few seconds, and skip themselves when it isn't
running. Run only them with `./run-tests.sh ingest -m integration`, or leave
them out with `-m "not integration"`.

## Future potential updates

- Heading levels use a style ranking of their own rather than
  docling-hierarchical-pdf: its size-based clustering scrambled scanned
  books (pixel height is a noisy stand-in for font size, and it has no
  notion of ALL CAPS), and it needs docling's in-process result, which
  docling-serve doesn't return.

**Next, roughly in order:**

- **TLS and a LAN binding**, so a team can reach it: Caddy or
  `tailscale serve` in front, `QUERY_HTTPS_ONLY=true`, and optionally
  `QUERY_AUTH_MODE=proxy` to hand sign-in to an identity provider instead of
  storing passwords at all.
- **Tuning the concurrency limits** on the real hardware —
  `QUERY_MAX_CONCURRENT` and `QUERY_MAX_PER_USER` were set conservatively on
  a laptop-class machine.
- **Postgres**, on the day the query service runs more than one worker
  process. Until then SQLite in WAL mode is the right answer, and every query
  is already scoped by `user_id`.

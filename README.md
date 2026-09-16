# rag-stack

A self-contained, local RAG (retrieval-augmented generation) system for macOS
on Apple Silicon. One repo, one command (`./ragctl`), configured as if it
were running on a bigger machine — it's meant to be installed as-is on a
more powerful Mac later, not migrated.

**You provide the models.** Everything else — chat UI, vector DB, document
parsing, OCR, embeddings, reranking — is wired up and pinned to specific
versions.

## Architecture

```
            ┌──────────── Docker Desktop ────────────────────────┐
Browser ──► │ Open WebUI :3000 ──► Qdrant :6333 (vector DB)       │
            │   └ reranker bge-reranker-v2-m3 (CPU, in-container) │
            └───────┬──────────────────────┬─────────────────────┘
          host.docker.internal:11434  host.docker.internal:5001
                    ▼                      ▼
   Ollama.app (native, MLX)     docling-serve (native, on-device GPU)
   chat models / embeddings ◄────── VLM OCR fallback pipeline
                    ▲
   ingest/ (native): data/inbox → OCRmyPDF → docling → quality
          check → [VLM fallback if needed] → Open WebUI knowledge base
```

| Layer | Choice | Runs where |
|---|---|---|
| Model server | Ollama | native macOS, MLX/Metal |
| Chat models | you provide (everyday + hard-questions tiers) | Ollama |
| Embeddings | you provide (e.g. bge-m3) | Ollama |
| VLM OCR fallback | you provide (e.g. Qwen-VL) | Ollama |
| OCR preprocessing | OCRmyPDF | native macOS batch job |
| Document parsing / VLM pipeline | Docling (docling-serve) | native macOS |
| Vector DB | Qdrant | Docker |
| Reranker | bge-reranker-v2-m3 | inside the Open WebUI container, CPU |
| Chat UI / RAG orchestration | Open WebUI | Docker |

Ollama and Docling run **natively** (not in Docker) so they can use Apple's
MLX/Metal acceleration. Qdrant and Open WebUI run in Docker for easy,
reproducible packaging. Everything the stack installs lives inside this
repo, in Docker volumes prefixed `ragstack_`, or in LaunchAgents labelled
`com.ragstack.*` — nothing is scattered across the system, so it's easy to
audit or remove.

## Requirements

- macOS on Apple Silicon (arm64).
- [Homebrew](https://brew.sh).
- [Docker Desktop](https://www.docker.com/products/docker-desktop/), running,
  with at least 8GB of RAM allocated (Settings → Resources). On a bigger
  machine, raise this — 16GB is a good target on a Mac Studio.
- [Ollama.app](https://ollama.com/download), running.
- Your chat, embedding and VLM models already pulled with `ollama pull`.

## Disk space — free this BEFORE running `./ragctl install`

**`ragctl` never deletes, prunes or frees anything for you.** It only checks
free space and refuses to install if there isn't enough. You free the space
yourself, however you prefer, then run the installer.

| Item | Approx. size |
|---|---|
| Homebrew deps (tesseract, ghostscript, qpdf, pngquant, leptonica…) | 0.6 GB (+0.7 GB with `tesseract-lang` for non-English OCR) |
| Open WebUI image | ~6 GB |
| Qdrant image | ~0.3 GB |
| Reranker `bge-reranker-v2-m3` (downloaded by Open WebUI on first use) | ~2.3 GB |
| docling-serve Python env (torch, transformers, opencv, Gradio UI) + its own Python 3.12 | ~3.5 GB |
| Docling layout/table/formula/picture models | ~1.5 GB |
| Ingest tool's Python env | ~0.2 GB |
| uv's download cache (not cleared automatically) | ~2 GB |
| **Stack install subtotal** | **~16 GB** |
| Working room: uploads, Qdrant index, Open WebUI DB, ingest work dir | reserve ~5 GB |
| macOS headroom (swap, snapshots, updates) — keep clear at all times | ~20 GB |
| **Total free space needed before install** | **≈ 41 GB, plus the size of any Ollama models you still need to pull** |

`./ragctl install` checks this automatically (`MIN_FREE_GB` in `.env`,
default 41) and stops with a clear message if you're short.

## Install

```bash
cd rag-stack
cp .env.example .env
```

Edit `.env`:
- `CHAT_MODEL_EVERYDAY`, `CHAT_MODEL_HARD`, `EMBED_MODEL`, `VLM_MODEL` —
  exact names as they appear in `ollama list`.
- Leave everything else at its default unless you know you want to change it.

```bash
./ragctl install
```

This checks prerequisites and disk space, installs Homebrew + Python
dependencies (all inside this repo), sets Ollama tuning via `launchctl`,
installs LaunchAgents for docling-serve (and, optionally, auto-ingestion),
and starts Qdrant + Open WebUI.

Then, in the browser:

1. Open **http://127.0.0.1:3000** and create the admin account (first
   account created becomes admin).
2. Admin Panel → your user (top right) → Settings → Account → **API keys**
   → create a key. Put it in `.env` as `OWUI_API_KEY`.
3. Run:
   ```bash
   ./ragctl webui-setup
   ```
   This creates the shared knowledge base and two chat presets, **Everyday**
   and **Hard questions**, each pointed at your configured models with the
   knowledge base attached.
4. In `.env`, set `ENABLE_SIGNUP=false` so nobody else can self-register,
   then run `./ragctl reload`.
5. Run:
   ```bash
   ./ragctl health
   ```
   to confirm every service is reachable and the container can reach the
   host services.

## Using it

**Chat uploads** — the simplest path. In Open WebUI, attach a file directly
to a chat, or add it to the knowledge base under Workspace → Knowledge.
Open WebUI sends it to docling-serve for parsing automatically.

**Batch ingestion** — for OCR-heavy or bulk documents. Drop files into
`data/inbox/` and run:

```bash
./ragctl ingest
```

Each file goes through: OCRmyPDF (adds a text layer to scanned pages only;
untouched if the PDF is already digital) → Docling's standard parser →
a quality check → and, only if that check fails (or you pass
`--force-vlm`), Docling's VLM pipeline using your configured `VLM_MODEL` as
an OCR fallback. Results are pushed into the same knowledge base the chat
UI uses. Originals move to `data/processed/` or `data/failed/`, and every
run is logged to `data/manifest.jsonl` (file hash, route taken, character
count) so re-running `ingest` never reprocesses a file twice.

Set `INGEST_AUTORUN=true` in `.env` and run `./ragctl reload` to have this
happen automatically whenever a file lands in `data/inbox/`.

## Operating

```bash
./ragctl status              # what's running and reachable
./ragctl health              # deeper end-to-end connectivity check
./ragctl logs [docling|ollama|ingest|qdrant|webui]
./ragctl models check        # confirm .env model names exist in Ollama
./ragctl up / down / restart
./ragctl reload              # re-apply .env changes to running services
```

**A gotcha worth knowing:** most of Open WebUI's RAG settings (embedding
model, reranker, chunk size, hybrid search…) are "PersistentConfig" — they
only take effect on the very first start against a fresh database. After
that, they live in Open WebUI's own settings and env changes are ignored.
To change them later, use **Admin Panel → Settings → Documents** in the
browser, or wipe the `ragstack_openwebui` volume and reinstall.

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
other machines on the network should reach Open WebUI.

## Uninstall

```bash
./ragctl uninstall             # removes containers, volumes, LaunchAgents, local Python envs
./ragctl uninstall --purge-data  # also deletes data/ (inbox, processed docs, logs, manifest)
```

Ollama.app itself is left installed and running — this stack only tunes its
environment variables via `launchctl setenv`, which clears on the next
reboot (or run `launchctl unsetenv <VAR>` to clear it immediately).

## Troubleshooting

- **`./ragctl health` shows "open-webui cannot reach host Ollama/docling-serve"**
  — Docker Desktop's `host.docker.internal` isn't resolving. Restart Docker
  Desktop; confirm `compose.yaml`'s `extra_hosts: host-gateway` entry is
  present (it is, by default).
- **docling-serve won't start** — check `./ragctl logs docling`.
  `UVICORN_WORKERS` must stay at `1` (set by the LaunchAgent) — more than
  one worker breaks its task tracking.
- **A model "not found" in `ollama list`** — run `./ragctl models check`, fix
  the name in `.env`, then `./ragctl reload`.
- **Ingest quality gate keeps triggering the VLM fallback unnecessarily** —
  raise `MIN_CHARS_PER_PAGE` in `.env`, or check the OCR language
  (`OCR_LANG`) matches your documents.
- **Versions**: `compose.yaml` pins `qdrant/qdrant` and
  `ghcr.io/open-webui/open-webui` to specific tags, checked at the time this
  repo was written. If a pull fails because a tag was retired, check the
  projects' release pages and bump the tag in `compose.yaml`.

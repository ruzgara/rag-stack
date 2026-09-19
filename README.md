# rag-stack

A self-contained, local document-ingestion pipeline for macOS on Apple
Silicon, meant to grow into a full local RAG (retrieval-augmented
generation) system. One repo, one command (`./ragctl`), configured as if it
were running on a bigger machine — it's meant to be installed as-is on a
more powerful Mac later, not migrated.

## Status

**v2 is mid-migration.** Open WebUI has been removed — it previously
provided chunking, embedding at ingest time, Qdrant writes, hybrid search,
reranking, retrieval and the chat UI itself. All of that is gone with it.
Today this repo is **ingestion only**: documents go in, OCR and parsing
happen, and clean markdown comes out on disk. There is currently **no
chat interface, no query-time retrieval, no reranking, and nothing writes
to Qdrant**. The retrieval/chat layer is being rebuilt with custom
components that don't exist yet — not "coming soon" with a defined plan,
just genuinely not there.

**You provide the models.** What's currently wired up and pinned to
specific versions: OCR, document parsing, and the Ollama-backed VLM OCR
fallback.

## Architecture

```
   ingest/ (native): data/inbox → OCRmyPDF → docling-serve → quality
          check → [VLM fallback if needed] → data/markdown/*.md
                    │                              ▲
                    ▼                              │
   docling-serve (native, on-device GPU) ───────────┘
                    │
                    ▼
   Ollama.app (native, MLX) — VLM OCR fallback model

            ┌──────────── Docker Desktop ────────────────────┐
            │ Qdrant :6333 (vector DB) — unused for now,      │
            │ reserved for the future retrieval layer         │
            └──────────────────────────────────────────────────┘
```

| Layer | Choice | Runs where |
|---|---|---|
| Model server | Ollama | native macOS, MLX/Metal |
| VLM OCR fallback | you provide (e.g. Qwen-VL) | Ollama |
| OCR preprocessing | OCRmyPDF | native macOS batch job |
| Document parsing / VLM pipeline | Docling (docling-serve) | native macOS |
| Vector DB | Qdrant | Docker (present, currently unused) |
| Chat UI / retrieval / reranking | not yet built | — |

Ollama and Docling run **natively** (not in Docker) so they can use Apple's
MLX/Metal acceleration. Qdrant runs in Docker for easy, reproducible
packaging (`compose.yaml` still lists the old `open-webui` service and
`ragstack_openwebui` volume too — that cleanup hasn't been done yet, so
don't be surprised if `ragctl up` still starts an Open WebUI container for
now). Everything the stack installs lives inside this repo, in Docker
volumes prefixed `ragstack_`, or in LaunchAgents labelled `com.ragstack.*`
— nothing is scattered across the system, so it's easy to audit or remove.

## Requirements

- macOS on Apple Silicon (arm64).
- [Homebrew](https://brew.sh).
- [Docker Desktop](https://www.docker.com/products/docker-desktop/), running,
  with at least 8GB of RAM allocated (Settings → Resources). On a bigger
  machine, raise this — 16GB is a good target on a Mac Studio.
- [Ollama.app](https://ollama.com/download), running.
- Your VLM model already pulled with `ollama pull` (used as the OCR
  fallback — see below).

## Install

```bash
cd rag-stack
cp .env.example .env
```

Edit `.env`:
- `VLM_MODEL` — exact name as it appears in `ollama list`. This is the only
  model the current ingestion pipeline actually calls, for the VLM OCR
  fallback. `CHAT_MODEL_EVERYDAY`, `CHAT_MODEL_HARD` and `EMBED_MODEL` are
  still in `.env.example` but nothing consumes them yet — they're held for
  the future chat/retrieval components.
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

**Batch ingestion** is currently the only path in. Drop files into
`data/inbox/` and run:

```bash
./ragctl ingest
```

(equivalent to `uv run services/ingest/ingest.py`, which also accepts
`--force-vlm`, `--file PATH` to process a single file, and `-v`/`--verbose`.)

Each file goes through: OCRmyPDF (adds a text layer to scanned pages only;
untouched if the PDF already has text — set `FORCE_OCR=true` in `.env` to
re-OCR every page unconditionally instead, which is useful when a PDF
already has a text layer but it's garbage from a bad prior OCR pass) →
Docling's standard parser → a quality check → and, only if that check
fails (or you pass `--force-vlm`), Docling's VLM pipeline using your
configured `VLM_MODEL` as an OCR fallback. The result is written to
`data/markdown/<stem>.md`. Originals move to `data/processed/` or
`data/failed/`, and every run is logged to `data/manifest.jsonl` (file
hash, route taken, character count, markdown path) so re-running `ingest`
never reprocesses a file twice.

Set `INGEST_AUTORUN=true` in `.env` and run `./ragctl reload` to have this
happen automatically whenever a file lands in `data/inbox/`.

Nothing currently reads `data/markdown/` back out — there's no chat or
search layer yet. See [Status](#status).

## Operating

```bash
./ragctl status              # what's running and reachable
./ragctl health               # checks Ollama (+ model checks + an embedding
                               # smoke test), docling-serve, and Qdrant
./ragctl logs [docling|ollama|ingest|qdrant]
./ragctl models check         # confirm .env model names exist in Ollama
./ragctl up / down / restart
./ragctl reload               # re-apply .env changes to running services
./ragctl clean --yes          # wipe inbox/work/processed/failed + manifest
./ragctl preflight            # check prerequisites and disk space
```

`ragctl clean --yes` wipes `data/inbox`, `work`, `processed`, `failed` and
`manifest.jsonl` — it resets the ingestion pipeline to a clean slate.
It does **not** currently clear `data/markdown/` (a known gap — the
converted output is left behind even though the manifest that tracked it
is wiped). Model caches (`data/hf-cache`, `data/docling-artifacts`) are
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
  raise `MIN_CHARS_PER_PAGE` in `.env`, or check the OCR language
  (`OCR_LANG`) matches your documents.
- **A scanned PDF's text looks plausible but is subtly wrong** (misread
  characters, swapped words) — this can slip past the quality gate
  entirely, since it only checks for *obviously* broken text (too short,
  full of junk characters), not wrong-but-readable text. If the PDF
  already carries a bad OCR layer from before it reached you, set
  `FORCE_OCR=true` in `.env` so OCRmyPDF re-OCRs every page instead of
  skipping pages that already "have text."
- **Versions**: `compose.yaml` pins `qdrant/qdrant` to a specific tag,
  checked at the time this repo was written — that's the only image that
  currently matters for this stack. If a pull fails because the tag was
  retired, check Qdrant's release page and bump the tag in `compose.yaml`.

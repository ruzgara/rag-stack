"""
The rag-stack query service: HTTP API over retrieval, chat and storage.

Native API (for the rag-stack UI) under /api, OpenAI-compatible API under /v1
(see openai_compat.py). Run by the com.ragstack.query LaunchAgent:

  uvicorn api:app --host 127.0.0.1 --port $QUERY_PORT

It always binds to 127.0.0.1: there are no user accounts yet, so anyone who
can reach it can read every conversation. If QUERY_API_KEY is set, every
endpoint except /api/health requires `Authorization: Bearer <key>`.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Iterator

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from chat import ChatConfig, ChatError, ChatService
from db import ChatDB, NotFound
from retrieval import REPO_ROOT, Retriever, SearchConfig, load_env

DATA_DIR = REPO_ROOT / "data"
INBOX = DATA_DIR / "inbox"
MANIFEST = DATA_DIR / "manifest.jsonl"
LOG_DIR = DATA_DIR / "logs"

# Same list as services/ingest/ingest.py; files with other suffixes would sit
# in the inbox unprocessed.
SUPPORTED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp",
                      ".docx", ".pptx", ".xlsx", ".html", ".htm", ".md", ".txt", ".asciidoc"}


class IngestRunner:
    """Runs `ragctl ingest` then `ragctl embed` in the background after an
    upload, one run at a time. Uploads arriving during a run trigger one
    more run afterwards. With INGEST_AUTORUN=true the inbox LaunchAgent
    already does this, so the runner stays idle."""

    def __init__(self, autorun: bool):
        self.autorun = autorun
        self._lock = threading.Lock()
        self._running = False
        self._pending = False
        self.last_run: dict | None = None

    def trigger(self) -> str:
        if self.autorun:
            return "handled by the inbox watcher (INGEST_AUTORUN=true)"
        with self._lock:
            if self._running:
                self._pending = True
                return "queued after the current run"
            self._running = True
        threading.Thread(target=self._loop, daemon=True).start()
        return "started"

    def _loop(self) -> None:
        while True:
            t0 = time.time()
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            code = 0
            with (LOG_DIR / "ingest-api.log").open("a") as log:
                log.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} upload-triggered run ====\n")
                log.flush()
                # Each service has its own uv environment; don't leak this one's.
                env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
                for step in ("ingest", "embed"):
                    code = subprocess.run([str(REPO_ROOT / "ragctl"), step], stdout=log, stderr=subprocess.STDOUT,
                                          env=env).returncode
                    if code != 0:
                        break
            self.last_run = {"started_at": t0, "finished_at": time.time(), "exit_code": code}
            with self._lock:
                if not self._pending:
                    self._running = False
                    return
                self._pending = False

    def status(self) -> dict:
        return {"autorun": self.autorun, "running": self._running, "pending": self._pending, "last_run": self.last_run}


class State:
    retriever: Retriever
    chat: ChatService
    db: ChatDB
    ingest: IngestRunner
    api_key: str


state = State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    env = load_env()
    state.retriever = Retriever(SearchConfig.from_env(env))
    state.chat = ChatService(state.retriever, ChatConfig.from_env(env))
    state.db = ChatDB(DATA_DIR / "chat.db")
    state.ingest = IngestRunner(autorun=(env.get("INGEST_AUTORUN") or "false").strip().lower() == "true")
    state.api_key = (env.get("QUERY_API_KEY") or "").strip()
    if state.retriever.cfg.rerank:
        # Load the reranker onto the GPU now rather than on the first question.
        threading.Thread(target=lambda: state.retriever.reranker, daemon=True).start()
    yield
    state.retriever.close()


app = FastAPI(title="rag-stack query service", lifespan=lifespan)


def require_key(request: Request) -> None:
    if state.api_key and request.headers.get("authorization", "") != f"Bearer {state.api_key}":
        raise HTTPException(401, "missing or invalid API key", headers={"WWW-Authenticate": "Bearer"})


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ------------------------------------------------------------ conversations --

class NewConversation(BaseModel):
    title: str | None = None
    preset: str | None = None


class ConversationUpdate(BaseModel):
    title: str | None = None
    preset: str | None = None


class NewMessage(BaseModel):
    content: str = Field(min_length=1)
    preset: str | None = None  # defaults to the conversation's preset


def _check_preset(name: str | None) -> str:
    try:
        return state.chat.cfg.preset(name).name
    except ChatError as exc:
        raise HTTPException(422, str(exc)) from None


def _get_conversation(conv_id: str, with_messages: bool = True) -> dict:
    try:
        return state.db.get_conversation(conv_id, with_messages=with_messages)
    except NotFound:
        raise HTTPException(404, "conversation not found") from None


@app.post("/api/conversations", dependencies=[Depends(require_key)], status_code=201)
def create_conversation(body: NewConversation) -> dict:
    return state.db.create_conversation(_check_preset(body.preset), body.title)


@app.get("/api/conversations", dependencies=[Depends(require_key)])
def list_conversations() -> list[dict]:
    return state.db.list_conversations()


@app.get("/api/conversations/{conv_id}", dependencies=[Depends(require_key)])
def get_conversation(conv_id: str) -> dict:
    return _get_conversation(conv_id)


@app.patch("/api/conversations/{conv_id}", dependencies=[Depends(require_key)])
def update_conversation(conv_id: str, body: ConversationUpdate) -> dict:
    preset = _check_preset(body.preset) if body.preset is not None else None
    try:
        return state.db.update_conversation(conv_id, title=body.title, preset=preset)
    except NotFound:
        raise HTTPException(404, "conversation not found") from None


@app.delete("/api/conversations/{conv_id}", dependencies=[Depends(require_key)], status_code=204)
def delete_conversation(conv_id: str) -> None:
    try:
        state.db.delete_conversation(conv_id)
    except NotFound:
        raise HTTPException(404, "conversation not found") from None


@app.post("/api/conversations/{conv_id}/messages", dependencies=[Depends(require_key)])
def send_message(conv_id: str, body: NewMessage) -> StreamingResponse:
    """Streams server-sent events:
      user      the stored question {id}
      rewrite   {query, search}: whether to search at all, and the (standalone) search query
      searching {query, found_by}: a search is running (auto or tool)
      listing   {document}: the model asked what the knowledge base holds
      sources   {sources: [...]}: passages added, numbered for citation
      thinking  {text}: reasoning tokens (presets with thinking on)
      token     {text}: answer tokens
      done      {message_id, answer, cited, invalid_citations, sources, ...}
      title     {title}: after the first answer, a generated conversation title
      error     {error}: the turn failed; partial output is stored with status "error"

    The turn runs in its own thread and is stored when it finishes, even if
    the client disconnects halfway: reopening the conversation shows it.
    """
    conv = _get_conversation(conv_id, with_messages=False)
    preset = _check_preset(body.preset or conv["preset"])
    history = state.db.history(conv_id)
    first_turn = not history
    user_msg = state.db.add_user_message(conv_id, body.content)

    def stream() -> Iterator[str]:
        yield sse("user", {"id": user_msg["id"]})
        tokens: list[str] = []
        sources: list[dict] = []
        queries: list[str] = []
        finished = False
        try:
            for ev in state.chat.run_turn(body.content, history, preset):
                kind = ev["type"]
                if kind == "done":
                    r = ev["result"]
                    saved = state.db.add_assistant_message(
                        conv_id, content=r.answer, model=r.model, preset=r.preset, search_queries=r.search_queries,
                        sources=[asdict(s) for s in r.sources], cited=set(r.cited), stats=r.stats)
                    finished = True
                    yield sse("done", {"message_id": saved["id"], "answer": r.answer, "model": r.model,
                                       "preset": r.preset, "search_queries": r.search_queries, "cited": r.cited,
                                       "invalid_citations": r.invalid_citations, "stats": r.stats,
                                       "sources": [{**asdict(s), "cited": s.number in r.cited} for s in r.sources]})
                    if first_turn and not conv["title"]:
                        title = state.chat.generate_title(preset, body.content, r.answer)
                        if title:
                            state.db.update_conversation(conv_id, title=title)
                            yield sse("title", {"title": title})
                    continue
                if kind == "token":
                    tokens.append(ev["text"])
                elif kind == "sources":
                    sources.extend(ev["sources"])
                elif kind == "searching":
                    queries.append(ev["query"])
                yield sse(kind, {k: v for k, v in ev.items() if k != "type"})
        except Exception as exc:  # noqa: BLE001 - store and report whatever broke the turn
            if not finished:
                state.db.add_assistant_message(
                    conv_id, content="".join(tokens), model=state.chat.cfg.preset(preset).model, preset=preset,
                    search_queries=queries, sources=sources, cited=set(), status="error", error=str(exc))
            yield sse("error", {"error": str(exc)})

    # The turn runs on its own thread and feeds a queue. If the client goes
    # away, only the relay stops; the answer still completes and is stored.
    events: queue.Queue[str | None] = queue.Queue()

    def produce() -> None:
        try:
            for item in stream():
                events.put(item)
        finally:
            events.put(None)

    threading.Thread(target=produce, daemon=True).start()

    async def relay():
        while (item := await run_in_threadpool(events.get)) is not None:
            yield item

    return StreamingResponse(relay(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------------ search --

class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    limit: int | None = Field(default=None, ge=1, le=100)
    fused: bool = False  # true: the hybrid-search list without reranking


@app.post("/api/search", dependencies=[Depends(require_key)])
def search(body: SearchRequest) -> dict:
    t0 = time.monotonic()
    try:
        if body.fused:
            hits = state.retriever.search(body.query, limit=body.limit)
        else:
            hits = state.retriever.retrieve(body.query, limit=body.limit)
    except (RuntimeError, httpx.HTTPError) as exc:
        raise HTTPException(503, str(exc)) from None
    return {"query": body.query, "took_ms": round((time.monotonic() - t0) * 1000),
            "hits": [asdict(h) for h in hits]}


# --------------------------------------------------------------- documents --

@app.get("/api/documents", dependencies=[Depends(require_key)])
def documents() -> dict:
    """Latest manifest row per document, plus files waiting in the inbox."""
    latest: dict[str, dict] = {}
    if MANIFEST.exists():
        with MANIFEST.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                prev = latest.get(rec["sha256"])
                # A failed re-run doesn't undo an earlier success.
                if prev is None or rec.get("status") == "processed" or prev.get("status") != "processed":
                    latest[rec["sha256"]] = rec
    docs = [{"sha256": r["sha256"], "filename": r["filename"], "status": r.get("status"), "route": r.get("route"),
             "pages": r.get("pages"), "chars": r.get("chars"), "error": r.get("error"),
             "processed_at": r.get("timestamp")} for r in latest.values()]
    docs.sort(key=lambda d: d["processed_at"] or 0, reverse=True)
    inbox = sorted(p.name for p in INBOX.iterdir() if p.is_file() and not p.name.startswith(".")) if INBOX.exists() else []
    return {"documents": docs, "inbox": inbox, "ingest": state.ingest.status()}


@app.post("/api/upload", dependencies=[Depends(require_key)], status_code=202)
def upload(files: list[UploadFile] = File(...)) -> dict:
    """Save files into data/inbox and start ingest + embed."""
    INBOX.mkdir(parents=True, exist_ok=True)
    saved, rejected = [], []
    for up in files:
        name = Path(up.filename or "").name  # no directory components
        if not name or name.startswith(".") or Path(name).suffix.lower() not in SUPPORTED_SUFFIXES:
            rejected.append({"filename": up.filename, "reason": "unsupported file type"})
            continue
        dest = INBOX / name
        n = 1
        while dest.exists():
            dest = INBOX / f"{Path(name).stem} ({n}){Path(name).suffix}"
            n += 1
        # Write elsewhere first and move in whole, so the inbox never holds a
        # half-written file for ingest to pick up.
        with tempfile.NamedTemporaryFile(dir=DATA_DIR, delete=False) as tmp:
            shutil.copyfileobj(up.file, tmp)
        os.replace(tmp.name, dest)
        saved.append(dest.name)
    status = state.ingest.trigger() if saved else "nothing to ingest"
    return {"saved": saved, "rejected": rejected, "ingest": status}


# ------------------------------------------------------------------ health --

@app.get("/api/health")
def health() -> JSONResponse:
    checks: dict[str, dict] = {}
    cfg = state.retriever.cfg
    with httpx.Client(timeout=5) as client:
        try:
            client.get(f"{cfg.ollama_url}/api/version").raise_for_status()
            checks["ollama"] = {"ok": True}
        except httpx.HTTPError as exc:
            checks["ollama"] = {"ok": False, "error": str(exc)}
        try:
            resp = client.post(f"{cfg.qdrant_url}/collections/{cfg.collection}/points/count", json={"exact": False})
            if resp.status_code == 404:
                checks["qdrant"] = {"ok": False, "error": f"collection {cfg.collection} doesn't exist; run ragctl embed"}
            else:
                resp.raise_for_status()
                checks["qdrant"] = {"ok": True, "collection": cfg.collection, "chunks": resp.json()["result"]["count"]}
        except httpx.HTTPError as exc:
            checks["qdrant"] = {"ok": False, "error": str(exc)}
    try:
        state.db.ping()
        checks["database"] = {"ok": True}
    except Exception as exc:  # noqa: BLE001
        checks["database"] = {"ok": False, "error": str(exc)}
    if cfg.rerank:
        r = state.retriever._reranker
        checks["reranker"] = ({"ok": True, "model": r.cfg.model, "device": r.device} if r
                              else {"ok": True, "loading": True})
    checks["presets"] = {"ok": True, **{name: {"model": p.model, "tools": p.tools, "think": p.think}
                                        for name, p in state.chat.cfg.presets.items()}}
    ok = all(c["ok"] for c in checks.values())
    return JSONResponse({"ok": ok, **checks}, status_code=200 if ok else 503)


@app.get("/ui", include_in_schema=False)
def test_ui() -> FileResponse:
    """Temporary test UI (static/test-ui.html) for trying the API in a browser.
    Same origin as the API, so no CORS setup is needed. Remove with the real UI."""
    return FileResponse(Path(__file__).parent / "static" / "test-ui.html")


from openai_compat import router as openai_router  # noqa: E402

app.include_router(openai_router, dependencies=[Depends(require_key)])

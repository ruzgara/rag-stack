"""
Chat history storage for the query service: SQLite at data/chat.db.

Every row carries a user_id. There are no user accounts yet, so everything
is stored under LOCAL_USER; when login exists, the API passes the real user
and every query below is already scoped by it.

Each assistant message keeps a copy of the sources it was given (text
included), so an old answer's citations still resolve after the index is
rebuilt or re-ingested.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

LOCAL_USER = "local"

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    title       TEXT,
    preset      TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS conversations_user ON conversations (user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    user_id         TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content         TEXT NOT NULL,
    -- assistant only:
    model           TEXT,
    preset          TEXT,
    search_queries  TEXT,   -- JSON list: the auto-search query, then each tool query
    status          TEXT NOT NULL DEFAULT 'complete',  -- complete | error
    error           TEXT,
    stats           TEXT,   -- JSON: timings, token counts
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_conversation ON messages (conversation_id, created_at);

CREATE TABLE IF NOT EXISTS message_sources (
    message_id   TEXT NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
    number       INTEGER NOT NULL,   -- the [n] the answer cites it by
    cited        INTEGER NOT NULL,   -- 1 if the answer actually cited it
    found_by     TEXT NOT NULL,      -- auto | tool
    query        TEXT NOT NULL,      -- the search that found it
    point_id     TEXT NOT NULL,
    doc_sha      TEXT NOT NULL,
    filename     TEXT NOT NULL,
    chunk_index  INTEGER NOT NULL,
    pages        TEXT NOT NULL,      -- JSON list
    headings     TEXT NOT NULL,      -- JSON list
    text         TEXT NOT NULL,
    rerank_score REAL,
    PRIMARY KEY (message_id, number)
);
"""


class NotFound(Exception):
    pass


class ChatDB:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as db:
            db.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer
            db.executescript(SCHEMA)
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        # A connection per operation: requests run on several threads, and
        # sqlite3 connections must not be shared between them.
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:  # commits, or rolls back on error
                yield db
        finally:
            db.close()

    def ping(self) -> None:
        with self._conn() as db:
            db.execute("SELECT 1")

    # ------------------------------------------------------- conversations --

    def create_conversation(self, preset: str, title: str | None = None, user_id: str = LOCAL_USER) -> dict:
        now = time.time()
        conv = {"id": uuid.uuid4().hex, "title": title, "preset": preset, "created_at": now, "updated_at": now}
        with self._conn() as db:
            db.execute(
                "INSERT INTO conversations (id, user_id, title, preset, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (conv["id"], user_id, title, preset, now, now),
            )
        return conv

    def list_conversations(self, user_id: str = LOCAL_USER) -> list[dict]:
        with self._conn() as db:
            rows = db.execute(
                """SELECT c.id, c.title, c.preset, c.created_at, c.updated_at, COUNT(m.id) AS message_count
                   FROM conversations c LEFT JOIN messages m ON m.conversation_id = c.id
                   WHERE c.user_id = ? GROUP BY c.id ORDER BY c.updated_at DESC""",
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_conversation(self, conv_id: str, user_id: str = LOCAL_USER, with_messages: bool = True) -> dict:
        with self._conn() as db:
            row = db.execute(
                "SELECT id, title, preset, created_at, updated_at FROM conversations WHERE id = ? AND user_id = ?",
                (conv_id, user_id),
            ).fetchone()
            if row is None:
                raise NotFound(conv_id)
            conv = dict(row)
            if not with_messages:
                return conv
            messages = [self._message_dict(r) for r in db.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at", (conv_id,))]
            sources: dict[str, list[dict]] = {}
            for s in db.execute(
                """SELECT s.* FROM message_sources s JOIN messages m ON m.id = s.message_id
                   WHERE m.conversation_id = ? ORDER BY s.message_id, s.number""", (conv_id,)):
                sources.setdefault(s["message_id"], []).append(self._source_dict(s))
        for m in messages:
            if m["role"] == "assistant":
                m["sources"] = sources.get(m["id"], [])
        conv["messages"] = messages
        return conv

    def update_conversation(self, conv_id: str, *, title: str | None = None, preset: str | None = None,
                            user_id: str = LOCAL_USER) -> dict:
        self.get_conversation(conv_id, user_id, with_messages=False)  # raises NotFound
        with self._conn() as db:
            if title is not None:
                db.execute("UPDATE conversations SET title = ? WHERE id = ?", (title, conv_id))
            if preset is not None:
                db.execute("UPDATE conversations SET preset = ? WHERE id = ?", (preset, conv_id))
        return self.get_conversation(conv_id, user_id, with_messages=False)

    def delete_conversation(self, conv_id: str, user_id: str = LOCAL_USER) -> None:
        with self._conn() as db:
            if db.execute("DELETE FROM conversations WHERE id = ? AND user_id = ?", (conv_id, user_id)).rowcount == 0:
                raise NotFound(conv_id)

    # ------------------------------------------------------------ messages --

    def history(self, conv_id: str, user_id: str = LOCAL_USER) -> list[dict]:
        """The conversation as chat messages, for the model. Failed
        answers are left out; so are their questions."""
        with self._conn() as db:
            rows = db.execute(
                "SELECT role, content, status FROM messages WHERE conversation_id = ? AND user_id = ? ORDER BY created_at",
                (conv_id, user_id),
            ).fetchall()
        out: list[dict] = []
        for r in rows:
            if r["role"] == "assistant" and r["status"] != "complete":
                if out and out[-1]["role"] == "user":
                    out.pop()
                continue
            out.append({"role": r["role"], "content": r["content"]})
        # A trailing question without an answer (the turn in progress, or one
        # that died with the process) isn't history.
        if out and out[-1]["role"] == "user":
            out.pop()
        return out

    def add_user_message(self, conv_id: str, content: str, user_id: str = LOCAL_USER) -> dict:
        now = time.time()
        msg = {"id": uuid.uuid4().hex, "role": "user", "content": content, "created_at": now}
        with self._conn() as db:
            db.execute(
                "INSERT INTO messages (id, conversation_id, user_id, role, content, created_at) VALUES (?, ?, ?, 'user', ?, ?)",
                (msg["id"], conv_id, user_id, content, now),
            )
            db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conv_id))
        return msg

    def add_assistant_message(self, conv_id: str, *, content: str, model: str, preset: str, search_queries: list[str],
                              sources: list[dict], cited: set[int], status: str = "complete",
                              error: str | None = None, stats: dict | None = None,
                              user_id: str = LOCAL_USER) -> dict:
        now = time.time()
        msg_id = uuid.uuid4().hex
        with self._conn() as db:
            db.execute(
                """INSERT INTO messages (id, conversation_id, user_id, role, content, model, preset, search_queries,
                                         status, error, stats, created_at)
                   VALUES (?, ?, ?, 'assistant', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (msg_id, conv_id, user_id, content, model, preset, json.dumps(search_queries), status, error,
                 json.dumps(stats or {}), now),
            )
            db.executemany(
                """INSERT INTO message_sources (message_id, number, cited, found_by, query, point_id, doc_sha,
                                                filename, chunk_index, pages, headings, text, rerank_score)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(msg_id, s["number"], int(s["number"] in cited), s["found_by"], s["query"], s["point_id"],
                  s["doc_sha"], s["filename"], s["chunk_index"], json.dumps(s["pages"]), json.dumps(s["headings"]),
                  s["text"], s.get("rerank_score")) for s in sources],
            )
            db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conv_id))
        return {"id": msg_id, "created_at": now}

    # ------------------------------------------------------------- helpers --

    @staticmethod
    def _message_dict(r: sqlite3.Row) -> dict:
        m = {"id": r["id"], "role": r["role"], "content": r["content"], "created_at": r["created_at"]}
        if r["role"] == "assistant":
            m.update(model=r["model"], preset=r["preset"], search_queries=json.loads(r["search_queries"] or "[]"),
                     status=r["status"], error=r["error"], stats=json.loads(r["stats"] or "{}"))
        return m

    @staticmethod
    def _source_dict(s: sqlite3.Row) -> dict:
        return {"number": s["number"], "cited": bool(s["cited"]), "found_by": s["found_by"], "query": s["query"],
                "point_id": s["point_id"], "doc_sha": s["doc_sha"], "filename": s["filename"],
                "chunk_index": s["chunk_index"], "pages": json.loads(s["pages"]),
                "headings": json.loads(s["headings"]), "text": s["text"], "rerank_score": s["rerank_score"]}

"""
Chat history storage for the query service: SQLite at data/chat.db.

Every row carries a user_id and every query is scoped by it -- there are no
defaults here, so a caller that forgets to say whose data it wants gets an
error rather than someone else's. LOCAL_USER is what rows written before
accounts existed carry; the first admin created adopts them (accounts.py).

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

SCHEMA_VERSION = 4
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

CREATE TABLE IF NOT EXISTS uploads (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    filename    TEXT NOT NULL,   -- the name given to the file in data/inbox
    bytes       INTEGER NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS uploads_user ON uploads (user_id, created_at DESC);
"""

ACCOUNTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id             TEXT PRIMARY KEY,
    email          TEXT NOT NULL UNIQUE,   -- stored lowercased
    password_hash  TEXT,                   -- null when identity comes from a proxy
    display_name   TEXT,
    is_admin       INTEGER NOT NULL DEFAULT 0,
    disabled       INTEGER NOT NULL DEFAULT 0,
    -- Bumped to invalidate this user's signed session cookies everywhere
    -- (password change, "log out everywhere"). There is no session table.
    session_epoch  INTEGER NOT NULL DEFAULT 1,
    created_at     REAL NOT NULL,
    last_login_at  REAL
);

CREATE TABLE IF NOT EXISTS groups (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS group_members (
    group_id TEXT NOT NULL REFERENCES groups (id) ON DELETE CASCADE,
    user_id  TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, user_id)
);

-- A named collection of documents. Created here so grants can point at one;
-- membership and retrieval filtering come later.
CREATE TABLE IF NOT EXISTS libraries (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at  REAL NOT NULL
);

-- One row per granted (or refused) permission. library_id is null for the
-- global capabilities. A user's own grant beats its groups'; within either,
-- deny beats allow. See accounts.effective_permissions.
CREATE TABLE IF NOT EXISTS grants (
    id           TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL CHECK (subject_type IN ('user', 'group')),
    subject_id   TEXT NOT NULL,
    permission   TEXT NOT NULL,
    library_id   TEXT REFERENCES libraries (id) ON DELETE CASCADE,
    effect       TEXT NOT NULL CHECK (effect IN ('allow', 'deny')),
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS grants_subject ON grants (subject_type, subject_id);
-- One grant per subject, permission and library. IFNULL because SQLite counts
-- NULLs as distinct in a unique index, which would let the global permissions
-- (library_id IS NULL) be granted twice with opposite effects.
CREATE UNIQUE INDEX IF NOT EXISTS grants_unique
    ON grants (subject_type, subject_id, permission, IFNULL(library_id, ''));
"""

# Which indexed document (by sha256, from the ingest manifest -- no
# re-embedding needed) belongs to which library, and which libraries a
# conversation has attached for retrieval. Both cascade: deleting a library
# drops its memberships and detaches it from every conversation.
LIBRARIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS library_documents (
    library_id TEXT NOT NULL REFERENCES libraries (id) ON DELETE CASCADE,
    doc_sha    TEXT NOT NULL,
    added_at   REAL NOT NULL,
    PRIMARY KEY (library_id, doc_sha)
);
CREATE INDEX IF NOT EXISTS library_documents_sha ON library_documents (doc_sha);

CREATE TABLE IF NOT EXISTS conversation_libraries (
    conversation_id TEXT NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    library_id      TEXT NOT NULL REFERENCES libraries (id) ON DELETE CASCADE,
    PRIMARY KEY (conversation_id, library_id)
);
"""

# Schema changes since version 1, applied in order to an existing database.
# The CREATE TABLE above covers a fresh one; this covers yours.
MIGRATIONS: dict[int, str] = {
    2: """
    CREATE TABLE IF NOT EXISTS uploads (
        id          TEXT PRIMARY KEY,
        user_id     TEXT NOT NULL,
        filename    TEXT NOT NULL,
        bytes       INTEGER NOT NULL,
        created_at  REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS uploads_user ON uploads (user_id, created_at DESC);
    """,
    3: ACCOUNTS_SCHEMA,
    4: LIBRARIES_SCHEMA,
}

# A fresh database gets everything in one go.
SCHEMA += ACCOUNTS_SCHEMA + LIBRARIES_SCHEMA


class NotFound(Exception):
    pass


class ChatDB:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as db:
            db.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer
            was = db.execute("PRAGMA user_version").fetchone()[0]
            db.executescript(SCHEMA)
            # An empty database is at the current version once SCHEMA has run;
            # an older one gets each migration above its version, in order.
            if was:
                for version in sorted(v for v in MIGRATIONS if v > was):
                    db.executescript(MIGRATIONS[version])
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

    def create_conversation(self, preset: str, title: str | None = None, *, user_id: str,
                            library_ids: list[str] | None = None) -> dict:
        now = time.time()
        conv = {"id": uuid.uuid4().hex, "title": title, "preset": preset, "created_at": now, "updated_at": now}
        with self._conn() as db:
            db.execute(
                "INSERT INTO conversations (id, user_id, title, preset, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (conv["id"], user_id, title, preset, now, now),
            )
            if library_ids:
                db.executemany("INSERT INTO conversation_libraries (conversation_id, library_id) VALUES (?, ?)",
                               [(conv["id"], lib_id) for lib_id in library_ids])
        conv["library_ids"] = library_ids or []
        return conv

    def list_conversations(self, user_id: str) -> list[dict]:
        with self._conn() as db:
            rows = db.execute(
                """SELECT c.id, c.title, c.preset, c.created_at, c.updated_at, COUNT(m.id) AS message_count
                   FROM conversations c LEFT JOIN messages m ON m.conversation_id = c.id
                   WHERE c.user_id = ? GROUP BY c.id ORDER BY c.updated_at DESC""",
                (user_id,),
            ).fetchall()
            # A second, un-joined query rather than a second join: joining
            # conversation_libraries alongside messages would multiply rows
            # and throw off message_count's GROUP BY.
            libs: dict[str, list[str]] = {}
            for r in db.execute(
                """SELECT cl.conversation_id, cl.library_id FROM conversation_libraries cl
                   JOIN conversations c ON c.id = cl.conversation_id WHERE c.user_id = ?""", (user_id,)):
                libs.setdefault(r["conversation_id"], []).append(r["library_id"])
        return [{**dict(r), "library_ids": libs.get(r["id"], [])} for r in rows]

    def get_conversation(self, conv_id: str, user_id: str, with_messages: bool = True) -> dict:
        with self._conn() as db:
            row = db.execute(
                "SELECT id, title, preset, created_at, updated_at FROM conversations WHERE id = ? AND user_id = ?",
                (conv_id, user_id),
            ).fetchone()
            if row is None:
                raise NotFound(conv_id)
            conv = dict(row)
            conv["library_ids"] = [r["library_id"] for r in db.execute(
                "SELECT library_id FROM conversation_libraries WHERE conversation_id = ?", (conv_id,))]
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
                            library_ids: list[str] | None = None, user_id: str) -> dict:
        self.get_conversation(conv_id, user_id, with_messages=False)  # raises NotFound
        with self._conn() as db:
            if title is not None:
                db.execute("UPDATE conversations SET title = ? WHERE id = ?", (title, conv_id))
            if preset is not None:
                db.execute("UPDATE conversations SET preset = ? WHERE id = ?", (preset, conv_id))
            if library_ids is not None:
                db.execute("DELETE FROM conversation_libraries WHERE conversation_id = ?", (conv_id,))
                db.executemany("INSERT INTO conversation_libraries (conversation_id, library_id) VALUES (?, ?)",
                               [(conv_id, lib_id) for lib_id in library_ids])
        return self.get_conversation(conv_id, user_id, with_messages=False)

    def delete_conversation(self, conv_id: str, user_id: str) -> None:
        with self._conn() as db:
            if db.execute("DELETE FROM conversations WHERE id = ? AND user_id = ?", (conv_id, user_id)).rowcount == 0:
                raise NotFound(conv_id)

    # ------------------------------------------------------------ messages --

    def history(self, conv_id: str, user_id: str) -> list[dict]:
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

    def add_user_message(self, conv_id: str, content: str, user_id: str) -> dict:
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
                              user_id: str) -> dict:
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

    # ------------------------------------------------------------- uploads --

    def record_upload(self, filename: str, size: int, user_id: str) -> None:
        with self._conn() as db:
            db.execute("INSERT INTO uploads (id, user_id, filename, bytes, created_at) VALUES (?, ?, ?, ?, ?)",
                       (uuid.uuid4().hex, user_id, filename, size, time.time()))

    def upload_usage(self, window_seconds: float, user_id: str) -> tuple[int, int]:
        """How many files this user uploaded within the window, and their total
        size. Ingest quotas are counted here rather than from the manifest,
        which has no user and drops nothing when a file fails to convert."""
        with self._conn() as db:
            row = db.execute(
                "SELECT COUNT(*) AS files, COALESCE(SUM(bytes), 0) AS total FROM uploads "
                "WHERE user_id = ? AND created_at >= ?",
                (user_id, time.time() - window_seconds)).fetchone()
        return row["files"], row["total"]

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

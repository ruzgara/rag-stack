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

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    title        TEXT,
    preset       TEXT NOT NULL,
    -- Which workspace this chat belongs to; null for one predating
    -- workspaces, or created without one. Deleting a workspace hides its
    -- chats with it (see ChatDB.list_conversations) -- a chat only makes
    -- sense inside its workspace.
    workspace_id TEXT REFERENCES workspaces (id) ON DELETE CASCADE,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    -- Soft delete, same shape as workspaces.deleted: set instead of
    -- removing the row, so nothing about the chat is actually lost.
    -- updated_at doubles as the deleted-at time, same as workspaces'
    -- last_used_at -- restoring bumps it again.
    deleted      INTEGER NOT NULL DEFAULT 0,
    -- Archived: hidden from the normal chat list like a delete, but still
    -- fully open-able and repliable -- unlike deleted, this never blocks
    -- get_conversation. Independent of deleted; archiving or unarchiving
    -- also bumps updated_at, same double-duty as above.
    archived     INTEGER NOT NULL DEFAULT 0
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
    display_name   TEXT NOT NULL,
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
    -- 'locked' (the default): only an admin can add a member. 'open': a
    -- user may add themselves (Accounts.join_group) -- an admin can still
    -- add anyone to either kind.
    type        TEXT NOT NULL DEFAULT 'locked' CHECK (type IN ('open', 'locked')),
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
    -- Who created it -- metadata only (a "Mine" tag), not an authorization
    -- source: read/write/share still come entirely from grants, an owner
    -- included. Null for one made without an acting user (ragctl). Set
    -- null rather than cascading on delete: the library outlives whoever
    -- happened to create it.
    owner_id    TEXT REFERENCES users (id) ON DELETE SET NULL,
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
-- The reverse lookup: which subjects hold a grant on a given library (a
-- library's "who has access" view -- accounts.grants_on_library). Only
-- library grants have a non-null library_id, which is exactly what this
-- is for; the global ones are found through grants_subject instead.
CREATE INDEX IF NOT EXISTS grants_library ON grants (library_id, subject_type, subject_id);
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

# A workspace is owned by one user and holds libraries and chats: chats
# belong to at most one workspace (conversations.workspace_id, added below)
# and cascade with it, since a chat only makes sense inside its workspace.
# Libraries are a many-to-many attachment, same shape as conversation_libraries
# -- a library itself stays a global, admin-managed row (accounts.py).
WORKSPACES_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id            TEXT PRIMARY KEY,
    owner_id      TEXT NOT NULL,
    name          TEXT NOT NULL,
    description   TEXT,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    -- Bumped whenever a turn runs in one of this workspace's conversations
    -- (see touch_workspace) -- drives "most recently used" ordering. Starts
    -- equal to created_at, so a never-used workspace still sorts sanely.
    -- Also doubles as the deleted-at time: delete_workspace touches it too,
    -- so there's no separate timestamp for that.
    last_used_at  REAL NOT NULL DEFAULT 0,
    -- Soft delete: set instead of removing the row, so its conversations
    -- (which cascade on a real DELETE) stay put. Excluded from every read
    -- path below; nothing purges these yet.
    deleted       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS workspaces_owner ON workspaces (owner_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS workspaces_last_used ON workspaces (owner_id, last_used_at DESC);

CREATE TABLE IF NOT EXISTS workspace_libraries (
    workspace_id TEXT NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
    library_id   TEXT NOT NULL REFERENCES libraries (id) ON DELETE CASCADE,
    added_at     REAL NOT NULL,
    PRIMARY KEY (workspace_id, library_id)
);
"""

# A chat inside a workspace has no libraries of its own: it searches whatever
# its workspace holds at the time of each turn, so attaching a library to the
# workspace reaches every chat in it, old ones included. conversation_libraries
# is only read and written for chats outside any workspace.

# Everything in one go -- no migrations. This system has never shipped
# outside dev, so there's no installed base to carry forward; when the
# schema changes, it changes here and dev databases get dropped and
# recreated (see ragctl / data/chat.db).
SCHEMA += ACCOUNTS_SCHEMA + LIBRARIES_SCHEMA + WORKSPACES_SCHEMA


class NotFound(Exception):
    pass


class WorkspaceLibraries(ValueError):
    """Raised on an attempt to give a chat inside a workspace its own
    libraries -- they come from the workspace (see the note above SCHEMA)."""


class ChatDB:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as db:
            db.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer
            db.executescript(SCHEMA)

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

    # --------------------------------------------------------- workspaces --

    def create_workspace(self, name: str, description: str | None = None, *, user_id: str) -> dict:
        now = time.time()
        ws = {"id": uuid.uuid4().hex, "name": name, "description": description,
              "created_at": now, "updated_at": now, "last_used_at": now}
        with self._conn() as db:
            db.execute(
                "INSERT INTO workspaces (id, owner_id, name, description, created_at, updated_at, last_used_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ws["id"], user_id, name, description, now, now, now),
            )
        ws["library_ids"] = []
        return ws

    def list_workspaces(self, user_id: str, deleted: bool = False) -> list[dict]:
        """`deleted=False` (the default) is the normal workspace list.
        `deleted=True` is the Recently Deleted tray, for restore_workspace."""
        with self._conn() as db:
            rows = db.execute(
                """SELECT w.id, w.name, w.description, w.created_at, w.updated_at, w.last_used_at,
                          COUNT(c.id) AS conversation_count
                   FROM workspaces w LEFT JOIN conversations c ON c.workspace_id = w.id
                   WHERE w.owner_id = ? AND w.deleted = ?
                   GROUP BY w.id ORDER BY w.last_used_at DESC""",
                (user_id, int(deleted)),
            ).fetchall()
            libs: dict[str, list[str]] = {}
            for r in db.execute(
                """SELECT wl.workspace_id, wl.library_id FROM workspace_libraries wl
                   JOIN workspaces w ON w.id = wl.workspace_id WHERE w.owner_id = ?""", (user_id,)):
                libs.setdefault(r["workspace_id"], []).append(r["library_id"])
        return [{**dict(r), "library_ids": libs.get(r["id"], [])} for r in rows]

    def get_workspace(self, workspace_id: str, user_id: str) -> dict:
        with self._conn() as db:
            row = db.execute(
                "SELECT id, name, description, created_at, updated_at, last_used_at FROM workspaces "
                "WHERE id = ? AND owner_id = ? AND NOT deleted",
                (workspace_id, user_id),
            ).fetchone()
            if row is None:
                raise NotFound(workspace_id)
            ws = dict(row)
            ws["library_ids"] = [r["library_id"] for r in db.execute(
                "SELECT library_id FROM workspace_libraries WHERE workspace_id = ?", (workspace_id,))]
        return ws

    def touch_workspace(self, workspace_id: str, user_id: str) -> None:
        """Bumps last_used_at -- called when a turn runs in one of this
        workspace's conversations, so the switcher can sort by actual use
        rather than by edits to the workspace's own name/libraries."""
        with self._conn() as db:
            db.execute("UPDATE workspaces SET last_used_at = ? WHERE id = ? AND owner_id = ? AND NOT deleted",
                       (time.time(), workspace_id, user_id))

    def update_workspace(self, workspace_id: str, *, name: str | None = None, description: str | None = None,
                         library_ids: list[str] | None = None, user_id: str) -> dict:
        self.get_workspace(workspace_id, user_id)  # raises NotFound
        now = time.time()
        with self._conn() as db:
            if name is not None:
                db.execute("UPDATE workspaces SET name = ?, updated_at = ? WHERE id = ?", (name, now, workspace_id))
            if description is not None:
                db.execute("UPDATE workspaces SET description = ?, updated_at = ? WHERE id = ?",
                           (description, now, workspace_id))
            if library_ids is not None:
                db.execute("DELETE FROM workspace_libraries WHERE workspace_id = ?", (workspace_id,))
                db.executemany(
                    "INSERT INTO workspace_libraries (workspace_id, library_id, added_at) VALUES (?, ?, ?)",
                    [(workspace_id, lib_id, now) for lib_id in library_ids])
        return self.get_workspace(workspace_id, user_id)

    def delete_workspace(self, workspace_id: str, user_id: str) -> None:
        """Marks the workspace deleted rather than removing the row -- a soft
        delete, so its conversations (which would cascade on a real DELETE)
        stay in place, merely inaccessible while the workspace is deleted
        (list_conversations/get_conversation hide them via the join on
        workspaces.deleted -- nothing about a chat itself changes here).
        Bumps last_used_at too, so it doubles as the deleted-at time -- no
        separate timestamp column for that."""
        with self._conn() as db:
            if db.execute(
                "UPDATE workspaces SET deleted = 1, last_used_at = ? WHERE id = ? AND owner_id = ? AND NOT deleted",
                (time.time(), workspace_id, user_id)).rowcount == 0:
                raise NotFound(workspace_id)

    def restore_workspace(self, workspace_id: str, user_id: str) -> dict:
        """The other half of delete_workspace -- its conversations were
        never touched, so restoring just makes both visible again."""
        with self._conn() as db:
            if db.execute(
                "UPDATE workspaces SET deleted = 0, last_used_at = ? WHERE id = ? AND owner_id = ? AND deleted",
                (time.time(), workspace_id, user_id)).rowcount == 0:
                raise NotFound(workspace_id)
        return self.get_workspace(workspace_id, user_id)

    # ------------------------------------------------------- conversations --

    def create_conversation(self, preset: str, title: str | None = None, *, user_id: str,
                            library_ids: list[str] | None = None, workspace_id: str | None = None) -> dict:
        now = time.time()
        conv = {"id": uuid.uuid4().hex, "title": title, "preset": preset, "workspace_id": workspace_id,
                "created_at": now, "updated_at": now, "deleted": 0, "archived": 0}
        if workspace_id is not None and library_ids is not None:
            raise WorkspaceLibraries("a chat in a workspace uses the workspace's libraries")
        with self._conn() as db:
            db.execute(
                "INSERT INTO conversations (id, user_id, title, preset, workspace_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (conv["id"], user_id, title, preset, workspace_id, now, now),
            )
            if library_ids:
                db.executemany("INSERT INTO conversation_libraries (conversation_id, library_id) VALUES (?, ?)",
                               [(conv["id"], lib_id) for lib_id in library_ids])
            conv["library_ids"] = self._library_ids(db, conv["id"], workspace_id)
        return conv

    @staticmethod
    def _library_ids(db: sqlite3.Connection, conv_id: str, workspace_id: str | None) -> list[str]:
        """A chat's effective libraries: its workspace's if it has one,
        otherwise its own."""
        if workspace_id is not None:
            return [r["library_id"] for r in db.execute(
                "SELECT library_id FROM workspace_libraries WHERE workspace_id = ?", (workspace_id,))]
        return [r["library_id"] for r in db.execute(
            "SELECT library_id FROM conversation_libraries WHERE conversation_id = ?", (conv_id,))]

    def list_conversations(self, user_id: str, workspace_id: str | None = None, deleted: bool = False,
                           archived: bool = False) -> list[dict]:
        """`deleted=False, archived=False` (the default) is the normal chat
        list: this chat's own delete or archive, or its workspace's delete,
        all hide it. `archived=True` is the Archived list (still excludes
        deleted chats and deleted workspaces). `deleted=True` is the
        Recently Deleted tray and ignores `archived` entirely -- only this
        chat's own delete puts it there; a chat merely hidden because its
        workspace was deleted isn't something the user asked to delete, so
        it doesn't show up to restore this way (restoring the workspace
        brings it back instead)."""
        with self._conn() as db:
            params: tuple = (user_id,)
            if deleted:
                where = "c.user_id = ? AND c.deleted"
            else:
                where = ("c.user_id = ? AND NOT c.deleted AND c.archived = ? "
                        "AND (c.workspace_id IS NULL OR NOT w.deleted)")
                params += (int(archived),)
            if workspace_id is not None:
                where += " AND c.workspace_id = ?"
                params += (workspace_id,)
            rows = db.execute(
                f"""SELECT c.id, c.title, c.preset, c.workspace_id, c.created_at, c.updated_at, c.archived,
                          COUNT(m.id) AS message_count
                   FROM conversations c LEFT JOIN messages m ON m.conversation_id = c.id
                        LEFT JOIN workspaces w ON w.id = c.workspace_id
                   WHERE {where} GROUP BY c.id ORDER BY c.updated_at DESC""",
                params,
            ).fetchall()
            # A second, un-joined query rather than a second join: joining
            # conversation_libraries alongside messages would multiply rows
            # and throw off message_count's GROUP BY.
            libs: dict[str, list[str]] = {}
            for r in db.execute(
                """SELECT cl.conversation_id, cl.library_id FROM conversation_libraries cl
                   JOIN conversations c ON c.id = cl.conversation_id
                   WHERE c.user_id = ? AND c.workspace_id IS NULL""", (user_id,)):
                libs.setdefault(r["conversation_id"], []).append(r["library_id"])
            ws_libs: dict[str, list[str]] = {}
            for r in db.execute(
                """SELECT wl.workspace_id, wl.library_id FROM workspace_libraries wl
                   JOIN workspaces w ON w.id = wl.workspace_id WHERE w.owner_id = ?""", (user_id,)):
                ws_libs.setdefault(r["workspace_id"], []).append(r["library_id"])
        return [{**dict(r), "library_ids": ws_libs.get(r["workspace_id"], []) if r["workspace_id"] is not None
                 else libs.get(r["id"], [])} for r in rows]

    def get_conversation(self, conv_id: str, user_id: str, with_messages: bool = True) -> dict:
        with self._conn() as db:
            # Same deleted/soft-deleted-workspace exclusion as
            # list_conversations(deleted=False).
            row = db.execute(
                """SELECT c.id, c.title, c.preset, c.workspace_id, c.created_at, c.updated_at, c.archived
                   FROM conversations c LEFT JOIN workspaces w ON w.id = c.workspace_id
                   WHERE c.id = ? AND c.user_id = ? AND NOT c.deleted
                         AND (c.workspace_id IS NULL OR NOT w.deleted)""",
                (conv_id, user_id),
            ).fetchone()
            if row is None:
                raise NotFound(conv_id)
            conv = dict(row)
            conv["library_ids"] = self._library_ids(db, conv_id, conv["workspace_id"])
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
        conv = self.get_conversation(conv_id, user_id, with_messages=False)  # raises NotFound
        if library_ids is not None and conv["workspace_id"] is not None:
            raise WorkspaceLibraries("a chat in a workspace uses the workspace's libraries; change the workspace's")
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
        """Marks the chat deleted rather than removing it -- see the
        `deleted` column comment. Bumps updated_at, which doubles as the
        deleted-at time."""
        with self._conn() as db:
            if db.execute(
                "UPDATE conversations SET deleted = 1, updated_at = ? WHERE id = ? AND user_id = ? AND NOT deleted",
                (time.time(), conv_id, user_id)).rowcount == 0:
                raise NotFound(conv_id)

    def restore_conversation(self, conv_id: str, user_id: str) -> dict:
        with self._conn() as db:
            if db.execute(
                "UPDATE conversations SET deleted = 0, updated_at = ? WHERE id = ? AND user_id = ? AND deleted",
                (time.time(), conv_id, user_id)).rowcount == 0:
                raise NotFound(conv_id)
        return self.get_conversation(conv_id, user_id, with_messages=False)

    def archive_conversation(self, conv_id: str, user_id: str) -> dict:
        """Archiving hides a chat from the normal list without touching
        `deleted` -- it's still fully open-able and repliable, just out of
        the way. A deleted chat can't be archived; restore it first."""
        with self._conn() as db:
            if db.execute(
                "UPDATE conversations SET archived = 1, updated_at = ? "
                "WHERE id = ? AND user_id = ? AND NOT deleted AND NOT archived",
                (time.time(), conv_id, user_id)).rowcount == 0:
                raise NotFound(conv_id)
        return self.get_conversation(conv_id, user_id, with_messages=False)

    def unarchive_conversation(self, conv_id: str, user_id: str) -> dict:
        with self._conn() as db:
            if db.execute(
                "UPDATE conversations SET archived = 0, updated_at = ? "
                "WHERE id = ? AND user_id = ? AND NOT deleted AND archived",
                (time.time(), conv_id, user_id)).rowcount == 0:
                raise NotFound(conv_id)
        return self.get_conversation(conv_id, user_id, with_messages=False)

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

    def prepare_retry(self, conv_id: str, user_id: str) -> str:
        """Deletes the conversation's last message, if it's an answer, so a
        fresh one can replace it -- the question it was answering stays in
        place (message_sources cascades with the deleted answer). Returns
        that question's content, for the caller to rerun the turn with.

        Raises NotFound if the chat doesn't end in an answer: there's
        nothing to retry mid-question, on a chat with no messages yet, or
        on one that already ends in a still-unanswered question."""
        with self._conn() as db:
            rows = db.execute(
                "SELECT id, role, content FROM messages WHERE conversation_id = ? AND user_id = ? "
                "ORDER BY created_at DESC LIMIT 2", (conv_id, user_id)).fetchall()
            if len(rows) < 2 or rows[0]["role"] != "assistant" or rows[1]["role"] != "user":
                raise NotFound(conv_id)
            db.execute("DELETE FROM messages WHERE id = ?", (rows[0]["id"],))
            return rows[1]["content"]

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

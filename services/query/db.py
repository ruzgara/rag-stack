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

# The workspace every admin shares, and nobody else can see: where a deleted
# user's libraries go (Accounts.delete_user). It's created with the database,
# owned by this placeholder rather than any one user -- a user id is a uuid
# hex, so it can't collide -- and can't be renamed or deleted (api.py). Each
# admin's chats in it are still their own (conversations.user_id).
ADMIN_WORKSPACE_ID = "admin-workspace"
ADMIN_WORKSPACE_OWNER = "admins"
ADMIN_WORKSPACE_NAME = "Admin Workspace"

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
    -- When this user last opened the chat (get_conversation with_messages,
    -- i.e. actually viewing it) or finished a turn in it themselves (they
    -- watched it stream, so it's not unread either). Starts equal to
    -- created_at, same reasoning as workspaces.last_used_at: a chat you
    -- just created isn't "unread". Compare against the last message's
    -- created_at (list_conversations/get_conversation compute this as
    -- last_message_at), not updated_at -- that also moves on delete,
    -- archive, restore and unarchive, none of which are something to read.
    last_read_at REAL NOT NULL,
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
    status          TEXT NOT NULL DEFAULT 'complete',  -- complete | error | stopped
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
    doc_sha     TEXT NOT NULL,   -- the document it became (content-addressed, so shared by identical uploads)
    created_at  REAL NOT NULL,
    -- Set when the uploader deletes it (see api.delete_upload). The row is
    -- kept rather than removed so it still counts against the ingest quota --
    -- converting it cost the GPU time whether or not it was later deleted --
    -- but it no longer lists, and no longer counts as someone holding the
    -- document.
    deleted_at  REAL
);
CREATE INDEX IF NOT EXISTS uploads_sha ON uploads (doc_sha);
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

-- Personal API tokens: a bearer credential that acts as its user, for
-- scripts and OpenAI-compatible clients. Only a hash is kept -- the token
-- itself is shown once, when it's made -- and a token is revoked by deleting
-- its row. It carries no permissions of its own: every request re-reads the
-- user, so disabling, demoting or deleting them applies to their tokens too.
CREATE TABLE IF NOT EXISTS api_tokens (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    token_hash    TEXT NOT NULL UNIQUE,   -- sha256 hex of the whole token
    hint          TEXT NOT NULL,          -- its first few characters, to tell tokens apart
    created_at    REAL NOT NULL,
    last_used_at  REAL,
    expires_at    REAL                    -- null: never
);
CREATE INDEX IF NOT EXISTS api_tokens_user ON api_tokens (user_id, created_at DESC);

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

-- A named collection of documents, owned by a user and shared through
-- library_access. Which documents it holds is library_documents (below);
-- retrieval is filtered to the documents in the libraries a user can read.
CREATE TABLE IF NOT EXISTS libraries (
    id          TEXT PRIMARY KEY,
    -- Unique per owner, not globally: a system-wide UNIQUE let any user
    -- probe whether a name was taken (learning a library they can't see
    -- exists) or squat a name before its rightful owner claimed it. Scoped
    -- uniqueness is enforced in accounts.py (SQLite's UNIQUE would treat
    -- every NULL owner_id as distinct, which isn't what we want for
    -- ragctl-created/orphaned libraries either).
    name        TEXT NOT NULL,
    description TEXT,
    -- Who created it -- an authorization source, not just metadata: the
    -- owner has full access (accounts.library_role) without a row in
    -- library_access. Null for one made without an acting user (ragctl) or
    -- whose owner was deleted. Set null rather than cascading on delete: the
    -- library outlives whoever happened to create it, manageable afterwards
    -- only by an admin.
    owner_id    TEXT REFERENCES users (id) ON DELETE SET NULL,
    created_at  REAL NOT NULL
);

-- Sharing: one row says a subject (a user or a group) holds read or write on
-- one library, on top of whatever the owner already has. There is no deny --
-- access is additive, and the highest role across a user's own row and its
-- groups' wins (accounts.library_role).
CREATE TABLE IF NOT EXISTS library_access (
    library_id   TEXT NOT NULL REFERENCES libraries (id) ON DELETE CASCADE,
    subject_type TEXT NOT NULL CHECK (subject_type IN ('user', 'group')),
    subject_id   TEXT NOT NULL,
    role         TEXT NOT NULL CHECK (role IN ('read', 'write')),
    created_at   REAL NOT NULL,
    PRIMARY KEY (library_id, subject_type, subject_id)
);
-- The reverse lookup: which libraries a subject (user or its groups) has
-- access to -- accounts.library_role and readable_libraries. subject_id is
-- polymorphic (a user or group id), so there's no foreign key here; deleting
-- a user or group must delete its rows explicitly (Accounts.delete_user,
-- delete_group).
CREATE INDEX IF NOT EXISTS library_access_subject ON library_access (subject_type, subject_id);
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
# -- a library itself belongs to its owner, not to any workspace, and can sit
# in several (accounts.py).
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


# A chat is visible unless it sits in the admin workspace and the caller
# isn't an admin (any more). Takes (is_admin,); needs `w` joined as the
# chat's workspace.
_ADMIN_CHAT_VISIBLE = f"(c.workspace_id IS NULL OR w.owner_id != '{ADMIN_WORKSPACE_OWNER}' OR ?)"


def _visible(alias: str = "w") -> str:
    """SQL for "a workspace this caller can see": their own, or the admin
    workspace if they're an admin. Takes (user_id, is_admin) as parameters."""
    col = f"{alias}.owner_id" if alias else "owner_id"
    return f"({col} = ? OR ({col} = '{ADMIN_WORKSPACE_OWNER}' AND ?))"


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
            now = time.time()
            db.execute(
                "INSERT OR IGNORE INTO workspaces (id, owner_id, name, description, created_at, updated_at, "
                "last_used_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ADMIN_WORKSPACE_ID, ADMIN_WORKSPACE_OWNER, ADMIN_WORKSPACE_NAME,
                 "Libraries left behind by deleted users. Only admins can see this workspace.", now, now, now))

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

    def list_workspaces(self, user_id: str, deleted: bool = False, *, is_admin: bool = False) -> list[dict]:
        """`deleted=False` (the default) is the normal workspace list.
        `deleted=True` is the Recently Deleted tray, for restore_workspace.
        An admin's list also has the admin workspace; conversation_count is
        only ever the caller's own chats, which matters there, since every
        admin's chats sit in it."""
        with self._conn() as db:
            rows = db.execute(
                f"""SELECT w.id, w.name, w.description, w.created_at, w.updated_at, w.last_used_at,
                          COUNT(c.id) AS conversation_count
                   FROM workspaces w LEFT JOIN conversations c ON c.workspace_id = w.id AND c.user_id = ?
                   WHERE {_visible()} AND w.deleted = ?
                   GROUP BY w.id ORDER BY w.last_used_at DESC""",
                (user_id, user_id, int(is_admin), int(deleted)),
            ).fetchall()
            libs: dict[str, list[str]] = {}
            for r in db.execute(
                f"""SELECT wl.workspace_id, wl.library_id FROM workspace_libraries wl
                   JOIN workspaces w ON w.id = wl.workspace_id WHERE {_visible()}""", (user_id, int(is_admin))):
                libs.setdefault(r["workspace_id"], []).append(r["library_id"])
        return [{**dict(r), "library_ids": libs.get(r["id"], [])} for r in rows]

    def get_workspace(self, workspace_id: str, user_id: str, *, is_admin: bool = False) -> dict:
        with self._conn() as db:
            row = db.execute(
                "SELECT id, name, description, created_at, updated_at, last_used_at FROM workspaces "
                f"WHERE id = ? AND {_visible('')} AND NOT deleted",
                (workspace_id, user_id, int(is_admin)),
            ).fetchone()
            if row is None:
                raise NotFound(workspace_id)
            ws = dict(row)
            ws["library_ids"] = [r["library_id"] for r in db.execute(
                "SELECT library_id FROM workspace_libraries WHERE workspace_id = ?", (workspace_id,))]
        return ws

    def touch_workspace(self, workspace_id: str, user_id: str, *, is_admin: bool = False) -> None:
        """Bumps last_used_at -- called when a turn runs in one of this
        workspace's conversations, so the switcher can sort by actual use
        rather than by edits to the workspace's own name/libraries."""
        with self._conn() as db:
            db.execute(f"UPDATE workspaces SET last_used_at = ? WHERE id = ? AND {_visible('')} AND NOT deleted",
                       (time.time(), workspace_id, user_id, int(is_admin)))

    def update_workspace(self, workspace_id: str, *, name: str | None = None, description: str | None = None,
                         library_ids: list[str] | None = None, user_id: str, is_admin: bool = False) -> dict:
        self.get_workspace(workspace_id, user_id, is_admin=is_admin)  # raises NotFound
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
        return self.get_workspace(workspace_id, user_id, is_admin=is_admin)

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
                "created_at": now, "updated_at": now, "last_read_at": now, "deleted": 0, "archived": 0}
        if workspace_id is not None and library_ids is not None:
            raise WorkspaceLibraries("a chat in a workspace uses the workspace's libraries")
        with self._conn() as db:
            db.execute(
                "INSERT INTO conversations (id, user_id, title, preset, workspace_id, created_at, updated_at, "
                "last_read_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (conv["id"], user_id, title, preset, workspace_id, now, now, now),
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
                           archived: bool = False, *, is_admin: bool = False) -> list[dict]:
        """`deleted=False, archived=False` (the default) is the normal chat
        list: this chat's own delete or archive, or its workspace's delete,
        all hide it. `archived=True` is the Archived list (still excludes
        deleted chats and deleted workspaces). `deleted=True` is the
        Recently Deleted tray and ignores `archived` entirely -- only this
        chat's own delete puts it there; a chat merely hidden because its
        workspace was deleted isn't something the user asked to delete, so
        it doesn't show up to restore this way (restoring the workspace
        brings it back instead). Chats in the admin workspace only list for
        an admin: someone demoted keeps them, out of sight, until promoted
        again."""
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
            where += f" AND {_ADMIN_CHAT_VISIBLE}"
            params += (int(is_admin),)
            rows = db.execute(
                f"""SELECT c.id, c.title, c.preset, c.workspace_id, c.created_at, c.updated_at, c.last_read_at,
                          c.archived, COUNT(m.id) AS message_count,
                          -- What "unread" actually compares against: only new
                          -- messages count as activity, not a delete/archive/
                          -- restore, which also bump updated_at (see their
                          -- own methods below) but aren't something to read.
                          COALESCE(MAX(m.created_at), c.created_at) AS last_message_at
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
                f"""SELECT wl.workspace_id, wl.library_id FROM workspace_libraries wl
                   JOIN workspaces w ON w.id = wl.workspace_id WHERE {_visible()}""", (user_id, int(is_admin))):
                ws_libs.setdefault(r["workspace_id"], []).append(r["library_id"])
        return [{**dict(r), "library_ids": ws_libs.get(r["workspace_id"], []) if r["workspace_id"] is not None
                 else libs.get(r["id"], [])} for r in rows]

    def get_conversation(self, conv_id: str, user_id: str, with_messages: bool = True, *,
                         is_admin: bool = False) -> dict:
        """`with_messages=True` (the default) is a real open of the chat --
        it counts as reading it, so last_read_at is bumped to now.
        `with_messages=False` is used for internal existence/ownership
        checks (update, add-message, retry) and must not affect it."""
        with self._conn() as db:
            # Same deleted/soft-deleted-workspace and admin-workspace
            # exclusions as list_conversations(deleted=False).
            row = db.execute(
                """SELECT c.id, c.title, c.preset, c.workspace_id, c.created_at, c.updated_at, c.last_read_at,
                          c.archived,
                          COALESCE((SELECT MAX(created_at) FROM messages WHERE conversation_id = c.id), c.created_at)
                              AS last_message_at
                   FROM conversations c LEFT JOIN workspaces w ON w.id = c.workspace_id
                   WHERE c.id = ? AND c.user_id = ? AND NOT c.deleted
                         AND (c.workspace_id IS NULL OR NOT w.deleted) AND """ + _ADMIN_CHAT_VISIBLE,
                (conv_id, user_id, int(is_admin)),
            ).fetchone()
            if row is None:
                raise NotFound(conv_id)
            conv = dict(row)
            conv["library_ids"] = self._library_ids(db, conv_id, conv["workspace_id"])
            if not with_messages:
                return conv
            conv["last_read_at"] = time.time()
            db.execute("UPDATE conversations SET last_read_at = ? WHERE id = ?", (conv["last_read_at"], conv_id))
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
                            library_ids: list[str] | None = None, user_id: str, is_admin: bool = False) -> dict:
        conv = self.get_conversation(conv_id, user_id, with_messages=False, is_admin=is_admin)  # raises NotFound
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
        return self.get_conversation(conv_id, user_id, with_messages=False, is_admin=is_admin)

    def delete_conversation(self, conv_id: str, user_id: str) -> None:
        """Marks the chat deleted rather than removing it -- see the
        `deleted` column comment. Bumps updated_at, which doubles as the
        deleted-at time."""
        with self._conn() as db:
            if db.execute(
                "UPDATE conversations SET deleted = 1, updated_at = ? WHERE id = ? AND user_id = ? AND NOT deleted",
                (time.time(), conv_id, user_id)).rowcount == 0:
                raise NotFound(conv_id)

    def restore_conversation(self, conv_id: str, user_id: str, *, is_admin: bool = False) -> dict:
        with self._conn() as db:
            if db.execute(
                "UPDATE conversations SET deleted = 0, updated_at = ? WHERE id = ? AND user_id = ? AND deleted",
                (time.time(), conv_id, user_id)).rowcount == 0:
                raise NotFound(conv_id)
        return self.get_conversation(conv_id, user_id, with_messages=False, is_admin=is_admin)

    def archive_conversation(self, conv_id: str, user_id: str, *, is_admin: bool = False) -> dict:
        """Archiving hides a chat from the normal list without touching
        `deleted` -- it's still fully open-able and repliable, just out of
        the way. A deleted chat can't be archived; restore it first."""
        with self._conn() as db:
            if db.execute(
                "UPDATE conversations SET archived = 1, updated_at = ? "
                "WHERE id = ? AND user_id = ? AND NOT deleted AND NOT archived",
                (time.time(), conv_id, user_id)).rowcount == 0:
                raise NotFound(conv_id)
        return self.get_conversation(conv_id, user_id, with_messages=False, is_admin=is_admin)

    def unarchive_conversation(self, conv_id: str, user_id: str, *, is_admin: bool = False) -> dict:
        with self._conn() as db:
            if db.execute(
                "UPDATE conversations SET archived = 0, updated_at = ? "
                "WHERE id = ? AND user_id = ? AND NOT deleted AND archived",
                (time.time(), conv_id, user_id)).rowcount == 0:
                raise NotFound(conv_id)
        return self.get_conversation(conv_id, user_id, with_messages=False, is_admin=is_admin)

    # ------------------------------------------------------------ messages --

    def history(self, conv_id: str, user_id: str) -> list[dict]:
        """The conversation as chat messages, for the model. Failed
        answers are left out; so are their questions. A stopped answer
        stays if it got anywhere -- the user read that much of it, and may
        well follow up on it ("go on")."""
        with self._conn() as db:
            rows = db.execute(
                "SELECT role, content, status FROM messages WHERE conversation_id = ? AND user_id = ? ORDER BY created_at",
                (conv_id, user_id),
            ).fetchall()
        out: list[dict] = []
        for r in rows:
            if r["role"] == "assistant" and r["status"] != "complete" and not (
                    r["status"] == "stopped" and r["content"]):
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
            # A chat has one owner, and this is the answer to their own turn
            # -- they watched it stream, so it counts as read, not just as
            # activity. Without this, the chat you're actively watching
            # would show up unread the moment the answer finishes, since it
            # lands after last_read_at was last bumped (on open).
            db.execute("UPDATE conversations SET updated_at = ?, last_read_at = ? WHERE id = ?",
                      (now, now, conv_id))
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

    def record_upload(self, filename: str, size: int, doc_sha: str, user_id: str) -> dict:
        row = {"id": uuid.uuid4().hex, "filename": filename, "bytes": size, "doc_sha": doc_sha,
               "created_at": time.time()}
        with self._conn() as db:
            db.execute("INSERT INTO uploads (id, user_id, filename, bytes, doc_sha, created_at) "
                       "VALUES (?, ?, ?, ?, ?, ?)",
                       (row["id"], user_id, filename, size, doc_sha, row["created_at"]))
        return row

    def list_uploads(self, user_id: str) -> list[dict]:
        """This user's uploads, newest first, leaving out ones they deleted."""
        with self._conn() as db:
            return [dict(r) for r in db.execute(
                "SELECT id, filename, bytes, doc_sha, created_at FROM uploads "
                "WHERE user_id = ? AND deleted_at IS NULL ORDER BY created_at DESC", (user_id,))]

    def get_upload(self, upload_id: str, user_id: str) -> dict:
        """One of this user's live uploads; NotFound for anyone else's, same
        as for one that doesn't exist or was deleted."""
        with self._conn() as db:
            row = db.execute("SELECT id, filename, bytes, doc_sha, created_at FROM uploads "
                             "WHERE id = ? AND user_id = ? AND deleted_at IS NULL", (upload_id, user_id)).fetchone()
        if row is None:
            raise NotFound(upload_id)
        return dict(row)

    def document_held_by_others(self, doc_sha: str, user_id: str) -> bool:
        """Whether someone other than this user has a live upload of this
        document -- their own copy of the same bytes, which deleting this
        user's upload must not take away."""
        with self._conn() as db:
            return db.execute("SELECT 1 FROM uploads WHERE doc_sha = ? AND user_id != ? AND deleted_at IS NULL",
                              (doc_sha, user_id)).fetchone() is not None

    def delete_uploads(self, doc_sha: str, user_id: str) -> list[str]:
        """Marks every live upload this user made of this document deleted --
        uploading the same file twice is still one document -- and returns
        their inbox filenames. The rows stay, for the quota (see the
        deleted_at column)."""
        with self._conn() as db:
            names = [r["filename"] for r in db.execute(
                "SELECT filename FROM uploads WHERE doc_sha = ? AND user_id = ? AND deleted_at IS NULL",
                (doc_sha, user_id))]
            db.execute("UPDATE uploads SET deleted_at = ? WHERE doc_sha = ? AND user_id = ? AND deleted_at IS NULL",
                       (time.time(), doc_sha, user_id))
        return names

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

    def inbox_uploaders(self, filenames: list[str]) -> dict[str, str]:
        """Who uploaded each of these inbox files: the newest upload recorded
        under each name. A name is only ever held by one file in the inbox at
        a time (api._claim_inbox_name), but it's reused once ingest has moved
        that file out -- so an older upload of the same name isn't this
        file's, and only the newest row counts."""
        if not filenames:
            return {}
        placeholders = ",".join("?" * len(filenames))
        with self._conn() as db:
            rows = db.execute(
                f"""SELECT filename, user_id FROM uploads u WHERE filename IN ({placeholders})
                    AND deleted_at IS NULL
                    AND created_at = (SELECT MAX(created_at) FROM uploads WHERE filename = u.filename)""",
                tuple(filenames)).fetchall()
        return {r["filename"]: r["user_id"] for r in rows}

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

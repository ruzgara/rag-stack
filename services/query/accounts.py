"""
Users, groups and libraries, over the same SQLite file as the chats.

Authorization is ownership plus a plain access list, not grants and not
roles. A library's owner (libraries.owner_id) has full control of it --
rename, delete, add documents, share -- with no row anywhere saying so. Every
other user or group that should be able to read or write it gets one row in
library_access naming a role, "read" or "write". There is no deny and no
precedence to work out: the highest role across a user's own row and its
groups' wins (library_role). An admin (users.is_admin) can do everything to
every library, also with no row of its own. Chatting needs no permission at
all -- every account may.

There is no session table: identity travels in a signed cookie (see auth.py)
and is checked against the user row on every request, so disabling a user
takes effect immediately. users.session_epoch invalidates a user's cookies
everywhere when it is bumped.
"""

from __future__ import annotations

import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field

from db import LOCAL_USER, ChatDB, NotFound

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LENGTH = 10

ROLES = ("read", "write")


def _escape_like(value: str) -> str:
    """Backslash-escapes SQLite LIKE's own wildcards so a query containing
    literal % or _ doesn't turn into a pattern -- paired with `ESCAPE '\\'`
    on the LIKE itself."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Denied(Exception):
    """The caller is known, but not allowed to do this."""


class Invalid(Exception):
    """The request itself doesn't make sense (bad email, weak password, ...)."""


@dataclass
class User:
    id: str
    email: str
    display_name: str
    is_admin: bool
    disabled: bool
    session_epoch: int
    created_at: float
    last_login_at: float | None = None
    password_hash: str | None = None
    groups: list[str] = field(default_factory=list)

    def public(self) -> dict:
        """The user as the API returns it -- never the hash."""
        return {"id": self.id, "email": self.email, "display_name": self.display_name,
                "is_admin": self.is_admin, "disabled": self.disabled, "groups": self.groups,
                "created_at": self.created_at, "last_login_at": self.last_login_at}


def normalise_email(email: str) -> str:
    cleaned = (email or "").strip().lower()
    if not EMAIL_RE.match(cleaned):
        raise Invalid(f"{email!r} is not an email address")
    return cleaned


def normalise_display_name(display_name: str | None) -> str:
    cleaned = (display_name or "").strip()
    if not cleaned:
        raise Invalid("a name is required")
    return cleaned


class Accounts:
    """User and library storage. Shares ChatDB's connection handling so
    accounts and chats stay in one file, and one transaction where it matters."""

    def __init__(self, db: ChatDB):
        self.db = db

    # ------------------------------------------------------------- reading --

    def _row_to_user(self, db: sqlite3.Connection, row: sqlite3.Row) -> User:
        groups = [r["name"] for r in db.execute(
            "SELECT g.name FROM groups g JOIN group_members m ON m.group_id = g.id "
            "WHERE m.user_id = ? ORDER BY g.name", (row["id"],))]
        return User(id=row["id"], email=row["email"], display_name=row["display_name"],
                    is_admin=bool(row["is_admin"]), disabled=bool(row["disabled"]),
                    session_epoch=row["session_epoch"], created_at=row["created_at"],
                    last_login_at=row["last_login_at"], password_hash=row["password_hash"], groups=groups)

    def user(self, user_id: str) -> User:
        with self.db._conn() as db:
            row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if not row:
                raise NotFound(f"no user {user_id}")
            return self._row_to_user(db, row)

    def by_email(self, email: str) -> User | None:
        with self.db._conn() as db:
            row = db.execute("SELECT * FROM users WHERE email = ?", (normalise_email(email),)).fetchone()
            return self._row_to_user(db, row) if row else None

    def users(self) -> list[User]:
        with self.db._conn() as db:
            return [self._row_to_user(db, r) for r in db.execute("SELECT * FROM users ORDER BY email")]

    def search_users(self, query: str, limit: int = 20) -> list[User]:
        """Finds enabled users whose email or display name contains query
        (case-insensitive). % and _ in query are literal, not LIKE
        wildcards -- unescaped, a query of just "_" would LIKE-match every
        single-char-containing value, i.e. everyone."""
        needle = f"%{_escape_like(query.strip().lower())}%"
        with self.db._conn() as db:
            rows = db.execute(
                "SELECT * FROM users WHERE NOT disabled "
                "AND (lower(email) LIKE ? ESCAPE '\\' OR lower(display_name) LIKE ? ESCAPE '\\') "
                "ORDER BY email LIMIT ?", (needle, needle, limit)).fetchall()
            return [self._row_to_user(db, r) for r in rows]

    def any_users(self) -> bool:
        with self.db._conn() as db:
            return db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    # ------------------------------------------------------------- writing --

    def create_user(self, email: str, password_hash: str | None = None, display_name: str | None = None,
                    is_admin: bool = False) -> User:
        """Creates a user. The first admin also adopts anything stored before
        there were accounts, so existing conversations aren't orphaned."""
        email = normalise_email(email)
        display_name = normalise_display_name(display_name)
        user_id = uuid.uuid4().hex
        with self.db._conn() as db:
            if db.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
                raise Invalid(f"{email} already has an account")
            db.execute("INSERT INTO users (id, email, password_hash, display_name, is_admin, created_at) "
                       "VALUES (?, ?, ?, ?, ?, ?)",
                       (user_id, email, password_hash, display_name, int(is_admin), time.time()))
            if is_admin and not db.execute("SELECT 1 FROM users WHERE is_admin = 1 AND id != ?",
                                           (user_id,)).fetchone():
                for table in ("conversations", "messages", "uploads"):
                    db.execute(f"UPDATE {table} SET user_id = ? WHERE user_id = ?", (user_id, LOCAL_USER))
        return self.user(user_id)

    def set_password(self, user_id: str, password_hash: str) -> None:
        """Changes the password and invalidates this user's sessions."""
        with self.db._conn() as db:
            if db.execute("UPDATE users SET password_hash = ?, session_epoch = session_epoch + 1 WHERE id = ?",
                          (password_hash, user_id)).rowcount == 0:
                raise NotFound(f"no user {user_id}")

    def update_user(self, user_id: str, *, display_name: str | None = None, is_admin: bool | None = None,
                    disabled: bool | None = None) -> User:
        if display_name is not None:
            display_name = normalise_display_name(display_name)
        sets, values = [], []
        for column, value in (("display_name", display_name), ("is_admin", is_admin), ("disabled", disabled)):
            if value is not None:
                sets.append(f"{column} = ?")
                values.append(int(value) if isinstance(value, bool) else value)
        if not sets:
            return self.user(user_id)
        # A disabled or demoted user shouldn't keep a live session.
        if disabled or is_admin is False:
            sets.append("session_epoch = session_epoch + 1")
        with self.db._conn() as db:
            if db.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", (*values, user_id)).rowcount == 0:
                raise NotFound(f"no user {user_id}")
        return self.user(user_id)

    def record_login(self, user_id: str) -> None:
        with self.db._conn() as db:
            db.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (time.time(), user_id))

    def delete_user(self, user_id: str) -> None:
        """Removes the account and its access-list rows. Conversations are
        kept -- they are a record of what the machine answered, and dropping
        them silently would be a surprising thing for an admin action to do.
        Libraries this user owned are kept too, orphaned (owner_id set null
        by the foreign key) rather than deleted."""
        with self.db._conn() as db:
            if db.execute("DELETE FROM users WHERE id = ?", (user_id,)).rowcount == 0:
                raise NotFound(f"no user {user_id}")
            db.execute("DELETE FROM library_access WHERE subject_type = 'user' AND subject_id = ?", (user_id,))

    # -------------------------------------------------------------- groups --

    def create_group(self, name: str, description: str | None = None, kind: str = "locked") -> dict:
        name = (name or "").strip()
        if not name:
            raise Invalid("a group needs a name")
        if kind not in ("open", "locked"):
            raise Invalid(f"a group's type is open or locked, not {kind!r}")
        group = {"id": uuid.uuid4().hex, "name": name, "description": description, "type": kind,
                 "created_at": time.time()}
        with self.db._conn() as db:
            if db.execute("SELECT 1 FROM groups WHERE name = ?", (name,)).fetchone():
                raise Invalid(f"a group called {name!r} already exists")
            db.execute("INSERT INTO groups (id, name, description, type, created_at) VALUES (?, ?, ?, ?, ?)",
                       (group["id"], name, description, kind, group["created_at"]))
        return group

    def groups(self) -> list[dict]:
        with self.db._conn() as db:
            return [{**dict(r), "members": [m["user_id"] for m in db.execute(
                "SELECT user_id FROM group_members WHERE group_id = ?", (r["id"],))]}
                for r in db.execute("SELECT * FROM groups ORDER BY name")]

    def delete_group(self, group_id: str) -> None:
        with self.db._conn() as db:
            if db.execute("DELETE FROM groups WHERE id = ?", (group_id,)).rowcount == 0:
                raise NotFound(f"no group {group_id}")
            db.execute("DELETE FROM library_access WHERE subject_type = 'group' AND subject_id = ?", (group_id,))

    def add_member(self, group_id: str, user_id: str) -> None:
        """The admin path: adds anyone to any group, open or locked, no
        questions asked (PUT /api/admin/groups/.../members/...). See
        join_group for the self-service, open-groups-only version."""
        with self.db._conn() as db:
            try:
                db.execute("INSERT OR IGNORE INTO group_members (group_id, user_id) VALUES (?, ?)",
                           (group_id, user_id))
            except sqlite3.IntegrityError as exc:  # a missing group or user
                raise NotFound(str(exc)) from None

    def remove_member(self, group_id: str, user_id: str) -> None:
        with self.db._conn() as db:
            db.execute("DELETE FROM group_members WHERE group_id = ? AND user_id = ?", (group_id, user_id))

    def join_group(self, group_id: str, user: User) -> None:
        """Self-service: a user may add themselves to an open group. A
        locked group still needs an admin -- add_member, not this."""
        with self.db._conn() as db:
            row = db.execute("SELECT type FROM groups WHERE id = ?", (group_id,)).fetchone()
        if row is None:
            raise NotFound(f"no group {group_id}")
        if row["type"] != "open":
            raise Denied(f"{user.email} can't join a locked group without an admin")
        self.add_member(group_id, user.id)

    def leave_group(self, group_id: str, user: User) -> None:
        """The mirror of join_group: always allowed, open or locked -- a
        group deciding who it lets in doesn't mean it can hold someone in."""
        self.remove_member(group_id, user.id)

    # ----------------------------------------------------------- libraries --

    def create_library(self, name: str, description: str | None = None, owner_id: str | None = None) -> dict:
        """owner_id, if given, is itself the access grant -- library_role
        gives the owner "owner" with no row in library_access needed.

        Name uniqueness is scoped to the owner (NULL owner is its own scope,
        for ragctl/orphaned libraries), not global -- otherwise any user
        could learn a library they can't see exists, or squat a name before
        its rightful owner claimed it, just by trying to create it."""
        name = (name or "").strip()
        if not name:
            raise Invalid("a library needs a name")
        library = {"id": uuid.uuid4().hex, "name": name, "description": description, "owner_id": owner_id,
                  "created_at": time.time()}
        with self.db._conn() as db:
            if db.execute("SELECT 1 FROM libraries WHERE name = ? AND owner_id IS ?", (name, owner_id)).fetchone():
                raise Invalid(f"you already have a library called {name!r}")
            db.execute("INSERT INTO libraries (id, name, description, owner_id, created_at) VALUES (?, ?, ?, ?, ?)",
                       (library["id"], name, description, owner_id, library["created_at"]))
        return library

    def update_library(self, library_id: str, *, name: str | None = None, description: str | None = None) -> dict:
        if name is not None:
            name = name.strip()
            if not name:
                raise Invalid("a library needs a name")
        sets, values = [], []
        if name is not None:
            sets.append("name = ?")
            values.append(name)
        if description is not None:
            sets.append("description = ?")
            values.append(description)
        if sets:
            with self.db._conn() as db:
                if name is not None:
                    owner_row = db.execute("SELECT owner_id FROM libraries WHERE id = ?", (library_id,)).fetchone()
                    if owner_row is None:
                        raise NotFound(f"no library {library_id}")
                    if db.execute(
                            "SELECT 1 FROM libraries WHERE name = ? AND owner_id IS ? AND id != ?",
                            (name, owner_row["owner_id"], library_id)).fetchone():
                        raise Invalid(f"you already have a library called {name!r}")
                if db.execute(f"UPDATE libraries SET {', '.join(sets)} WHERE id = ?",
                              (*values, library_id)).rowcount == 0:
                    raise NotFound(f"no library {library_id}")
        return self.library(library_id)

    def libraries(self) -> list[dict]:
        with self.db._conn() as db:
            return [{**dict(r), "document_count": db.execute(
                "SELECT COUNT(*) FROM library_documents WHERE library_id = ?", (r["id"],)).fetchone()[0]}
                for r in db.execute("SELECT * FROM libraries ORDER BY name")]

    def library(self, library_id: str) -> dict:
        with self.db._conn() as db:
            row = db.execute("SELECT * FROM libraries WHERE id = ?", (library_id,)).fetchone()
            if not row:
                raise NotFound(f"no library {library_id}")
            documents = [r["doc_sha"] for r in db.execute(
                "SELECT doc_sha FROM library_documents WHERE library_id = ? ORDER BY added_at", (library_id,))]
        return {**dict(row), "documents": documents}

    def delete_library(self, library_id: str) -> None:
        """Removes the library. Its access-list rows, document memberships
        and attachments to conversations all cascade -- they name library_id
        as a foreign key with ON DELETE CASCADE."""
        with self.db._conn() as db:
            if db.execute("DELETE FROM libraries WHERE id = ?", (library_id,)).rowcount == 0:
                raise NotFound(f"no library {library_id}")

    def add_document(self, library_id: str, doc_sha: str) -> None:
        with self.db._conn() as db:
            if not db.execute("SELECT 1 FROM libraries WHERE id = ?", (library_id,)).fetchone():
                raise NotFound(f"no library {library_id}")
            db.execute("INSERT OR IGNORE INTO library_documents (library_id, doc_sha, added_at) VALUES (?, ?, ?)",
                       (library_id, doc_sha, time.time()))

    def remove_document(self, library_id: str, doc_sha: str) -> None:
        with self.db._conn() as db:
            db.execute("DELETE FROM library_documents WHERE library_id = ? AND doc_sha = ?", (library_id, doc_sha))

    def libraries_of(self, doc_sha: str) -> list[str]:
        """Which libraries a document belongs to, by its sha256."""
        with self.db._conn() as db:
            return [r["library_id"] for r in db.execute(
                "SELECT library_id FROM library_documents WHERE doc_sha = ?", (doc_sha,))]

    def can_read_document(self, user_id: str, doc_sha: str) -> bool:
        """Whether this user may read one document's content (its chunks),
        as opposed to just knowing it exists via /api/documents: an admin
        always can, everyone else only through read (or write, or
        ownership) on a library the document belongs to. A document in no
        library is unreadable by anyone but an admin, same as document_scope
        leaves it out of every non-admin search."""
        readable = self.readable_libraries(user_id)
        return readable is None or bool(readable & set(self.libraries_of(doc_sha)))

    # --------------------------------------------------------------- access --

    def library_role(self, user_id: str, library_id: str) -> str | None:
        """This user's role on one library: "owner" (they created it, or
        they're an admin), "write", "read", or None. The highest role wins
        across the user's own library_access row and any of its groups'; a
        user can hold "read" through one group and "write" through another
        and the answer is "write"."""
        role, _source = self.library_role_source(user_id, library_id)
        return role

    def library_role_source(self, user_id: str, library_id: str) -> tuple[str | None, str | None]:
        """Like library_role, but also says where it came from: "owner"
        (they created it), "admin" (they only see it because they're an
        admin, not because of any row), "user" (their own library_access
        row) or "group" (only via a group's). GET /api/libraries uses this
        to show a caller why they can see something they didn't create."""
        user = self.user(user_id)
        if user.disabled:
            return None, None
        with self.db._conn() as db:
            lib = db.execute("SELECT owner_id FROM libraries WHERE id = ?", (library_id,)).fetchone()
            if lib is not None and lib["owner_id"] == user_id:
                return "owner", "owner"
            if user.is_admin:
                # Unconditional, even for a library_id that doesn't exist --
                # same as an admin's old blanket "*" grant, so a bad id is
                # still a 404 from the caller rather than a 403 first.
                return "owner", "admin"
            if lib is None:
                return None, None
            own = db.execute(
                "SELECT role FROM library_access WHERE library_id = ? AND subject_type = 'user' AND subject_id = ?",
                (library_id, user_id)).fetchone()
            group_roles = [r["role"] for r in db.execute(
                """SELECT role FROM library_access WHERE library_id = ? AND subject_type = 'group'
                       AND subject_id IN (SELECT group_id FROM group_members WHERE user_id = ?)""",
                (library_id, user_id))]
        own_role = own["role"] if own else None
        group_role = "write" if "write" in group_roles else ("read" if "read" in group_roles else None)
        rank = {None: 0, "read": 1, "write": 2}
        if rank[own_role] == 0 and rank[group_role] == 0:
            return None, None
        return (own_role, "user") if rank[own_role] >= rank[group_role] else (group_role, "group")

    def can_read(self, user_id: str, library_id: str) -> bool:
        return self.library_role(user_id, library_id) is not None

    def can_write(self, user_id: str, library_id: str) -> bool:
        return self.library_role(user_id, library_id) in ("owner", "write")

    def can_manage(self, user_id: str, library_id: str) -> bool:
        """Owner or admin: rename, delete, add/remove documents, and manage
        who else has access."""
        return self.library_role(user_id, library_id) == "owner"

    def library_access(self, library_id: str) -> list[dict]:
        """Who has access to this library beyond its owner -- the library's
        own "who can see this" view, for its sharing screen. Each row's
        `name` is the subject's display name (a user) or its name (a
        group), resolved here so the API and CLI don't each do it."""
        with self.db._conn() as db:
            rows = db.execute(
                "SELECT subject_type, subject_id, role FROM library_access WHERE library_id = ? "
                "ORDER BY subject_type, subject_id", (library_id,)).fetchall()
            out = []
            for r in rows:
                if r["subject_type"] == "user":
                    u = db.execute("SELECT display_name, email FROM users WHERE id = ?",
                                   (r["subject_id"],)).fetchone()
                    name = (u["display_name"] or u["email"]) if u else r["subject_id"]
                else:
                    g = db.execute("SELECT name FROM groups WHERE id = ?", (r["subject_id"],)).fetchone()
                    name = g["name"] if g else r["subject_id"]
                out.append({"subject_type": r["subject_type"], "subject_id": r["subject_id"],
                           "name": name, "role": r["role"]})
        return out

    def set_library_access(self, library_id: str, subject_type: str, subject_id: str, role: str) -> None:
        """Upsert: sharing with someone already on the list changes their
        role rather than adding a second row (the primary key is
        library_id, subject_type, subject_id)."""
        if subject_type not in ("user", "group"):
            raise Invalid(f"a subject is a user or a group, not {subject_type!r}")
        if role not in ROLES:
            raise Invalid(f"a role is read or write, not {role!r}")
        with self.db._conn() as db:
            lib = db.execute("SELECT owner_id FROM libraries WHERE id = ?", (library_id,)).fetchone()
            if lib is None:
                raise NotFound(f"no library {library_id}")
            table = "users" if subject_type == "user" else "groups"
            if not db.execute(f"SELECT 1 FROM {table} WHERE id = ?", (subject_id,)).fetchone():
                raise NotFound(f"no {subject_type} {subject_id}")
            if subject_type == "user" and subject_id == lib["owner_id"]:
                raise Invalid("the owner already has full access")
            db.execute(
                """INSERT INTO library_access (library_id, subject_type, subject_id, role, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (library_id, subject_type, subject_id) DO UPDATE SET role = excluded.role""",
                (library_id, subject_type, subject_id, role, time.time()))

    def remove_library_access(self, library_id: str, subject_type: str, subject_id: str) -> None:
        with self.db._conn() as db:
            db.execute("DELETE FROM library_access WHERE library_id = ? AND subject_type = ? AND subject_id = ?",
                       (library_id, subject_type, subject_id))

    # ---------------------------------------------------------- resolution --

    def readable_libraries(self, user_id: str) -> set[str] | None:
        """Library ids this user can search, or None for all of them (an
        admin). Retrieval turns this into a filter."""
        user = self.user(user_id)
        if user.disabled:
            return set()
        if user.is_admin:
            return None
        with self.db._conn() as db:
            rows = db.execute(
                """SELECT id AS library_id FROM libraries WHERE owner_id = ?
                   UNION
                   SELECT library_id FROM library_access WHERE
                       (subject_type = 'user' AND subject_id = ?) OR
                       (subject_type = 'group' AND subject_id IN
                           (SELECT group_id FROM group_members WHERE user_id = ?))""",
                (user_id, user_id, user_id))
            return {r["library_id"] for r in rows}

    def library_overview(self, user_id: str, selected: list[str] | None = None) -> list[dict]:
        """The libraries this user can read, annotated for the chat model
        with whether each is in scope for the current turn (`in_scope` --
        accessible and, if the caller narrowed to `selected` libraries, one
        of those). Libraries the user can't read are left out entirely --
        the model never learns they exist."""
        readable = self.readable_libraries(user_id)
        selected_set = set(selected) if selected else None
        overview = []
        for lib in self.libraries():
            if readable is not None and lib["id"] not in readable:
                continue
            overview.append({"name": lib["name"], "description": lib["description"],
                             "document_count": lib["document_count"],
                             "in_scope": selected_set is None or lib["id"] in selected_set})
        return overview

    def document_scope(self, user_id: str, selected: list[str] | None = None) -> set[str] | None:
        """Which documents (by sha256) a search may return: None for no
        restriction at all (an admin with nothing selected), otherwise the
        documents in the readable libraries -- narrowed to `selected` (a
        conversation's attached libraries) when it names any, even for an
        admin, else every library the user can read, so a fresh conversation
        with nothing attached isn't silently mute. `selected` is re-checked
        against current access on every call, not trusted from when it was
        attached: losing read access takes effect on the next search, not
        the next attach."""
        readable = self.readable_libraries(user_id)
        if not selected:
            if readable is None:
                return None
            libs = readable
        else:
            libs = set(selected) if readable is None else readable & set(selected)
        if not libs:
            return set()
        with self.db._conn() as db:
            placeholders = ",".join("?" * len(libs))
            return {r["doc_sha"] for r in db.execute(
                f"SELECT DISTINCT doc_sha FROM library_documents WHERE library_id IN ({placeholders})",
                tuple(libs))}

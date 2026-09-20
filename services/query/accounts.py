"""
Users, groups and permissions, over the same SQLite file as the chats.

Permissions are grants, not roles. One row says "this subject (a user or a
group) is allowed, or refused, this permission" -- optionally on one library
rather than globally. Resolving them for a user is a fixed order:

    is_admin              -> everything, no questions
    the user's own grant   -> deny beats allow
    any of its groups'     -> deny beats allow
    nothing                -> refused

so a group can hand a permission to a team and a single user can be lifted
out of it, or held back from it, without touching the group. Which way round
an override goes is up to whoever wrote the grant; there is no rule here that
a user grant is more generous, only that it is more specific.

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

# Global capabilities, and the per-library ones. A grant naming anything else
# is rejected: a typo shouldn't become a permission nobody has.
GLOBAL_PERMISSIONS = {
    "users.manage": "create and edit users, groups and grants",
    "libraries.manage": "create, rename and delete libraries",
    "chat.use": "ask questions at all",
}
LIBRARY_PERMISSIONS = {
    "library.read": "search and cite the documents in a library",
    "library.write": "add documents to a library",
}
PERMISSIONS = {**GLOBAL_PERMISSIONS, **LIBRARY_PERMISSIONS}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LENGTH = 10


class Denied(Exception):
    """The caller is known, but not allowed to do this."""


class Invalid(Exception):
    """The request itself doesn't make sense (bad email, weak password, ...)."""


@dataclass
class User:
    id: str
    email: str
    display_name: str | None
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


def check_permission(permission: str, library_id: str | None) -> None:
    if permission not in PERMISSIONS:
        raise Invalid(f"unknown permission {permission!r}; known: {', '.join(sorted(PERMISSIONS))}")
    if library_id and permission not in LIBRARY_PERMISSIONS:
        raise Invalid(f"{permission!r} is a global permission; it can't be granted on one library")
    if not library_id and permission in LIBRARY_PERMISSIONS:
        raise Invalid(f"{permission!r} has to name a library")


class Accounts:
    """User and permission storage. Shares ChatDB's connection handling so
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

    def any_users(self) -> bool:
        with self.db._conn() as db:
            return db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    # ------------------------------------------------------------- writing --

    def create_user(self, email: str, password_hash: str | None = None, display_name: str | None = None,
                    is_admin: bool = False) -> User:
        """Creates a user. The first admin also adopts anything stored before
        there were accounts, so existing conversations aren't orphaned."""
        email = normalise_email(email)
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
        """Removes the account and its grants. Conversations are kept -- they
        are a record of what the machine answered, and dropping them silently
        would be a surprising thing for an admin action to do."""
        with self.db._conn() as db:
            if db.execute("DELETE FROM users WHERE id = ?", (user_id,)).rowcount == 0:
                raise NotFound(f"no user {user_id}")
            db.execute("DELETE FROM grants WHERE subject_type = 'user' AND subject_id = ?", (user_id,))

    # -------------------------------------------------------------- groups --

    def create_group(self, name: str, description: str | None = None) -> dict:
        name = (name or "").strip()
        if not name:
            raise Invalid("a group needs a name")
        group = {"id": uuid.uuid4().hex, "name": name, "description": description, "created_at": time.time()}
        with self.db._conn() as db:
            if db.execute("SELECT 1 FROM groups WHERE name = ?", (name,)).fetchone():
                raise Invalid(f"a group called {name!r} already exists")
            db.execute("INSERT INTO groups (id, name, description, created_at) VALUES (?, ?, ?, ?)",
                       (group["id"], name, description, group["created_at"]))
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
            db.execute("DELETE FROM grants WHERE subject_type = 'group' AND subject_id = ?", (group_id,))

    def add_member(self, group_id: str, user_id: str) -> None:
        with self.db._conn() as db:
            try:
                db.execute("INSERT OR IGNORE INTO group_members (group_id, user_id) VALUES (?, ?)",
                           (group_id, user_id))
            except sqlite3.IntegrityError as exc:  # a missing group or user
                raise NotFound(str(exc)) from None

    def remove_member(self, group_id: str, user_id: str) -> None:
        with self.db._conn() as db:
            db.execute("DELETE FROM group_members WHERE group_id = ? AND user_id = ?", (group_id, user_id))

    # ----------------------------------------------------------- libraries --

    def create_library(self, name: str, description: str | None = None) -> dict:
        name = (name or "").strip()
        if not name:
            raise Invalid("a library needs a name")
        library = {"id": uuid.uuid4().hex, "name": name, "description": description, "created_at": time.time()}
        with self.db._conn() as db:
            if db.execute("SELECT 1 FROM libraries WHERE name = ?", (name,)).fetchone():
                raise Invalid(f"a library called {name!r} already exists")
            db.execute("INSERT INTO libraries (id, name, description, created_at) VALUES (?, ?, ?, ?)",
                       (library["id"], name, description, library["created_at"]))
        return library

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
        """Removes the library. Its grants, document memberships and
        attachments to conversations all cascade -- they name library_id as a
        foreign key with ON DELETE CASCADE."""
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

    # -------------------------------------------------------------- grants --

    def grant(self, subject_type: str, subject_id: str, permission: str, library_id: str | None = None,
              effect: str = "allow") -> None:
        if subject_type not in ("user", "group"):
            raise Invalid(f"a grant's subject is a user or a group, not {subject_type!r}")
        if effect not in ("allow", "deny"):
            raise Invalid(f"a grant either allows or denies, not {effect!r}")
        check_permission(permission, library_id)
        with self.db._conn() as db:
            table = "users" if subject_type == "user" else "groups"
            if not db.execute(f"SELECT 1 FROM {table} WHERE id = ?", (subject_id,)).fetchone():
                raise NotFound(f"no {subject_type} {subject_id}")
            db.execute(
                """INSERT INTO grants (id, subject_type, subject_id, permission, library_id, effect, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (subject_type, subject_id, permission, IFNULL(library_id, ''))
                   DO UPDATE SET effect = excluded.effect""",
                (uuid.uuid4().hex, subject_type, subject_id, permission, library_id, effect, time.time()))

    def revoke(self, subject_type: str, subject_id: str, permission: str, library_id: str | None = None) -> None:
        """Removes the grant entirely, which is not the same as denying it: the
        subject falls back to whatever its groups say."""
        with self.db._conn() as db:
            db.execute("DELETE FROM grants WHERE subject_type = ? AND subject_id = ? AND permission = ? "
                       "AND library_id IS ?", (subject_type, subject_id, permission, library_id))

    def grants_of(self, subject_type: str, subject_id: str) -> list[dict]:
        with self.db._conn() as db:
            return [dict(r) for r in db.execute(
                "SELECT permission, library_id, effect FROM grants WHERE subject_type = ? AND subject_id = ? "
                "ORDER BY permission, library_id", (subject_type, subject_id))]

    # ---------------------------------------------------------- resolution --

    def effective_permissions(self, user_id: str) -> dict[str, set[str | None]]:
        """Every permission this user has, as {permission: {library_id or None}}.

        One query for the user's own grants and its groups' together; the
        ordering then decides. An admin gets a wildcard, because listing every
        library an admin can read would be a lie by omission the moment one is
        added.
        """
        user = self.user(user_id)
        if user.disabled:
            return {}
        if user.is_admin:
            return {name: {"*"} for name in PERMISSIONS}

        with self.db._conn() as db:
            rows = db.execute(
                """SELECT g.permission, g.library_id, g.effect,
                          CASE g.subject_type WHEN 'user' THEN 1 ELSE 0 END AS own
                   FROM grants g
                   WHERE (g.subject_type = 'user' AND g.subject_id = ?)
                      OR (g.subject_type = 'group' AND g.subject_id IN
                          (SELECT group_id FROM group_members WHERE user_id = ?))""",
                (user_id, user_id)).fetchall()

        # Highest precedence wins: the user's own grants over its groups', and
        # a deny over an allow at the same level.
        decided: dict[tuple[str, str | None], tuple[int, str]] = {}
        for row in rows:
            key = (row["permission"], row["library_id"])
            rank = (2 if row["own"] else 0) + (1 if row["effect"] == "deny" else 0)
            if key not in decided or rank > decided[key][0]:
                decided[key] = (rank, row["effect"])

        out: dict[str, set[str | None]] = {}
        for (permission, library_id), (_, effect) in decided.items():
            if effect == "allow":
                out.setdefault(permission, set()).add(library_id)
        return out

    def allowed(self, user_id: str, permission: str, library_id: str | None = None) -> bool:
        granted = self.effective_permissions(user_id).get(permission, set())
        return bool(granted) if library_id is None else bool({library_id, "*"} & granted)

    def require(self, user: User, permission: str, library_id: str | None = None) -> None:
        if not self.allowed(user.id, permission, library_id):
            where = f" on library {library_id}" if library_id else ""
            raise Denied(f"{user.email} doesn't have {permission}{where}")

    def readable_libraries(self, user_id: str) -> set[str] | None:
        """Library ids this user can search, or None for all of them (an
        admin). Retrieval turns this into a filter."""
        granted = self.effective_permissions(user_id).get("library.read", set())
        return None if "*" in granted else {lib for lib in granted if lib}

    def document_scope(self, user_id: str, selected: list[str] | None = None) -> set[str] | None:
        """Which documents (by sha256) a search may return: None for no
        restriction at all (an admin), otherwise the documents in the
        readable libraries -- narrowed to `selected` (a conversation's
        attached libraries) when it names any, else every library the user
        can read, so a fresh conversation with nothing attached isn't
        silently mute. `selected` is re-checked against current grants on
        every call, not trusted from when it was attached: revoking
        library.read takes effect on the next search, not the next attach."""
        readable = self.readable_libraries(user_id)
        if readable is None:
            return None
        libs = readable if not selected else readable & set(selected)
        if not libs:
            return set()
        with self.db._conn() as db:
            placeholders = ",".join("?" * len(libs))
            return {r["doc_sha"] for r in db.execute(
                f"SELECT DISTINCT doc_sha FROM library_documents WHERE library_id IN ({placeholders})",
                tuple(libs))}

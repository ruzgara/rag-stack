"""
Who is calling: password login today, a trusted proxy header later.

Everything that could go wrong cryptographically is delegated. Passwords are
hashed by pwdlib (argon2id), and the session cookie is signed by Starlette's
SessionMiddleware (itsdangerous). Nothing here invents a scheme.

Identity arrives through one seam -- identify(request) -> Identity | None --
so swapping passwords for SSO later is a change in this file and nowhere else:
permissions, endpoints and storage all work from the User the seam resolves.
Two modes, chosen by QUERY_AUTH_MODE:

  password  (default) a signed cookie set by POST /api/auth/login
  proxy     a header set by an authenticating reverse proxy (tailscale serve,
            oauth2-proxy). Only ever trusted in this mode, and only sound when
            the service is reachable through that proxy alone -- otherwise
            anyone on the network can name themselves in a header.

The cookie holds a user id and the user's session_epoch, and both are checked
against the row on every request, so disabling a user or changing a password
takes effect at once without a session table to clean up.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, Request
from pwdlib import PasswordHash

from accounts import Accounts, Denied, Invalid, MIN_PASSWORD_LENGTH, User, normalise_email

_hasher = PasswordHash.recommended()

# Login attempts per email and per client address, and how long a lockout
# lasts. Enough to make guessing pointless without locking a fumbling user out
# for the afternoon.
MAX_ATTEMPTS = 8
LOCKOUT_SECONDS = 300.0
SESSION_MAX_AGE = 14 * 24 * 3600


def looks_like_email(email: str) -> bool:
    try:
        normalise_email(email)
        return True
    except Invalid:
        return False


def hash_password(password: str) -> str:
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise Invalid(f"a password needs at least {MIN_PASSWORD_LENGTH} characters")
    return _hasher.hash(password)


def verify_password(password: str, stored: str | None) -> tuple[bool, str | None]:
    """Whether the password matches, and a fresh hash when the stored one uses
    outdated parameters (pwdlib decides that, not us)."""
    if not stored:
        return False, None
    return _hasher.verify_and_update(password, stored)


@dataclass
class Identity:
    user_id: str | None = None      # password mode: whom the cookie names
    email: str | None = None        # proxy mode: whom the proxy asserts
    display_name: str | None = None
    epoch: int | None = None
    source: str = "session"


class Throttle:
    """Counts failed logins per key and refuses further tries for a while."""

    def __init__(self, max_attempts: int = MAX_ATTEMPTS, lockout: float = LOCKOUT_SECONDS):
        self.max_attempts, self.lockout = max_attempts, lockout
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}

    def check(self, *keys: str) -> None:
        now = time.monotonic()
        with self._lock:
            for key in keys:
                recent = [t for t in self._failures.get(key, []) if now - t < self.lockout]
                self._failures[key] = recent
                if len(recent) >= self.max_attempts:
                    wait = self.lockout - (now - recent[0])
                    raise HTTPException(429, f"too many failed attempts; try again in {wait / 60:.0f} min",
                                        headers={"Retry-After": str(int(wait))})

    def failed(self, *keys: str) -> None:
        now = time.monotonic()
        with self._lock:
            for key in keys:
                self._failures.setdefault(key, []).append(now)

    def passed(self, *keys: str) -> None:
        with self._lock:
            for key in keys:
                self._failures.pop(key, None)


def session_secret(env: dict, data_dir: Path) -> str:
    """The key the session cookie is signed with. Taken from the environment
    when set; otherwise generated once and kept in data/session.key, so a
    restart doesn't sign everyone out and nobody has to invent a secret to get
    started. Deleting the file signs everyone out."""
    if configured := (env.get("QUERY_SESSION_SECRET") or "").strip():
        return configured
    path = data_dir / "session.key"
    if path.exists():
        return path.read_text().strip()
    secret = secrets.token_urlsafe(48)
    data_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(secret)
    path.chmod(0o600)
    return secret


class Auth:
    """The identity seam, plus login and logout."""

    def __init__(self, accounts: Accounts, env: dict | None = None):
        env = env or {}
        self.accounts = accounts
        self.mode = (env.get("QUERY_AUTH_MODE") or "password").strip().lower()
        self.proxy_header = (env.get("QUERY_PROXY_USER_HEADER") or "X-Forwarded-Email").strip()
        self.proxy_name_header = (env.get("QUERY_PROXY_NAME_HEADER") or "X-Forwarded-User").strip()
        self.throttle = Throttle()
        if self.mode not in ("password", "proxy"):
            raise Invalid(f"QUERY_AUTH_MODE is 'password' or 'proxy', not {self.mode!r}")

    # ------------------------------------------------------------- the seam --

    def identify(self, request: Request) -> Identity | None:
        """Who this request claims to be, without deciding whether they exist."""
        if self.mode == "proxy":
            email = request.headers.get(self.proxy_header, "").strip()
            if not email:
                return None
            return Identity(email=email, display_name=request.headers.get(self.proxy_name_header) or None,
                            source="proxy")
        session = request.session or {}
        if not session.get("user_id"):
            return None
        return Identity(user_id=session["user_id"], epoch=session.get("epoch"), source="session")

    def current_user(self, request: Request) -> User | None:
        """The caller as a stored user, or None. In proxy mode a user seen for
        the first time is created without a password, since the proxy vouches
        for it; in password mode nothing is ever created here."""
        identity = self.identify(request)
        if not identity:
            return None
        if identity.source == "proxy":
            try:
                email = normalise_email(identity.email or "")
            except Invalid:
                return None
            user = self.accounts.by_email(email)
            if not user:
                # The proxy may not send a name header; fall back to the
                # email's local part so account creation never fails on a
                # missing name it has no way to prompt for.
                name = identity.display_name or email.split("@", 1)[0]
                user = self.accounts.create_user(email, display_name=name,
                                                 is_admin=not self.accounts.any_users())
            return None if user.disabled else user
        try:
            user = self.accounts.user(identity.user_id or "")
        except Exception:
            return None
        # A cookie from before a password change or a disabling is stale.
        if user.disabled or identity.epoch != user.session_epoch:
            return None
        return user

    # ---------------------------------------------------------- login flow --

    def login(self, request: Request, email: str, password: str) -> User:
        client = request.client.host if request.client else "unknown"
        keys = (f"email:{(email or '').strip().lower()}", f"addr:{client}")
        self.throttle.check(*keys)
        user = self.accounts.by_email(email) if looks_like_email(email) else None
        ok, updated = verify_password(password, user.password_hash if user else None)
        if not user or not ok or user.disabled:
            self.throttle.failed(*keys)
            # One message for every failure: a different one for "no such
            # account" would tell a stranger which emails have accounts.
            raise HTTPException(401, "wrong email or password")
        self.throttle.passed(*keys)
        if updated:
            self.accounts.set_password(user.id, updated)
            user = self.accounts.user(user.id)
        self.accounts.record_login(user.id)
        request.session.update({"user_id": user.id, "epoch": user.session_epoch})
        return self.accounts.user(user.id)  # re-read, so the reply carries this login

    @staticmethod
    def logout(request: Request) -> None:
        request.session.clear()


# ------------------------------------------------------------- dependencies --

def as_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, Denied):
        return HTTPException(403, str(exc))
    if isinstance(exc, Invalid):
        return HTTPException(400, str(exc))
    return HTTPException(500, str(exc))

"""
Logging in, and what the identity seam accepts. The hashing and the cookie
signing belong to pwdlib and itsdangerous; what's tested here is the policy
around them -- that a stale cookie is refused, that failures don't say which
emails exist, and that a proxy header is only ever trusted in proxy mode.
"""

from __future__ import annotations

import argparse
import io

import pytest
from accounts import Accounts, Invalid
from auth import Auth, Throttle, hash_password, session_secret, verify_password
from db import ChatDB
from fastapi import HTTPException

PASSWORD = "correct horse battery"


@pytest.fixture
def acc(tmp_path):
    return Accounts(ChatDB(tmp_path / "chat.db"))


class FakeRequest:
    """Enough of a Request for the seam: a session dict, headers, a client."""

    def __init__(self, session: dict | None = None, headers: dict | None = None):
        self.session = session if session is not None else {}
        self.headers = headers or {}
        self.client = type("Client", (), {"host": "10.0.0.7"})()


def member(acc: Accounts, email: str = "member@example.com", password: str = PASSWORD, **kw):
    return acc.create_user(email, hash_password(password), **kw)


# --------------------------------------------------------------- passwords --

def test_a_password_round_trips_and_a_wrong_one_does_not():
    stored = hash_password(PASSWORD)
    assert stored.startswith("$argon2id$")  # delegated to pwdlib, not hand-rolled
    assert verify_password(PASSWORD, stored)[0]
    assert not verify_password("wrong", stored)[0]


def test_a_short_password_is_refused():
    with pytest.raises(Invalid, match="at least"):
        hash_password("short")


def test_a_user_without_a_password_can_never_verify():
    assert verify_password("anything", None) == (False, None)


# ------------------------------------------------------------------- login --

def test_logging_in_names_the_user_in_the_session(acc):
    user = member(acc)
    auth = Auth(acc)
    request = FakeRequest()

    signed_in = auth.login(request, "Member@example.com", PASSWORD)

    assert signed_in.id == user.id
    assert request.session == {"user_id": user.id, "epoch": user.session_epoch}
    assert signed_in.last_login_at is not None  # the reply reflects this login


@pytest.mark.parametrize("email, password", [
    ("member@example.com", "wrong"),
    ("nobody@example.com", PASSWORD),
    ("not-an-email", PASSWORD),
])
def test_every_failure_says_the_same_thing(acc, email, password):
    """A distinct message for an unknown account would tell a stranger which
    emails have one."""
    member(acc)
    with pytest.raises(HTTPException) as exc:
        Auth(acc).login(FakeRequest(), email, password)
    assert (exc.value.status_code, exc.value.detail) == (401, "wrong email or password")


def test_a_disabled_user_cannot_log_in(acc):
    user = member(acc)
    acc.update_user(user.id, disabled=True)
    with pytest.raises(HTTPException) as exc:
        Auth(acc).login(FakeRequest(), user.email, PASSWORD)
    assert exc.value.status_code == 401


def test_logging_out_empties_the_session(acc):
    request = FakeRequest({"user_id": "someone", "epoch": 1})
    Auth(acc).logout(request)
    assert request.session == {}


# ---------------------------------------------------------------- sessions --

def test_the_session_cookie_identifies_the_user(acc):
    user = member(acc)
    auth = Auth(acc)
    assert auth.current_user(FakeRequest({"user_id": user.id, "epoch": user.session_epoch})).id == user.id


def test_an_empty_session_is_nobody(acc):
    assert Auth(acc).current_user(FakeRequest()) is None


def test_a_cookie_from_before_a_password_change_is_refused(acc):
    user = member(acc)
    stale = FakeRequest({"user_id": user.id, "epoch": user.session_epoch})
    acc.set_password(user.id, hash_password("a whole new password"))

    assert Auth(acc).current_user(stale) is None


def test_a_cookie_belonging_to_a_disabled_user_is_refused(acc):
    user = member(acc)
    request = FakeRequest({"user_id": user.id, "epoch": user.session_epoch})
    acc.update_user(user.id, disabled=True)

    assert Auth(acc).current_user(request) is None


def test_a_cookie_naming_a_deleted_user_is_refused(acc):
    user = member(acc)
    request = FakeRequest({"user_id": user.id, "epoch": user.session_epoch})
    acc.delete_user(user.id)

    assert Auth(acc).current_user(request) is None


def test_the_signing_key_survives_a_restart(tmp_path):
    first = session_secret({}, tmp_path)
    assert session_secret({}, tmp_path) == first          # same key, so sessions live on
    assert (tmp_path / "session.key").stat().st_mode & 0o777 == 0o600
    assert session_secret({"QUERY_SESSION_SECRET": "from-env"}, tmp_path) == "from-env"


# ------------------------------------------------------------- proxy mode --

def test_a_proxy_header_is_ignored_in_password_mode(acc):
    """Otherwise anyone on the network could name themselves in a header."""
    member(acc, "member@example.com")
    request = FakeRequest(headers={"X-Forwarded-Email": "member@example.com"})

    assert Auth(acc, {"QUERY_AUTH_MODE": "password"}).current_user(request) is None


def test_in_proxy_mode_the_header_identifies_the_user(acc):
    user = member(acc)
    auth = Auth(acc, {"QUERY_AUTH_MODE": "proxy"})

    assert auth.current_user(FakeRequest(headers={"X-Forwarded-Email": "Member@example.com"})).id == user.id


def test_proxy_mode_creates_a_user_it_has_not_seen_before(acc):
    auth = Auth(acc, {"QUERY_AUTH_MODE": "proxy"})

    user = auth.current_user(FakeRequest(headers={"X-Forwarded-Email": "new@example.com",
                                                 "X-Forwarded-User": "New Person"}))

    assert (user.email, user.display_name) == ("new@example.com", "New Person")
    assert user.is_admin  # the first user through the door has to be able to manage the rest
    assert acc.by_email("new@example.com") is not None


def test_proxy_mode_still_refuses_a_disabled_user_and_a_junk_header(acc):
    user = member(acc)
    acc.update_user(user.id, disabled=True)
    auth = Auth(acc, {"QUERY_AUTH_MODE": "proxy"})

    assert auth.current_user(FakeRequest(headers={"X-Forwarded-Email": user.email})) is None
    assert auth.current_user(FakeRequest(headers={"X-Forwarded-Email": "not an email"})) is None
    assert auth.current_user(FakeRequest(headers={})) is None


def test_the_proxy_header_name_is_configurable(acc):
    user = member(acc)
    auth = Auth(acc, {"QUERY_AUTH_MODE": "proxy", "QUERY_PROXY_USER_HEADER": "Tailscale-User-Login"})

    assert auth.current_user(FakeRequest(headers={"Tailscale-User-Login": user.email})).id == user.id


def test_an_unknown_auth_mode_is_refused(acc):
    with pytest.raises(Invalid, match="password"):
        Auth(acc, {"QUERY_AUTH_MODE": "magic"})


# ---------------------------------------------------------------- throttle --

def test_repeated_failures_lock_the_account_out_briefly(acc):
    auth = Auth(acc)
    member(acc)
    for _ in range(auth.throttle.max_attempts):
        with pytest.raises(HTTPException) as exc:
            auth.login(FakeRequest(), "member@example.com", "wrong")
        assert exc.value.status_code == 401

    with pytest.raises(HTTPException) as exc:
        auth.login(FakeRequest(), "member@example.com", PASSWORD)  # even the right password
    assert exc.value.status_code == 429 and "Retry-After" in exc.value.headers


def test_a_successful_login_clears_the_count(acc):
    auth = Auth(acc)
    member(acc)
    for _ in range(auth.throttle.max_attempts - 1):
        with pytest.raises(HTTPException):
            auth.login(FakeRequest(), "member@example.com", "wrong")

    auth.login(FakeRequest(), "member@example.com", PASSWORD)

    with pytest.raises(HTTPException) as exc:  # counted from zero again
        auth.login(FakeRequest(), "member@example.com", "wrong")
    assert exc.value.status_code == 401


def test_the_lockout_expires():
    throttle = Throttle(max_attempts=2, lockout=0.0)
    throttle.failed("email:a", "email:b")
    throttle.failed("email:a")
    throttle.check("email:a")  # the window has already passed, so no exception


# ------------------------------------------------------- the password prompt --
# usercli's prompt guards a correctness trap: piping into getpass echoes the
# password and stores whatever the pipe happened to deliver, which is how an
# account ends up with a password nobody knows.

def test_a_piped_password_is_refused_unless_it_was_asked_for(monkeypatch):
    import usercli

    monkeypatch.setattr(usercli.sys.stdin, "isatty", lambda: False, raising=False)
    with pytest.raises(SystemExit, match="terminal"):
        usercli.ask_password(argparse.Namespace())


def test_password_stdin_reads_exactly_one_line(monkeypatch):
    import usercli

    monkeypatch.setattr(usercli.sys, "stdin", io.StringIO("a good password\nnot this\n"))
    assert usercli.ask_password(argparse.Namespace(password_stdin=True)) == "a good password"


def test_password_stdin_with_nothing_piped_in_is_an_error(monkeypatch):
    import usercli

    monkeypatch.setattr(usercli.sys, "stdin", io.StringIO(""))
    with pytest.raises(SystemExit, match="nothing on stdin"):
        usercli.ask_password(argparse.Namespace(password_stdin=True))


def test_at_a_terminal_it_asks_twice_and_checks_they_match(monkeypatch):
    import usercli

    monkeypatch.setattr(usercli.sys.stdin, "isatty", lambda: True, raising=False)
    answers = iter(["typed once", "typed differently"])
    monkeypatch.setattr(usercli.getpass, "getpass", lambda prompt="": next(answers))
    with pytest.raises(SystemExit, match="don't match"):
        usercli.ask_password(argparse.Namespace())

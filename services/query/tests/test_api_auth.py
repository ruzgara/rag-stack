"""
The two API-level identity paths: the service key standing in for a user, and
endpoints refusing callers they can't identify.
"""

from __future__ import annotations

import asyncio

import api
import pytest
from accounts import Accounts
from auth import Auth
from chat import TurnResult
from db import ChatDB
from fastapi import HTTPException
from limits import Gate, GateConfig


class FakeRequest:
    def __init__(self, headers: dict | None = None, session: dict | None = None):
        self.headers = headers or {}
        self.session = session if session is not None else {}
        self.client = type("Client", (), {"host": "10.0.0.7"})()


@pytest.fixture
def service(tmp_path, monkeypatch):
    db = ChatDB(tmp_path / "chat.db")
    accounts = Accounts(db)
    monkeypatch.setattr(api.state, "db", db, raising=False)
    monkeypatch.setattr(api.state, "accounts", accounts, raising=False)
    monkeypatch.setattr(api.state, "auth", Auth(accounts), raising=False)
    monkeypatch.setattr(api.state, "api_key", "secret-key", raising=False)
    return accounts


def test_an_unidentified_caller_is_refused(service):
    with pytest.raises(HTTPException) as exc:
        api.current_user(FakeRequest())
    assert exc.value.status_code == 401


def test_the_service_key_acts_as_its_own_account(service):
    """Scripts and the OpenAI-compatible endpoint get a user of their own, so
    their conversations and quota are theirs and grants still apply to them."""
    user = api.current_user(FakeRequest({"authorization": "Bearer secret-key"}))

    assert user.email == api.SERVICE_ACCOUNT_EMAIL
    assert service.by_email(api.SERVICE_ACCOUNT_EMAIL) is not None


def test_the_service_account_is_never_an_admin_even_on_an_empty_install(service):
    """Otherwise whoever held the key before anyone ran ragctl user add
    --admin would own the install."""
    assert service.users() == []
    assert api.current_user(FakeRequest({"authorization": "Bearer secret-key"})).is_admin is False


def test_the_service_account_is_reused_not_recreated(service):
    first = api.current_user(FakeRequest({"authorization": "Bearer secret-key"}))
    second = api.current_user(FakeRequest({"authorization": "Bearer secret-key"}))
    assert first.id == second.id
    assert len(service.users()) == 1


def test_a_wrong_or_missing_key_identifies_nobody(service):
    for header in ({}, {"authorization": "Bearer wrong"}, {"authorization": "secret-key"}):
        with pytest.raises(HTTPException):
            api.current_user(FakeRequest(header))
    assert service.users() == []


def test_the_service_key_is_ignored_when_none_is_configured(service, monkeypatch):
    monkeypatch.setattr(api.state, "api_key", "")
    with pytest.raises(HTTPException):
        api.current_user(FakeRequest({"authorization": "Bearer secret-key"}))


def test_whoami_has_no_permissions_key(service):
    """The permissions map went away with grants -- what a caller may do on
    any given library is per library now (GET /api/libraries' `role`), and
    is_admin (on `user`) is the only global fact left."""
    user = service.create_user("member@example.com", "hash", display_name="Member")
    body = api.whoami(user)
    assert set(body) == {"user", "auth_mode"}
    assert "permissions" not in body


def test_the_admin_dependency_refuses_and_allows(service):
    user = service.create_user("member@example.com", "hash", display_name="Member")

    with pytest.raises(HTTPException) as exc:
        api.admin(user)
    assert exc.value.status_code == 403

    admin = service.update_user(user.id, is_admin=True)
    assert api.admin(admin).id == user.id


# --------------------------------------------------------- personal tokens --

def signed_in(user) -> FakeRequest:
    return FakeRequest(session={"user_id": user.id, "epoch": user.session_epoch})


def bearing(token: str) -> FakeRequest:
    return FakeRequest({"authorization": f"Bearer {token}"})


def test_a_personal_token_acts_as_its_owner(service):
    member = service.create_user("member@example.com", "hash", display_name="Member")
    made = api.create_token(api.NewToken(name="script"), signed_in(member), user=member)

    assert api.current_user(bearing(made["token"])).id == member.id


def test_a_bad_token_is_refused_even_alongside_a_valid_session(service):
    """Presenting a credential means to be identified by it -- a dead token
    doesn't quietly fall back to whatever cookie came along."""
    member = service.create_user("member@example.com", "hash", display_name="Member")
    request = signed_in(member)
    request.headers = {"authorization": "Bearer rsk_revoked-or-made-up"}

    with pytest.raises(HTTPException) as exc:
        api.current_user(request)
    assert exc.value.status_code == 401


def test_a_token_is_returned_once_and_listed_without_it(service):
    member = service.create_user("member@example.com", "hash", display_name="Member")
    made = api.create_token(api.NewToken(name="script", expires_in_days=30), signed_in(member), user=member)

    [listed] = api.list_tokens(user=member)["tokens"]
    assert listed["id"] == made["id"] and "token" not in listed
    assert listed["expires_at"] is not None


def test_a_token_cannot_create_another_token(service):
    member = service.create_user("member@example.com", "hash", display_name="Member")
    token = api.create_token(api.NewToken(name="script"), signed_in(member), user=member)["token"]

    with pytest.raises(HTTPException) as exc:
        api.create_token(api.NewToken(name="copy"), bearing(token), user=member)
    assert exc.value.status_code == 403


def test_a_user_only_sees_and_revokes_their_own_tokens(service):
    member = service.create_user("member@example.com", "hash", display_name="Member")
    other = service.create_user("other@example.com", "hash", display_name="Other")
    made = api.create_token(api.NewToken(name="script"), signed_in(member), user=member)

    assert api.list_tokens(user=other)["tokens"] == []
    with pytest.raises(HTTPException) as exc:
        api.revoke_token(made["id"], user=other)
    assert exc.value.status_code == 404

    api.revoke_token(made["id"], user=member)
    with pytest.raises(HTTPException):
        api.current_user(bearing(made["token"]))


def test_a_token_carries_its_users_current_standing_not_a_snapshot(service):
    admin = service.create_user("admin@example.com", "hash", display_name="Admin", is_admin=True)
    token = api.create_token(api.NewToken(name="script"), signed_in(admin), user=admin)["token"]
    assert api.admin(api.current_user(bearing(token))).id == admin.id

    service.update_user(admin.id, is_admin=False)

    with pytest.raises(HTTPException) as exc:
        api.admin(api.current_user(bearing(token)))
    assert exc.value.status_code == 403


def test_a_token_needs_a_name(service):
    member = service.create_user("member@example.com", "hash", display_name="Member")
    with pytest.raises(HTTPException) as exc:
        api.create_token(api.NewToken(name="  "), signed_in(member), user=member)
    assert exc.value.status_code == 400


# ------------------------------------------------- the streaming turn path --
# api.send_message stores the answer from inside its own thread, so the storage
# calls there aren't exercised by the ChatService tests. This drains one turn.

def drain(response) -> list[str]:
    async def collect():
        return [chunk async for chunk in response.body_iterator]

    return asyncio.run(collect())


@pytest.fixture
def chat(service, monkeypatch):
    """A ChatService stand-in whose turn is one token and one done event."""
    result = TurnResult(answer="Two sources agree [1].", model="m", preset="everyday",
                        search_queries=["q"], sources=[], cited=[1], stats={})

    class FakeChat:
        cfg = type("Cfg", (), {"preset": staticmethod(lambda name: type("P", (), {"model": "m",
                                                                                  "name": "everyday"})()),
                               "presets": {"everyday": None}})()

        def run_turn(self, question, history, preset, **kw):
            yield {"type": "token", "text": "Two sources agree [1]."}
            yield {"type": "done", "result": result}

        def generate_title(self, *a, **kw):
            return "A title"

    monkeypatch.setattr(api.state, "chat", FakeChat(), raising=False)
    monkeypatch.setattr(api.state, "gate", Gate(GateConfig()), raising=False)
    return service


def test_a_turn_is_stored_against_the_user_who_asked(chat):
    user = chat.create_user("member@example.com", "hash", display_name="Member")
    conv = api.state.db.create_conversation("everyday", user_id=user.id)

    events = drain(api.send_message(conv["id"], api.NewMessage(content="who pays?"), user))

    assert any(e.startswith("event: done") for e in events), events
    stored = api.state.db.get_conversation(conv["id"], user_id=user.id)
    assert [(m["role"], m["content"]) for m in stored["messages"]] == [
        ("user", "who pays?"), ("assistant", "Two sources agree [1].")]
    assert stored["title"] == "A title"


def test_a_turn_that_breaks_is_stored_as_an_error_for_that_user(chat, monkeypatch):
    user = chat.create_user("member@example.com", "hash", display_name="Member")
    conv = api.state.db.create_conversation("everyday", user_id=user.id)

    def explode(self, question, history, preset, **kw):
        yield {"type": "token", "text": "half an ans"}
        raise RuntimeError("ollama went away")

    monkeypatch.setattr(type(api.state.chat), "run_turn", explode)

    events = drain(api.send_message(conv["id"], api.NewMessage(content="who pays?"), user))

    assert any("ollama went away" in e for e in events)
    answer = api.state.db.get_conversation(conv["id"], user_id=user.id)["messages"][-1]
    assert (answer["status"], answer["content"]) == ("error", "half an ans")


# ------------------------------------------------------------------- retry --

def test_retrying_replaces_the_last_answer_and_keeps_the_question(chat):
    user = chat.create_user("member@example.com", "hash", display_name="Member")
    conv = api.state.db.create_conversation("everyday", user_id=user.id)
    drain(api.send_message(conv["id"], api.NewMessage(content="who pays?"), user))
    first_answer_id = api.state.db.get_conversation(conv["id"], user_id=user.id)["messages"][-1]["id"]

    events = drain(api.retry_message(conv["id"], user))

    assert any(e.startswith("event: done") for e in events), events
    assert not any(e.startswith("event: user") for e in events)  # the question isn't new
    stored = api.state.db.get_conversation(conv["id"], user_id=user.id)
    assert [(m["role"], m["content"]) for m in stored["messages"]] == [
        ("user", "who pays?"), ("assistant", "Two sources agree [1].")]
    assert stored["messages"][-1]["id"] != first_answer_id  # a fresh answer, not the old one


def test_retrying_a_chat_with_nothing_to_retry_is_404(chat):
    user = chat.create_user("member@example.com", "hash", display_name="Member")
    conv = api.state.db.create_conversation("everyday", user_id=user.id)  # no messages yet

    with pytest.raises(HTTPException) as exc:
        api.retry_message(conv["id"], user)
    assert exc.value.status_code == 404


def test_a_failed_turn_can_be_retried(chat, monkeypatch):
    user = chat.create_user("member@example.com", "hash", display_name="Member")
    conv = api.state.db.create_conversation("everyday", user_id=user.id)

    def explode(self, question, history, preset, **kw):
        raise RuntimeError("ollama went away")

    monkeypatch.setattr(type(api.state.chat), "run_turn", explode)
    drain(api.send_message(conv["id"], api.NewMessage(content="who pays?"), user))
    assert api.state.db.get_conversation(conv["id"], user_id=user.id)["messages"][-1]["status"] == "error"

    monkeypatch.setattr(type(api.state.chat), "run_turn", chat_fixture_run_turn)
    events = drain(api.retry_message(conv["id"], user))

    assert any(e.startswith("event: done") for e in events), events
    stored = api.state.db.get_conversation(conv["id"], user_id=user.id)
    assert [(m["role"], m["content"]) for m in stored["messages"]] == [
        ("user", "who pays?"), ("assistant", "Two sources agree [1].")]
    assert stored["messages"][-1]["status"] == "complete"


def chat_fixture_run_turn(self, question, history, preset, **kw):
    yield {"type": "token", "text": "Two sources agree [1]."}
    yield {"type": "done", "result": TurnResult(answer="Two sources agree [1].", model="m", preset="everyday",
                                                search_queries=["q"], sources=[], cited=[1], stats={})}

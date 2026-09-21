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


def test_a_permission_dependency_refuses_and_allows(service):
    user = service.create_user("member@example.com", "hash", display_name="Member")
    guard = api.requires("chat.use")

    with pytest.raises(HTTPException) as exc:
        guard(user)
    assert exc.value.status_code == 403

    service.grant("user", user.id, "chat.use")
    assert guard(user).id == user.id


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
    chat.grant("user", user.id, "chat.use")
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

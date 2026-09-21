"""
Deleting, restoring, archiving and unarchiving a chat: soft delete via the
API, same pattern as workspaces (see test_api_workspaces.py). Endpoint
functions are called directly, like test_api_libraries.py.
"""

from __future__ import annotations

import api
import pytest
from accounts import Accounts
from db import ChatDB
from fastapi import HTTPException


@pytest.fixture
def acc(tmp_path, monkeypatch):
    db = ChatDB(tmp_path / "chat.db")
    accounts = Accounts(db)
    monkeypatch.setattr(api.state, "db", db, raising=False)
    monkeypatch.setattr(api.state, "accounts", accounts, raising=False)
    fake_chat = type("Chat", (), {"cfg": type("Cfg", (), {
        "preset": staticmethod(lambda name: type("P", (), {"name": name or "everyday"})())})()})()
    monkeypatch.setattr(api.state, "chat", fake_chat, raising=False)
    return accounts


@pytest.fixture
def member(acc):
    user = acc.create_user("member@example.com")
    acc.grant("user", user.id, "chat.use")
    return user


@pytest.fixture
def other_member(acc):
    user = acc.create_user("other@example.com")
    acc.grant("user", user.id, "chat.use")
    return user


def test_deleting_a_chat_moves_it_to_the_deleted_list(member):
    conv = api.create_conversation(api.NewConversation(preset="everyday"), user=member)

    api.delete_conversation(conv["id"], user=member)

    assert api.list_conversations(user=member) == []
    assert [c["id"] for c in api.list_conversations(deleted=True, user=member)] == [conv["id"]]
    with pytest.raises(HTTPException) as exc:
        api.get_conversation(conv["id"], user=member)
    assert exc.value.status_code == 404


def test_restoring_a_deleted_chat_brings_it_back(member):
    conv = api.create_conversation(api.NewConversation(preset="everyday"), user=member)
    api.delete_conversation(conv["id"], user=member)

    restored = api.restore_conversation(conv["id"], user=member)

    assert restored["id"] == conv["id"]
    assert [c["id"] for c in api.list_conversations(user=member)] == [conv["id"]]
    assert api.list_conversations(deleted=True, user=member) == []


def test_restoring_a_chat_that_isnt_deleted_is_404(member):
    conv = api.create_conversation(api.NewConversation(preset="everyday"), user=member)

    with pytest.raises(HTTPException) as exc:
        api.restore_conversation(conv["id"], user=member)
    assert exc.value.status_code == 404


def test_a_user_cannot_delete_or_restore_someone_elses_chat(member, other_member):
    conv = api.create_conversation(api.NewConversation(preset="everyday"), user=member)

    with pytest.raises(HTTPException) as exc:
        api.delete_conversation(conv["id"], user=other_member)
    assert exc.value.status_code == 404

    api.delete_conversation(conv["id"], user=member)
    with pytest.raises(HTTPException) as exc:
        api.restore_conversation(conv["id"], user=other_member)
    assert exc.value.status_code == 404


# ------------------------------------------------------------------ archive --

def test_archiving_a_chat_moves_it_to_the_archived_list_but_it_stays_open(member):
    conv = api.create_conversation(api.NewConversation(preset="everyday"), user=member)

    api.archive_conversation(conv["id"], user=member)

    assert api.list_conversations(user=member) == []
    assert [c["id"] for c in api.list_conversations(archived=True, user=member)] == [conv["id"]]
    assert api.get_conversation(conv["id"], user=member)["id"] == conv["id"]  # still open-able


def test_unarchiving_a_chat_brings_it_back_to_the_normal_list(member):
    conv = api.create_conversation(api.NewConversation(preset="everyday"), user=member)
    api.archive_conversation(conv["id"], user=member)

    api.unarchive_conversation(conv["id"], user=member)

    assert [c["id"] for c in api.list_conversations(user=member)] == [conv["id"]]
    assert api.list_conversations(archived=True, user=member) == []


def test_a_deleted_chat_cannot_be_archived_via_the_api(member):
    conv = api.create_conversation(api.NewConversation(preset="everyday"), user=member)
    api.delete_conversation(conv["id"], user=member)

    with pytest.raises(HTTPException) as exc:
        api.archive_conversation(conv["id"], user=member)
    assert exc.value.status_code == 404


def test_a_user_cannot_archive_or_unarchive_someone_elses_chat(member, other_member):
    conv = api.create_conversation(api.NewConversation(preset="everyday"), user=member)

    with pytest.raises(HTTPException) as exc:
        api.archive_conversation(conv["id"], user=other_member)
    assert exc.value.status_code == 404

    api.archive_conversation(conv["id"], user=member)
    with pytest.raises(HTTPException) as exc:
        api.unarchive_conversation(conv["id"], user=other_member)
    assert exc.value.status_code == 404

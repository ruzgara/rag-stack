"""
The library endpoints: admin CRUD, document assignment, and the checks that
keep a conversation from attaching a library its owner can't read. Endpoint
functions are called directly, like test_api_uploads.py and test_api_auth.py.
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
    # create_conversation/update_conversation check the preset name against
    # this; nothing here runs an actual turn.
    fake_chat = type("Chat", (), {"cfg": type("Cfg", (), {
        "preset": staticmethod(lambda name: type("P", (), {"name": name or "everyday"})())})()})()
    monkeypatch.setattr(api.state, "chat", fake_chat, raising=False)
    return accounts


@pytest.fixture
def admin(acc):
    return acc.create_user("admin@example.com", is_admin=True)


@pytest.fixture
def member(acc):
    user = acc.create_user("member@example.com")
    acc.grant("user", user.id, "chat.use")
    return user


# ----------------------------------------------------------- admin library CRUD --

def test_an_admin_creates_lists_and_deletes_a_library(admin):
    lib = api.admin_create_library(api.NewLibrary(name="Project finance"), admin=admin)
    assert api.admin_list_libraries(admin=admin)["libraries"] == [{**lib, "document_count": 0}]

    api.admin_delete_library(lib["id"], admin=admin)
    assert api.admin_list_libraries(admin=admin)["libraries"] == []


def test_deleting_an_unknown_library_is_404(admin):
    with pytest.raises(HTTPException) as exc:
        api.admin_delete_library("no-such-library", admin=admin)
    assert exc.value.status_code == 404


def test_a_non_admin_cannot_manage_libraries(acc, member):
    with pytest.raises(HTTPException) as exc:
        api.requires("libraries.manage")(member)
    assert exc.value.status_code == 403


def test_documents_can_be_assigned_to_and_removed_from_a_library(admin):
    lib = api.admin_create_library(api.NewLibrary(name="Project finance"), admin=admin)

    api.admin_add_document(lib["id"], "sha-1", admin=admin)
    assert api.admin_list_libraries(admin=admin)["libraries"][0]["document_count"] == 1

    api.admin_remove_document(lib["id"], "sha-1", admin=admin)
    assert api.admin_list_libraries(admin=admin)["libraries"][0]["document_count"] == 0


# --------------------------------------------------------- listing for a user --

def test_an_admin_sees_every_library_a_member_sees_only_readable_ones(acc, admin, member):
    readable = api.admin_create_library(api.NewLibrary(name="Readable"), admin=admin)
    other = api.admin_create_library(api.NewLibrary(name="Other"), admin=admin)
    acc.grant("user", member.id, "library.read", readable["id"])

    assert {l["id"] for l in api.list_libraries(user=admin)["libraries"]} == {readable["id"], other["id"]}
    assert {l["id"] for l in api.list_libraries(user=member)["libraries"]} == {readable["id"]}


# -------------------------------------------------- attaching to a conversation --

def test_a_conversation_can_only_attach_a_readable_library(acc, member):
    lib = acc.create_library("Off limits")

    with pytest.raises(HTTPException) as exc:
        api.create_conversation(api.NewConversation(preset="everyday", library_ids=[lib["id"]]), user=member)
    assert exc.value.status_code == 403


def test_a_conversation_cannot_attach_an_unknown_library(member):
    with pytest.raises(HTTPException) as exc:
        api.create_conversation(api.NewConversation(preset="everyday", library_ids=["nope"]), user=member)
    assert exc.value.status_code == 404


def test_a_conversation_attaches_a_readable_library(acc, member):
    lib = acc.create_library("Project finance")
    acc.grant("user", member.id, "library.read", lib["id"])

    conv = api.create_conversation(api.NewConversation(preset="everyday", library_ids=[lib["id"]]), user=member)

    assert conv["library_ids"] == [lib["id"]]
    assert api.get_conversation(conv["id"], user=member)["library_ids"] == [lib["id"]]


def test_updating_a_conversations_libraries_is_checked_the_same_way(acc, member):
    conv = api.create_conversation(api.NewConversation(preset="everyday"), user=member)
    lib = acc.create_library("Off limits")

    with pytest.raises(HTTPException) as exc:
        api.update_conversation(conv["id"], api.ConversationUpdate(library_ids=[lib["id"]]), user=member)
    assert exc.value.status_code == 403

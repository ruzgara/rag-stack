"""
The workspace endpoints: CRUD for a user's own workspaces, and how they
interact with conversations and libraries. Endpoint functions are called
directly, like test_api_libraries.py.
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
    return acc.create_user("member@example.com", display_name="Member")


@pytest.fixture
def other_member(acc):
    return acc.create_user("other@example.com", display_name="Other")


# --------------------------------------------------------------------- CRUD --

def test_a_user_creates_lists_gets_and_deletes_their_own_workspace(member):
    starter = [w["id"] for w in api.list_workspaces(user=member)["workspaces"]]  # see Accounts.create_user
    ws = api.create_workspace(api.NewWorkspace(name="Research"), user=member)
    assert api.list_workspaces(user=member)["workspaces"][0]["id"] == ws["id"]
    assert api.get_workspace(ws["id"], user=member)["name"] == "Research"

    api.delete_workspace(ws["id"], user=member)
    assert [w["id"] for w in api.list_workspaces(user=member)["workspaces"]] == starter


def test_a_workspace_needs_a_name(member):
    with pytest.raises(HTTPException) as exc:
        api.create_workspace(api.NewWorkspace(name="   "), user=member)
    assert exc.value.status_code == 422


def test_updating_a_workspace_rejects_a_blank_name(member):
    ws = api.create_workspace(api.NewWorkspace(name="Research"), user=member)
    with pytest.raises(HTTPException) as exc:
        api.update_workspace(ws["id"], api.WorkspaceUpdate(name="  "), user=member)
    assert exc.value.status_code == 422


def test_a_user_cannot_see_or_touch_another_users_workspace(member, other_member):
    ws = api.create_workspace(api.NewWorkspace(name="Research"), user=member)

    for call in (lambda: api.get_workspace(ws["id"], user=other_member),
                lambda: api.update_workspace(ws["id"], api.WorkspaceUpdate(name="Renamed"), user=other_member),
                lambda: api.delete_workspace(ws["id"], user=other_member)):
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 404


def test_deleting_an_unknown_workspace_is_404(member):
    with pytest.raises(HTTPException) as exc:
        api.delete_workspace("no-such-workspace", user=member)
    assert exc.value.status_code == 404


def test_a_deleted_workspace_can_be_restored(member):
    starter = [w["id"] for w in api.list_workspaces(user=member)["workspaces"]]  # see Accounts.create_user
    ws = api.create_workspace(api.NewWorkspace(name="Research"), user=member)
    api.delete_workspace(ws["id"], user=member)
    assert [w["id"] for w in api.list_workspaces(user=member)["workspaces"]] == starter

    restored = api.restore_workspace(ws["id"], user=member)

    assert restored["id"] == ws["id"]
    assert [w["id"] for w in api.list_workspaces(user=member)["workspaces"]] == [ws["id"], *starter]
    assert api.list_workspaces(deleted=True, user=member)["workspaces"] == []


def test_restoring_a_workspace_that_isnt_deleted_is_404(member):
    ws = api.create_workspace(api.NewWorkspace(name="Research"), user=member)
    with pytest.raises(HTTPException) as exc:
        api.restore_workspace(ws["id"], user=member)
    assert exc.value.status_code == 404


def test_a_user_cannot_restore_someone_elses_workspace(member, other_member):
    ws = api.create_workspace(api.NewWorkspace(name="Research"), user=member)
    api.delete_workspace(ws["id"], user=member)

    with pytest.raises(HTTPException) as exc:
        api.restore_workspace(ws["id"], user=other_member)
    assert exc.value.status_code == 404


# ------------------------------------------------------------- attaching --

def test_a_workspace_can_only_attach_a_readable_library(acc, member):
    lib = acc.create_library("Off limits")
    ws = api.create_workspace(api.NewWorkspace(name="Research"), user=member)

    with pytest.raises(HTTPException) as exc:
        api.update_workspace(ws["id"], api.WorkspaceUpdate(library_ids=[lib["id"]]), user=member)
    assert exc.value.status_code == 403


def test_a_workspace_attaches_a_readable_library(acc, member):
    lib = acc.create_library("Project finance")
    acc.set_library_access(lib["id"], "user", member.id, "read")
    ws = api.create_workspace(api.NewWorkspace(name="Research"), user=member)

    updated = api.update_workspace(ws["id"], api.WorkspaceUpdate(library_ids=[lib["id"]]), user=member)

    assert updated["library_ids"] == [lib["id"]]


# ------------------------------------------------------------ conversations --

def test_a_conversation_can_be_created_inside_a_workspace(member):
    ws = api.create_workspace(api.NewWorkspace(name="Research"), user=member)

    conv = api.create_conversation(api.NewConversation(preset="everyday", workspace_id=ws["id"]), user=member)

    assert conv["workspace_id"] == ws["id"]
    assert [c["id"] for c in api.list_workspace_conversations(ws["id"], user=member)] == [conv["id"]]


def test_a_library_added_to_a_workspace_reaches_its_existing_chats(acc, member):
    lib = acc.create_library("Project finance")
    acc.set_library_access(lib["id"], "user", member.id, "read")
    ws = api.create_workspace(api.NewWorkspace(name="Research"), user=member)
    conv = api.create_conversation(api.NewConversation(preset="everyday", workspace_id=ws["id"]), user=member)
    assert conv["library_ids"] == []

    api.update_workspace(ws["id"], api.WorkspaceUpdate(library_ids=[lib["id"]]), user=member)

    assert api.get_conversation(conv["id"], user=member)["library_ids"] == [lib["id"]]


def test_a_chat_in_a_workspace_rejects_its_own_libraries(acc, member):
    lib = acc.create_library("Project finance")
    acc.set_library_access(lib["id"], "user", member.id, "read")
    ws = api.create_workspace(api.NewWorkspace(name="Research"), user=member)

    with pytest.raises(HTTPException) as exc:
        api.create_conversation(api.NewConversation(preset="everyday", workspace_id=ws["id"],
                                                    library_ids=[lib["id"]]), user=member)
    assert exc.value.status_code == 422

    conv = api.create_conversation(api.NewConversation(preset="everyday", workspace_id=ws["id"]), user=member)
    with pytest.raises(HTTPException) as exc:
        api.update_conversation(conv["id"], api.ConversationUpdate(library_ids=[lib["id"]]), user=member)
    assert exc.value.status_code == 422


def test_a_conversation_cannot_be_created_in_someone_elses_workspace(member, other_member):
    ws = api.create_workspace(api.NewWorkspace(name="Research"), user=member)

    with pytest.raises(HTTPException) as exc:
        api.create_conversation(api.NewConversation(preset="everyday", workspace_id=ws["id"]), user=other_member)
    assert exc.value.status_code == 404


def test_listing_conversations_of_an_unknown_workspace_is_404(member):
    with pytest.raises(HTTPException) as exc:
        api.list_workspace_conversations("no-such-workspace", user=member)
    assert exc.value.status_code == 404


def test_deleting_a_workspace_deletes_its_conversations(member):
    ws = api.create_workspace(api.NewWorkspace(name="Research"), user=member)
    conv = api.create_conversation(api.NewConversation(preset="everyday", workspace_id=ws["id"]), user=member)

    api.delete_workspace(ws["id"], user=member)

    with pytest.raises(HTTPException) as exc:
        api.get_conversation(conv["id"], user=member)
    assert exc.value.status_code == 404


def test_restoring_a_workspace_makes_its_conversations_reachable_again(member):
    ws = api.create_workspace(api.NewWorkspace(name="Research"), user=member)
    conv = api.create_conversation(api.NewConversation(preset="everyday", workspace_id=ws["id"]), user=member)
    api.delete_workspace(ws["id"], user=member)

    api.restore_workspace(ws["id"], user=member)

    assert api.get_conversation(conv["id"], user=member)["id"] == conv["id"]
    assert [c["id"] for c in api.list_workspace_conversations(ws["id"], user=member)] == [conv["id"]]

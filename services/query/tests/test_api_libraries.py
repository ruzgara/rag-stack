"""
The library endpoints: admin CRUD, document assignment, and the checks that
keep a conversation from attaching a library its owner can't read. Endpoint
functions are called directly, like test_api_uploads.py and test_api_auth.py.
"""

from __future__ import annotations

import api
import pytest
from accounts import Accounts
from conftest import SEARCH_ENV, FakeQdrant, point
from db import ChatDB
from fastapi import HTTPException
from retrieval import Retriever, SearchConfig


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
    return acc.create_user("admin@example.com", display_name="Admin", is_admin=True)


@pytest.fixture
def member(acc):
    user = acc.create_user("member@example.com", display_name="Member")
    acc.grant("user", user.id, "chat.use")
    return user


# ----------------------------------------------------------- admin library CRUD --

def test_an_admin_creates_lists_and_deletes_a_library(admin):
    lib = api.admin_create_library(api.NewLibrary(name="Project finance"), admin=admin)
    assert api.admin_list_libraries(admin=admin)["libraries"] == [{**lib, "document_count": 0}]

    api.admin_delete_library(lib["id"], admin=admin)
    assert api.admin_list_libraries(admin=admin)["libraries"] == []


def test_a_library_created_via_the_api_is_owned_by_the_creating_admin(admin):
    lib = api.admin_create_library(api.NewLibrary(name="Project finance"), admin=admin)
    assert lib["owner_id"] == admin.id


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


def test_a_librarys_grants_are_listed_the_other_way_round(acc, admin, member):
    lib = api.admin_create_library(api.NewLibrary(name="Project finance"), admin=admin)
    assert api.admin_library_grants(lib["id"], admin=admin)["grants"] == []

    acc.grant("user", member.id, "library.read", lib["id"])

    assert api.admin_library_grants(lib["id"], admin=admin)["grants"] == [
        {"subject_type": "user", "subject_id": member.id, "permission": "library.read", "effect": "allow"}]


# --------------------------------------------------------- self-service creation --

def test_a_plain_member_can_create_a_library(acc, member):
    lib = api.create_library(api.NewLibrary(name="Project finance", description="filings"), user=member)

    assert lib["owner_id"] == member.id
    assert lib["description"] == "filings"


def test_a_member_created_library_appears_in_their_own_list_and_is_usable(acc, member):
    lib = api.create_library(api.NewLibrary(name="Project finance"), user=member)

    assert {l["id"] for l in api.list_libraries(user=member)["libraries"]} == {lib["id"]}

    detail = api.library_detail(lib["id"], user=member)
    assert detail["id"] == lib["id"]

    assert acc.allowed(member.id, "library.write", lib["id"])
    assert acc.allowed(member.id, "library.share", lib["id"])

    target = acc.create_user("target@example.com", display_name="Target")
    api.share_library(lib["id"], api.ShareRequest(subject_type="user", subject_id=target.id,
                                                   permission="library.read"), user=member)
    assert acc.allowed(target.id, "library.read", lib["id"])


def test_another_user_cannot_see_a_library_they_did_not_create_or_receive(acc, member):
    api.create_library(api.NewLibrary(name="Project finance"), user=member)
    other = acc.create_user("other@example.com", display_name="Other")

    assert api.list_libraries(user=other)["libraries"] == []
    with pytest.raises(HTTPException) as exc:
        api.library_detail((acc.libraries()[0])["id"], user=other)
    assert exc.value.status_code == 403


def test_a_blank_library_name_is_rejected_for_self_service_creation(member):
    with pytest.raises(HTTPException) as exc:
        api.create_library(api.NewLibrary(name="   "), user=member)
    assert exc.value.status_code == 400


def test_the_admin_endpoint_still_grants_nothing_automatically(acc, admin):
    lib = api.admin_create_library(api.NewLibrary(name="Project finance"), admin=admin)
    assert acc.grants_of("user", admin.id) == []
    assert api.list_libraries(user=admin)["libraries"][0]["id"] == lib["id"]  # sees it only as an admin


# --------------------------------------------------------- listing for a user --

def test_an_admin_sees_every_library_a_member_sees_only_readable_ones(acc, admin, member):
    readable = api.admin_create_library(api.NewLibrary(name="Readable"), admin=admin)
    other = api.admin_create_library(api.NewLibrary(name="Other"), admin=admin)
    acc.grant("user", member.id, "library.read", readable["id"])

    assert {l["id"] for l in api.list_libraries(user=admin)["libraries"]} == {readable["id"], other["id"]}
    assert {l["id"] for l in api.list_libraries(user=member)["libraries"]} == {readable["id"]}


# ------------------------------------------------------------- library detail --

def test_a_member_with_library_read_sees_its_documents(acc, admin, member, tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        '{"sha256": "sha-1", "filename": "10-K.pdf", "status": "processed", "route": "docling", '
        '"pages": 42, "chars": 90000, "timestamp": 100}\n')
    monkeypatch.setattr(api, "MANIFEST", manifest)
    lib = api.admin_create_library(api.NewLibrary(name="Project finance"), admin=admin)
    api.admin_add_document(lib["id"], "sha-1", admin=admin)
    acc.grant("user", member.id, "library.read", lib["id"])

    detail = api.library_detail(lib["id"], user=member)

    assert detail["document_count"] == 1
    assert detail["documents"] == [{"sha256": "sha-1", "filename": "10-K.pdf", "status": "processed",
                                    "route": "docling", "pages": 42, "chars": 90000, "error": None,
                                    "processed_at": 100}]


def test_a_member_without_library_read_cannot_see_its_detail(acc, admin, member):
    lib = api.admin_create_library(api.NewLibrary(name="Project finance"), admin=admin)

    with pytest.raises(HTTPException) as exc:
        api.library_detail(lib["id"], user=member)
    assert exc.value.status_code == 403


def test_an_unknown_librarys_detail_is_404(member):
    with pytest.raises(HTTPException) as exc:
        api.library_detail("no-such-library", user=member)
    assert exc.value.status_code == 404


# ------------------------------------------------------------- self-service share --

def test_a_user_with_share_and_read_can_share_read_with_another(acc, admin, member):
    lib = api.admin_create_library(api.NewLibrary(name="Project finance"), admin=admin)
    target = acc.create_user("target@example.com", display_name="Target")
    acc.grant("user", member.id, "library.share", lib["id"])
    acc.grant("user", member.id, "library.read", lib["id"])

    api.share_library(lib["id"], api.ShareRequest(subject_type="user", subject_id=target.id,
                                                   permission="library.read"), user=member)

    assert acc.grants_of("user", target.id) == [
        {"permission": "library.read", "library_id": lib["id"], "effect": "allow"}]


def test_sharing_without_library_share_is_403(acc, admin, member):
    lib = api.admin_create_library(api.NewLibrary(name="Project finance"), admin=admin)
    target = acc.create_user("target@example.com", display_name="Target")
    acc.grant("user", member.id, "library.read", lib["id"])  # no library.share

    with pytest.raises(HTTPException) as exc:
        api.share_library(lib["id"], api.ShareRequest(subject_type="user", subject_id=target.id,
                                                       permission="library.read"), user=member)
    assert exc.value.status_code == 403


def test_sharing_more_than_you_hold_is_403(acc, admin, member):
    lib = api.admin_create_library(api.NewLibrary(name="Project finance"), admin=admin)
    target = acc.create_user("target@example.com", display_name="Target")
    acc.grant("user", member.id, "library.share", lib["id"])  # no library.write

    with pytest.raises(HTTPException) as exc:
        api.share_library(lib["id"], api.ShareRequest(subject_type="user", subject_id=target.id,
                                                       permission="library.write"), user=member)
    assert exc.value.status_code == 403


def test_sharing_library_share_itself_is_rejected(acc, admin, member):
    lib = api.admin_create_library(api.NewLibrary(name="Project finance"), admin=admin)
    target = acc.create_user("target@example.com", display_name="Target")
    acc.grant("user", member.id, "library.share", lib["id"])
    acc.grant("user", member.id, "library.read", lib["id"])

    with pytest.raises(HTTPException) as exc:
        api.share_library(lib["id"], api.ShareRequest(subject_type="user", subject_id=target.id,
                                                       permission="library.share"), user=member)
    assert exc.value.status_code == 400


def test_unsharing_removes_access(acc, admin, member):
    lib = api.admin_create_library(api.NewLibrary(name="Project finance"), admin=admin)
    target = acc.create_user("target@example.com", display_name="Target")
    acc.grant("user", member.id, "library.share", lib["id"])
    acc.grant("user", member.id, "library.read", lib["id"])
    api.share_library(lib["id"], api.ShareRequest(subject_type="user", subject_id=target.id,
                                                   permission="library.read"), user=member)

    api.unshare_library(lib["id"], api.ShareRequest(subject_type="user", subject_id=target.id,
                                                     permission="library.read"), user=member)

    assert acc.grants_of("user", target.id) == []


def test_a_user_who_can_share_sees_who_has_access(acc, admin, member):
    lib = api.admin_create_library(api.NewLibrary(name="Project finance"), admin=admin)
    acc.grant("user", member.id, "library.share", lib["id"])
    acc.grant("user", member.id, "library.read", lib["id"])

    grants = api.library_grants(lib["id"], user=member)["grants"]

    assert {(g["subject_type"], g["subject_id"], g["permission"]) for g in grants} == {
        ("user", member.id, "library.share"), ("user", member.id, "library.read")}


def test_a_user_without_library_share_cannot_list_grants(acc, admin, member):
    lib = api.admin_create_library(api.NewLibrary(name="Project finance"), admin=admin)
    acc.grant("user", member.id, "library.read", lib["id"])

    with pytest.raises(HTTPException) as exc:
        api.library_grants(lib["id"], user=member)
    assert exc.value.status_code == 403


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


# ---------------------------------------------------------- document chunks --

@pytest.fixture
def qdrant(monkeypatch):
    """A real Retriever over an in-memory Qdrant, wired into api.state."""
    fake = FakeQdrant([])
    cfg = SearchConfig.from_env({**SEARCH_ENV, "OLLAMA_PORT": "1", "QDRANT_PORT": "1", "RAG_RERANK": "false"})
    retriever = Retriever(cfg, client=fake.client())
    monkeypatch.setattr(api.state, "retriever", retriever, raising=False)
    return fake


def test_a_member_with_library_read_can_read_a_documents_chunks(acc, member, qdrant, tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text('{"sha256": "sha-1", "filename": "10-K.pdf", "status": "processed"}\n')
    monkeypatch.setattr(api, "MANIFEST", manifest)
    lib = acc.create_library("Project finance")
    acc.add_document(lib["id"], "sha-1")
    acc.grant("user", member.id, "library.read", lib["id"])
    qdrant.points = [
        point("p2", 1, ["Loans"], "second chunk", doc_sha="sha-1"),
        point("p1", 0, ["Intro"], "first chunk", doc_sha="sha-1"),
        point("other", 0, ["Elsewhere"], "not this doc", doc_sha="sha-2"),
    ]

    result = api.document_chunks("sha-1", user=member)

    assert result["filename"] == "10-K.pdf"
    assert result["total"] == 2
    assert [c["chunk_index"] for c in result["chunks"]] == [0, 1]
    assert result["chunks"][0]["text"] == "first chunk"


def test_document_chunks_can_be_paged(acc, member, qdrant, tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text('{"sha256": "sha-1", "filename": "10-K.pdf", "status": "processed"}\n')
    monkeypatch.setattr(api, "MANIFEST", manifest)
    lib = acc.create_library("Project finance")
    acc.add_document(lib["id"], "sha-1")
    acc.grant("user", member.id, "library.read", lib["id"])
    qdrant.points = [point(f"p{i}", i, [], f"chunk {i}", doc_sha="sha-1") for i in range(3)]

    result = api.document_chunks("sha-1", offset=1, limit=1, user=member)

    assert result["total"] == 3
    assert [c["chunk_index"] for c in result["chunks"]] == [1]


def test_a_member_without_library_read_cannot_read_a_documents_chunks(acc, member, qdrant, tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text('{"sha256": "sha-1", "filename": "10-K.pdf", "status": "processed"}\n')
    monkeypatch.setattr(api, "MANIFEST", manifest)
    lib = acc.create_library("Project finance")
    acc.add_document(lib["id"], "sha-1")

    with pytest.raises(HTTPException) as exc:
        api.document_chunks("sha-1", user=member)
    assert exc.value.status_code == 403


def test_an_admin_can_read_an_unassigned_documents_chunks(admin, qdrant, tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text('{"sha256": "sha-1", "filename": "10-K.pdf", "status": "processed"}\n')
    monkeypatch.setattr(api, "MANIFEST", manifest)
    qdrant.points = [point("p1", 0, [], "chunk", doc_sha="sha-1")]

    result = api.document_chunks("sha-1", user=admin)

    assert result["total"] == 1


def test_an_unknown_documents_chunks_are_404(member, qdrant, tmp_path, monkeypatch):
    monkeypatch.setattr(api, "MANIFEST", tmp_path / "manifest.jsonl")

    with pytest.raises(HTTPException) as exc:
        api.document_chunks("no-such-doc", user=member)
    assert exc.value.status_code == 404

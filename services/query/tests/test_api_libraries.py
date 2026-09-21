"""
The library endpoints: self-service CRUD by ownership, document assignment
guarded by read-before-write, and the sharing (access-list) surface. Endpoint
functions are called directly, like test_api_uploads.py and test_api_auth.py.
"""

from __future__ import annotations

import api
import pytest
from accounts import DEFAULT_LIBRARY, Accounts, User
from conftest import SEARCH_ENV, FakeQdrant, point
from db import ChatDB
from fastapi import HTTPException
from retrieval import Retriever, SearchConfig


class FakeRetriever:
    """Only what a purge asks of it (see test_api_uploads.py)."""

    def __init__(self):
        self.deleted: list[str] = []

    def delete_document(self, doc_sha: str) -> None:
        self.deleted.append(doc_sha)


@pytest.fixture
def acc(tmp_path, monkeypatch):
    db = ChatDB(tmp_path / "chat.db")
    accounts = Accounts(db)
    monkeypatch.setattr(api.state, "db", db, raising=False)
    monkeypatch.setattr(api.state, "accounts", accounts, raising=False)
    # Taking a document out of its last library purges it (api.remove_document),
    # so these tests need an index to delete from and a data directory to
    # delete in -- neither of them the real one.
    monkeypatch.setattr(api.state, "retriever", FakeRetriever(), raising=False)
    monkeypatch.setattr(api, "DATA_DIR", tmp_path)
    monkeypatch.setattr(api, "INBOX", tmp_path / "inbox")
    monkeypatch.setattr(api, "MANIFEST", tmp_path / "manifest.jsonl")
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
    return acc.create_user("member@example.com", display_name="Member")


def own(user: User) -> str:
    """The library every account starts with (see Accounts.create_user)."""
    return next(lib["id"] for lib in api.state.accounts.libraries()
                if lib["owner_id"] == user.id and lib["name"] == DEFAULT_LIBRARY)


# --------------------------------------------------------------- creation --

def test_any_signed_in_user_creates_a_library_and_becomes_its_owner(member):
    lib = api.create_library(api.NewLibrary(name="Project finance", description="filings"), user=member)

    assert lib["owner_id"] == member.id
    assert lib["description"] == "filings"


def test_a_member_created_library_appears_in_their_own_list_and_is_usable(acc, member):
    lib = api.create_library(api.NewLibrary(name="Project finance"), user=member)

    assert {l["id"] for l in api.list_libraries(user=member)["libraries"]} == {lib["id"], own(member)}

    detail = api.library_detail(lib["id"], user=member)
    assert detail["id"] == lib["id"]
    assert detail["role"] == "owner"

    assert acc.can_write(member.id, lib["id"])
    assert acc.can_manage(member.id, lib["id"])


def test_another_user_cannot_see_a_library_they_did_not_create_or_receive(acc, member):
    api.create_library(api.NewLibrary(name="Project finance"), user=member)
    other = acc.create_user("other@example.com", display_name="Other")

    assert [l["id"] for l in api.list_libraries(user=other)["libraries"]] == [own(other)]
    with pytest.raises(HTTPException) as exc:
        api.library_detail(own(member), user=other)
    assert exc.value.status_code == 403


def test_a_blank_library_name_is_rejected(member):
    with pytest.raises(HTTPException) as exc:
        api.create_library(api.NewLibrary(name="   "), user=member)
    assert exc.value.status_code == 400


def test_an_admin_created_library_has_the_admin_as_owner(admin):
    lib = api.create_library(api.NewLibrary(name="Project finance"), user=admin)
    assert lib["owner_id"] == admin.id


# --------------------------------------------------------- listing for a user --

def test_an_admin_sees_every_library_a_member_sees_only_readable_ones(acc, admin, member):
    readable = api.create_library(api.NewLibrary(name="Readable"), user=admin)
    other = api.create_library(api.NewLibrary(name="Other"), user=admin)
    acc.set_library_access(readable["id"], "user", member.id, "read")

    assert {l["id"] for l in api.list_libraries(user=admin)["libraries"]} == \
        {readable["id"], other["id"], own(admin), own(member)}
    assert {l["id"] for l in api.list_libraries(user=member)["libraries"]} == {readable["id"], own(member)}


def test_list_libraries_reports_role_source_and_owner_name(acc, admin, member):
    owned = api.create_library(api.NewLibrary(name="Owned"), user=member)
    shared_direct = api.create_library(api.NewLibrary(name="Shared direct"), user=admin)
    shared_group = api.create_library(api.NewLibrary(name="Shared via group"), user=admin)
    admin_only = api.create_library(api.NewLibrary(name="Admin only"), user=admin)
    group = acc.create_group("Analysts")
    acc.add_member(group["id"], member.id)
    acc.set_library_access(shared_direct["id"], "user", member.id, "write")
    acc.set_library_access(shared_group["id"], "group", group["id"], "read")

    by_name = {l["name"]: l for l in api.list_libraries(user=member)["libraries"]}

    assert by_name["Owned"]["role"] == "owner" and by_name["Owned"]["source"] == "owner"
    assert by_name["Owned"]["owner_name"] == "Member"
    assert by_name["Shared direct"]["role"] == "write" and by_name["Shared direct"]["source"] == "user"
    assert by_name["Shared via group"]["role"] == "read" and by_name["Shared via group"]["source"] == "group"
    assert "Admin only" not in by_name  # not shared, not owned -- a member never sees it

    admin_listing = {l["name"]: l for l in api.list_libraries(user=admin)["libraries"]}
    assert admin_listing["Owned"]["role"] == "owner" and admin_listing["Owned"]["source"] == "admin"
    assert admin_listing["Admin only"]["role"] == "owner" and admin_listing["Admin only"]["source"] == "owner"


# ------------------------------------------------------------- library detail --

def test_a_member_with_read_access_sees_its_documents(acc, admin, member, tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        '{"sha256": "sha-1", "filename": "10-K.pdf", "status": "processed", "route": "docling", '
        '"pages": 42, "chars": 90000, "timestamp": 100}\n')
    monkeypatch.setattr(api, "MANIFEST", manifest)
    lib = api.create_library(api.NewLibrary(name="Project finance"), user=admin)
    acc.add_document(lib["id"], "sha-1")
    acc.set_library_access(lib["id"], "user", member.id, "read")

    detail = api.library_detail(lib["id"], user=member)

    assert detail["document_count"] == 1
    assert detail["role"] == "read"
    assert detail["documents"] == [{"sha256": "sha-1", "filename": "10-K.pdf", "status": "processed",
                                    "route": "docling", "pages": 42, "chars": 90000, "error": None,
                                    "processed_at": 100}]


def test_a_member_without_access_cannot_see_its_detail(acc, admin, member):
    lib = api.create_library(api.NewLibrary(name="Project finance"), user=admin)

    with pytest.raises(HTTPException) as exc:
        api.library_detail(lib["id"], user=member)
    assert exc.value.status_code == 403


def test_an_unknown_librarys_detail_is_404(member):
    with pytest.raises(HTTPException) as exc:
        api.library_detail("no-such-library", user=member)
    assert exc.value.status_code == 404


# ------------------------------------------------------------- rename / delete --

def test_the_owner_can_rename_and_delete_their_library(member):
    lib = api.create_library(api.NewLibrary(name="Project finance"), user=member)

    updated = api.update_library(lib["id"], api.LibraryUpdate(name="Renamed"), user=member)
    assert updated["name"] == "Renamed"

    api.delete_library(lib["id"], user=member)
    assert [l["id"] for l in api.list_libraries(user=member)["libraries"]] == [own(member)]


def test_an_admin_can_rename_and_delete_any_library(acc, admin, member):
    lib = api.create_library(api.NewLibrary(name="Project finance"), user=member)

    api.update_library(lib["id"], api.LibraryUpdate(name="Renamed"), user=admin)
    api.delete_library(lib["id"], user=admin)

    assert {l["id"] for l in acc.libraries()} == {own(admin), own(member)}


def test_a_writer_who_is_not_the_owner_cannot_rename_or_delete(acc, member):
    owner = acc.create_user("owner@example.com", display_name="Owner")
    lib = api.create_library(api.NewLibrary(name="Project finance"), user=owner)
    acc.set_library_access(lib["id"], "user", member.id, "write")

    with pytest.raises(HTTPException) as exc:
        api.update_library(lib["id"], api.LibraryUpdate(name="Renamed"), user=member)
    assert exc.value.status_code == 403

    with pytest.raises(HTTPException) as exc:
        api.delete_library(lib["id"], user=member)
    assert exc.value.status_code == 403


def test_deleting_an_unknown_library_is_404_for_an_admin(admin):
    """An admin clears the manage check unconditionally, so this is where a
    bad id actually gets checked -- for a member, an id they were never
    given access to is a 403 before it gets this far."""
    with pytest.raises(HTTPException) as exc:
        api.delete_library("no-such-library", user=admin)
    assert exc.value.status_code == 404


def test_deleting_an_unknown_library_is_403_for_a_non_admin(member):
    with pytest.raises(HTTPException) as exc:
        api.delete_library("no-such-library", user=member)
    assert exc.value.status_code == 403


# ------------------------------------------------------------------- access --

def test_the_owner_can_share_read_and_write(acc, member):
    lib = api.create_library(api.NewLibrary(name="Project finance"), user=member)
    target = acc.create_user("target@example.com", display_name="Target")

    api.put_library_access(lib["id"], api.AccessRequest(subject_type="user", subject_id=target.id, role="read"),
                           user=member)

    assert acc.library_role(target.id, lib["id"]) == "read"
    access = api.get_library_access(lib["id"], user=member)
    assert access["owner"] == {"id": member.id, "name": "Member"}
    assert access["access"] == [{"subject_type": "user", "subject_id": target.id, "name": "Target", "role": "read"}]


def test_an_admin_can_share_a_library_they_do_not_own(acc, admin, member):
    lib = api.create_library(api.NewLibrary(name="Project finance"), user=member)
    target = acc.create_user("target@example.com", display_name="Target")

    api.put_library_access(lib["id"], api.AccessRequest(subject_type="user", subject_id=target.id, role="write"),
                           user=admin)

    assert acc.library_role(target.id, lib["id"]) == "write"


def test_a_reader_cannot_share_manage_or_see_the_access_list(acc, member):
    owner = acc.create_user("owner@example.com", display_name="Owner")
    lib = api.create_library(api.NewLibrary(name="Project finance"), user=owner)
    acc.set_library_access(lib["id"], "user", member.id, "read")
    target = acc.create_user("target@example.com", display_name="Target")

    with pytest.raises(HTTPException) as exc:
        api.put_library_access(lib["id"], api.AccessRequest(subject_type="user", subject_id=target.id, role="read"),
                               user=member)
    assert exc.value.status_code == 403

    with pytest.raises(HTTPException) as exc:
        api.get_library_access(lib["id"], user=member)
    assert exc.value.status_code == 403


def test_a_writer_who_is_not_the_owner_cannot_share_either(acc, member):
    owner = acc.create_user("owner@example.com", display_name="Owner")
    lib = api.create_library(api.NewLibrary(name="Project finance"), user=owner)
    acc.set_library_access(lib["id"], "user", member.id, "write")
    target = acc.create_user("target@example.com", display_name="Target")

    with pytest.raises(HTTPException) as exc:
        api.put_library_access(lib["id"], api.AccessRequest(subject_type="user", subject_id=target.id, role="read"),
                               user=member)
    assert exc.value.status_code == 403


def test_the_owner_cannot_be_added_to_the_access_list(member):
    lib = api.create_library(api.NewLibrary(name="Project finance"), user=member)

    with pytest.raises(HTTPException) as exc:
        api.put_library_access(lib["id"], api.AccessRequest(subject_type="user", subject_id=member.id, role="read"),
                               user=member)
    assert exc.value.status_code == 400


def test_removing_access_revokes_it(acc, member):
    lib = api.create_library(api.NewLibrary(name="Project finance"), user=member)
    target = acc.create_user("target@example.com", display_name="Target")
    api.put_library_access(lib["id"], api.AccessRequest(subject_type="user", subject_id=target.id, role="read"),
                           user=member)

    api.delete_library_access(lib["id"], "user", target.id, user=member)

    assert acc.library_role(target.id, lib["id"]) is None


def test_removing_access_is_not_owner_only_but_still_needs_manage(acc, member):
    owner = acc.create_user("owner@example.com", display_name="Owner")
    lib = api.create_library(api.NewLibrary(name="Project finance"), user=owner)
    target = acc.create_user("target@example.com", display_name="Target")
    acc.set_library_access(lib["id"], "user", target.id, "read")

    with pytest.raises(HTTPException) as exc:
        api.delete_library_access(lib["id"], "user", target.id, user=member)
    assert exc.value.status_code == 403


# --------------------------------------------------------- document assignment --

def test_a_writer_can_add_a_document_they_can_already_read(acc, member):
    reader_lib = api.create_library(api.NewLibrary(name="Source"), user=member)
    acc.add_document(reader_lib["id"], "sha-1")
    target_lib = api.create_library(api.NewLibrary(name="Target"), user=member)

    api.add_document(target_lib["id"], "sha-1", user=member)

    assert sorted(acc.libraries_of("sha-1")) == sorted([reader_lib["id"], target_lib["id"]])


def test_a_writer_cannot_add_a_document_they_cannot_read(acc, member):
    """The rule that stops a writer pulling any indexed document into a
    library they control and reading it from there."""
    other_owner = acc.create_user("other@example.com", display_name="Other")
    off_limits = api.create_library(api.NewLibrary(name="Off limits"), user=other_owner)
    acc.add_document(off_limits["id"], "sha-1")
    target_lib = api.create_library(api.NewLibrary(name="Target"), user=member)

    with pytest.raises(HTTPException) as exc:
        api.add_document(target_lib["id"], "sha-1", user=member)
    assert exc.value.status_code == 403
    assert acc.libraries_of("sha-1") == [off_limits["id"]]


def test_an_admin_can_add_any_document(acc, admin, member):
    lib = api.create_library(api.NewLibrary(name="Project finance"), user=member)

    api.add_document(lib["id"], "sha-never-indexed", user=admin)

    assert acc.libraries_of("sha-never-indexed") == [lib["id"]]


def test_a_reader_cannot_add_documents(acc, member):
    owner = acc.create_user("owner@example.com", display_name="Owner")
    lib = api.create_library(api.NewLibrary(name="Project finance"), user=owner)
    acc.set_library_access(lib["id"], "user", member.id, "read")

    with pytest.raises(HTTPException) as exc:
        api.add_document(lib["id"], "sha-1", user=member)
    assert exc.value.status_code == 403


def test_a_writer_can_remove_a_document_without_needing_to_read_it_elsewhere(acc, member):
    lib = api.create_library(api.NewLibrary(name="Project finance"), user=member)
    acc.add_document(lib["id"], "sha-1")
    acc.add_document(own(member), "sha-1")  # elsewhere too, so this only unassigns

    assert api.remove_document(lib["id"], "sha-1", user=member) == {"purged": False}

    assert acc.libraries_of("sha-1") == [own(member)]
    assert api.state.retriever.deleted == []


def test_a_reader_cannot_remove_documents(acc, member):
    owner = acc.create_user("owner@example.com", display_name="Owner")
    lib = api.create_library(api.NewLibrary(name="Project finance"), user=owner)
    acc.add_document(lib["id"], "sha-1")
    acc.set_library_access(lib["id"], "user", member.id, "read")

    with pytest.raises(HTTPException) as exc:
        api.remove_document(lib["id"], "sha-1", user=member)
    assert exc.value.status_code == 403


# -------------------------------------------------- attaching to a conversation --

def test_a_conversation_can_only_attach_a_readable_library(acc, member):
    other_owner = acc.create_user("other@example.com", display_name="Other")
    lib = api.create_library(api.NewLibrary(name="Off limits"), user=other_owner)

    with pytest.raises(HTTPException) as exc:
        api.create_conversation(api.NewConversation(preset="everyday", library_ids=[lib["id"]]), user=member)
    assert exc.value.status_code == 403


def test_a_conversation_cannot_attach_an_unknown_library(member):
    with pytest.raises(HTTPException) as exc:
        api.create_conversation(api.NewConversation(preset="everyday", library_ids=["nope"]), user=member)
    assert exc.value.status_code == 404


def test_a_conversation_attaches_a_readable_library(member):
    lib = api.create_library(api.NewLibrary(name="Project finance"), user=member)

    conv = api.create_conversation(api.NewConversation(preset="everyday", library_ids=[lib["id"]]), user=member)

    assert conv["library_ids"] == [lib["id"]]
    assert api.get_conversation(conv["id"], user=member)["library_ids"] == [lib["id"]]


def test_updating_a_conversations_libraries_is_checked_the_same_way(acc, member):
    conv = api.create_conversation(api.NewConversation(preset="everyday"), user=member)
    other_owner = acc.create_user("other@example.com", display_name="Other")
    lib = api.create_library(api.NewLibrary(name="Off limits"), user=other_owner)

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


def test_a_member_with_read_access_can_read_a_documents_chunks(acc, member, qdrant, tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text('{"sha256": "sha-1", "filename": "10-K.pdf", "status": "processed"}\n')
    monkeypatch.setattr(api, "MANIFEST", manifest)
    lib = api.create_library(api.NewLibrary(name="Project finance"), user=member)
    acc.add_document(lib["id"], "sha-1")
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
    lib = api.create_library(api.NewLibrary(name="Project finance"), user=member)
    acc.add_document(lib["id"], "sha-1")
    qdrant.points = [point(f"p{i}", i, [], f"chunk {i}", doc_sha="sha-1") for i in range(3)]

    result = api.document_chunks("sha-1", offset=1, limit=1, user=member)

    assert result["total"] == 3
    assert [c["chunk_index"] for c in result["chunks"]] == [1]


def test_a_member_without_access_cannot_read_a_documents_chunks(acc, admin, member, qdrant, tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text('{"sha256": "sha-1", "filename": "10-K.pdf", "status": "processed"}\n')
    monkeypatch.setattr(api, "MANIFEST", manifest)
    lib = api.create_library(api.NewLibrary(name="Project finance"), user=admin)
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

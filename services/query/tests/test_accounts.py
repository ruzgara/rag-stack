"""
Users, groups and the permission precedence rules. The precedence is the part
worth testing hardest: it decides who can read what, and a mistake there is
silent.
"""

from __future__ import annotations

import pytest
from accounts import Accounts, Denied, Invalid, User
from db import LOCAL_USER, ChatDB, NotFound


@pytest.fixture
def acc(tmp_path):
    return Accounts(ChatDB(tmp_path / "chat.db"))


@pytest.fixture
def library(acc):
    """A library id to hang per-library grants on (the libraries table exists
    before the endpoints that manage it)."""
    with acc.db._conn() as db:
        db.execute("INSERT INTO libraries (id, name, created_at) VALUES ('lib-1', 'Project finance', 0)")
    return "lib-1"


def user(acc: Accounts, email: str = "member@example.com", **kw) -> User:
    return acc.create_user(email, password_hash="hash", **kw)


# ------------------------------------------------------------------ users --

def test_a_user_is_stored_with_a_lowercased_email(acc):
    created = user(acc, "  Member@Example.COM ")
    assert created.email == "member@example.com"
    assert acc.by_email("MEMBER@example.com").id == created.id
    assert "password_hash" not in created.public()  # never leaves the service


@pytest.mark.parametrize("email", ["", "  ", "nope", "no@domain", "two@@at.com", "spaces in@it.com"])
def test_a_bad_email_is_refused(acc, email):
    with pytest.raises(Invalid):
        user(acc, email)


def test_the_same_email_cannot_be_registered_twice(acc):
    user(acc)
    with pytest.raises(Invalid, match="already has an account"):
        user(acc)


def test_a_missing_user_is_not_found(acc):
    with pytest.raises(NotFound):
        acc.user("nobody")
    assert acc.by_email("nobody@example.com") is None


def test_the_first_admin_adopts_what_was_stored_before_accounts_existed(acc):
    """Conversations written under LOCAL_USER shouldn't disappear on upgrade."""
    conv = acc.db.create_conversation("everyday", "Old chat", user_id=LOCAL_USER)
    acc.db.record_upload("old.pdf", 10, user_id=LOCAL_USER)

    admin = user(acc, "admin@example.com", is_admin=True)

    assert [c["id"] for c in acc.db.list_conversations(user_id=admin.id)] == [conv["id"]]
    assert acc.db.upload_usage(3600, user_id=admin.id) == (1, 10)


def test_a_later_admin_adopts_nothing(acc):
    acc.db.create_conversation("everyday", "Old chat", user_id=LOCAL_USER)
    user(acc, "first@example.com", is_admin=True)

    second = user(acc, "second@example.com", is_admin=True)

    assert acc.db.list_conversations(user_id=second.id) == []


def test_changing_a_password_invalidates_existing_sessions(acc):
    created = user(acc)
    acc.set_password(created.id, "new-hash")
    assert acc.user(created.id).session_epoch == created.session_epoch + 1


def test_disabling_a_user_also_invalidates_their_sessions(acc):
    created = user(acc)
    updated = acc.update_user(created.id, disabled=True)
    assert updated.disabled and updated.session_epoch == created.session_epoch + 1


def test_deleting_a_user_keeps_their_conversations_but_drops_their_grants(acc):
    created = user(acc)
    acc.grant("user", created.id, "chat.use")
    conv = acc.db.create_conversation("everyday", user_id=created.id)

    acc.delete_user(created.id)

    assert acc.grants_of("user", created.id) == []
    assert [c["id"] for c in acc.db.list_conversations(user_id=created.id)] == [conv["id"]]


# ----------------------------------------------------------------- groups --

def test_a_user_lists_the_groups_it_belongs_to(acc):
    created = user(acc)
    group = acc.create_group("Analysts")
    acc.add_member(group["id"], created.id)
    assert acc.user(created.id).groups == ["Analysts"]


def test_a_group_name_is_unique_and_not_blank(acc):
    acc.create_group("Analysts")
    with pytest.raises(Invalid, match="already exists"):
        acc.create_group("Analysts")
    with pytest.raises(Invalid, match="needs a name"):
        acc.create_group("   ")


def test_deleting_a_group_removes_its_memberships_and_grants(acc):
    created = user(acc)
    group = acc.create_group("Analysts")
    acc.add_member(group["id"], created.id)
    acc.grant("group", group["id"], "chat.use")

    acc.delete_group(group["id"])

    assert acc.user(created.id).groups == []
    assert acc.grants_of("group", group["id"]) == []


def test_a_membership_in_a_group_that_does_not_exist_is_refused(acc):
    created = user(acc)
    with pytest.raises(NotFound):
        acc.add_member("no-such-group", created.id)


# ------------------------------------------------------------ permissions --

def test_a_user_with_no_grants_can_do_nothing(acc):
    created = user(acc)
    assert acc.effective_permissions(created.id) == {}
    assert not acc.allowed(created.id, "chat.use")
    with pytest.raises(Denied, match="doesn't have chat.use"):
        acc.require(created, "chat.use")


def test_a_direct_grant_is_enough(acc):
    created = user(acc)
    acc.grant("user", created.id, "chat.use")
    assert acc.allowed(created.id, "chat.use")


def test_a_group_grant_reaches_its_members(acc):
    created = user(acc)
    group = acc.create_group("Analysts")
    acc.add_member(group["id"], created.id)
    acc.grant("group", group["id"], "chat.use")

    assert acc.allowed(created.id, "chat.use")

    acc.remove_member(group["id"], created.id)
    assert not acc.allowed(created.id, "chat.use")


def test_a_user_deny_overrides_a_group_allow(acc):
    """The point of per-user overrides: lift one person out of a team grant."""
    created = user(acc)
    group = acc.create_group("Analysts")
    acc.add_member(group["id"], created.id)
    acc.grant("group", group["id"], "chat.use")
    acc.grant("user", created.id, "chat.use", effect="deny")

    assert not acc.allowed(created.id, "chat.use")


def test_a_user_allow_overrides_a_group_deny(acc):
    created = user(acc)
    group = acc.create_group("Contractors")
    acc.add_member(group["id"], created.id)
    acc.grant("group", group["id"], "chat.use", effect="deny")
    acc.grant("user", created.id, "chat.use")

    assert acc.allowed(created.id, "chat.use")


def test_between_two_groups_a_deny_wins(acc):
    created = user(acc)
    for name, effect in (("Analysts", "allow"), ("Suspended", "deny")):
        group = acc.create_group(name)
        acc.add_member(group["id"], created.id)
        acc.grant("group", group["id"], "chat.use", effect=effect)

    assert not acc.allowed(created.id, "chat.use")


def test_revoking_is_not_denying(acc):
    """Removing a user's deny lets the group's allow through again."""
    created = user(acc)
    group = acc.create_group("Analysts")
    acc.add_member(group["id"], created.id)
    acc.grant("group", group["id"], "chat.use")
    acc.grant("user", created.id, "chat.use", effect="deny")

    acc.revoke("user", created.id, "chat.use")

    assert acc.allowed(created.id, "chat.use")


def test_granting_the_same_permission_twice_updates_it_rather_than_duplicating(acc):
    created = user(acc)
    acc.grant("user", created.id, "chat.use")
    acc.grant("user", created.id, "chat.use", effect="deny")

    assert acc.grants_of("user", created.id) == [{"permission": "chat.use", "library_id": None, "effect": "deny"}]


def test_an_admin_has_everything_without_any_grants(acc, library):
    admin = user(acc, "admin@example.com", is_admin=True)
    assert acc.allowed(admin.id, "users.manage")
    assert acc.allowed(admin.id, "library.read", library)
    assert acc.readable_libraries(admin.id) is None  # all of them, now and later


def test_a_disabled_user_loses_every_permission(acc):
    created = user(acc)
    acc.grant("user", created.id, "chat.use")
    acc.update_user(created.id, disabled=True)

    assert acc.effective_permissions(created.id) == {}


def test_library_permissions_are_per_library(acc, library):
    created = user(acc)
    acc.grant("user", created.id, "library.read", library)

    assert acc.allowed(created.id, "library.read", library)
    assert not acc.allowed(created.id, "library.read", "another-library")
    assert acc.readable_libraries(created.id) == {library}


def test_a_typo_in_a_permission_name_is_refused(acc):
    created = user(acc)
    with pytest.raises(Invalid, match="unknown permission"):
        acc.grant("user", created.id, "chat.uses")


def test_a_global_permission_cannot_name_a_library_and_a_library_one_must(acc, library):
    created = user(acc)
    with pytest.raises(Invalid, match="global permission"):
        acc.grant("user", created.id, "chat.use", library)
    with pytest.raises(Invalid, match="has to name a library"):
        acc.grant("user", created.id, "library.read")


def test_a_grant_for_a_subject_that_does_not_exist_is_refused(acc):
    with pytest.raises(NotFound):
        acc.grant("user", "nobody", "chat.use")
    with pytest.raises(Invalid, match="user or a group"):
        acc.grant("robot", "nobody", "chat.use")
    with pytest.raises(Invalid, match="allows or denies"):
        acc.grant("user", "nobody", "chat.use", effect="maybe")


# ---------------------------------------------------------------- libraries --

def test_a_library_is_created_and_listed_with_its_document_count(acc):
    lib = acc.create_library("Project finance", "bond and infrastructure filings")
    assert acc.libraries() == [{**lib, "document_count": 0}]
    assert acc.library(lib["id"])["documents"] == []


def test_a_library_name_is_unique_and_not_blank(acc):
    acc.create_library("Project finance")
    with pytest.raises(Invalid, match="already exists"):
        acc.create_library("Project finance")
    with pytest.raises(Invalid, match="needs a name"):
        acc.create_library("   ")


def test_a_missing_library_is_not_found(acc):
    with pytest.raises(NotFound):
        acc.library("no-such-library")
    with pytest.raises(NotFound):
        acc.add_document("no-such-library", "sha")


def test_adding_the_same_document_twice_does_not_duplicate_it(acc):
    lib = acc.create_library("Project finance")
    acc.add_document(lib["id"], "sha-1")
    acc.add_document(lib["id"], "sha-1")
    assert acc.library(lib["id"])["documents"] == ["sha-1"]
    assert acc.libraries()[0]["document_count"] == 1


def test_removing_a_document_is_quiet_even_if_it_was_never_there(acc):
    lib = acc.create_library("Project finance")
    acc.remove_document(lib["id"], "sha-1")  # no error
    acc.add_document(lib["id"], "sha-1")
    acc.remove_document(lib["id"], "sha-1")
    assert acc.library(lib["id"])["documents"] == []


def test_a_document_lists_every_library_it_belongs_to(acc):
    a, b = acc.create_library("A"), acc.create_library("B")
    acc.add_document(a["id"], "sha-1")
    acc.add_document(b["id"], "sha-1")
    assert sorted(acc.libraries_of("sha-1")) == sorted([a["id"], b["id"]])
    assert acc.libraries_of("sha-2") == []


def test_deleting_a_library_drops_its_grants_and_document_memberships(acc):
    lib = acc.create_library("Project finance")
    created = user(acc)
    acc.grant("user", created.id, "library.read", lib["id"])
    acc.add_document(lib["id"], "sha-1")

    acc.delete_library(lib["id"])

    assert acc.grants_of("user", created.id) == []
    assert acc.libraries() == []
    with pytest.raises(NotFound):
        acc.library(lib["id"])


def test_deleting_a_library_detaches_it_from_conversations(acc):
    lib = acc.create_library("Project finance")
    created = user(acc)
    conv = acc.db.create_conversation("everyday", user_id=created.id, library_ids=[lib["id"]])

    acc.delete_library(lib["id"])

    assert acc.db.get_conversation(conv["id"], user_id=created.id, with_messages=False)["library_ids"] == []


# -------------------------------------------------------------- doc scope --

def test_an_admin_has_no_document_restriction(acc):
    admin = user(acc, "admin@example.com", is_admin=True)
    assert acc.document_scope(admin.id) is None


def test_an_admin_still_narrows_to_a_selected_library(acc):
    """An admin can read every library, but picking one in the UI has to
    actually filter -- not fall through to "no restriction" just because
    the admin's own read access is unrestricted."""
    admin = user(acc, "admin@example.com", is_admin=True)
    a, b = acc.create_library("A"), acc.create_library("B")
    acc.add_document(a["id"], "sha-a")
    acc.add_document(b["id"], "sha-b")

    assert acc.document_scope(admin.id, [a["id"]]) == {"sha-a"}


def test_a_user_with_no_readable_libraries_gets_an_empty_scope(acc):
    created = user(acc)
    assert acc.document_scope(created.id) == set()


def test_document_scope_defaults_to_every_readable_library(acc):
    created = user(acc)
    a, b = acc.create_library("A"), acc.create_library("B")
    acc.add_document(a["id"], "sha-a")
    acc.add_document(b["id"], "sha-b")
    acc.grant("user", created.id, "library.read", a["id"])
    acc.grant("user", created.id, "library.read", b["id"])

    assert acc.document_scope(created.id) == {"sha-a", "sha-b"}


def test_document_scope_narrows_to_the_selected_libraries(acc):
    created = user(acc)
    a, b = acc.create_library("A"), acc.create_library("B")
    acc.add_document(a["id"], "sha-a")
    acc.add_document(b["id"], "sha-b")
    acc.grant("user", created.id, "library.read", a["id"])
    acc.grant("user", created.id, "library.read", b["id"])

    assert acc.document_scope(created.id, [a["id"]]) == {"sha-a"}


def test_document_scope_ignores_a_selected_library_the_user_cannot_read(acc):
    """Selecting a library you can't read isn't a way around the grant --
    it's the same as selecting nothing from it."""
    created = user(acc)
    readable, other = acc.create_library("Readable"), acc.create_library("Other")
    acc.add_document(readable["id"], "sha-r")
    acc.add_document(other["id"], "sha-o")
    acc.grant("user", created.id, "library.read", readable["id"])

    assert acc.document_scope(created.id, [readable["id"], other["id"]]) == {"sha-r"}


def test_document_scope_is_recomputed_from_current_grants(acc):
    """A conversation's attached libraries are only ever a starting point --
    a revoked library.read stops applying on the very next call, with
    nothing to invalidate."""
    created = user(acc)
    lib = acc.create_library("Project finance")
    acc.add_document(lib["id"], "sha-1")
    acc.grant("user", created.id, "library.read", lib["id"])
    assert acc.document_scope(created.id, [lib["id"]]) == {"sha-1"}

    acc.revoke("user", created.id, "library.read", lib["id"])

    assert acc.document_scope(created.id, [lib["id"]]) == set()


# ----------------------------------------------------------- overview --

def test_library_overview_omits_libraries_the_user_cannot_read(acc):
    created = user(acc)
    attached, readable_only = acc.create_library("Attached"), acc.create_library("Readable only")
    acc.create_library("Locked")  # no grant -- must not appear at all
    acc.add_document(attached["id"], "sha-a")
    acc.grant("user", created.id, "library.read", attached["id"])
    acc.grant("user", created.id, "library.read", readable_only["id"])

    overview = {o["name"]: o for o in acc.library_overview(created.id, [attached["id"]])}

    assert set(overview) == {"Attached", "Readable only"}
    assert overview["Attached"]["in_scope"] and overview["Attached"]["document_count"] == 1
    assert not overview["Readable only"]["in_scope"]


def test_library_overview_with_nothing_selected_is_in_scope_for_every_readable_library(acc):
    created = user(acc)
    readable = acc.create_library("Readable")
    acc.create_library("Locked")  # no grant -- must not appear at all
    acc.grant("user", created.id, "library.read", readable["id"])

    overview = {o["name"]: o for o in acc.library_overview(created.id)}

    assert set(overview) == {"Readable"}
    assert overview["Readable"]["in_scope"]


def test_library_overview_for_an_admin_is_every_library_in_scope_by_default(acc):
    admin = user(acc, "admin@example.com", is_admin=True)
    acc.create_library("A")
    acc.create_library("B")

    overview = acc.library_overview(admin.id)

    assert {o["name"] for o in overview} == {"A", "B"}
    assert all(o["in_scope"] for o in overview)


def test_library_overview_for_an_admin_narrows_to_a_selection_like_everyone_else(acc):
    admin = user(acc, "admin@example.com", is_admin=True)
    a, b = acc.create_library("A"), acc.create_library("B")

    overview = {o["name"]: o for o in acc.library_overview(admin.id, [a["id"]])}

    assert overview["A"]["in_scope"] and not overview["B"]["in_scope"]

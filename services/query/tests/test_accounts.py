"""
Users, groups and the ownership/sharing model for libraries: who owns what,
who a library has been shared with, and how the two combine (library_role).
"""

from __future__ import annotations

import pytest
from accounts import Accounts, Denied, Invalid, User
from db import LOCAL_USER, ChatDB, NotFound


@pytest.fixture
def acc(tmp_path):
    return Accounts(ChatDB(tmp_path / "chat.db"))


def user(acc: Accounts, email: str = "member@example.com", **kw) -> User:
    kw.setdefault("display_name", "Test User")
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


@pytest.mark.parametrize("display_name", [None, "", "   "])
def test_a_user_needs_a_name_to_be_created(acc, display_name):
    with pytest.raises(Invalid, match="name is required"):
        acc.create_user("member@example.com", password_hash="hash", display_name=display_name)


def test_a_users_name_cannot_be_cleared_by_an_update(acc):
    created = user(acc)
    with pytest.raises(Invalid, match="name is required"):
        acc.update_user(created.id, display_name="   ")


def test_a_missing_user_is_not_found(acc):
    with pytest.raises(NotFound):
        acc.user("nobody")
    assert acc.by_email("nobody@example.com") is None


def test_search_users_matches_email_or_name_case_insensitively(acc):
    jane = user(acc, "jane@example.com", display_name="Jane Doe")
    other = user(acc, "other@example.com", display_name="Someone Else")

    assert [u.id for u in acc.search_users("jane")] == [jane.id]
    assert {u.id for u in acc.search_users("EXAMPLE.COM")} == {jane.id, other.id}
    assert [u.id for u in acc.search_users("doe")] == [jane.id]


def test_search_users_finds_nothing_for_an_unmatched_query(acc):
    user(acc)
    assert acc.search_users("nobody-like-this") == []


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


def test_deleting_a_user_keeps_their_conversations_but_drops_their_library_access(acc):
    created = user(acc)
    lib = acc.create_library("Project finance")
    acc.set_library_access(lib["id"], "user", created.id, "read")
    conv = acc.db.create_conversation("everyday", user_id=created.id)

    acc.delete_user(created.id)

    assert acc.library_access(lib["id"]) == []
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


def test_deleting_a_group_removes_its_memberships_and_access(acc):
    created = user(acc)
    group = acc.create_group("Analysts")
    acc.add_member(group["id"], created.id)
    lib = acc.create_library("Project finance")
    acc.set_library_access(lib["id"], "group", group["id"], "read")

    acc.delete_group(group["id"])

    assert acc.user(created.id).groups == []
    assert acc.library_access(lib["id"]) == []


def test_a_membership_in_a_group_that_does_not_exist_is_refused(acc):
    created = user(acc)
    with pytest.raises(NotFound):
        acc.add_member("no-such-group", created.id)


def test_a_group_defaults_to_locked(acc):
    group = acc.create_group("Analysts")
    assert group["type"] == "locked"
    assert acc.groups()[0]["type"] == "locked"


def test_a_groups_type_is_open_or_locked(acc):
    with pytest.raises(Invalid, match="open or locked"):
        acc.create_group("Analysts", kind="public")


# --------------------------------------------------------- self-service join --

def test_a_user_can_join_an_open_group_themselves(acc):
    joiner = user(acc, "joiner@example.com")
    group = acc.create_group("Everyone", kind="open")

    acc.join_group(group["id"], joiner)

    assert acc.user(joiner.id).groups == ["Everyone"]


def test_a_user_cannot_join_a_locked_group_themselves(acc):
    joiner = user(acc, "joiner@example.com")
    group = acc.create_group("Analysts", kind="locked")

    with pytest.raises(Denied):
        acc.join_group(group["id"], joiner)
    assert acc.user(joiner.id).groups == []


def test_joining_an_unknown_group_is_not_found(acc):
    joiner = user(acc, "joiner@example.com")
    with pytest.raises(NotFound):
        acc.join_group("no-such-group", joiner)


def test_a_user_can_leave_any_group_they_are_in(acc):
    member = user(acc, "member@example.com")
    open_group = acc.create_group("Everyone", kind="open")
    locked_group = acc.create_group("Analysts", kind="locked")
    acc.add_member(open_group["id"], member.id)
    acc.add_member(locked_group["id"], member.id)  # admin put them there

    acc.leave_group(open_group["id"], member)
    acc.leave_group(locked_group["id"], member)  # leaving a locked group needs no admin either

    assert acc.user(member.id).groups == []


def test_admins_can_still_add_anyone_to_a_locked_group(acc):
    """add_member (the admin path) is unaffected by type -- only the
    self-service join_group checks it."""
    created = user(acc)
    group = acc.create_group("Analysts", kind="locked")
    acc.add_member(group["id"], created.id)
    assert acc.user(created.id).groups == ["Analysts"]


# ---------------------------------------------------------------- libraries --

def test_a_library_is_created_and_listed_with_its_document_count(acc):
    lib = acc.create_library("Project finance", "bond and infrastructure filings")
    assert acc.libraries() == [{**lib, "document_count": 0}]
    assert acc.library(lib["id"])["documents"] == []


def test_a_library_records_who_created_it(acc):
    creator = user(acc, "creator@example.com")
    lib = acc.create_library("Project finance", owner_id=creator.id)
    assert lib["owner_id"] == creator.id
    assert acc.libraries()[0]["owner_id"] == creator.id
    assert acc.library(lib["id"])["owner_id"] == creator.id


def test_a_library_made_without_an_acting_user_has_no_owner(acc):
    lib = acc.create_library("Project finance")
    assert lib["owner_id"] is None


def test_deleting_the_owner_keeps_the_library_but_clears_the_owner(acc):
    creator = user(acc, "creator@example.com")
    lib = acc.create_library("Project finance", owner_id=creator.id)

    acc.delete_user(creator.id)

    assert acc.library(lib["id"])["owner_id"] is None


def test_a_library_name_is_unique_and_not_blank(acc):
    acc.create_library("Project finance")
    with pytest.raises(Invalid, match="already have a library"):
        acc.create_library("Project finance")
    with pytest.raises(Invalid, match="needs a name"):
        acc.create_library("   ")


def test_library_name_uniqueness_is_scoped_per_owner(acc):
    """Global uniqueness let a user probe for a hidden library's name (a 400
    on creation confirms it exists) or squat a name before its rightful
    owner claimed it -- scoping to the owner closes both."""
    a = user(acc, "a@example.com")
    b = user(acc, "b@example.com")

    mine = acc.create_library("Project finance", owner_id=a.id)
    theirs = acc.create_library("Project finance", owner_id=b.id)
    assert mine["id"] != theirs["id"]

    with pytest.raises(Invalid, match="already have a library"):
        acc.create_library("Project finance", owner_id=a.id)


def test_the_owner_alone_has_full_access_no_row_needed(acc):
    owner = user(acc, "owner@example.com")
    lib = acc.create_library("Project finance", "bond and infrastructure filings", owner_id=owner.id)

    assert acc.library_role(owner.id, lib["id"]) == "owner"
    assert acc.library_access(lib["id"]) == []
    assert acc.readable_libraries(owner.id) == {lib["id"]}


def test_updating_a_library_renames_it_and_checks_uniqueness(acc):
    lib = acc.create_library("Project finance")
    acc.create_library("Other")

    updated = acc.update_library(lib["id"], name="Renamed", description="new description")
    assert updated["name"] == "Renamed"
    assert updated["description"] == "new description"

    with pytest.raises(Invalid, match="already have a library"):
        acc.update_library(lib["id"], name="Other")
    with pytest.raises(Invalid, match="needs a name"):
        acc.update_library(lib["id"], name="   ")


def test_updating_an_unknown_library_is_not_found(acc):
    with pytest.raises(NotFound):
        acc.update_library("no-such-library", name="New name")


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


def test_deleting_a_library_drops_its_access_list_and_document_memberships(acc):
    lib = acc.create_library("Project finance")
    created = user(acc)
    acc.set_library_access(lib["id"], "user", created.id, "read")
    acc.add_document(lib["id"], "sha-1")

    acc.delete_library(lib["id"])

    assert acc.libraries() == []
    with pytest.raises(NotFound):
        acc.library(lib["id"])


def test_deleting_a_library_detaches_it_from_conversations(acc):
    lib = acc.create_library("Project finance")
    created = user(acc)
    conv = acc.db.create_conversation("everyday", user_id=created.id, library_ids=[lib["id"]])

    acc.delete_library(lib["id"])

    assert acc.db.get_conversation(conv["id"], user_id=created.id, with_messages=False)["library_ids"] == []


# ------------------------------------------------------------ library_role --

def test_a_user_with_no_access_has_no_role(acc):
    lib = acc.create_library("Project finance")
    created = user(acc)
    assert acc.library_role(created.id, lib["id"]) is None
    assert not acc.can_read(created.id, lib["id"])
    assert not acc.can_write(created.id, lib["id"])
    assert not acc.can_manage(created.id, lib["id"])


def test_a_direct_read_row_is_enough_to_read_but_not_write(acc):
    lib = acc.create_library("Project finance")
    created = user(acc)
    acc.set_library_access(lib["id"], "user", created.id, "read")

    assert acc.library_role(created.id, lib["id"]) == "read"
    assert acc.can_read(created.id, lib["id"])
    assert not acc.can_write(created.id, lib["id"])
    assert not acc.can_manage(created.id, lib["id"])


def test_a_direct_write_row_can_read_and_write_but_not_manage(acc):
    lib = acc.create_library("Project finance")
    created = user(acc)
    acc.set_library_access(lib["id"], "user", created.id, "write")

    assert acc.library_role(created.id, lib["id"]) == "write"
    assert acc.can_read(created.id, lib["id"])
    assert acc.can_write(created.id, lib["id"])
    assert not acc.can_manage(created.id, lib["id"])


def test_a_group_row_reaches_its_members(acc):
    lib = acc.create_library("Project finance")
    created = user(acc)
    group = acc.create_group("Analysts")
    acc.add_member(group["id"], created.id)
    acc.set_library_access(lib["id"], "group", group["id"], "read")

    assert acc.library_role(created.id, lib["id"]) == "read"

    acc.remove_member(group["id"], created.id)
    assert acc.library_role(created.id, lib["id"]) is None


def test_the_highest_role_wins_across_user_and_group_rows(acc):
    """Read through one group, write through another (or directly): the
    answer is write, not whichever row happens to be found first."""
    lib = acc.create_library("Project finance")
    created = user(acc)
    read_group = acc.create_group("Readers")
    write_group = acc.create_group("Writers")
    acc.add_member(read_group["id"], created.id)
    acc.add_member(write_group["id"], created.id)
    acc.set_library_access(lib["id"], "group", read_group["id"], "read")
    acc.set_library_access(lib["id"], "group", write_group["id"], "write")

    assert acc.library_role(created.id, lib["id"]) == "write"


def test_a_direct_read_row_does_not_downgrade_a_groups_write(acc):
    lib = acc.create_library("Project finance")
    created = user(acc)
    group = acc.create_group("Writers")
    acc.add_member(group["id"], created.id)
    acc.set_library_access(lib["id"], "user", created.id, "read")
    acc.set_library_access(lib["id"], "group", group["id"], "write")

    assert acc.library_role(created.id, lib["id"]) == "write"


def test_the_owner_outranks_any_row(acc):
    owner = user(acc, "owner@example.com")
    lib = acc.create_library("Project finance", owner_id=owner.id)
    assert acc.library_role(owner.id, lib["id"]) == "owner"
    assert acc.can_manage(owner.id, lib["id"])


def test_an_admin_is_owner_of_every_library_without_any_row(acc):
    admin = user(acc, "admin@example.com", is_admin=True)
    lib = acc.create_library("Project finance")
    assert acc.library_role(admin.id, lib["id"]) == "owner"
    assert acc.can_manage(admin.id, lib["id"])
    assert acc.readable_libraries(admin.id) is None  # all of them, now and later


def test_a_disabled_user_has_no_role_anywhere(acc):
    created = user(acc)
    lib = acc.create_library("Project finance", owner_id=created.id)
    acc.update_user(created.id, disabled=True)

    assert acc.library_role(created.id, lib["id"]) is None
    assert acc.readable_libraries(created.id) == set()


def test_library_role_on_an_unknown_library_is_none_for_a_normal_user(acc):
    created = user(acc)
    assert acc.library_role(created.id, "no-such-library") is None


def test_readable_libraries_is_owned_plus_shared(acc):
    owner = user(acc, "owner@example.com")
    owned = acc.create_library("Owned", owner_id=owner.id)
    shared = acc.create_library("Shared")
    acc.create_library("Locked")  # no access -- must not appear
    acc.set_library_access(shared["id"], "user", owner.id, "read")

    assert acc.readable_libraries(owner.id) == {owned["id"], shared["id"]}


# ---------------------------------------------------------------- sharing --

def test_setting_access_validates_role_and_subject(acc):
    lib = acc.create_library("Project finance")
    created = user(acc)
    with pytest.raises(Invalid, match="a role is read or write"):
        acc.set_library_access(lib["id"], "user", created.id, "admin")
    with pytest.raises(Invalid, match="a subject is a user or a group"):
        acc.set_library_access(lib["id"], "robot", created.id, "read")
    with pytest.raises(NotFound):
        acc.set_library_access(lib["id"], "user", "nobody", "read")
    with pytest.raises(NotFound):
        acc.set_library_access("no-such-library", "user", created.id, "read")


def test_the_owner_cannot_be_added_to_their_own_access_list(acc):
    owner = user(acc, "owner@example.com")
    lib = acc.create_library("Project finance", owner_id=owner.id)
    with pytest.raises(Invalid, match="owner already has"):
        acc.set_library_access(lib["id"], "user", owner.id, "read")


def test_sharing_twice_updates_the_role_rather_than_duplicating(acc):
    lib = acc.create_library("Project finance")
    created = user(acc)
    acc.set_library_access(lib["id"], "user", created.id, "read")
    acc.set_library_access(lib["id"], "user", created.id, "write")

    assert acc.library_access(lib["id"]) == [
        {"subject_type": "user", "subject_id": created.id, "name": created.display_name, "role": "write"}]


def test_library_access_reports_a_users_name_and_a_groups_name(acc):
    lib = acc.create_library("Project finance")
    reader = user(acc, "reader@example.com")
    group = acc.create_group("Analysts")
    acc.set_library_access(lib["id"], "user", reader.id, "read")
    acc.set_library_access(lib["id"], "group", group["id"], "write")

    access = {(a["subject_type"], a["subject_id"]): a for a in acc.library_access(lib["id"])}
    assert access[("user", reader.id)] == {"subject_type": "user", "subject_id": reader.id,
                                            "name": "Test User", "role": "read"}
    assert access[("group", group["id"])] == {"subject_type": "group", "subject_id": group["id"],
                                               "name": "Analysts", "role": "write"}


def test_removing_access_is_quiet_even_if_there_was_none(acc):
    lib = acc.create_library("Project finance")
    created = user(acc)
    acc.remove_library_access(lib["id"], "user", created.id)  # no error
    acc.set_library_access(lib["id"], "user", created.id, "read")

    acc.remove_library_access(lib["id"], "user", created.id)

    assert acc.library_access(lib["id"]) == []
    assert acc.library_role(created.id, lib["id"]) is None


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
    acc.set_library_access(a["id"], "user", created.id, "read")
    acc.set_library_access(b["id"], "user", created.id, "read")

    assert acc.document_scope(created.id) == {"sha-a", "sha-b"}


def test_document_scope_narrows_to_the_selected_libraries(acc):
    created = user(acc)
    a, b = acc.create_library("A"), acc.create_library("B")
    acc.add_document(a["id"], "sha-a")
    acc.add_document(b["id"], "sha-b")
    acc.set_library_access(a["id"], "user", created.id, "read")
    acc.set_library_access(b["id"], "user", created.id, "read")

    assert acc.document_scope(created.id, [a["id"]]) == {"sha-a"}


def test_document_scope_ignores_a_selected_library_the_user_cannot_read(acc):
    """Selecting a library you can't read isn't a way around access -- it's
    the same as selecting nothing from it."""
    created = user(acc)
    readable, other = acc.create_library("Readable"), acc.create_library("Other")
    acc.add_document(readable["id"], "sha-r")
    acc.add_document(other["id"], "sha-o")
    acc.set_library_access(readable["id"], "user", created.id, "read")

    assert acc.document_scope(created.id, [readable["id"], other["id"]]) == {"sha-r"}


def test_document_scope_is_recomputed_from_current_access(acc):
    """A conversation's attached libraries are only ever a starting point --
    losing read access stops applying on the very next call, with nothing
    to invalidate."""
    created = user(acc)
    lib = acc.create_library("Project finance")
    acc.add_document(lib["id"], "sha-1")
    acc.set_library_access(lib["id"], "user", created.id, "read")
    assert acc.document_scope(created.id, [lib["id"]]) == {"sha-1"}

    acc.remove_library_access(lib["id"], "user", created.id)

    assert acc.document_scope(created.id, [lib["id"]]) == set()


def test_can_read_document_follows_library_access(acc):
    created = user(acc)
    lib = acc.create_library("Project finance")
    acc.add_document(lib["id"], "sha-1")

    assert not acc.can_read_document(created.id, "sha-1")

    acc.set_library_access(lib["id"], "user", created.id, "read")
    assert acc.can_read_document(created.id, "sha-1")


def test_an_admin_can_read_any_document(acc):
    admin = user(acc, "admin@example.com", is_admin=True)
    assert acc.can_read_document(admin.id, "sha-unassigned")


# ----------------------------------------------------------- overview --

def test_library_overview_omits_libraries_the_user_cannot_read(acc):
    created = user(acc)
    attached, readable_only = acc.create_library("Attached"), acc.create_library("Readable only")
    acc.create_library("Locked")  # no access -- must not appear at all
    acc.add_document(attached["id"], "sha-a")
    acc.set_library_access(attached["id"], "user", created.id, "read")
    acc.set_library_access(readable_only["id"], "user", created.id, "read")

    overview = {o["name"]: o for o in acc.library_overview(created.id, [attached["id"]])}

    assert set(overview) == {"Attached", "Readable only"}
    assert overview["Attached"]["in_scope"] and overview["Attached"]["document_count"] == 1
    assert not overview["Readable only"]["in_scope"]


def test_library_overview_with_nothing_selected_is_in_scope_for_every_readable_library(acc):
    created = user(acc)
    readable = acc.create_library("Readable")
    acc.create_library("Locked")  # no access -- must not appear at all
    acc.set_library_access(readable["id"], "user", created.id, "read")

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

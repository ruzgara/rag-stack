"""
The admin user list, and the permissions it reports.

The list is what an admin reads to decide whether someone can do something,
so it reports permissions already resolved rather than the raw grants: a deny
removes a permission, and a UI showing the deny next to the permission name
reads like the opposite of what it means.
"""

from __future__ import annotations

import api
import pytest
from accounts import Accounts
from auth import Auth
from db import ChatDB


@pytest.fixture
def service(tmp_path, monkeypatch):
    db = ChatDB(tmp_path / "chat.db")
    accounts = Accounts(db)
    monkeypatch.setattr(api.state, "db", db, raising=False)
    monkeypatch.setattr(api.state, "accounts", accounts, raising=False)
    monkeypatch.setattr(api.state, "auth", Auth(accounts), raising=False)
    return accounts


@pytest.fixture
def admin(service):
    return service.create_user("admin@example.com", password_hash="hash", is_admin=True)


def users_by_email(admin) -> dict[str, dict]:
    return {u["email"]: u for u in api.admin_list_users(admin)["users"]}


def test_a_user_with_nothing_granted_has_no_permissions(service, admin):
    service.create_user("new@example.com", password_hash="hash")
    assert users_by_email(admin)["new@example.com"]["effective"] == {}


def test_a_permission_inherited_from_a_group_is_reported(service, admin):
    """The grant is on the group, so a list of the user's own grants would
    show nothing -- and the user can still ask questions."""
    user = service.create_user("member@example.com", password_hash="hash")
    group = service.create_group("Analysts")
    service.add_member(group["id"], user.id)
    service.grant("group", group["id"], "chat.use")

    assert "chat.use" in users_by_email(admin)["member@example.com"]["effective"]


def test_a_users_own_deny_removes_a_permission_their_group_grants(service, admin):
    """The case the admin console got wrong: the user holds a deny, the group
    holds an allow, and the resolved answer is that they cannot read it."""
    user = service.create_user("member@example.com", password_hash="hash")
    group = service.create_group("Analysts")
    service.add_member(group["id"], user.id)
    with service.db._conn() as db:
        db.execute("INSERT INTO libraries (id, name, created_at) VALUES ('lib-1', 'Project finance', 0)")
    service.grant("group", group["id"], "library.read", "lib-1")
    service.grant("group", group["id"], "chat.use")
    service.grant("user", user.id, "library.read", "lib-1", "deny")

    effective = users_by_email(admin)["member@example.com"]["effective"]

    assert "library.read" not in effective      # the deny won
    assert "chat.use" in effective              # and took nothing else with it
    assert not service.allowed(user.id, "library.read", "lib-1")


def test_a_library_permission_names_the_library_it_applies_to(service, admin):
    user = service.create_user("member@example.com", password_hash="hash")
    with service.db._conn() as db:
        db.execute("INSERT INTO libraries (id, name, created_at) VALUES ('lib-1', 'Project finance', 0)")
    service.grant("user", user.id, "library.read", "lib-1")

    assert users_by_email(admin)["member@example.com"]["effective"]["library.read"] == ["lib-1"]


def test_an_admin_is_reported_as_holding_everything(service, admin):
    effective = users_by_email(admin)["admin@example.com"]["effective"]
    assert set(effective) == {"users.manage", "libraries.manage", "chat.use", "library.read", "library.write",
                              "library.share"}


def test_a_disabled_user_holds_nothing_whatever_was_granted(service, admin):
    user = service.create_user("gone@example.com", password_hash="hash")
    service.grant("user", user.id, "chat.use")
    service.update_user(user.id, disabled=True)

    assert users_by_email(admin)["gone@example.com"]["effective"] == {}


def test_the_password_hash_is_never_in_the_list(service, admin):
    assert all("password_hash" not in u for u in api.admin_list_users(admin)["users"])

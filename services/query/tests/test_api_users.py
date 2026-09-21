"""
The admin user list. It's the plain user rows now -- what each may do on a
given library is per library (GET /api/libraries' `role`), not a global map
to resolve here.
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
    return service.create_user("admin@example.com", password_hash="hash", display_name="Admin", is_admin=True)


def users_by_email(admin) -> dict[str, dict]:
    return {u["email"]: u for u in api.admin_list_users(admin)["users"]}


def test_the_user_list_has_no_effective_permissions_field(service, admin):
    service.create_user("new@example.com", password_hash="hash", display_name="New")
    assert "effective" not in users_by_email(admin)["new@example.com"]


def test_the_user_list_reports_groups_and_admin_status(service, admin):
    user = service.create_user("member@example.com", password_hash="hash", display_name="Member")
    group = service.create_group("Analysts")
    service.add_member(group["id"], user.id)

    row = users_by_email(admin)["member@example.com"]

    assert row["groups"] == ["Analysts"]
    assert row["is_admin"] is False
    assert users_by_email(admin)["admin@example.com"]["is_admin"] is True


def test_the_password_hash_is_never_in_the_list(service, admin):
    assert all("password_hash" not in u for u in api.admin_list_users(admin)["users"])


# ------------------------------------------------------------------ search --

def test_search_users_finds_a_match_by_name_or_email(service, admin):
    service.create_user("jane@example.com", password_hash="hash", display_name="Jane Doe")

    by_name = api.search_users(q="jane", user=admin)["users"]
    by_email = api.search_users(q="jane@example.com", user=admin)["users"]

    assert by_name == by_email == [{"id": by_name[0]["id"], "name": "Jane Doe", "email": "jane@example.com"}]


def test_search_users_result_has_no_extra_fields(service, admin):
    service.create_user("jane@example.com", password_hash="hash", display_name="Jane Doe")
    row = api.search_users(q="jane", user=admin)["users"][0]
    assert set(row) == {"id", "name", "email"}


def test_search_users_finds_nothing_for_an_unmatched_query(service, admin):
    assert api.search_users(q="nobody-like-this", user=admin)["users"] == []


def test_search_users_is_available_to_any_signed_in_user_not_just_admins(service, admin):
    member = service.create_user("member@example.com", password_hash="hash", display_name="Member")
    assert api.search_users(q="member", user=member)["users"][0]["email"] == "member@example.com"

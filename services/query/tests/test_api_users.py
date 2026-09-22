"""
The admin user list. It's the plain user rows now -- what each may do on a
given library is per library (GET /api/libraries' `role`), not a global map
to resolve here.
"""

from __future__ import annotations

import api
import pytest
from accounts import Accounts
from fastapi import HTTPException
from pydantic import ValidationError
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


# ------------------------------------------ admins are made at the terminal --

def test_creating_a_user_over_the_api_cannot_make_an_admin(service, admin):
    with pytest.raises(ValidationError):
        api.NewUser(email="new@example.com", display_name="New", password="long enough pw", is_admin=True)

    created = api.admin_create_user(api.NewUser(email="new@example.com", display_name="New",
                                                password="long enough pw"), admin)
    assert created["is_admin"] is False


def test_promoting_or_demoting_over_the_api_is_not_even_expressible(service, admin):
    for is_admin in (True, False):
        with pytest.raises(ValidationError):
            api.UserUpdate(is_admin=is_admin)


@pytest.mark.parametrize("body", [{"display_name": "Renamed"}, {"disabled": True}, {"password": "long enough pw"}])
def test_another_admins_account_cannot_be_changed_over_the_api(service, admin, body):
    other = service.create_user("other@example.com", password_hash="hash", display_name="Other", is_admin=True)

    with pytest.raises(HTTPException) as exc:
        api.admin_update_user(other.id, api.UserUpdate(**body), admin)

    assert exc.value.status_code == 403 and "ragctl user" in exc.value.detail
    after = service.user(other.id)
    assert (after.display_name, after.disabled, after.password_hash) == ("Other", False, "hash")


def test_an_admin_cannot_change_their_own_account_over_the_api_either(service, admin):
    with pytest.raises(HTTPException) as exc:
        api.admin_update_user(admin.id, api.UserUpdate(display_name="Renamed"), admin)
    assert exc.value.status_code == 403


def test_an_admin_account_cannot_be_deleted_over_the_api(service, admin):
    other = service.create_user("other@example.com", password_hash="hash", display_name="Other", is_admin=True)

    for target in (other, admin):
        with pytest.raises(HTTPException) as exc:
            api.admin_delete_user(target.id, admin)
        assert exc.value.status_code == 403
    assert service.by_email("other@example.com") is not None


def test_members_are_still_managed_over_the_api(service, admin):
    member = service.create_user("member@example.com", password_hash="hash", display_name="Member")

    updated = api.admin_update_user(member.id, api.UserUpdate(display_name="Renamed", disabled=True), admin)
    assert (updated["display_name"], updated["disabled"]) == ("Renamed", True)

    api.admin_delete_user(member.id, admin)
    assert service.by_email("member@example.com") is None


def test_managing_an_unknown_user_is_404(service, admin):
    for call in (lambda: api.admin_update_user("nobody", api.UserUpdate(display_name="X"), admin),
                 lambda: api.admin_delete_user("nobody", admin)):
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 404


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


def test_search_users_excludes_disabled_accounts(service, admin):
    jane = service.create_user("jane@example.com", password_hash="hash", display_name="Jane Doe")
    service.update_user(jane.id, disabled=True)

    assert api.search_users(q="jane", user=admin)["users"] == []


def test_search_users_does_not_treat_underscore_as_a_wildcard(service, admin):
    service.create_user("jane@example.com", password_hash="hash", display_name="Jane Doe")

    assert api.search_users(q="_", user=admin)["users"] == []

"""
Groups' open/locked type and self-service join/leave. Admin group CRUD is
otherwise exercised through Accounts directly (test_accounts.py); this is
about the endpoints layered on top. Endpoint functions are called directly,
like test_api_libraries.py.
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
    return accounts


@pytest.fixture
def admin(acc):
    return acc.create_user("admin@example.com", display_name="Admin", is_admin=True)


@pytest.fixture
def member(acc):
    return acc.create_user("member@example.com", display_name="Member")


def test_an_admin_creates_a_group_with_a_type(admin):
    group = api.admin_create_group(api.NewGroup(name="Everyone", type="open"), admin=admin)
    assert group["type"] == "open"


def test_a_group_defaults_to_locked_via_the_api(admin):
    group = api.admin_create_group(api.NewGroup(name="Analysts"), admin=admin)
    assert group["type"] == "locked"


def test_listing_groups_is_self_service_and_omits_members(acc, member):
    acc.create_group("Everyone", kind="open")
    acc.create_group("Analysts", kind="locked")

    groups = api.list_groups(user=member)["groups"]

    assert {(g["name"], g["type"]) for g in groups} == {("Everyone", "open"), ("Analysts", "locked")}
    assert all("members" not in g for g in groups)


def test_a_member_joins_an_open_group(acc, member):
    group = acc.create_group("Everyone", kind="open")

    api.join_group(group["id"], user=member)

    assert acc.user(member.id).groups == ["Everyone"]


def test_a_member_cannot_join_a_locked_group(acc, member):
    group = acc.create_group("Analysts", kind="locked")

    with pytest.raises(HTTPException) as exc:
        api.join_group(group["id"], user=member)
    assert exc.value.status_code == 403
    assert acc.user(member.id).groups == []


def test_joining_an_unknown_group_is_404(member):
    with pytest.raises(HTTPException) as exc:
        api.join_group("no-such-group", user=member)
    assert exc.value.status_code == 404


def test_a_member_leaves_a_group_they_are_in(acc, member):
    group = acc.create_group("Everyone", kind="open")
    api.join_group(group["id"], user=member)

    api.leave_group(group["id"], user=member)

    assert acc.user(member.id).groups == []


def test_leaving_a_group_you_are_not_in_is_quiet(member):
    api.leave_group("no-such-group", user=member)  # no error either way

"""
The UI-serving edges added to api.py alongside ui/: the /api and /v1
catch-all 404s (an unknown path must get JSON, never the SPA's index.html),
route registration order (the catch-all must not shadow /docs, /admin or the
real API), and /api/health reporting auth_mode for a signed-out UI.

These call the route functions and inspect api.app.routes directly rather
than going through a live TestClient -- api.py's lifespan builds a real
Retriever/ChatService from the repo's actual .env on startup, which the rest
of this test suite deliberately avoids touching (see conftest.py).
"""

from __future__ import annotations

import json

import api
import pytest
from accounts import Accounts
from auth import Auth
from chat import ChatConfig, ChatService
from db import ChatDB
from limits import Gate, GateConfig, UploadLimits
from retrieval import Retriever, SearchConfig
from conftest import CHAT_ENV, SEARCH_ENV


@pytest.fixture
def service(tmp_path, monkeypatch):
    """api.state wired to a temp database and a retriever/chat pointed at
    closed local ports, so health()'s real network calls fail fast (a
    connection refusal) instead of hanging or reaching anything real."""
    db = ChatDB(tmp_path / "chat.db")
    accounts = Accounts(db)
    search_cfg = SearchConfig.from_env({**SEARCH_ENV, "OLLAMA_PORT": "1", "QDRANT_PORT": "1", "RAG_RERANK": "false"})
    retriever = Retriever(search_cfg)
    chat_cfg = ChatConfig.from_env({**CHAT_ENV, "OLLAMA_PORT": "1"})

    monkeypatch.setattr(api.state, "db", db, raising=False)
    monkeypatch.setattr(api.state, "accounts", accounts, raising=False)
    monkeypatch.setattr(api.state, "auth", Auth(accounts), raising=False)
    monkeypatch.setattr(api.state, "api_key", "", raising=False)
    monkeypatch.setattr(api.state, "gate", Gate(GateConfig()), raising=False)
    monkeypatch.setattr(api.state, "uploads", UploadLimits(), raising=False)
    monkeypatch.setattr(api.state, "ingest", api.IngestRunner(autorun=False), raising=False)
    monkeypatch.setattr(api.state, "retriever", retriever, raising=False)
    monkeypatch.setattr(api.state, "chat", ChatService(retriever, chat_cfg), raising=False)
    yield accounts
    retriever.close()


# --------------------------------------------------------------- catch-all --

def test_unknown_api_path_handler_returns_json_404():
    # The handler itself, called directly -- see test_api_and_v1_routes_
    # accept_every_method below for proof that GET, POST and OPTIONS alike
    # actually reach this same function rather than a 405.
    for fn in (api.api_not_found, api.v1_not_found):
        resp = fn("nope")
        assert resp.status_code == 404
        body = json.loads(bytes(resp.body))
        assert "nope" in body["detail"]


def test_api_and_v1_routes_accept_every_method():
    # Confirms the registered route -- not just the handler function -- is
    # reachable by GET, POST and OPTIONS alike, so a stray call of any verb
    # against an unknown /api or /v1 path gets this JSON 404, never FastAPI's
    # default 405 (right path, wrong method) or the SPA's index.html.
    for prefix in ("/api/{rest:path}", "/v1/{rest:path}"):
        route = next(r for r in api.app.routes if getattr(r, "path", None) == prefix)
        assert {"GET", "POST", "OPTIONS"} <= set(route.methods)


def test_api_not_found_body_matches_the_normal_error_shape():
    # {"detail": string} -- the same envelope every other endpoint uses, so a
    # client's error handling doesn't need a special case for a typo'd path.
    resp = api.api_not_found("conversations/oops/nope")
    body = json.loads(bytes(resp.body))
    assert isinstance(body["detail"], str)


# --------------------------------------------------------- route ordering --

def _index(path_prefix: str) -> int:
    for i, route in enumerate(api.app.routes):
        path = getattr(route, "path", None)
        if path and path.startswith(path_prefix):
            return i
    raise AssertionError(f"no route matches {path_prefix!r}")


def test_admin_and_docs_precede_the_ui_catch_all():
    catch_all = _index("/{full_path")
    assert _index("/admin") < catch_all
    assert _index("/docs") < catch_all
    assert _index("/openapi.json") < catch_all


def test_api_and_v1_catch_alls_precede_the_ui_catch_all():
    ui_catch_all = _index("/{full_path")
    assert _index("/api/{rest") < ui_catch_all
    assert _index("/v1/{rest") < ui_catch_all


def test_admin_ui_still_returns_the_admin_page():
    resp = api.admin_ui()
    assert resp.path.name == "admin.html"


# -------------------------------------------------------------------- health --

def test_health_reports_auth_mode(service):
    resp = api.health()
    body = json.loads(bytes(resp.body))
    assert body["auth"] == {"ok": True, "mode": "password"}


def test_health_auth_mode_reflects_proxy(service, monkeypatch):
    monkeypatch.setattr(api.state.auth, "mode", "proxy", raising=False)
    resp = api.health()
    body = json.loads(bytes(resp.body))
    assert body["auth"]["mode"] == "proxy"

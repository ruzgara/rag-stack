"""
The system endpoints: who may reach them, and what a reload actually applies.

The gating matters more than it looks. Editing .env can set
QUERY_AUTH_MODE=proxy, after which auth.py believes a header -- so these
endpoints check users.is_admin directly (`superuser`), the only admin
concept there is.
"""

from __future__ import annotations

import api
import pytest
import settings
from accounts import Accounts
from auth import Auth
from db import ChatDB
from fastapi import HTTPException
from limits import Gate, GateConfig, UploadLimits

VALID = """CHAT_MODEL_EVERYDAY=small-model
CHAT_MODEL_HARD=big-model
EMBED_MODEL=bge-m3
RAG_RERANK=false
"""


@pytest.fixture
def service(tmp_path, monkeypatch):
    """api.state wired to a temp database and a temp .env, with no network."""
    db = ChatDB(tmp_path / "chat.db")
    accounts = Accounts(db)
    monkeypatch.setattr(api.state, "db", db, raising=False)
    monkeypatch.setattr(api.state, "accounts", accounts, raising=False)
    monkeypatch.setattr(api.state, "auth", Auth(accounts), raising=False)
    monkeypatch.setattr(api.state, "api_key", "", raising=False)
    monkeypatch.setattr(api.state, "gate", Gate(GateConfig()), raising=False)
    monkeypatch.setattr(api.state, "uploads", UploadLimits(), raising=False)
    monkeypatch.setattr(api.state, "ingest", api.IngestRunner(autorun=False), raising=False)

    env_path = tmp_path / ".env"
    env_path.write_text(VALID)
    monkeypatch.setattr(settings, "ENV_PATH", env_path)
    monkeypatch.setattr(settings, "BACKUP_DIR", tmp_path / "env-backups")
    monkeypatch.setattr(settings, "ENV_EXAMPLE_PATH", tmp_path / ".env.example")
    return accounts


@pytest.fixture
def admin(service):
    return service.create_user("admin@example.com", password_hash="hash", display_name="Admin", is_admin=True)


# -------------------------------------------------------------------- gate --

def test_an_admin_reaches_the_system_endpoints(admin):
    assert api.superuser(admin) is admin


def test_an_ordinary_user_is_refused(service):
    user = service.create_user("member@example.com", password_hash="hash", display_name="Member")
    with pytest.raises(HTTPException) as exc:
        api.superuser(user)
    assert exc.value.status_code == 403


# ---------------------------------------------------------------- env edits --

def test_reading_the_env_returns_the_file_and_its_backups(admin):
    body = api.admin_read_env(admin)
    assert body["text"] == VALID
    assert body["problems"] == []
    assert body["backups"] == []


def test_checking_reports_problems_without_writing(admin):
    body = api.admin_check_env(api.EnvCheck(text=VALID + "CHAT_TEMPERATURE=warm\n"), admin)

    assert any("CHAT_TEMPERATURE" in p for p in body["problems"])
    assert settings.ENV_PATH.read_text() == VALID


def test_checking_reports_the_reload_tier_a_change_would_need(admin):
    body = api.admin_check_env(api.EnvCheck(text=VALID + "QUERY_PORT=9000\n"), admin)
    assert body["tier"] == "stack"


def test_saving_writes_the_file(admin):
    body = api.admin_write_env(api.EnvSave(text=VALID + "CHAT_TEMPERATURE=0.5\n"), admin)

    assert "CHAT_TEMPERATURE=0.5" in settings.ENV_PATH.read_text()
    assert body["tier"] == "runtime"
    assert body["backup"]


def test_saving_something_invalid_is_a_400_and_changes_nothing(admin):
    with pytest.raises(HTTPException) as exc:
        api.admin_write_env(api.EnvSave(text=VALID + "CHAT_TEMPERATURE=warm\n"), admin)

    assert exc.value.status_code == 400
    assert settings.ENV_PATH.read_text() == VALID


def test_saving_over_someone_elses_edit_is_a_409(admin):
    stale = settings.stamp()
    api.admin_write_env(api.EnvSave(text=VALID + "CHAT_TEMPERATURE=0.5\n"), admin)

    with pytest.raises(HTTPException) as exc:
        api.admin_write_env(api.EnvSave(text=VALID + "CHAT_TEMPERATURE=0.9\n", stamp=stale), admin)
    assert exc.value.status_code == 409


def test_a_backup_can_be_read_back(admin):
    saved = api.admin_write_env(api.EnvSave(text=VALID + "CHAT_TEMPERATURE=0.5\n"), admin)
    assert api.admin_read_env_backup(saved["backup"], admin)["text"] == VALID


def test_an_unknown_backup_name_is_a_404(admin):
    with pytest.raises(HTTPException) as exc:
        api.admin_read_env_backup("../../../etc/passwd", admin)
    assert exc.value.status_code == 404


# ------------------------------------------------------------------ reload --

def test_a_reload_rebuilds_the_runtime_from_the_environment(admin, monkeypatch):
    monkeypatch.setattr(api, "load_env", lambda: {**settings.parse(VALID), "CHAT_TEMPERATURE": "0.7"})
    api.build_runtime(api.load_env())
    before = api.state.retriever

    monkeypatch.setattr(api, "load_env", lambda: {**settings.parse(VALID), "CHAT_TEMPERATURE": "0.9"})
    body = api.admin_reload(admin)

    assert body["reloaded"] is True
    assert api.state.chat.cfg.temperature == 0.9
    assert api.state.retriever is not before  # genuinely rebuilt, not patched in place


def test_a_reload_picks_up_the_api_key_and_the_upload_quota(admin, monkeypatch):
    monkeypatch.setattr(api, "load_env", lambda: {**settings.parse(VALID),
                                                 "QUERY_API_KEY": "fresh-key", "INGEST_MAX_FILES_PER_WINDOW": "3"})
    api.admin_reload(admin)

    assert api.state.api_key == "fresh-key"
    assert api.state.uploads.max_files == 3


def test_a_reload_that_fails_leaves_the_running_configuration_alone(admin, monkeypatch):
    monkeypatch.setattr(api, "load_env", lambda: settings.parse(VALID))
    api.build_runtime(api.load_env())
    working = api.state.chat

    monkeypatch.setattr(api, "load_env", lambda: {**settings.parse(VALID), "CHAT_TEMPERATURE": "warm"})
    with pytest.raises(HTTPException) as exc:
        api.admin_reload(admin)

    assert exc.value.status_code == 400
    assert api.state.chat is working  # still serving the configuration that worked


def test_a_reload_says_which_keys_it_could_not_apply(admin, monkeypatch):
    """A reload that silently skipped the session secret would be worse than
    one that refused: the admin would believe it had taken effect."""
    monkeypatch.setattr(api, "load_env", lambda: settings.parse(VALID))
    body = api.admin_reload(admin)

    assert "QUERY_SESSION_SECRET" in body["not_applied"]
    assert "QUERY_PORT" in body["not_applied"]


def test_the_ingest_autorun_flag_follows_the_environment(admin, monkeypatch):
    monkeypatch.setattr(api, "load_env", lambda: {**settings.parse(VALID), "INGEST_AUTORUN": "true"})
    api.admin_reload(admin)
    assert api.state.ingest.autorun is True

    monkeypatch.setattr(api, "load_env", lambda: settings.parse(VALID))
    api.admin_reload(admin)
    assert api.state.ingest.autorun is False


# ------------------------------------------------------------ stack reload --

def test_a_second_stack_reload_while_one_runs_is_refused(admin, monkeypatch):
    """One `ragctl reload` at a time: two would race over the same rendered
    files and LaunchAgents."""
    monkeypatch.setattr(api.stack_reloader, "_running", True)
    with pytest.raises(HTTPException) as exc:
        api.admin_stack_reload(admin)
    assert exc.value.status_code == 409


def test_the_stack_reload_runs_ragctl_without_touching_the_query_agent(admin, monkeypatch):
    """--skip-query is what stops the command booting out the process that
    started it, between the bootout and the bootstrap."""
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(api.subprocess, "run", fake_run)
    api.stack_reloader._run()

    assert seen["argv"][1:] == ["reload", "--skip-query"]
    assert api.stack_reloader.last_run["exit_code"] == 0


def test_a_failed_stack_reload_is_reported_not_swallowed(admin, monkeypatch):
    monkeypatch.setattr(api.subprocess, "run",
                        lambda argv, **kw: type("Completed", (), {"returncode": 2})())
    api.stack_reloader._run()

    assert api.stack_reloader.last_run["exit_code"] == 2
    assert api.stack_reloader.status()["running"] is False

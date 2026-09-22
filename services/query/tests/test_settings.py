"""
Editing .env over HTTP. The risk here is not a wrong value -- it is a write
that leaves the stack unable to start, or a value that ragctl's `source`
executes, so those are what the tests push on.
"""

from __future__ import annotations

import os

import pytest
import settings
from accounts import Invalid

VALID = """# rag-stack
CHAT_MODEL_EVERYDAY=small-model
CHAT_MODEL_HARD=big-model
EMBED_MODEL=bge-m3
"""


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """settings reads module-level paths, so point them at a temp copy."""
    path = tmp_path / ".env"
    path.write_text(VALID)
    monkeypatch.setattr(settings, "ENV_PATH", path)
    monkeypatch.setattr(settings, "BACKUP_DIR", tmp_path / "env-backups")
    monkeypatch.setattr(settings, "ENV_EXAMPLE_PATH", tmp_path / ".env.example")
    return path


# ------------------------------------------------------------------ parsing --

def test_a_setting_that_is_not_a_shell_identifier_is_refused():
    assert "not a valid setting name" in settings.lint("2FAST=yes")[0]


def test_a_line_that_is_neither_a_comment_nor_an_assignment_is_refused():
    assert "has no '='" in settings.lint("CHAT_MODEL_EVERYDAY\n")[0]


@pytest.mark.parametrize("value", ["$(rm -rf /)", "`id`", "prefix$(whoami)suffix"])
def test_command_substitution_in_a_value_is_refused(value):
    """ragctl does `set -a; source .env`, so a value like this would run."""
    problems = settings.lint(f"CHAT_MODEL_EVERYDAY={value}")
    assert problems and "source .env" in problems[0]


@pytest.mark.parametrize("value", ["foo; touch /tmp/pwn", "foo && touch /tmp/pwn", "foo | tee /tmp/pwn",
                                   "foo > /tmp/pwn", "foo < /etc/passwd", "two words", "$HOME", '"$(id)"',
                                   "'unbalanced", "back\\slash"])
def test_anything_the_shell_would_act_on_is_refused(value):
    """dotenv reads each of these as a plain string, but `source` runs them."""
    problems = settings.lint(f"CHAT_MODEL_EVERYDAY={value}")
    assert problems and "source .env" in problems[0]


def test_a_space_before_the_equals_is_refused():
    """`reboot =1` is bash running `reboot`, not an assignment."""
    assert "not a valid setting name" in settings.lint("reboot =1")[0]


@pytest.mark.parametrize("value", ["", "http://127.0.0.1:11434", "qwen3:30b-a3b", "'a; b'", '"a b"', "x # note"])
def test_values_bash_and_dotenv_read_alike_are_accepted(value):
    assert settings.lint(f"CHAT_MODEL_EVERYDAY={value}") == []


def test_comments_and_blank_lines_are_fine():
    assert settings.lint("# a comment\n\n   \nKEY=value\n") == []


def test_export_prefixed_lines_are_accepted():
    assert settings.lint("export CHAT_MODEL_EVERYDAY=small\n") == []


# --------------------------------------------------------------- validation --

def test_a_valid_file_has_no_problems():
    assert settings.validate(VALID) == []


def test_validation_uses_the_real_config_loaders():
    """The messages come from the constructors the service actually runs, so
    they can't drift from what a failed start would say."""
    problems = settings.validate(VALID + "CHAT_TEMPERATURE=warm\n")
    assert problems == ["chat: CHAT_TEMPERATURE must be a number, got 'warm'"]


def test_a_missing_model_is_a_problem():
    assert any("EMBED_MODEL" in p for p in settings.validate("CHAT_MODEL_EVERYDAY=a\nCHAT_MODEL_HARD=b\n"))


def test_an_unknown_auth_mode_is_a_problem():
    problems = settings.validate(VALID + "QUERY_AUTH_MODE=sso\n")
    assert any("QUERY_AUTH_MODE" in p for p in problems)


def test_structural_problems_are_reported_without_running_the_loaders():
    """A file that doesn't parse would fail the loaders in confusing ways;
    only the parse errors come back."""
    problems = settings.validate("2FAST=yes\nCHAT_TEMPERATURE=warm\n")
    assert all("not a valid setting name" in p for p in problems)


# ------------------------------------------------------------------- saving --

def test_saving_replaces_the_file_and_keeps_a_backup(env_file):
    result = settings.save(VALID + "CHAT_TEMPERATURE=0.5\n")

    assert "CHAT_TEMPERATURE=0.5" in env_file.read_text()
    assert result["backup"] is not None
    assert settings.backup_text(result["backup"]) == VALID


def test_an_invalid_file_is_refused_and_nothing_is_written(env_file):
    with pytest.raises(Invalid):
        settings.save(VALID + "CHAT_TEMPERATURE=warm\n")

    assert env_file.read_text() == VALID
    assert settings.backups() == []


def test_a_stale_stamp_is_refused_so_two_admins_dont_overwrite_each_other(env_file):
    stale = settings.stamp()
    settings.save(VALID + "CHAT_TEMPERATURE=0.5\n")  # someone else saves

    with pytest.raises(settings.Conflict):
        settings.save(VALID + "CHAT_TEMPERATURE=0.9\n", stale)

    assert "CHAT_TEMPERATURE=0.5" in env_file.read_text()


def test_the_matching_stamp_is_accepted(env_file):
    settings.save(VALID + "CHAT_TEMPERATURE=0.5\n", settings.stamp())
    assert "CHAT_TEMPERATURE=0.5" in env_file.read_text()


def test_a_saved_file_ends_with_a_newline(env_file):
    settings.save(VALID.rstrip("\n"))
    assert env_file.read_text().endswith("\n")


def test_comments_survive_a_round_trip(env_file):
    text = "# keep me\nCHAT_MODEL_EVERYDAY=small-model\nCHAT_MODEL_HARD=b\nEMBED_MODEL=bge-m3\n# and me\n"
    settings.save(text)
    assert settings.read()["text"] == text


def test_only_the_most_recent_backups_are_kept(env_file, monkeypatch):
    monkeypatch.setattr(settings, "KEEP_BACKUPS", 2)
    for value in ("0.1", "0.2", "0.3", "0.4"):
        settings.save(VALID + f"CHAT_TEMPERATURE={value}\n")
    assert len(settings.backups()) == 2


def test_a_backup_name_from_the_url_cannot_escape_the_backup_directory(env_file):
    for name in ("../../../etc/passwd", "env-x/../../.env", "..", "env-2020"):
        with pytest.raises(Invalid):
            settings.backup_text(name)


# ------------------------------------------------------------------ impact --

def test_an_ordinary_setting_only_needs_a_live_reload():
    assert settings.impact(VALID, VALID + "CHAT_TEMPERATURE=0.5\n")["tier"] == "runtime"


def test_the_session_secret_needs_a_restart():
    """SessionMiddleware is built at import time, before the app starts, so no
    in-process reload can reach it."""
    result = settings.impact(VALID, VALID + "QUERY_SESSION_SECRET=new\n")
    assert result["tier"] == "restart"
    assert result["needs_restart"] == ["QUERY_SESSION_SECRET"]


def test_a_port_needs_the_whole_stack():
    """The port is baked into the rendered LaunchAgent script, so only ragctl
    can change it."""
    result = settings.impact(VALID, VALID.replace("EMBED_MODEL=bge-m3", "EMBED_MODEL=bge-m3\nQUERY_PORT=9000"))
    assert result["tier"] == "stack"
    assert result["needs_stack"] == ["QUERY_PORT"]


def test_an_unchanged_file_needs_nothing():
    assert settings.impact(VALID, VALID)["tier"] == "none"


def test_a_removed_setting_counts_as_a_change():
    assert settings.impact(VALID + "CHAT_TEMPERATURE=0.5\n", VALID)["changed"] == ["CHAT_TEMPERATURE"]


def test_reordering_and_recommenting_is_not_a_change():
    reordered = "EMBED_MODEL=bge-m3\n# moved\nCHAT_MODEL_HARD=big-model\nCHAT_MODEL_EVERYDAY=small-model\n"
    assert settings.impact(VALID, reordered)["changed"] == []


# --------------------------------------------------------------- shadowing --

def test_a_key_the_process_environment_overrides_is_reported(monkeypatch):
    """load_env() lets os.environ win, so editing such a key in the UI would
    appear to work and change nothing."""
    monkeypatch.setenv("CHAT_MODEL_EVERYDAY", "from-the-environment")
    assert settings.shadowed(VALID) == ["CHAT_MODEL_EVERYDAY"]


def test_a_key_the_environment_agrees_with_is_not_reported(monkeypatch):
    monkeypatch.setenv("CHAT_MODEL_EVERYDAY", "small-model")
    assert settings.shadowed(VALID) == []


def test_validation_sees_what_the_service_would_see(monkeypatch):
    """os.environ wins over the file, so a broken value there breaks the
    service however good the file looks."""
    monkeypatch.setenv("CHAT_TEMPERATURE", "warm")
    assert any("CHAT_TEMPERATURE" in p for p in settings.validate(VALID))


# ------------------------------------------------------------------ reading --

def test_read_reports_the_current_file_with_its_stamp(env_file):
    current = settings.read()
    assert current["text"] == VALID
    assert current["problems"] == []
    assert current["stamp"] == settings.stamp()


def test_read_reports_problems_with_a_file_that_is_already_broken(env_file):
    env_file.write_text(VALID + "CHAT_TEMPERATURE=warm\n")
    assert settings.read()["problems"]

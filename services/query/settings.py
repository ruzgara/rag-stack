"""
The .env file, edited over HTTP, and the reload that makes an edit take effect.

.env is a documented 200-line file, so it is read and written as raw text: a
parse into key/value and back would throw away every comment explaining what
the keys mean. The editor round-trips exactly what the admin typed.

Three things make writing it from a browser safe enough to do at all:

  validation   the proposed text is run through the real config constructors
               (SearchConfig, ChatConfig, GateConfig, UploadLimits, Auth's
               mode check) before anything is written, so a typo is a 400 and
               not a service that won't start.
  shell safety ragctl `source`s this file (set -a; source .env), so a value
               holding a backtick or $( ) would be *executed* by the next
               ragctl run. Those are refused, as are keys that aren't
               identifiers.
  backups      every save copies the previous file to data/env-backups/ first
               and writes through a temp file + os.replace, so the file is
               never half-written and a bad save can be undone from the UI
               rather than from a shell.

Secrets are not masked. Masking cannot round-trip -- the mask would be saved
back over the real value -- so the whole file is shown as it is, and reaching
it requires an admin account (api.py gates these endpoints on is_admin, which
only the terminal can grant -- ragctl user admin -- because editing .env can
set QUERY_AUTH_MODE=proxy and hand identity to anyone who can set a header).

Reload works in tiers, because not everything is reachable from inside the
running process. `impact()` diffs the old and new text and says which tier an
edit needs:

  runtime   rebuild the retriever, chat service, gate, quotas and API key from
            a fresh load_env(). Most keys land here.
  restart   the query process has to start again: SessionMiddleware is
            installed at import time (QUERY_SESSION_SECRET, QUERY_HTTPS_ONLY)
            and the listening port is uvicorn's argument.
  stack     ragctl has to re-render the LaunchAgent scripts and restart the
            other services (ports, Ollama and docling tuning, INGEST_AUTORUN).
"""

from __future__ import annotations

import io
import os
import re
import tempfile
import time
from pathlib import Path

from dotenv import dotenv_values

from accounts import Invalid
from chat import ChatConfig
from limits import GateConfig, UploadLimits
from retrieval import REPO_ROOT, SearchConfig

ENV_PATH = REPO_ROOT / ".env"
ENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"
BACKUP_DIR = REPO_ROOT / "data" / "env-backups"
KEEP_BACKUPS = 20

# A .env key has to be a shell identifier: `source` would choke on anything
# else, and a key with a space in it is a typo that would silently do nothing.
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# ragctl runs `set -a; source .env`, so every line is also a line of bash. A
# value is only accepted in a form bash and dotenv read the same way, with
# nothing bash would execute or expand: a bare word of safe characters, a
# single-quoted string, or a double-quoted one free of $, ` and \. Anything
# else (;, &&, |, <, >, spaces, $VAR, `...`, $(...)) is refused rather than
# picked apart.
SAFE_VALUE = re.compile(r"""(?:[A-Za-z0-9_./:@%+,=-]*|'[^'\n]*'|"[^"$`\\!\n]*")(?:[ \t]+#.*)?""")

# Keys the running process cannot pick up: the session middleware is built
# once at import, before startup, and the port belongs to uvicorn's command
# line.
RESTART_KEYS = {"QUERY_SESSION_SECRET", "QUERY_HTTPS_ONLY", "QUERY_PORT"}

# Keys that only reach anything through ragctl: the rendered LaunchAgent
# scripts (launchd/*.tmpl) and compose.yaml read these, not api.py.
STACK_KEYS = {
    "QUERY_PORT", "BIND_ADDR", "QDRANT_PORT", "DOCLING_PORT", "OLLAMA_PORT",
    "OLLAMA_CONTEXT_LENGTH", "OLLAMA_NUM_PARALLEL", "OLLAMA_MAX_LOADED_MODELS", "OLLAMA_MAX_QUEUE",
    "DOCLING_NUM_THREADS", "DOCLING_LOC_NUM_WORKERS", "INGEST_AUTORUN",
}


class Conflict(Exception):
    """The file moved under the editor."""


def parse(text: str) -> dict[str, str]:
    """The keys a .env text defines, the same way the services read it."""
    return {k: (v or "") for k, v in dotenv_values(stream=io.StringIO(text)).items()}


def effective(text: str) -> dict[str, str]:
    """What the service would actually see. load_env() lets the real
    environment win over the file, so validation has to do the same or it
    would check a configuration nobody runs."""
    return {**parse(text), **os.environ}


def shadowed(text: str) -> list[str]:
    """Keys the file sets that the process environment overrides anyway --
    editing one of these in the UI changes nothing until the process is
    started without it. Worth saying out loud rather than letting an admin
    watch a save succeed and do nothing."""
    return sorted(k for k, v in parse(text).items() if k in os.environ and os.environ[k] != v)


def lint(text: str) -> list[str]:
    """Structural and shell-safety problems, as messages an admin can act on."""
    problems: list[str] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            problems.append(f"line {number}: {raw.strip()!r} is not a comment and has no '='")
            continue
        # Not stripped: `KEY =x` is bash running a command called KEY.
        key, value = line.split("=", 1)
        if not KEY_RE.match(key):
            problems.append(f"line {number}: {key.strip()!r} is not a valid setting name "
                            "(letters, digits and underscores, not starting with a digit, "
                            "with no space before the '=')")
        elif not SAFE_VALUE.fullmatch(value):
            problems.append(f"{key}: the value has characters the shell would act on "
                            "(spaces, ; & | < > $ ` \\ or unbalanced quotes) -- ragctl runs "
                            "`source .env`, which would execute it. Quote it with '...' if it "
                            "really needs them")
    return problems


def validate(text: str) -> list[str]:
    """Every reason this text would not start the service. Empty means good.

    The real config constructors do the checking, so the messages here are the
    same ones the service would print on a failed start, rather than a second
    set of rules that could drift from them.
    """
    problems = lint(text)
    if problems:
        # The constructors would fail confusingly on a file that doesn't parse.
        return problems
    env = effective(text)
    for describe, build in (
        ("search", lambda: SearchConfig.from_env(env)),
        ("chat", lambda: ChatConfig.from_env(env)),
        ("limits", lambda: GateConfig.from_env(env)),
        ("uploads", lambda: UploadLimits.from_env(env)),
        ("auth", lambda: _check_auth_mode(env)),
    ):
        try:
            build()
        except Exception as exc:  # noqa: BLE001 -- any failure is a reason to refuse
            problems.append(f"{describe}: {exc}")
    return problems


def _check_auth_mode(env: dict) -> None:
    mode = (env.get("QUERY_AUTH_MODE") or "password").strip().lower()
    if mode not in ("password", "proxy"):
        raise ValueError(f"QUERY_AUTH_MODE is 'password' or 'proxy', not {mode!r}")


def read() -> dict:
    """The current file, with the stamp a save has to quote back."""
    text = ENV_PATH.read_text() if ENV_PATH.exists() else ""
    return {"text": text, "stamp": stamp(), "path": str(ENV_PATH),
            "shadowed": shadowed(text), "problems": validate(text)}


def example() -> str:
    return ENV_EXAMPLE_PATH.read_text() if ENV_EXAMPLE_PATH.exists() else ""


def stamp() -> str:
    """Identifies the file's current content, so two admins editing at once
    don't silently overwrite each other."""
    if not ENV_PATH.exists():
        return "absent"
    st = ENV_PATH.stat()
    return f"{st.st_mtime_ns}-{st.st_size}"


def impact(old: str, new: str) -> dict:
    """Which keys an edit changes, and how far a reload has to go to apply
    them. 'stack' implies 'restart' implies 'runtime'."""
    before, after = parse(old), parse(new)
    changed = sorted({k for k in {*before, *after} if before.get(k) != after.get(k)})
    needs_stack = sorted(k for k in changed if k in STACK_KEYS)
    needs_restart = sorted(k for k in changed if k in RESTART_KEYS)
    tier = "stack" if needs_stack else "restart" if needs_restart else "runtime" if changed else "none"
    return {"changed": changed, "needs_restart": needs_restart, "needs_stack": needs_stack, "tier": tier}


def save(text: str, expected_stamp: str | None = None) -> dict:
    """Validates, backs up and replaces .env. Returns what changed and what
    reload tier it needs."""
    if expected_stamp is not None and expected_stamp != stamp():
        raise Conflict(".env changed on disk since you opened it; reload the page and reapply your edit")
    problems = validate(text)
    if problems:
        raise Invalid("; ".join(problems))
    if text and not text.endswith("\n"):
        text += "\n"
    previous = ENV_PATH.read_text() if ENV_PATH.exists() else ""
    backup = _back_up(previous)
    _atomic_write(ENV_PATH, text)
    return {"stamp": stamp(), "backup": backup, "shadowed": shadowed(text), **impact(previous, text)}


def _atomic_write(path: Path, text: str) -> None:
    """Writes through a temp file in the same directory, so a crash mid-write
    leaves the old file intact rather than a truncated one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _back_up(previous: str) -> str | None:
    if not previous:
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    # The counter matters: two saves in the same second would otherwise share
    # a name, and the second would quietly overwrite the first backup.
    stem = f"env-{time.strftime('%Y%m%d-%H%M%S')}"
    name = next(f"{stem}-{n:03d}" for n in range(1, 1000) if not (BACKUP_DIR / f"{stem}-{n:03d}").exists())
    _atomic_write(BACKUP_DIR / name, previous)
    _prune_backups()
    return name


def _prune_backups() -> None:
    kept = sorted(BACKUP_DIR.glob("env-*"), key=lambda p: p.name, reverse=True)
    for stale in kept[KEEP_BACKUPS:]:
        stale.unlink(missing_ok=True)


def backups() -> list[dict]:
    if not BACKUP_DIR.exists():
        return []
    return [{"name": p.name, "saved_at": p.stat().st_mtime, "bytes": p.stat().st_size}
            for p in sorted(BACKUP_DIR.glob("env-*"), key=lambda p: p.name, reverse=True)]


def backup_text(name: str) -> str:
    """A backup's contents, by name. The name is checked rather than joined
    blindly: it arrives from an HTTP path."""
    if not re.fullmatch(r"env-[0-9]{8}-[0-9]{6}-[0-9]+", name):
        raise Invalid(f"{name!r} is not a backup name")
    path = BACKUP_DIR / name
    if not path.exists():
        raise Invalid(f"no backup {name}")
    return path.read_text()

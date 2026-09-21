"""
The rag-stack query service: HTTP API over retrieval, chat and storage.

Native API (for the rag-stack UI) under /api, OpenAI-compatible API under /v1
(see openai_compat.py). Run by the com.ragstack.query LaunchAgent:

  uvicorn api:app --host 127.0.0.1 --port $QUERY_PORT

It binds to 127.0.0.1. Every endpoint except /api/health identifies its
caller -- a signed session cookie, a proxy header, or QUERY_API_KEY as the
service account -- and, where it matters, checks what that caller may do
(see accounts.py: library ownership and sharing, and users.is_admin for
everything else). Admins additionally get /api/admin/system, which edits
.env and reloads the service; the admin UI for all of it is served at
/admin.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Iterator

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from starlette.middleware.sessions import SessionMiddleware
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from chat import ChatConfig, ChatError, ChatService
from accounts import Accounts, Denied, Invalid, User
from auth import SESSION_MAX_AGE, Auth, as_http_error, hash_password, session_secret, verify_password
import settings
from db import ChatDB, NotFound, WorkspaceLibraries
from limits import Busy, Gate, GateConfig, UploadLimits
from retrieval import REPO_ROOT, Retriever, SearchConfig, load_env

DATA_DIR = REPO_ROOT / "data"
INBOX = DATA_DIR / "inbox"
MANIFEST = DATA_DIR / "manifest.jsonl"
LOG_DIR = DATA_DIR / "logs"

# Same list as services/ingest/ingest.py; files with other suffixes would sit
# in the inbox unprocessed.
SUPPORTED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp",
                      ".docx", ".pptx", ".xlsx", ".html", ".htm", ".md", ".txt", ".asciidoc"}


class IngestRunner:
    """Runs `ragctl ingest` then `ragctl embed` in the background after an
    upload, one run at a time. Uploads arriving during a run trigger one
    more run afterwards. With INGEST_AUTORUN=true the inbox LaunchAgent
    already does this, so the runner stays idle."""

    def __init__(self, autorun: bool):
        self.autorun = autorun
        self._lock = threading.Lock()
        self._running = False
        self._pending = False
        self.last_run: dict | None = None

    def trigger(self) -> str:
        if self.autorun:
            return "handled by the inbox watcher (INGEST_AUTORUN=true)"
        with self._lock:
            if self._running:
                self._pending = True
                return "queued after the current run"
            self._running = True
        threading.Thread(target=self._loop, daemon=True).start()
        return "started"

    def _loop(self) -> None:
        while True:
            t0 = time.time()
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            code = 0
            with (LOG_DIR / "ingest-api.log").open("a") as log:
                log.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} upload-triggered run ====\n")
                log.flush()
                # Each service has its own uv environment; don't leak this one's.
                env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
                for step in ("ingest", "embed"):
                    code = subprocess.run([str(REPO_ROOT / "ragctl"), step], stdout=log, stderr=subprocess.STDOUT,
                                          env=env).returncode
                    if code != 0:
                        break
            self.last_run = {"started_at": t0, "finished_at": time.time(), "exit_code": code}
            with self._lock:
                if not self._pending:
                    self._running = False
                    return
                self._pending = False

    def status(self) -> dict:
        return {"autorun": self.autorun, "running": self._running, "pending": self._pending, "last_run": self.last_run}


class State:
    retriever: Retriever
    chat: ChatService
    db: ChatDB
    ingest: IngestRunner
    gate: Gate
    uploads: UploadLimits
    accounts: Accounts
    auth: Auth
    api_key: str


state = State()


def build_runtime(env: dict) -> None:
    """Builds everything that reads .env, from a fresh environment.

    Startup and POST /api/admin/system/reload both come through here, so a
    reload configures the service exactly as a restart would rather than
    through a second, drifting code path. The database and the accounts on it
    are deliberately not rebuilt: their path is fixed, and dropping the
    connection would only interrupt requests in flight.

    Everything is constructed before anything is swapped in, so a config that
    turns out to be bad leaves the running service untouched.
    """
    retriever = Retriever(SearchConfig.from_env(env))
    try:
        chat = ChatService(retriever, ChatConfig.from_env(env))
        gate = Gate(GateConfig.from_env(env))
        uploads = UploadLimits.from_env(env)
        auth = Auth(state.accounts, env)
    except BaseException:
        retriever.close()
        raise

    previous = getattr(state, "retriever", None)
    state.retriever, state.chat, state.gate, state.uploads, state.auth = retriever, chat, gate, uploads, auth
    state.ingest.autorun = (env.get("INGEST_AUTORUN") or "false").strip().lower() == "true"
    state.api_key = (env.get("QUERY_API_KEY") or "").strip()
    if previous is not None and previous is not retriever:
        previous.close()
    if retriever.cfg.rerank:
        # Load the reranker onto the GPU now rather than on the first question.
        threading.Thread(target=lambda: retriever.reranker, daemon=True).start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    env = load_env()
    state.db = ChatDB(DATA_DIR / "chat.db")
    state.accounts = Accounts(state.db)
    state.ingest = IngestRunner(autorun=False)
    build_runtime(env)
    yield
    state.retriever.close()


app = FastAPI(title="rag-stack query service", lifespan=lifespan)

# The session cookie's signing key and flags are middleware, which has to be in
# place before the app starts, so they are read here rather than in lifespan.
# https_only is off by default because the service starts on plain http; turn
# it on (QUERY_HTTPS_ONLY=true) once it is behind TLS, so the cookie can't
# leak over http.
_env = load_env()
app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret(_env, DATA_DIR),
    session_cookie="ragstack_session",
    max_age=SESSION_MAX_AGE,
    same_site="lax",  # a cross-site POST carries no cookie, which closes CSRF
    https_only=(_env.get("QUERY_HTTPS_ONLY") or "false").strip().lower() in ("1", "true", "yes", "on"),
)


# A real address shape, because every account is stored as an email.
SERVICE_ACCOUNT_EMAIL = "service@rag-stack.local"


def service_key_user(request: Request) -> User | None:
    """The caller presenting QUERY_API_KEY, as a user. Scripts and the
    OpenAI-compatible endpoint authenticate this way; the key is a service
    account with its own row and its own grants, not a way past them."""
    if not state.api_key or request.headers.get("authorization", "") != f"Bearer {state.api_key}":
        return None
    account = state.accounts.by_email(SERVICE_ACCOUNT_EMAIL)
    if not account:
        # Created on first use so its conversations and quota are its own.
        account = state.accounts.create_user(SERVICE_ACCOUNT_EMAIL, display_name="API key",
                                             is_admin=not state.accounts.any_users())
    return account


def current_user(request: Request) -> User:
    """Every endpoint but /api/health and login goes through here."""
    user = service_key_user(request) or state.auth.current_user(request)
    if not user:
        raise HTTPException(401, "not signed in", headers={"WWW-Authenticate": "Bearer"})
    return user


def admin(user: User = Depends(current_user)) -> User:
    """The only admin concept there is: users.is_admin. No partial admins --
    users.manage and libraries.manage are gone, so this one dependency
    covers every admin-only route."""
    if not user.is_admin:
        raise HTTPException(403, "only an admin can do this")
    return user


def superuser(user: User = Depends(current_user)) -> User:
    """Same check as `admin`, kept as its own dependency for the system
    endpoints: they edit .env, and .env can set QUERY_AUTH_MODE=proxy --
    after which anyone able to set a header is anyone they like. There's
    only one kind of admin now, but the distinct name and message keep that
    reasoning attached to the routes it's about."""
    if not user.is_admin:
        raise HTTPException(403, "only an admin can change the system configuration")
    return user


CurrentUser = Depends(current_user)
Admin = Depends(admin)
Superuser = Depends(superuser)


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"



# ------------------------------------------------------------------- auth --

class Credentials(BaseModel):
    email: str
    password: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


class NewUser(BaseModel):
    email: str
    password: str | None = None      # omitted in proxy mode
    display_name: str
    is_admin: bool = False


class UserUpdate(BaseModel):
    display_name: str | None = None
    is_admin: bool | None = None
    disabled: bool | None = None
    password: str | None = None


class NewGroup(BaseModel):
    name: str
    description: str | None = None
    type: str = "locked"             # open | locked -- see Accounts.create_group


class AccessRequest(BaseModel):
    """Who gets a role on a library, named in the URL -- see
    Accounts.set_library_access."""
    subject_type: str                # user | group
    subject_id: str
    role: str                        # read | write


class NewLibrary(BaseModel):
    name: str
    description: str | None = None


class LibraryUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class NewWorkspace(BaseModel):
    name: str
    description: str | None = None


class WorkspaceUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    library_ids: list[str] | None = None


def me(user: User) -> dict:
    """The caller, for a UI to render itself around. What they may do on any
    given library isn't here -- it's per library (GET /api/libraries'
    `role`), not a global map -- user.is_admin is the only global fact."""
    return {"user": user.public(), "auth_mode": state.auth.mode}


@app.post("/api/auth/login")
def login(body: Credentials, request: Request) -> dict:
    if state.auth.mode != "password":
        raise HTTPException(400, f"this service authenticates through a proxy ({state.auth.mode} mode)")
    return me(state.auth.login(request, body.email, body.password))


@app.post("/api/auth/logout", status_code=204)
def logout(request: Request) -> None:
    state.auth.logout(request)


@app.get("/api/auth/me")
def whoami(user: User = CurrentUser) -> dict:
    return me(user)


@app.post("/api/auth/password", status_code=204)
def change_password(body: PasswordChange, user: User = CurrentUser) -> None:
    """Changes your own password, which signs your other sessions out."""
    ok, _ = verify_password(body.current_password, user.password_hash)
    if not ok:
        raise HTTPException(403, "current password is wrong")
    try:
        state.accounts.set_password(user.id, hash_password(body.new_password))
    except Invalid as exc:
        raise as_http_error(exc) from None


# ------------------------------------------------------------------ admin --
# What the admin UI at /admin calls, and what `ragctl user` does locally --
# the two are deliberately the same set of operations.

@app.get("/api/admin/users")
def admin_list_users(admin: User = Admin) -> dict:
    """Every user. What each may do on a given library is per library now
    (GET /api/libraries' `role`), not a global map to resolve here."""
    return {"users": [u.public() for u in state.accounts.users()]}


@app.post("/api/admin/users", status_code=201)
def admin_create_user(body: NewUser, admin: User = Admin) -> dict:
    try:
        password_hash = hash_password(body.password) if body.password else None
        if state.auth.mode == "password" and not password_hash:
            raise Invalid("a password is required while QUERY_AUTH_MODE=password")
        user = state.accounts.create_user(body.email, password_hash, body.display_name, body.is_admin)
    except Invalid as exc:
        raise as_http_error(exc) from None
    return user.public()


@app.patch("/api/admin/users/{user_id}")
def admin_update_user(user_id: str, body: UserUpdate, admin: User = Admin) -> dict:
    if user_id == admin.id and body.disabled:
        raise HTTPException(400, "you can't disable your own account")
    try:
        if body.password:
            state.accounts.set_password(user_id, hash_password(body.password))
        user = state.accounts.update_user(user_id, display_name=body.display_name, is_admin=body.is_admin,
                                          disabled=body.disabled)
    except NotFound:
        raise HTTPException(404, "no such user") from None
    except Invalid as exc:
        raise as_http_error(exc) from None
    return user.public()


@app.delete("/api/admin/users/{user_id}", status_code=204)
def admin_delete_user(user_id: str, admin: User = Admin) -> None:
    if user_id == admin.id:
        raise HTTPException(400, "you can't delete your own account")
    try:
        state.accounts.delete_user(user_id)
    except NotFound:
        raise HTTPException(404, "no such user") from None


@app.get("/api/admin/groups")
def admin_list_groups(admin: User = Admin) -> dict:
    return {"groups": state.accounts.groups()}


@app.post("/api/admin/groups", status_code=201)
def admin_create_group(body: NewGroup, admin: User = Admin) -> dict:
    try:
        return state.accounts.create_group(body.name, body.description, kind=body.type)
    except Invalid as exc:
        raise as_http_error(exc) from None


@app.delete("/api/admin/groups/{group_id}", status_code=204)
def admin_delete_group(group_id: str, admin: User = Admin) -> None:
    try:
        state.accounts.delete_group(group_id)
    except NotFound:
        raise HTTPException(404, "no such group") from None


@app.put("/api/admin/groups/{group_id}/members/{user_id}", status_code=204)
def admin_add_member(group_id: str, user_id: str, admin: User = Admin) -> None:
    try:
        state.accounts.add_member(group_id, user_id)
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from None


@app.delete("/api/admin/groups/{group_id}/members/{user_id}", status_code=204)
def admin_remove_member(group_id: str, user_id: str, admin: User = Admin) -> None:
    state.accounts.remove_member(group_id, user_id)


@app.get("/api/groups")
def list_groups(user: User = CurrentUser) -> dict:
    """Every group, for a self-service join/leave UI -- name, type and
    member count only, never the member list itself (that stays behind
    GET /api/admin/groups). Which of these the caller is already in is on
    GET /api/auth/me instead (User.groups)."""
    return {"groups": [{"id": g["id"], "name": g["name"], "description": g["description"], "type": g["type"],
                        "member_count": len(g["members"])} for g in state.accounts.groups()]}


@app.post("/api/groups/{group_id}/join", status_code=204)
def join_group(group_id: str, user: User = CurrentUser) -> None:
    """Self-service: only works on an open group -- see Accounts.join_group."""
    try:
        state.accounts.join_group(group_id, user)
    except NotFound:
        raise HTTPException(404, "no such group") from None
    except Denied as exc:
        raise as_http_error(exc) from None


@app.post("/api/groups/{group_id}/leave", status_code=204)
def leave_group(group_id: str, user: User = CurrentUser) -> None:
    """Always allowed, open or locked group -- see Accounts.leave_group."""
    state.accounts.leave_group(group_id, user)


# ----------------------------------------------------------------- system --
# Editing .env from the browser, and the reload that applies it. See
# settings.py for why the file is handled as raw text and what each reload
# tier can and cannot reach.


class EnvSave(BaseModel):
    text: str
    stamp: str | None = None     # the stamp from the GET, to catch a concurrent edit


class EnvCheck(BaseModel):
    text: str


class StackReloader:
    """Runs `ragctl reload --skip-query` in the background.

    --skip-query matters: a plain `ragctl reload` boots out
    com.ragstack.query, which is this process's own LaunchAgent, so the
    command would be killed by the very step it was running -- and killed
    between the bootout and the bootstrap, leaving the service down with
    nothing to bring it back. Skipping that step lets the command finish; the
    query service then applies the re-rendered config by restarting itself
    (POST /api/admin/system/restart), which launchd's KeepAlive turns into a
    fresh process.
    """

    LOG = "stack-reload.log"

    def __init__(self):
        self._lock = threading.Lock()
        self._running = False
        self.last_run: dict | None = None

    def start(self) -> None:
        with self._lock:
            if self._running:
                raise Busy("a stack reload is already running")
            self._running = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        started = time.time()
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        code = -1
        try:
            with (LOG_DIR / self.LOG).open("a") as log:
                log.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} reload from the admin UI ====\n")
                log.flush()
                env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
                code = subprocess.run([str(REPO_ROOT / "ragctl"), "reload", "--skip-query"],
                                      stdout=log, stderr=subprocess.STDOUT, env=env).returncode
        finally:
            self.last_run = {"started_at": started, "finished_at": time.time(), "exit_code": code}
            with self._lock:
                self._running = False

    def status(self) -> dict:
        return {"running": self._running, "last_run": self.last_run, "log": f"data/logs/{self.LOG}"}

    def tail(self, lines: int = 120) -> str:
        path = LOG_DIR / self.LOG
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:]) if path.exists() else ""


stack_reloader = StackReloader()


@app.get("/api/admin/system/env")
def admin_read_env(admin: User = Superuser) -> dict:
    """The .env file as text, with anything already wrong with it."""
    return {**settings.read(), "backups": settings.backups()}


@app.get("/api/admin/system/env/example")
def admin_read_env_example(admin: User = Superuser) -> dict:
    """.env.example, to copy a forgotten setting and its comment out of."""
    return {"text": settings.example()}


@app.post("/api/admin/system/env/check")
def admin_check_env(body: EnvCheck, admin: User = Superuser) -> dict:
    """Validates without writing, so the editor can say what's wrong as it's
    typed and what a save would need reloading."""
    current = settings.read()["text"]
    return {"problems": settings.validate(body.text), "shadowed": settings.shadowed(body.text),
            **settings.impact(current, body.text)}


@app.put("/api/admin/system/env")
def admin_write_env(body: EnvSave, admin: User = Superuser) -> dict:
    """Validates, backs up and replaces .env. Nothing is applied yet -- the
    reply says which reload tier the change needs."""
    try:
        return settings.save(body.text, body.stamp)
    except settings.Conflict as exc:
        raise HTTPException(409, str(exc)) from None
    except Invalid as exc:
        raise as_http_error(exc) from None


@app.get("/api/admin/system/env/backups")
def admin_list_env_backups(admin: User = Superuser) -> dict:
    return {"backups": settings.backups()}


@app.get("/api/admin/system/env/backups/{name}")
def admin_read_env_backup(name: str, admin: User = Superuser) -> dict:
    """A backup's text. Restoring is the same as saving it back, so the UI
    loads it into the editor rather than applying it behind the admin's back."""
    try:
        return {"name": name, "text": settings.backup_text(name)}
    except Invalid as exc:
        raise HTTPException(404, str(exc)) from None


@app.post("/api/admin/system/reload")
def admin_reload(admin: User = Superuser) -> dict:
    """Rebuilds everything that reads .env, in this process. Covers the
    retriever, chat presets, queue limits, upload quotas, the API key and the
    auth mode -- but not the session cookie or the listening port, which
    belong to middleware installed at import and to uvicorn's arguments. The
    reply says so rather than reporting a success that didn't reach them."""
    try:
        build_runtime(load_env())
    except Exception as exc:  # noqa: BLE001 -- a bad .env shouldn't 500 silently
        raise HTTPException(400, f"reload failed, the previous configuration is still running: {exc}") from None
    return {"reloaded": True,
            "not_applied": sorted(settings.RESTART_KEYS | settings.STACK_KEYS),
            "note": "these keys need a restart or a stack reload; the rest are live now"}


@app.post("/api/admin/system/stack-reload", status_code=202)
def admin_stack_reload(admin: User = Superuser) -> dict:
    """Re-renders the LaunchAgent scripts and restarts Qdrant, docling,
    Ollama's tuning and the inbox watcher. Runs in the background; poll this
    endpoint's GET for progress."""
    try:
        stack_reloader.start()
    except Busy as exc:
        raise HTTPException(409, str(exc)) from None
    return {"started": True, **stack_reloader.status()}


@app.get("/api/admin/system/stack-reload")
def admin_stack_reload_status(admin: User = Superuser) -> dict:
    return {**stack_reloader.status(), "tail": stack_reloader.tail()}


@app.post("/api/admin/system/restart", status_code=202)
def admin_restart(admin: User = Superuser) -> dict:
    """Restarts this process, for the keys a live reload can't reach.

    It exits non-zero and lets launchd start it again: com.ragstack.query is
    KeepAlive/SuccessfulExit=false, so a non-zero exit is exactly the case it
    relaunches. That also re-runs the rendered query-api.sh, which is how a
    changed QUERY_PORT takes effect -- so a stack reload (which re-renders it)
    belongs before this. Outside launchd nothing restarts it and the service
    stays down, which is why the reply says as much.
    """
    def bye() -> None:
        time.sleep(1.0)  # let this response reach the browser first
        os._exit(1)

    threading.Thread(target=bye, daemon=True).start()
    return {"restarting": True,
            "note": "launchd restarts com.ragstack.query on a non-zero exit; if this service was started "
                    "by hand it will stay down. Give it a few seconds, then reload the page."}


# -------------------------------------------------------------- libraries --

def _owner_name(owner_id: str | None) -> str | None:
    """The owner's display name (or email), for a UI to show without a
    second round trip -- None if the library has none (never owned, or its
    owner was deleted)."""
    if owner_id is None:
        return None
    try:
        owner = state.accounts.user(owner_id)
    except NotFound:
        return None
    return owner.display_name or owner.email


@app.get("/api/libraries")
def list_libraries(user: User = CurrentUser) -> dict:
    """Libraries this user can read -- every library for an admin, otherwise
    the ones they own plus the ones shared with them, directly or via a
    group. Each row carries `role` (what they may do with it) and `source`
    (why they can see it): "owner", "admin", "user" or "group"."""
    readable = state.accounts.readable_libraries(user.id)
    libraries = state.accounts.libraries()
    if readable is not None:
        libraries = [lib for lib in libraries if lib["id"] in readable]
    out = []
    for lib in libraries:
        role, source = state.accounts.library_role_source(user.id, lib["id"])
        out.append({**lib, "role": role, "source": source, "owner_name": _owner_name(lib["owner_id"])})
    return {"libraries": out}


@app.post("/api/libraries", status_code=201)
def create_library(body: NewLibrary, user: User = CurrentUser) -> dict:
    """Self-service creation: any signed-in user may start a library -- they
    become its owner, which is full access on its own (Accounts.library_role),
    with no separate grant step needed."""
    try:
        return state.accounts.create_library(body.name, body.description, owner_id=user.id)
    except Invalid as exc:
        raise as_http_error(exc) from None


@app.get("/api/libraries/{library_id}")
def library_detail(library_id: str, user: User = CurrentUser) -> dict:
    """A library's own documents (name, pages, ingest status/route), for
    anyone who can read it -- unlike /api/admin/documents, this isn't
    admin-only, and unlike /api/documents, it's scoped to the one library
    instead of every document on the machine."""
    try:
        lib = state.accounts.library(library_id)
    except NotFound:
        raise HTTPException(404, "no such library") from None
    role, _source = state.accounts.library_role_source(user.id, library_id)
    if role is None:
        raise HTTPException(403, f"{user.email} can't read library {library_id}")
    manifest = _manifest_documents()
    docs = [_document_row(manifest[sha]) for sha in lib["documents"] if sha in manifest]
    return {"id": lib["id"], "name": lib["name"], "description": lib["description"], "owner_id": lib["owner_id"],
            "owner_name": _owner_name(lib["owner_id"]), "created_at": lib["created_at"], "role": role,
            "document_count": len(lib["documents"]), "documents": docs}


@app.patch("/api/libraries/{library_id}")
def update_library(library_id: str, body: LibraryUpdate, user: User = CurrentUser) -> dict:
    if not state.accounts.can_manage(user.id, library_id):
        raise HTTPException(403, f"{user.email} can't manage library {library_id}")
    try:
        return state.accounts.update_library(library_id, name=body.name, description=body.description)
    except NotFound:
        raise HTTPException(404, "no such library") from None
    except Invalid as exc:
        raise as_http_error(exc) from None


@app.delete("/api/libraries/{library_id}", status_code=204)
def delete_library(library_id: str, user: User = CurrentUser) -> None:
    if not state.accounts.can_manage(user.id, library_id):
        raise HTTPException(403, f"{user.email} can't manage library {library_id}")
    try:
        state.accounts.delete_library(library_id)
    except NotFound:
        raise HTTPException(404, "no such library") from None


@app.put("/api/libraries/{library_id}/documents/{doc_sha}", status_code=204)
def add_document(library_id: str, doc_sha: str, user: User = CurrentUser) -> None:
    """Assigns an already-indexed document into a library. Requires write on
    the library -- and, so a writer can't pull a document they have no
    business seeing into their own library and read it from there, that the
    caller can already read that document elsewhere (admin, or through
    another library it's already in)."""
    if not state.accounts.can_write(user.id, library_id):
        raise HTTPException(403, f"{user.email} can't add documents to library {library_id}")
    if not (user.is_admin or state.accounts.can_read_document(user.id, doc_sha)):
        raise HTTPException(403, f"{user.email} can't read document {doc_sha}")
    try:
        state.accounts.add_document(library_id, doc_sha)
    except NotFound:
        raise HTTPException(404, "no such library") from None


@app.delete("/api/libraries/{library_id}/documents/{doc_sha}", status_code=204)
def remove_document(library_id: str, doc_sha: str, user: User = CurrentUser) -> None:
    if not state.accounts.can_write(user.id, library_id):
        raise HTTPException(403, f"{user.email} can't remove documents from library {library_id}")
    state.accounts.remove_document(library_id, doc_sha)


@app.get("/api/libraries/{library_id}/access")
def get_library_access(library_id: str, user: User = CurrentUser) -> dict:
    """Who has access to this library, for its owner (or an admin) to
    manage. `owner` is null for a library nobody owns any more."""
    try:
        lib = state.accounts.library(library_id)
    except NotFound:
        raise HTTPException(404, "no such library") from None
    if not state.accounts.can_manage(user.id, library_id):
        raise HTTPException(403, f"{user.email} can't manage library {library_id}")
    owner = None
    if lib["owner_id"]:
        owner = {"id": lib["owner_id"], "name": _owner_name(lib["owner_id"])}
    return {"owner": owner, "access": state.accounts.library_access(library_id)}


@app.put("/api/libraries/{library_id}/access", status_code=204)
def put_library_access(library_id: str, body: AccessRequest, user: User = CurrentUser) -> None:
    if not state.accounts.can_manage(user.id, library_id):
        raise HTTPException(403, f"{user.email} can't manage library {library_id}")
    try:
        state.accounts.set_library_access(library_id, body.subject_type, body.subject_id, body.role)
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except Invalid as exc:
        raise as_http_error(exc) from None


@app.delete("/api/libraries/{library_id}/access/{subject_type}/{subject_id}", status_code=204)
def delete_library_access(library_id: str, subject_type: str, subject_id: str, user: User = CurrentUser) -> None:
    if not state.accounts.can_manage(user.id, library_id):
        raise HTTPException(403, f"{user.email} can't manage library {library_id}")
    state.accounts.remove_library_access(library_id, subject_type, subject_id)


@app.get("/api/admin/documents")
def admin_list_documents(admin: User = Admin) -> dict:
    """Every indexed document, unfiltered, for assigning into libraries."""
    docs = state.chat.library.documents(sections=0)
    return {"documents": [
        {"sha256": d.sha256, "filename": d.filename, "pages": d.pages, "chunks": d.chunks,
         "libraries": state.accounts.libraries_of(d.sha256)}
        for d in docs]}


# -------------------------------------------------------------- workspaces --
# Owned by one person (never admin-gated -- a workspace is only ever yours),
# holding the libraries and chats it was given. Deleting one is a soft
# delete (see ChatDB.delete_workspace) that also hides its chats, though
# neither is actually removed; libraries themselves stay regardless, since
# they're the global, admin-managed rows from the section above. A chat in a
# workspace searches the workspace's libraries as they are at each turn, so
# changing them here changes what every chat in it searches.


def _get_workspace(workspace_id: str, user: User) -> dict:
    try:
        return state.db.get_workspace(workspace_id, user_id=user.id)
    except NotFound:
        raise HTTPException(404, "workspace not found") from None


@app.post("/api/workspaces", status_code=201)
def create_workspace(body: NewWorkspace, user: User = CurrentUser) -> dict:
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(422, "a workspace needs a name")
    return state.db.create_workspace(name, body.description, user_id=user.id)


@app.get("/api/workspaces")
def list_workspaces(deleted: bool = False, user: User = CurrentUser) -> dict:
    """The workspace list; pass `?deleted=true` for the Recently Deleted tray."""
    return {"workspaces": state.db.list_workspaces(user_id=user.id, deleted=deleted)}


@app.get("/api/workspaces/{workspace_id}")
def get_workspace(workspace_id: str, user: User = CurrentUser) -> dict:
    return _get_workspace(workspace_id, user)


@app.patch("/api/workspaces/{workspace_id}")
def update_workspace(workspace_id: str, body: WorkspaceUpdate, user: User = CurrentUser) -> dict:
    name = body.name.strip() if body.name is not None else None
    if name == "":
        raise HTTPException(422, "a workspace needs a name")
    _check_libraries(user, body.library_ids)
    try:
        return state.db.update_workspace(workspace_id, name=name, description=body.description,
                                         library_ids=body.library_ids, user_id=user.id)
    except NotFound:
        raise HTTPException(404, "workspace not found") from None


@app.delete("/api/workspaces/{workspace_id}", status_code=204)
def delete_workspace(workspace_id: str, user: User = CurrentUser) -> None:
    """Marks the workspace deleted, which also hides its chats -- see
    ChatDB.delete_workspace. Nothing is actually removed."""
    try:
        state.db.delete_workspace(workspace_id, user_id=user.id)
    except NotFound:
        raise HTTPException(404, "workspace not found") from None


@app.post("/api/workspaces/{workspace_id}/restore")
def restore_workspace(workspace_id: str, user: User = CurrentUser) -> dict:
    """Undoes delete_workspace -- its chats, never themselves touched,
    become reachable again too."""
    try:
        return state.db.restore_workspace(workspace_id, user_id=user.id)
    except NotFound:
        raise HTTPException(404, "workspace not found") from None


@app.get("/api/workspaces/{workspace_id}/conversations")
def list_workspace_conversations(workspace_id: str, user: User = CurrentUser) -> list[dict]:
    _get_workspace(workspace_id, user)  # 404 if missing or not this user's
    return state.db.list_conversations(user_id=user.id, workspace_id=workspace_id)


# ------------------------------------------------------------ conversations --


class NewConversation(BaseModel):
    title: str | None = None
    preset: str | None = None
    library_ids: list[str] | None = None
    workspace_id: str | None = None


class ConversationUpdate(BaseModel):
    title: str | None = None
    preset: str | None = None
    library_ids: list[str] | None = None


class NewMessage(BaseModel):
    content: str = Field(min_length=1)
    preset: str | None = None  # defaults to the conversation's preset


def _check_preset(name: str | None) -> str:
    try:
        return state.chat.cfg.preset(name).name
    except ChatError as exc:
        raise HTTPException(422, str(exc)) from None


def _check_libraries(user: User, library_ids: list[str] | None) -> None:
    """A conversation can only attach libraries that exist and that its
    owner can read -- checked here rather than left to the database's
    foreign key, so a bad id is a clean 404/403 instead of a 500."""
    if not library_ids:
        return
    unknown = set(library_ids) - {lib["id"] for lib in state.accounts.libraries()}
    if unknown:
        raise HTTPException(404, f"no such library: {', '.join(sorted(unknown))}")
    readable = state.accounts.readable_libraries(user.id)
    if readable is not None and not set(library_ids) <= readable:
        raise HTTPException(403, "you can't read one of these libraries")


def _get_conversation(conv_id: str, user: User, with_messages: bool = True) -> dict:
    try:
        return state.db.get_conversation(conv_id, user_id=user.id, with_messages=with_messages)
    except NotFound:
        raise HTTPException(404, "conversation not found") from None


@app.post("/api/conversations", status_code=201)
def create_conversation(body: NewConversation, user: User = CurrentUser) -> dict:
    """A chat inside a workspace takes no `library_ids`: it always searches
    whatever the workspace holds at the time of each turn."""
    _check_libraries(user, body.library_ids)
    if body.workspace_id is not None:
        _get_workspace(body.workspace_id, user)  # 404 if missing or not this user's
    try:
        return state.db.create_conversation(_check_preset(body.preset), body.title, user_id=user.id,
                                            library_ids=body.library_ids, workspace_id=body.workspace_id)
    except WorkspaceLibraries as exc:
        raise HTTPException(422, str(exc)) from None


@app.get("/api/conversations")
def list_conversations(deleted: bool = False, archived: bool = False, user: User = CurrentUser) -> list[dict]:
    """The chat list; pass `?deleted=true` for the Recently Deleted tray or
    `?archived=true` for the Archived list (mutually exclusive -- `deleted`
    wins if both are set)."""
    return state.db.list_conversations(user_id=user.id, deleted=deleted, archived=archived)


@app.get("/api/conversations/{conv_id}")
def get_conversation(conv_id: str, user: User = CurrentUser) -> dict:
    return _get_conversation(conv_id, user)


@app.patch("/api/conversations/{conv_id}")
def update_conversation(conv_id: str, body: ConversationUpdate, user: User = CurrentUser) -> dict:
    preset = _check_preset(body.preset) if body.preset is not None else None
    _check_libraries(user, body.library_ids)
    try:
        return state.db.update_conversation(conv_id, title=body.title, preset=preset,
                                            library_ids=body.library_ids, user_id=user.id)
    except NotFound:
        raise HTTPException(404, "conversation not found") from None
    except WorkspaceLibraries as exc:
        raise HTTPException(422, str(exc)) from None


@app.delete("/api/conversations/{conv_id}", status_code=204)
def delete_conversation(conv_id: str, user: User = CurrentUser) -> None:
    """Marks the chat deleted -- it moves to the Recently Deleted tray
    (`GET /api/conversations?deleted=true`) rather than being removed."""
    try:
        state.db.delete_conversation(conv_id, user_id=user.id)
    except NotFound:
        raise HTTPException(404, "conversation not found") from None


@app.post("/api/conversations/{conv_id}/restore")
def restore_conversation(conv_id: str, user: User = CurrentUser) -> dict:
    try:
        return state.db.restore_conversation(conv_id, user_id=user.id)
    except NotFound:
        raise HTTPException(404, "conversation not found") from None


@app.post("/api/conversations/{conv_id}/archive")
def archive_conversation(conv_id: str, user: User = CurrentUser) -> dict:
    """Unlike delete, an archived chat stays fully open-able -- it's just
    out of the normal list (`GET /api/conversations?archived=true` to see
    it). A deleted chat can't be archived; restore it first."""
    try:
        return state.db.archive_conversation(conv_id, user_id=user.id)
    except NotFound:
        raise HTTPException(404, "conversation not found") from None


@app.post("/api/conversations/{conv_id}/unarchive")
def unarchive_conversation(conv_id: str, user: User = CurrentUser) -> dict:
    try:
        return state.db.unarchive_conversation(conv_id, user_id=user.id)
    except NotFound:
        raise HTTPException(404, "conversation not found") from None


def _stream_turn(conv_id: str, user: User, question: str, history: list[dict], preset: str, doc_shas,
                 library_overview, *, first_turn: bool, has_title: bool) -> Iterator[str]:
    """Runs one turn and streams it as the SSE kinds documented on
    send_message, storing the answer (or the error) as it finishes. Shared
    by send_message and retry_message -- they differ only in what happens
    before this: whether a new user message is stored and a `user` event
    opens the stream."""
    tokens: list[str] = []
    sources: list[dict] = []
    queries: list[str] = []
    finished = False
    try:
        for ev in state.chat.run_turn(question, history, preset, doc_shas=doc_shas,
                                      library_overview=library_overview):
            kind = ev["type"]
            if kind == "done":
                r = ev["result"]
                saved = state.db.add_assistant_message(
                    conv_id, content=r.answer, model=r.model, preset=r.preset, search_queries=r.search_queries,
                    sources=[asdict(s) for s in r.sources], cited=set(r.cited), stats=r.stats,
                    user_id=user.id)
                finished = True
                yield sse("done", {"message_id": saved["id"], "answer": r.answer, "model": r.model,
                                   "preset": r.preset, "search_queries": r.search_queries, "cited": r.cited,
                                   "invalid_citations": r.invalid_citations, "stats": r.stats,
                                   "sources": [{**asdict(s), "cited": s.number in r.cited} for s in r.sources]})
                if first_turn and not has_title:
                    title = state.chat.generate_title(preset, question, r.answer)
                    if title:
                        state.db.update_conversation(conv_id, title=title, user_id=user.id)
                        yield sse("title", {"title": title})
                continue
            if kind == "token":
                tokens.append(ev["text"])
            elif kind == "sources":
                sources.extend(ev["sources"])
            elif kind == "searching":
                queries.append(ev["query"])
            yield sse(kind, {k: v for k, v in ev.items() if k != "type"})
    except Exception as exc:  # noqa: BLE001 - store and report whatever broke the turn
        if not finished:
            state.db.add_assistant_message(
                conv_id, content="".join(tokens), model=state.chat.cfg.preset(preset).model, preset=preset,
                search_queries=queries, sources=sources, cited=set(), status="error", error=str(exc),
                user_id=user.id)
        yield sse("error", {"error": str(exc)})


def _streamed(user_id: str, conv_id: str, gen: Iterator[str]) -> StreamingResponse:
    """The turn-runs-on-its-own-thread plumbing shared by send_message and
    retry_message: `gen` runs under the concurrency gate on a background
    thread and is relayed to the client as it produces SSE lines, so a
    disconnect only stops the relay -- the turn still finishes and is
    stored."""
    events: queue.Queue[str | None] = queue.Queue()

    def produce() -> None:
        try:
            with state.gate.admit(user_id, conv_id,
                                  on_position=lambda n: events.put(sse("queued", {"position": n}))):
                for item in gen:
                    events.put(item)
        except Busy as exc:
            events.put(sse("error", {"error": str(exc)}))
        finally:
            events.put(None)

    threading.Thread(target=produce, daemon=True).start()

    async def relay():
        while (item := await run_in_threadpool(events.get)) is not None:
            yield item

    return StreamingResponse(relay(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/conversations/{conv_id}/messages")
def send_message(conv_id: str, body: NewMessage, user: User = CurrentUser) -> StreamingResponse:
    """Streams server-sent events:
      queued    {position}: the machine is busy; this turn is waiting in line
      user      the stored question {id}
      rewrite   {query, search}: whether to search at all, and the (standalone) search query
      searching {query, found_by}: a search is running (auto or tool)
      listing   {document}: the model asked what the knowledge base holds
      sources   {sources: [...]}: passages added, numbered for citation
      thinking  {text}: reasoning tokens (presets with thinking on)
      token     {text}: answer tokens
      done      {message_id, answer, cited, invalid_citations, sources, ...}
      title     {title}: after the first answer, a generated conversation title
      error     {error}: the turn failed; partial output is stored with status "error"

    The turn runs in its own thread and is stored when it finishes, even if
    the client disconnects halfway: reopening the conversation shows it.

    Only so many turns run at once (see limits.py). The question is stored
    once this one is admitted, not when it arrives, so that two sends to the
    same conversation can't read each other's half-written history.
    """
    conv = _get_conversation(conv_id, user, with_messages=False)
    preset = _check_preset(body.preset or conv["preset"])
    # Recomputed on every turn, not trusted from when the conversation
    # attached these libraries: losing read access stops applying here,
    # not only on the next attach.
    doc_shas = state.accounts.document_scope(user.id, conv["library_ids"])
    library_overview = state.accounts.library_overview(user.id, conv["library_ids"])
    if conv["workspace_id"]:
        state.db.touch_workspace(conv["workspace_id"], user_id=user.id)

    def stream() -> Iterator[str]:
        history = state.db.history(conv_id, user_id=user.id)
        first_turn = not history
        user_msg = state.db.add_user_message(conv_id, body.content, user_id=user.id)
        yield sse("user", {"id": user_msg["id"]})
        yield from _stream_turn(conv_id, user, body.content, history, preset, doc_shas, library_overview,
                                first_turn=first_turn, has_title=bool(conv["title"]))

    return _streamed(user.id, conv_id, stream())


@app.post("/api/conversations/{conv_id}/retry")
def retry_message(conv_id: str, user: User = CurrentUser) -> StreamingResponse:
    """Regenerates the conversation's last answer: deletes it and reruns the
    turn for the question it was answering (which stays in place -- no new
    `user` event, since the question isn't new). Streams the same SSE kinds
    as send_message, minus `user`. 404s if the chat doesn't end in an
    answer to retry (mid-question, empty, or already retried).

    The old answer is deleted up front, before the turn is admitted --
    unlike send_message's new user message, there's no way to check "does
    this chat end in an answer" without already knowing which answer that
    is, so on the rare Busy rejection the old answer is gone and the retry
    itself needs retrying."""
    conv = _get_conversation(conv_id, user, with_messages=False)
    preset = _check_preset(conv["preset"])
    try:
        question = state.db.prepare_retry(conv_id, user_id=user.id)
    except NotFound:
        raise HTTPException(404, "nothing to retry") from None
    doc_shas = state.accounts.document_scope(user.id, conv["library_ids"])
    library_overview = state.accounts.library_overview(user.id, conv["library_ids"])
    if conv["workspace_id"]:
        state.db.touch_workspace(conv["workspace_id"], user_id=user.id)

    def stream() -> Iterator[str]:
        history = state.db.history(conv_id, user_id=user.id)
        yield from _stream_turn(conv_id, user, question, history, preset, doc_shas, library_overview,
                                first_turn=not history, has_title=bool(conv["title"]))

    return _streamed(user.id, conv_id, stream())


# ------------------------------------------------------------------ search --

class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    limit: int | None = Field(default=None, ge=1, le=100)
    fused: bool = False  # true: the hybrid-search list without reranking
    library_ids: list[str] | None = None  # default: every library the caller can read


@app.post("/api/search")
def search(body: SearchRequest, user: User = CurrentUser) -> dict:
    t0 = time.monotonic()
    _check_libraries(user, body.library_ids)
    doc_shas = state.accounts.document_scope(user.id, body.library_ids)
    try:
        # Reranking runs on the same GPU as the answers, so searches queue
        # with them rather than beside them.
        with state.gate.admit(user.id):
            if body.fused:
                hits = state.retriever.search(body.query, limit=body.limit, doc_shas=doc_shas)
            else:
                hits = state.retriever.retrieve(body.query, limit=body.limit, doc_shas=doc_shas)
    except Busy as exc:
        raise HTTPException(503, str(exc), headers={"Retry-After": "10"}) from None
    except (RuntimeError, httpx.HTTPError) as exc:
        raise HTTPException(503, str(exc)) from None
    return {"query": body.query, "took_ms": round((time.monotonic() - t0) * 1000),
            "hits": [asdict(h) for h in hits]}


# --------------------------------------------------------------- documents --

def _manifest_documents() -> dict[str, dict]:
    """The latest manifest row per document, by sha256 -- filename, pages,
    status, route, error, when it was processed. Shared by /api/documents
    (every document) and a library's own detail view (its documents only)."""
    latest: dict[str, dict] = {}
    if MANIFEST.exists():
        with MANIFEST.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                prev = latest.get(rec["sha256"])
                # A failed re-run doesn't undo an earlier success.
                if prev is None or rec.get("status") == "processed" or prev.get("status") != "processed":
                    latest[rec["sha256"]] = rec
    return latest


def _document_row(rec: dict) -> dict:
    return {"sha256": rec["sha256"], "filename": rec["filename"], "status": rec.get("status"),
            "route": rec.get("route"), "pages": rec.get("pages"), "chars": rec.get("chars"),
            "error": rec.get("error"), "processed_at": rec.get("timestamp")}


@app.get("/api/documents")
def documents(user: User = CurrentUser) -> dict:
    """Latest manifest row per document, plus files waiting in the inbox."""
    docs = [_document_row(r) for r in _manifest_documents().values()]
    docs.sort(key=lambda d: d["processed_at"] or 0, reverse=True)
    inbox = sorted(p.name for p in INBOX.iterdir() if p.is_file() and not p.name.startswith(".")) if INBOX.exists() else []
    return {"documents": docs, "inbox": inbox, "ingest": state.ingest.status()}


@app.get("/api/documents/{sha256}/chunks")
def document_chunks(sha256: str, offset: int = 0, limit: int | None = None, user: User = CurrentUser) -> dict:
    """One document's chunks, in order, with the document's total chunk
    count -- the source viewer's prev/next through a citation and the "n of
    total" it shows alongside. Unlike /api/documents (filenames only), this
    hands back passage text, so it needs read access to a library the
    document belongs to (or admin)."""
    rec = _manifest_documents().get(sha256)
    if rec is None:
        raise HTTPException(404, "no such document")
    if not state.accounts.can_read_document(user.id, sha256):
        raise HTTPException(403, f"{user.email} can't read document {sha256}")
    if offset < 0:
        raise HTTPException(422, "offset must be >= 0")
    if limit is not None and not 1 <= limit <= 200:
        raise HTTPException(422, "limit must be between 1 and 200")
    try:
        chunks, total = state.retriever.document_chunks(sha256, offset=offset, limit=limit)
    except (RuntimeError, httpx.HTTPError) as exc:
        raise HTTPException(503, str(exc)) from None
    return {"sha256": sha256, "filename": rec["filename"], "total": total, "chunks": chunks}


def _sha256(path: Path) -> str:
    # Same chunked read as services/ingest/ingest.py's sha256_of -- ingest
    # hashes the file as it sits in the inbox, so this has to match exactly
    # for a library assigned at upload time to land on the doc ingest produces.
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@app.post("/api/upload", status_code=202)
def upload(files: list[UploadFile] = File(...), library_id: Annotated[str | None, Form()] = None,
          user: User = CurrentUser) -> dict:
    """Save files into data/inbox and start ingest + embed, within this user's
    ingest quota (see limits.UploadLimits). A file that doesn't fit is
    rejected by name with the reason, and the rest still go through.

    `library_id`, if given, assigns every saved file to that library by the
    sha256 it will be ingested under -- computed here, before ingest ever
    runs, so the assignment is already in place by the time embedding makes
    the document searchable. Requires write on it."""
    INBOX.mkdir(parents=True, exist_ok=True)
    if library_id:
        if not state.accounts.can_write(user.id, library_id):
            raise HTTPException(403, f"{user.email} can't add documents to library {library_id}")
        if library_id not in {lib["id"] for lib in state.accounts.libraries()}:
            raise HTTPException(404, "no such library")
    limits = state.uploads.for_user(user.is_admin)
    files_used, bytes_used = state.db.upload_usage(limits.window_seconds, user_id=user.id)
    saved, rejected = [], []
    for up in files:
        name = Path(up.filename or "").name  # no directory components
        if not name or name.startswith(".") or Path(name).suffix.lower() not in SUPPORTED_SUFFIXES:
            rejected.append({"filename": up.filename, "reason": "unsupported file type"})
            continue
        # Write elsewhere first and move in whole, so the inbox never holds a
        # half-written file for ingest to pick up -- and so the size is known
        # before anything is admitted, whatever the client claimed.
        with tempfile.NamedTemporaryFile(dir=DATA_DIR, delete=False) as tmp:
            shutil.copyfileobj(up.file, tmp)
            size = tmp.tell()
        if reason := limits.rejection(size, files_used, bytes_used):
            os.unlink(tmp.name)
            rejected.append({"filename": name, "reason": reason})
            continue
        if library_id:
            state.accounts.add_document(library_id, _sha256(Path(tmp.name)))
        dest = _claim_inbox_name(name, tmp.name)
        files_used, bytes_used = files_used + 1, bytes_used + size
        state.db.record_upload(dest.name, size, user_id=user.id)
        saved.append(dest.name)
    status = state.ingest.trigger() if saved else "nothing to ingest"
    return {"saved": saved, "rejected": rejected, "ingest": status,
            "quota": limits.remaining(files_used, bytes_used)}


def _claim_inbox_name(name: str, written: str) -> Path:
    """Move a finished upload into the inbox under a free name. os.link fails
    rather than overwriting, so two sessions uploading the same filename at
    the same time can't land on each other."""
    stem, suffix = Path(name).stem, Path(name).suffix
    for n in range(1000):
        dest = INBOX / (name if n == 0 else f"{stem} ({n}){suffix}")
        try:
            os.link(written, dest)
        except FileExistsError:
            continue
        os.unlink(written)
        return dest
    raise HTTPException(409, f"too many files named {name!r} in the inbox")


@app.get("/api/quota")
def quota(user: User = CurrentUser) -> dict:
    """What's left of this user's ingest allowance, and how busy the machine is."""
    limits = state.uploads.for_user(user.is_admin)
    files_used, bytes_used = state.db.upload_usage(limits.window_seconds, user_id=user.id)
    return {"uploads": limits.remaining(files_used, bytes_used), "load": state.gate.stats()}


# ------------------------------------------------------------------ health --

@app.get("/api/health")
def health() -> JSONResponse:
    checks: dict[str, dict] = {}
    cfg = state.retriever.cfg
    with httpx.Client(timeout=5) as client:
        try:
            client.get(f"{cfg.ollama_url}/api/version").raise_for_status()
            checks["ollama"] = {"ok": True}
        except httpx.HTTPError as exc:
            checks["ollama"] = {"ok": False, "error": str(exc)}
        try:
            resp = client.post(f"{cfg.qdrant_url}/collections/{cfg.collection}/points/count", json={"exact": False})
            if resp.status_code == 404:
                checks["qdrant"] = {"ok": False, "error": f"collection {cfg.collection} doesn't exist; run ragctl embed"}
            else:
                resp.raise_for_status()
                checks["qdrant"] = {"ok": True, "collection": cfg.collection, "chunks": resp.json()["result"]["count"]}
        except httpx.HTTPError as exc:
            checks["qdrant"] = {"ok": False, "error": str(exc)}
    try:
        state.db.ping()
        checks["database"] = {"ok": True}
    except Exception as exc:  # noqa: BLE001
        checks["database"] = {"ok": False, "error": str(exc)}
    if cfg.rerank:
        r = state.retriever._reranker
        checks["reranker"] = ({"ok": True, "model": r.cfg.model, "device": r.device} if r
                              else {"ok": True, "loading": True})
    checks["load"] = {"ok": True, **state.gate.stats()}
    checks["presets"] = {"ok": True, **{name: {"model": p.model, "tools": p.tools, "think": p.think}
                                        for name, p in state.chat.cfg.presets.items()}}
    # /api/health is the only unauthenticated endpoint, and auth_mode is
    # otherwise only on /api/auth/me -- which 401s exactly when a signed-out
    # UI needs to know whether to show a sign-in form at all (proxy mode has
    # none). The mode is not a secret.
    checks["auth"] = {"ok": True, "mode": state.auth.mode}
    ok = all(c["ok"] for c in checks.values())
    return JSONResponse({"ok": ok, **checks}, status_code=200 if ok else 503)


@app.get("/admin", include_in_schema=False)
def admin_ui() -> FileResponse:
    """The admin console (static/admin.html): accounts, groups, libraries and
    the .env editor. Served to anyone -- the page itself holds no secrets and
    every call it makes is checked server-side, so it renders a sign-in form
    for a caller who turns out not to be an admin."""
    return FileResponse(Path(__file__).parent / "static" / "admin.html")


from openai_compat import router as openai_router  # noqa: E402

app.include_router(openai_router)


# -------------------------------------------------------------------- ui --
# The Svelte app (ui/, built to ui/dist, committed to git) served at /.
# Registered last on purpose: routes match in declaration order, so every
# /api, /v1 and /admin route above already wins over the catch-all here.
# /docs, /redoc and /openapi.json are registered inside FastAPI.__init__ and
# so precede everything regardless of where this block sits. REPO_ROOT, not
# Path(__file__).parent -- the service's cwd is services/query
# (launchd/query-api.sh.tmpl), not the repo root.

UI_DIST = REPO_ROOT / "ui" / "dist"
_ANY_METHOD = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


@app.api_route("/api/{rest:path}", methods=_ANY_METHOD, include_in_schema=False)
def api_not_found(rest: str) -> JSONResponse:
    """An unknown /api path is a JSON 404, the same shape as every other
    error. Without this the SPA catch-all below would answer a stray GET
    with index.html, and a stray POST with a 405 (the path matches a real
    route, the method doesn't) -- both worse than a 404 for an API caller."""
    return JSONResponse({"detail": f"no such endpoint: /api/{rest}"}, status_code=404)


@app.api_route("/v1/{rest:path}", methods=_ANY_METHOD, include_in_schema=False)
def v1_not_found(rest: str) -> JSONResponse:
    """Same for the OpenAI-compatible surface: a client pointed at the wrong
    /v1 path gets JSON back, never HTML."""
    return JSONResponse({"detail": f"no such endpoint: /v1/{rest}"}, status_code=404)


if (UI_DIST / "index.html").is_file():
    # check_dir=False: a build with no /assets directory (shouldn't happen,
    # but shouldn't crash the service either) must not raise at import time,
    # which StaticFiles(directory=...) otherwise does if the path is missing.
    app.mount("/assets", StaticFiles(directory=UI_DIST / "assets", check_dir=False), name="ui-assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def ui_app(full_path: str) -> FileResponse:
        """The single-page app. A real file in ui/dist is served as itself
        (favicon, manifest); anything else is index.html, so the client
        router owns paths like /c/<id> and /settings. html=True on
        StaticFiles is deliberately not used here instead -- it serves
        index.html for *directory* paths and 404s unknown files, which is
        not the same thing as an SPA fallback."""
        candidate = (UI_DIST / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(UI_DIST.resolve()):
            return FileResponse(candidate)
        return FileResponse(UI_DIST / "index.html")

else:
    @app.get("/", include_in_schema=False)
    def ui_missing() -> HTMLResponse:
        """A fresh clone before the UI was ever built. ui/dist is committed,
        so this normally never shows -- but a missing directory must not
        stop the API itself from serving."""
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>rag-stack</title>"
            "<body style='font:14px/1.5 system-ui,sans-serif;margin:40px;max-width:60ch'>"
            "<h1 style='font-size:16px'>The web UI isn't built</h1>"
            "<p><code>ui/dist</code> is missing. Build it with "
            "<code>./ragctl ui build</code> (needs Node), or use the "
            "<a href='/admin'>admin console</a> and the <a href='/docs'>API docs</a>.</p>",
            status_code=503,
        )

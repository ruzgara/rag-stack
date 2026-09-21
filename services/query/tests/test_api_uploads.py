"""
The upload endpoint's quota and its handling of two sessions at once. The
endpoint functions are called directly: they need a database and a couple of
directories, not a running Ollama or Qdrant.
"""

from __future__ import annotations

import io
import threading

import accounts as accounts_module
import api
import pytest
from accounts import DEFAULT_LIBRARY, Accounts, User
from db import ChatDB
from fastapi import HTTPException, UploadFile
from limits import Gate, GateConfig, UploadLimits


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    """api.upload writing into a temp inbox, with a real database behind it."""
    box = tmp_path / "inbox"
    box.mkdir()
    monkeypatch.setattr(api, "INBOX", box)
    monkeypatch.setattr(api, "DATA_DIR", tmp_path)
    db = ChatDB(tmp_path / "chat.db")
    monkeypatch.setattr(api.state, "db", db, raising=False)
    monkeypatch.setattr(api.state, "accounts", Accounts(db), raising=False)
    monkeypatch.setattr(api.state, "uploads", UploadLimits(max_file_mb=1, max_files=3, max_mb=2, window_hours=24), raising=False)
    monkeypatch.setattr(api.state, "gate", Gate(GateConfig()), raising=False)
    monkeypatch.setattr(api.state, "ingest", type("Stub", (), {"trigger": lambda self: "started", "status": lambda self: {}})(), raising=False)
    return box


@pytest.fixture
def user(inbox):
    return api.state.accounts.create_user("member@example.com", display_name="Member")


def own(user: User) -> str:
    """The library every account starts with (see Accounts.create_user)."""
    return next(lib["id"] for lib in api.state.accounts.libraries()
                if lib["owner_id"] == user.id and lib["name"] == DEFAULT_LIBRARY)


def pdf(name: str, size: int = 1_000) -> UploadFile:
    return UploadFile(file=io.BytesIO(b"x" * size), filename=name)


def test_a_file_within_the_quota_is_saved_and_counted(inbox, user):
    result = api.upload(user=user, library_id=own(user), files=[pdf("book.pdf")])

    assert result["saved"] == ["book.pdf"] and result["rejected"] == []
    assert (inbox / "book.pdf").read_bytes() == b"x" * 1_000
    assert api.state.db.upload_usage(3600, user_id=user.id) == (1, 1_000)
    assert result["quota"]["files_left"] == 2


def test_an_oversized_file_is_rejected_and_nothing_is_written(inbox, user):
    result = api.upload(user=user, library_id=own(user), files=[pdf("huge.pdf", size=2_000_000)])

    assert result["saved"] == []
    assert "limit is 1 MB per file" in result["rejected"][0]["reason"]
    assert list(inbox.iterdir()) == []
    assert api.state.db.upload_usage(3600, user_id=user.id) == (0, 0)
    assert list(api.DATA_DIR.glob("tmp*")) == []  # the temp copy is cleaned up


def test_the_window_allowance_stops_the_file_that_would_cross_it(inbox, user):
    api.upload(user=user, library_id=own(user), files=[pdf("a.pdf", size=900_000), pdf("b.pdf", size=900_000)])

    result = api.upload(user=user, library_id=own(user), files=[pdf("c.pdf", size=900_000)])

    assert result["saved"] == [] and "2 MB per 24h" in result["rejected"][0]["reason"]
    assert result["quota"]["mb_left"] == 0.2


def test_the_file_count_stops_further_uploads(inbox, user):
    api.upload(user=user, library_id=own(user), files=[pdf("a.pdf"), pdf("b.pdf"), pdf("c.pdf")])

    result = api.upload(user=user, library_id=own(user), files=[pdf("d.pdf")])

    assert "3 files per 24h" in result["rejected"][0]["reason"]


def test_an_unsupported_file_type_never_reaches_the_quota(inbox, user):
    result = api.upload(user=user, library_id=own(user), files=[pdf("notes.exe")])

    assert result["rejected"] == [{"filename": "notes.exe", "reason": "unsupported file type"}]
    assert api.state.db.upload_usage(3600, user_id=user.id) == (0, 0)


def test_a_repeated_filename_gets_its_own_name(inbox, user):
    first = api.upload(user=user, library_id=own(user), files=[pdf("book.pdf")])
    second = api.upload(user=user, library_id=own(user), files=[pdf("book.pdf")])

    assert (first["saved"], second["saved"]) == (["book.pdf"], ["book (1).pdf"])
    assert {p.name for p in inbox.iterdir()} == {"book.pdf", "book (1).pdf"}


def test_two_sessions_uploading_the_same_name_dont_overwrite_each_other(inbox, user):
    """The name is claimed with a hard link, which fails rather than replacing,
    so the loser tries the next name instead of clobbering the winner."""
    results = []
    threads = [threading.Thread(target=lambda n=i: results.append(api.upload(user=user, library_id=own(user), files=[pdf("book.pdf", size=500 + n)])))
               for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    saved = sorted(name for r in results for name in r["saved"])
    assert len(saved) == 4 and len(set(saved)) == 4
    assert sum(p.stat().st_size for p in inbox.iterdir()) == sum(500 + i for i in range(4))


@pytest.fixture
def admin(inbox):
    return api.state.accounts.create_user("admin@example.com", display_name="Admin", is_admin=True)


def test_an_admin_can_upload_past_every_limit(inbox, admin):
    """The fixture's limits are 1 MB per file, 3 files, 2 MB per window."""
    result = api.upload(user=admin, library_id=own(admin), files=[pdf("huge.pdf", size=5_000_000),
                                           pdf("a.pdf"), pdf("b.pdf"), pdf("c.pdf"), pdf("d.pdf")])

    assert len(result["saved"]) == 5 and result["rejected"] == []
    assert result["quota"]["unlimited"] is True
    # Still recorded, so who added a document is known even when unmetered.
    assert api.state.db.upload_usage(3600, user_id=admin.id)[0] == 5


def test_an_admins_quota_reads_as_unlimited(inbox, admin):
    assert api.quota(admin)["uploads"] == {"unlimited": True, "files_used": 0, "mb_used": 0.0}


def test_the_quota_endpoint_reports_what_is_left(inbox, user):
    api.upload(user=user, library_id=own(user), files=[pdf("book.pdf")])

    body = api.quota(user)

    assert body["uploads"]["files_left"] == 2
    assert body["load"] == {"running": 0, "queued": 0, "max_concurrent": 2, "max_per_user": 2}


# -------------------------------------------------------- library assignment --

def test_an_upload_with_a_library_id_is_assigned_by_its_future_sha(inbox, user):
    """The sha is computed here, before ingest ever runs, so the assignment
    is already in place by the time embedding makes the document
    searchable -- no filename matching, no waiting on ingest."""
    lib = api.state.accounts.create_library("Project finance", owner_id=user.id)
    size = 1_000

    api.upload(user=user, library_id=lib["id"], files=[pdf("book.pdf", size=size)])

    import hashlib
    sha = hashlib.sha256(b"x" * size).hexdigest()
    assert api.state.accounts.libraries_of(sha) == [lib["id"]]


def test_an_upload_must_name_a_library():
    """Without one the document would be in no library, which only an admin
    can read -- not even the person who uploaded it."""
    schema = api.app.openapi()["paths"]["/api/upload"]["post"]["requestBody"]["content"]["multipart/form-data"]
    body = api.app.openapi()["components"]["schemas"][schema["schema"]["$ref"].rsplit("/", 1)[1]]
    assert "library_id" in body["required"]


def test_an_upload_to_your_own_library_is_listed_back_to_you(inbox, user, monkeypatch):
    size = 1_000
    import hashlib
    sha = hashlib.sha256(b"x" * size).hexdigest()
    manifest = inbox.parent / "manifest.jsonl"
    manifest.write_text(f'{{"sha256": "{sha}", "filename": "book.pdf", "status": "processed"}}\n')
    monkeypatch.setattr(api, "MANIFEST", manifest)

    api.upload(user=user, library_id=own(user), files=[pdf("book.pdf", size=size)])

    assert [d["filename"] for d in api.documents(user=user)["documents"]] == ["book.pdf"]


def test_an_upload_to_a_library_without_write_is_refused(inbox, user):
    lib = api.state.accounts.create_library("Off limits")

    with pytest.raises(HTTPException) as exc:
        api.upload(user=user, library_id=lib["id"], files=[pdf("book.pdf")])
    assert exc.value.status_code == 403
    assert list(inbox.iterdir()) == []  # nothing was saved


def test_an_upload_to_an_unknown_library_is_404(inbox, user):
    """An admin clears the write check unconditionally, so this is where a
    bad id actually gets checked -- for a member, write access to an id
    they were never given is a 403 before it gets this far."""
    admin = api.state.accounts.create_user("admin@example.com", display_name="Admin", is_admin=True)

    with pytest.raises(HTTPException) as exc:
        api.upload(user=admin, library_id="no-such-library", files=[pdf("book.pdf")])
    assert exc.value.status_code == 404


def test_a_hopeless_name_clash_is_reported_rather_than_looping(inbox, user, monkeypatch):
    monkeypatch.setattr(api.os, "link", lambda src, dst: (_ for _ in ()).throw(FileExistsError()))
    written = inbox.parent / "written.tmp"
    written.write_bytes(b"x")

    with pytest.raises(HTTPException) as exc:
        api._claim_inbox_name("book.pdf", str(written))
    assert exc.value.status_code == 409


# ------------------------------------------------------ document visibility --

@pytest.fixture
def manifest(inbox, monkeypatch):
    path = inbox.parent / "manifest.jsonl"
    path.write_text(
        '{"sha256": "sha-shared", "filename": "shared.pdf", "status": "processed", "timestamp": 2}\n'
        '{"sha256": "sha-private", "filename": "someone-elses-layoffs.pdf", "status": "processed", "timestamp": 1}\n'
        '{"sha256": "sha-loose", "filename": "in-no-library.pdf", "status": "processed", "timestamp": 3}\n')
    monkeypatch.setattr(api, "MANIFEST", path)
    return path


def test_a_member_is_only_listed_documents_they_can_read(manifest, user, admin):
    acc = api.state.accounts
    shared = acc.create_library("Shared", owner_id=admin.id)
    acc.add_document(shared["id"], "sha-shared")
    acc.set_library_access(shared["id"], "user", user.id, "read")
    private = acc.create_library("Private", owner_id=admin.id)
    acc.add_document(private["id"], "sha-private")

    assert [d["filename"] for d in api.documents(user=user)["documents"]] == ["shared.pdf"]
    assert [d["filename"] for d in api.documents(user=admin)["documents"]] == \
        ["in-no-library.pdf", "shared.pdf", "someone-elses-layoffs.pdf"]


def test_a_member_only_sees_their_own_files_waiting_in_the_inbox(inbox, user, admin):
    other = api.state.accounts.create_user("other@example.com", display_name="Other")
    api.upload(user=user, library_id=own(user), files=[pdf("mine.pdf")])
    api.upload(user=other, library_id=own(other), files=[pdf("theirs.pdf")])
    (inbox / "dropped-in-by-hand.pdf").write_bytes(b"x")  # ragctl/Finder: no uploader

    assert api.documents(user=user)["inbox"] == ["mine.pdf"]
    assert api.documents(user=other)["inbox"] == ["theirs.pdf"]
    assert api.documents(user=admin)["inbox"] == ["dropped-in-by-hand.pdf", "mine.pdf", "theirs.pdf"]


def test_a_reused_inbox_name_belongs_to_its_newest_uploader(inbox, user):
    """Once ingest moves a file out, its name is free again -- the earlier
    uploader of that name mustn't see the next person's file under it."""
    other = api.state.accounts.create_user("other@example.com", display_name="Other")
    api.upload(user=user, library_id=own(user), files=[pdf("report.pdf")])
    (inbox / "report.pdf").unlink()  # ingested
    api.upload(user=other, library_id=own(other), files=[pdf("report.pdf")])

    assert api.documents(user=user)["inbox"] == []
    assert api.documents(user=other)["inbox"] == ["report.pdf"]

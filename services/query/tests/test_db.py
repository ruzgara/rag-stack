import functools
import sqlite3

import pytest

from db import ChatDB, NotFound

USER = "user-under-test"
OTHER = "someone-else"

# Every method takes a user_id with no default, so nothing can read another
# user's rows by forgetting to say whose they are. These tests are about
# storage, so the fixture binds one user and the scoping itself is tested
# separately below.
SCOPED = ("create_conversation", "list_conversations", "get_conversation", "update_conversation",
          "delete_conversation", "history", "add_user_message", "add_assistant_message",
          "record_upload", "upload_usage")


class Scoped:
    def __init__(self, db: ChatDB, user_id: str = USER):
        self._db, self._user = db, user_id

    def __getattr__(self, name):
        attr = getattr(self._db, name)
        return functools.partial(attr, user_id=self._user) if name in SCOPED else attr

    def as_user(self, user_id: str) -> "Scoped":
        return Scoped(self._db, user_id)


@pytest.fixture
def db(tmp_path):
    return Scoped(ChatDB(tmp_path / "chat.db"))


def src(number: int) -> dict:
    return {"number": number, "found_by": "auto", "query": "q", "point_id": f"p{number}", "doc_sha": "d",
            "filename": "book.pdf", "chunk_index": number, "pages": [number], "headings": ["Ch", "S"],
            "text": f"passage {number}", "rerank_score": 0.9}


def answer(db, conv_id: str, content: str = "answer [1]", status: str = "complete", **kw) -> dict:
    return db.add_assistant_message(conv_id, content=content, model="m", preset="everyday",
                                    search_queries=kw.pop("search_queries", ["q"]),
                                    sources=kw.pop("sources", [src(1), src(2)]),
                                    cited=kw.pop("cited", {1}), status=status, **kw)


def test_conversation_lifecycle(db):
    conv = db.create_conversation("everyday")
    assert db.get_conversation(conv["id"])["messages"] == []
    db.update_conversation(conv["id"], title="Bonds", preset="hard")
    got = db.get_conversation(conv["id"], with_messages=False)
    assert (got["title"], got["preset"]) == ("Bonds", "hard")
    db.delete_conversation(conv["id"])
    with pytest.raises(NotFound):
        db.get_conversation(conv["id"])
    with pytest.raises(NotFound):
        db.delete_conversation(conv["id"])


def test_messages_and_sources_round_trip(db):
    conv = db.create_conversation("everyday")
    db.add_user_message(conv["id"], "who are the parties?")
    answer(db, conv["id"], stats={"total_s": 2.5})
    user, assistant = db.get_conversation(conv["id"])["messages"]
    assert user["content"] == "who are the parties?"
    assert assistant["search_queries"] == ["q"] and assistant["stats"] == {"total_s": 2.5}
    assert [(s["number"], s["cited"], s["pages"], s["headings"]) for s in assistant["sources"]] == [
        (1, True, [1], ["Ch", "S"]), (2, False, [2], ["Ch", "S"])]
    assert assistant["sources"][0]["text"] == "passage 1"  # a copy, independent of the index


def test_listing_is_newest_first_with_counts(db):
    a = db.create_conversation("everyday")
    b = db.create_conversation("everyday")
    db.add_user_message(a["id"], "later activity")
    listed = db.list_conversations()
    assert [c["id"] for c in listed] == [a["id"], b["id"]]
    assert [c["message_count"] for c in listed] == [1, 0]


def test_history_leaves_out_failed_answers_and_their_questions(db):
    conv = db.create_conversation("everyday")
    db.add_user_message(conv["id"], "q1")
    answer(db, conv["id"], "a1")
    db.add_user_message(conv["id"], "q2")
    answer(db, conv["id"], "", status="error", error="ollama down")
    db.add_user_message(conv["id"], "q3")
    answer(db, conv["id"], "a3")
    db.add_user_message(conv["id"], "q4 (in progress)")
    assert db.history(conv["id"]) == [
        {"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q3"}, {"role": "assistant", "content": "a3"},
    ]


def test_conversations_are_scoped_to_their_user(db):
    mine = db.create_conversation("everyday", user_id="alice")
    theirs = db.as_user(OTHER).create_conversation("everyday", "Theirs")
    assert [c["title"] for c in db.list_conversations(user_id="alice")] == [None]
    assert db.list_conversations(user_id="bob") == []
    with pytest.raises(NotFound):
        db.get_conversation(theirs["id"], user_id="alice")
    with pytest.raises(NotFound):
        db.get_conversation(mine["id"], user_id="bob")
    with pytest.raises(NotFound):
        db.delete_conversation(mine["id"], user_id="bob")


def test_deleting_a_conversation_deletes_its_messages_and_sources(db, tmp_path):
    conv = db.create_conversation("everyday")
    db.add_user_message(conv["id"], "q")
    answer(db, conv["id"])
    db.delete_conversation(conv["id"])
    with sqlite3.connect(tmp_path / "chat.db") as raw:
        assert raw.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert raw.execute("SELECT COUNT(*) FROM message_sources").fetchone()[0] == 0


# ------------------------------------------------------------ ingest quota --

def test_uploads_are_counted_within_the_window(db):
    db.record_upload("a.pdf", 1_000)
    db.record_upload("b.pdf", 2_500)
    assert db.upload_usage(3600) == (2, 3_500)


def test_uploads_outside_the_window_dont_count(db):
    db.record_upload("old.pdf", 5_000)
    with db._conn() as conn:
        conn.execute("UPDATE uploads SET created_at = created_at - 7200")
    assert db.upload_usage(3600) == (0, 0)


def test_uploads_are_counted_per_user(db):
    db.record_upload("mine.pdf", 1_000)
    db.record_upload("theirs.pdf", 9_000, user_id="someone-else")
    assert db.upload_usage(3600) == (1, 1_000)
    assert db.upload_usage(3600, user_id="someone-else") == (1, 9_000)


def test_an_older_database_is_migrated_in_place(tmp_path):
    """A chat.db written before the uploads table must keep its rows."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE conversations (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT,
                                    preset TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
        INSERT INTO conversations VALUES ('c1', 'local', 'Old chat', 'everyday', 1.0, 2.0);
        PRAGMA user_version=1;
    """)
    conn.commit()
    conn.close()

    db = Scoped(ChatDB(path), "local")

    assert [c["id"] for c in db.list_conversations()] == ["c1"]
    db.record_upload("new.pdf", 10)
    assert db.upload_usage(3600) == (1, 10)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        conn.execute("INSERT INTO libraries (id, name, created_at) VALUES ('lib', 'Finance', 0)")
        conn.execute("INSERT INTO library_documents (library_id, doc_sha, added_at) VALUES ('lib', 'sha', 0)")
        conn.execute("INSERT INTO conversation_libraries (conversation_id, library_id) VALUES ('c1', 'lib')")


# --------------------------------------------------------------- libraries --

def make_library(db: Scoped, library_id: str, name: str) -> None:
    with db._db._conn() as conn:
        conn.execute("INSERT INTO libraries (id, name, created_at) VALUES (?, ?, 0)", (library_id, name))


def test_a_conversation_can_have_libraries_attached(db):
    make_library(db, "lib-a", "A")
    make_library(db, "lib-b", "B")
    conv = db.create_conversation("everyday")
    assert conv["library_ids"] == []

    db.update_conversation(conv["id"], library_ids=["lib-a", "lib-b"])
    assert db.get_conversation(conv["id"], with_messages=False)["library_ids"] == ["lib-a", "lib-b"]

    db.update_conversation(conv["id"], library_ids=[])
    assert db.get_conversation(conv["id"], with_messages=False)["library_ids"] == []


def test_a_conversation_can_be_created_with_libraries_already_attached(db):
    make_library(db, "lib-a", "A")
    conv = db.create_conversation("everyday", library_ids=["lib-a"])
    assert conv["library_ids"] == ["lib-a"]
    assert db.get_conversation(conv["id"], with_messages=False)["library_ids"] == ["lib-a"]


def test_listing_conversations_reports_their_attached_libraries_too(db):
    make_library(db, "lib-a", "A")
    with_lib = db.create_conversation("everyday", library_ids=["lib-a"])
    without = db.create_conversation("everyday")

    by_id = {c["id"]: c["library_ids"] for c in db.list_conversations()}

    assert by_id == {with_lib["id"]: ["lib-a"], without["id"]: []}

import sqlite3

import pytest

from db import ChatDB, NotFound


@pytest.fixture
def db(tmp_path):
    return ChatDB(tmp_path / "chat.db")


def src(number: int) -> dict:
    return {"number": number, "found_by": "auto", "query": "q", "point_id": f"p{number}", "doc_sha": "d",
            "filename": "book.pdf", "chunk_index": number, "pages": [number], "headings": ["Ch", "S"],
            "text": f"passage {number}", "rerank_score": 0.9}


def answer(db: ChatDB, conv_id: str, content: str = "answer [1]", status: str = "complete", **kw) -> dict:
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
    assert db.list_conversations(user_id="bob") == []
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

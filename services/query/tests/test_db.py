import functools
import sqlite3

import pytest

from db import ADMIN_WORKSPACE_ID, ChatDB, NotFound, WorkspaceLibraries

USER = "user-under-test"
OTHER = "someone-else"

# Every method takes a user_id with no default, so nothing can read another
# user's rows by forgetting to say whose they are. These tests are about
# storage, so the fixture binds one user and the scoping itself is tested
# separately below.
SCOPED = ("create_conversation", "list_conversations", "get_conversation", "update_conversation",
          "delete_conversation", "restore_conversation", "archive_conversation", "unarchive_conversation",
          "history", "add_user_message", "add_assistant_message", "prepare_retry",
          "record_upload", "upload_usage", "list_uploads", "get_upload", "delete_uploads",
          "document_held_by_others",
          "create_workspace", "list_workspaces", "get_workspace", "update_workspace", "delete_workspace",
          "restore_workspace", "touch_workspace")


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


def test_a_new_conversation_starts_read_and_opening_it_bumps_last_read_at(db):
    conv = db.create_conversation("everyday")
    assert conv["last_read_at"] == conv["created_at"]

    listed = db.list_conversations()[0]
    assert listed["last_read_at"] == conv["created_at"]

    got_without_messages = db.get_conversation(conv["id"], with_messages=False)
    assert got_without_messages["last_read_at"] == conv["created_at"]  # a peek, not a real open

    opened = db.get_conversation(conv["id"])
    assert opened["last_read_at"] >= conv["created_at"]
    assert opened["last_read_at"] == db.list_conversations()[0]["last_read_at"]


def test_finishing_a_turn_counts_as_reading_it(db):
    """A chat you're actively watching stream must not turn up unread the
    moment the answer lands -- the answer is to your own turn, and it lands
    after the open that started it bumped last_read_at."""
    conv = db.create_conversation("everyday")
    db.get_conversation(conv["id"])  # open it, as the client does before sending
    db.add_user_message(conv["id"], "who are the parties?")

    answer(db, conv["id"])

    listed = db.list_conversations()[0]
    assert listed["last_read_at"] >= listed["last_message_at"]


def test_archiving_restoring_and_unarchiving_do_not_make_a_chat_unread(db):
    conv = db.create_conversation("everyday")
    db.add_user_message(conv["id"], "q")
    answer(db, conv["id"])
    db.get_conversation(conv["id"])  # catch up: read the answer above
    read_at = db.list_conversations()[0]["last_read_at"]

    db.archive_conversation(conv["id"])
    db.unarchive_conversation(conv["id"])
    listed = db.list_conversations()[0]
    assert listed["last_read_at"] == read_at  # unaffected...
    assert listed["updated_at"] > listed["last_message_at"]  # ...even though updated_at moved

    db.delete_conversation(conv["id"])
    restored = db.restore_conversation(conv["id"])
    assert restored["last_read_at"] == read_at


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


def test_preparing_a_retry_deletes_the_last_answer_and_returns_its_question(db):
    conv = db.create_conversation("everyday")
    db.add_user_message(conv["id"], "q1")
    answer(db, conv["id"], "a1")
    db.add_user_message(conv["id"], "q2")
    answer(db, conv["id"], "a2")

    question = db.prepare_retry(conv["id"])

    assert question == "q2"
    messages = db.get_conversation(conv["id"])["messages"]
    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "q1"), ("assistant", "a1"), ("user", "q2")]
    # history() already treats the now-trailing question as unanswered, so
    # a caller can rerun the turn without any special-casing.
    assert db.history(conv["id"]) == [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]


def test_preparing_a_retry_works_on_a_failed_answer_too(db):
    conv = db.create_conversation("everyday")
    db.add_user_message(conv["id"], "q1")
    answer(db, conv["id"], "", status="error", error="ollama down")

    question = db.prepare_retry(conv["id"])

    assert question == "q1"
    messages = db.get_conversation(conv["id"])["messages"]
    assert [m["role"] for m in messages] == ["user"]  # the failed answer is gone


def test_preparing_a_retry_on_an_empty_or_unanswered_chat_is_not_found(db):
    conv = db.create_conversation("everyday")
    with pytest.raises(NotFound):  # no messages at all
        db.prepare_retry(conv["id"])

    db.add_user_message(conv["id"], "q1")
    with pytest.raises(NotFound):  # still unanswered
        db.prepare_retry(conv["id"])


def test_preparing_a_retry_is_scoped_to_the_caller(db):
    conv = db.create_conversation("everyday", user_id="alice")
    db.add_user_message(conv["id"], "q1", user_id="alice")
    answer(db, conv["id"], "a1", user_id="alice")

    with pytest.raises(NotFound):
        db.prepare_retry(conv["id"], user_id="bob")


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


def test_deleting_a_conversation_is_soft_and_keeps_its_messages_and_sources(db, tmp_path):
    conv = db.create_conversation("everyday")
    db.add_user_message(conv["id"], "q")
    answer(db, conv["id"])
    db.delete_conversation(conv["id"])
    with sqlite3.connect(tmp_path / "chat.db") as raw:
        assert raw.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
        assert raw.execute("SELECT COUNT(*) FROM message_sources").fetchone()[0] == 2


def test_a_deleted_conversation_can_be_restored(db):
    conv = db.create_conversation("everyday")
    db.add_user_message(conv["id"], "q")
    db.delete_conversation(conv["id"])
    with pytest.raises(NotFound):
        db.get_conversation(conv["id"])

    restored = db.restore_conversation(conv["id"])

    assert restored["id"] == conv["id"]
    assert db.get_conversation(conv["id"])["messages"][0]["content"] == "q"
    with pytest.raises(NotFound):  # already restored
        db.restore_conversation(conv["id"])


def test_deleted_conversations_are_listed_separately(db):
    kept = db.create_conversation("everyday", title="Kept")
    removed = db.create_conversation("everyday", title="Removed")
    db.delete_conversation(removed["id"])

    assert [c["id"] for c in db.list_conversations()] == [kept["id"]]
    assert [c["id"] for c in db.list_conversations(deleted=True)] == [removed["id"]]


def test_a_chat_in_a_deleted_workspace_does_not_show_up_as_individually_deleted(db):
    ws = db.create_workspace("Research")
    conv = db.create_conversation("everyday", workspace_id=ws["id"])

    db.delete_workspace(ws["id"])

    assert db.list_conversations() == []
    # Hidden from the normal list (via the workspace), but this chat was
    # never itself deleted -- it doesn't belong in the restore tray.
    assert db.list_conversations(deleted=True) == []


def test_archiving_a_chat_hides_it_but_it_stays_open_able(db):
    conv = db.create_conversation("everyday")
    db.add_user_message(conv["id"], "q")

    archived = db.archive_conversation(conv["id"])

    assert archived["archived"] == 1
    # Unlike delete, get_conversation still finds it.
    got = db.get_conversation(conv["id"])
    assert got["archived"] == 1
    assert got["messages"][0]["content"] == "q"


def test_archived_chats_are_listed_separately(db):
    kept = db.create_conversation("everyday", title="Kept")
    stashed = db.create_conversation("everyday", title="Stashed")
    db.archive_conversation(stashed["id"])

    assert [c["id"] for c in db.list_conversations()] == [kept["id"]]
    assert [c["id"] for c in db.list_conversations(archived=True)] == [stashed["id"]]


def test_unarchiving_a_chat_returns_it_to_the_normal_list(db):
    conv = db.create_conversation("everyday")
    db.archive_conversation(conv["id"])

    unarchived = db.unarchive_conversation(conv["id"])

    assert unarchived["archived"] == 0
    assert [c["id"] for c in db.list_conversations()] == [conv["id"]]
    assert db.list_conversations(archived=True) == []


def test_archiving_twice_or_an_unknown_chat_is_not_found(db):
    conv = db.create_conversation("everyday")
    db.archive_conversation(conv["id"])
    with pytest.raises(NotFound):
        db.archive_conversation(conv["id"])
    with pytest.raises(NotFound):
        db.archive_conversation("no-such-chat")
    with pytest.raises(NotFound):  # never archived
        db.unarchive_conversation("no-such-chat")


def test_a_deleted_chat_cannot_be_archived(db):
    conv = db.create_conversation("everyday")
    db.delete_conversation(conv["id"])

    with pytest.raises(NotFound):
        db.archive_conversation(conv["id"])


# ------------------------------------------------------------ ingest quota --

def test_uploads_are_counted_within_the_window(db):
    db.record_upload("a.pdf", 1_000, "sha-a")
    db.record_upload("b.pdf", 2_500, "sha-b")
    assert db.upload_usage(3600) == (2, 3_500)


def test_uploads_outside_the_window_dont_count(db):
    db.record_upload("old.pdf", 5_000, "sha-old")
    with db._conn() as conn:
        conn.execute("UPDATE uploads SET created_at = created_at - 7200")
    assert db.upload_usage(3600) == (0, 0)


def test_uploads_are_counted_per_user(db):
    db.record_upload("mine.pdf", 1_000, "sha-mine")
    db.record_upload("theirs.pdf", 9_000, "sha-theirs", user_id="someone-else")
    assert db.upload_usage(3600) == (1, 1_000)
    assert db.upload_usage(3600, user_id="someone-else") == (1, 9_000)


# ----------------------------------------------------------------- uploads --

def test_a_deleted_upload_stops_listing_but_still_counts_against_the_quota(db):
    """Converting it cost the GPU time either way; deleting mustn't refund it."""
    kept = db.record_upload("kept.pdf", 1_000, "sha-kept")
    gone = db.record_upload("gone.pdf", 2_000, "sha-gone")

    assert db.delete_uploads("sha-gone") == ["gone.pdf"]

    assert [u["id"] for u in db.list_uploads()] == [kept["id"]]
    with pytest.raises(NotFound):
        db.get_upload(gone["id"])
    assert db.upload_usage(3600) == (2, 3_000)


def test_deleting_an_upload_takes_every_copy_of_that_document_by_that_user(db):
    db.record_upload("book.pdf", 1_000, "sha-1")
    db.record_upload("book (1).pdf", 1_000, "sha-1")
    db.record_upload("book.pdf", 1_000, "sha-1", user_id=OTHER)

    assert sorted(db.delete_uploads("sha-1")) == ["book (1).pdf", "book.pdf"]
    assert db.list_uploads() == []
    assert len(db.as_user(OTHER).list_uploads()) == 1  # someone else's copy is theirs


def test_someone_else_s_live_upload_counts_as_holding_the_document(db):
    db.record_upload("book.pdf", 1_000, "sha-1")
    assert not db.document_held_by_others("sha-1")  # only mine

    db.record_upload("book.pdf", 1_000, "sha-1", user_id=OTHER)
    assert db.document_held_by_others("sha-1")

    db.as_user(OTHER).delete_uploads("sha-1")
    assert not db.document_held_by_others("sha-1")  # a deleted one doesn't


def test_an_upload_is_only_ever_its_uploaders(db):
    theirs = db.as_user(OTHER).record_upload("theirs.pdf", 1_000, "sha-1")
    with pytest.raises(NotFound):
        db.get_upload(theirs["id"])


# --------------------------------------------------------- admin workspace --

def test_the_admin_workspace_exists_from_the_start_and_only_admins_see_it(db):
    assert [w["id"] for w in db.list_workspaces(is_admin=True)] == [ADMIN_WORKSPACE_ID]
    assert db.list_workspaces() == []
    assert db.get_workspace(ADMIN_WORKSPACE_ID, is_admin=True)["name"] == "Admin Workspace"
    with pytest.raises(NotFound):
        db.get_workspace(ADMIN_WORKSPACE_ID)


def test_the_admin_workspace_is_created_once(tmp_path):
    first = ChatDB(tmp_path / "chat.db")
    ChatDB(tmp_path / "chat.db")  # a restart
    assert len(first.list_workspaces(user_id="an-admin", is_admin=True)) == 1


def test_each_admin_sees_only_their_own_chats_in_the_admin_workspace(db):
    mine = db.create_conversation("everyday", workspace_id=ADMIN_WORKSPACE_ID)
    db.as_user(OTHER).create_conversation("everyday", workspace_id=ADMIN_WORKSPACE_ID)

    assert [c["id"] for c in db.list_conversations(workspace_id=ADMIN_WORKSPACE_ID, is_admin=True)] == [mine["id"]]
    [ws] = db.list_workspaces(is_admin=True)
    assert ws["conversation_count"] == 1


def test_a_demoted_admin_s_chats_in_the_admin_workspace_are_hidden(db):
    conv = db.create_conversation("everyday", workspace_id=ADMIN_WORKSPACE_ID)

    assert db.list_conversations() == []
    with pytest.raises(NotFound):
        db.get_conversation(conv["id"])
    assert db.get_conversation(conv["id"], is_admin=True)["id"] == conv["id"]  # back once promoted


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


# -------------------------------------------------------------- workspaces --

def test_workspace_lifecycle(db):
    ws = db.create_workspace("Research")
    assert ws["library_ids"] == []
    assert db.get_workspace(ws["id"])["name"] == "Research"

    db.update_workspace(ws["id"], name="Renamed", description="notes")
    got = db.get_workspace(ws["id"])
    assert (got["name"], got["description"]) == ("Renamed", "notes")

    db.delete_workspace(ws["id"])
    with pytest.raises(NotFound):
        db.get_workspace(ws["id"])
    with pytest.raises(NotFound):
        db.delete_workspace(ws["id"])


def test_a_workspace_can_have_libraries_attached(db):
    make_library(db, "lib-a", "A")
    make_library(db, "lib-b", "B")
    ws = db.create_workspace("Research")

    db.update_workspace(ws["id"], library_ids=["lib-a", "lib-b"])
    assert db.get_workspace(ws["id"])["library_ids"] == ["lib-a", "lib-b"]

    db.update_workspace(ws["id"], library_ids=[])
    assert db.get_workspace(ws["id"])["library_ids"] == []


def test_workspaces_are_scoped_to_their_owner(db):
    mine = db.create_workspace("Mine", user_id="alice")
    theirs = db.as_user(OTHER).create_workspace("Theirs")
    assert [w["name"] for w in db.list_workspaces(user_id="alice")] == ["Mine"]
    assert db.list_workspaces(user_id="bob") == []
    with pytest.raises(NotFound):
        db.get_workspace(theirs["id"], user_id="alice")
    with pytest.raises(NotFound):
        db.get_workspace(mine["id"], user_id="bob")
    with pytest.raises(NotFound):
        db.delete_workspace(mine["id"], user_id="bob")


def test_listing_workspaces_reports_their_conversation_count(db):
    ws = db.create_workspace("Research")
    db.create_conversation("everyday", workspace_id=ws["id"])
    db.create_conversation("everyday", workspace_id=ws["id"])
    db.create_conversation("everyday")  # not in the workspace

    assert db.list_workspaces()[0]["conversation_count"] == 2


def test_a_conversation_can_belong_to_a_workspace(db):
    ws = db.create_workspace("Research")
    conv = db.create_conversation("everyday", workspace_id=ws["id"])
    assert conv["workspace_id"] == ws["id"]
    assert db.get_conversation(conv["id"], with_messages=False)["workspace_id"] == ws["id"]
    assert [c["id"] for c in db.list_conversations(workspace_id=ws["id"])] == [conv["id"]]

    outside = db.create_conversation("everyday")
    assert outside["id"] not in [c["id"] for c in db.list_conversations(workspace_id=ws["id"])]


def test_deleting_a_workspace_deletes_its_conversations(db):
    ws = db.create_workspace("Research")
    conv = db.create_conversation("everyday", workspace_id=ws["id"])

    db.delete_workspace(ws["id"])

    with pytest.raises(NotFound):
        db.get_conversation(conv["id"])


def test_a_new_workspace_starts_last_used_at_its_creation(db):
    ws = db.create_workspace("Research")
    assert ws["last_used_at"] == ws["created_at"]
    assert db.get_workspace(ws["id"])["last_used_at"] == ws["created_at"]


def test_touching_a_workspace_moves_it_to_the_front(db):
    older = db.create_workspace("Older")
    newer = db.create_workspace("Newer")
    assert [w["id"] for w in db.list_workspaces()] == [newer["id"], older["id"]]

    db.touch_workspace(older["id"])
    assert [w["id"] for w in db.list_workspaces()] == [older["id"], newer["id"]]


def test_touching_someone_elses_workspace_does_nothing(db):
    ws = db.create_workspace("Mine", user_id="alice")
    db.touch_workspace(ws["id"], user_id="bob")  # no error, just no match
    assert db.get_workspace(ws["id"], user_id="alice")["last_used_at"] == ws["created_at"]


def test_a_deleted_workspace_is_marked_not_removed(db, tmp_path):
    ws = db.create_workspace("Research")
    db.delete_workspace(ws["id"])
    with db._db._conn() as conn:
        row = conn.execute("SELECT deleted, last_used_at FROM workspaces WHERE id = ?", (ws["id"],)).fetchone()
        assert row["deleted"] == 1
        assert row["last_used_at"] >= ws["created_at"]  # also serves as the deleted-at time


def test_a_deleted_workspace_is_hidden_but_its_conversations_stay_stored(db):
    ws = db.create_workspace("Research")
    conv = db.create_conversation("everyday", workspace_id=ws["id"])

    db.delete_workspace(ws["id"])

    assert db.list_workspaces() == []
    with pytest.raises(NotFound):
        db.get_workspace(ws["id"])
    # Hidden from the API's view (the join treats it as gone)...
    with pytest.raises(NotFound):
        db.get_conversation(conv["id"])
    # ...but the row itself was never actually deleted.
    with db._db._conn() as raw:
        assert raw.execute("SELECT COUNT(*) FROM conversations WHERE id = ?",
                           (conv["id"],)).fetchone()[0] == 1


def test_a_deleted_workspace_can_be_restored(db):
    ws = db.create_workspace("Research")
    db.delete_workspace(ws["id"])

    restored = db.restore_workspace(ws["id"])

    assert restored["id"] == ws["id"]
    assert [w["id"] for w in db.list_workspaces()] == [ws["id"]]
    with pytest.raises(NotFound):  # already restored
        db.restore_workspace(ws["id"])


def test_restoring_a_workspace_also_restores_its_conversations(db):
    ws = db.create_workspace("Research")
    conv = db.create_conversation("everyday", workspace_id=ws["id"])
    db.delete_workspace(ws["id"])
    with pytest.raises(NotFound):
        db.get_conversation(conv["id"])

    db.restore_workspace(ws["id"])

    # The conversation itself was never touched -- it's reachable again
    # purely because the workspace's own deleted flag flipped back.
    assert db.get_conversation(conv["id"])["id"] == conv["id"]
    assert [c["id"] for c in db.list_conversations(workspace_id=ws["id"])] == [conv["id"]]


def test_restoring_a_workspace_that_isnt_deleted_is_not_found(db):
    ws = db.create_workspace("Research")
    with pytest.raises(NotFound):
        db.restore_workspace(ws["id"])


def test_restoring_an_unknown_or_someone_elses_workspace_is_not_found(db):
    with pytest.raises(NotFound):
        db.restore_workspace("no-such-workspace")

    ws = db.create_workspace("Mine", user_id="alice")
    db.delete_workspace(ws["id"], user_id="alice")
    with pytest.raises(NotFound):
        db.restore_workspace(ws["id"], user_id="bob")


def test_deleted_workspaces_are_listed_separately(db):
    kept = db.create_workspace("Kept")
    removed = db.create_workspace("Removed")
    db.delete_workspace(removed["id"])

    assert [w["id"] for w in db.list_workspaces()] == [kept["id"]]
    assert [w["id"] for w in db.list_workspaces(deleted=True)] == [removed["id"]]


def test_a_conversation_in_a_workspace_uses_the_workspaces_libraries(db):
    make_library(db, "lib-a", "A")
    ws = db.create_workspace("Research")
    db.update_workspace(ws["id"], library_ids=["lib-a"])

    conv = db.create_conversation("everyday", workspace_id=ws["id"])
    assert conv["library_ids"] == ["lib-a"]


def test_changing_a_workspaces_libraries_reaches_its_existing_conversations(db):
    make_library(db, "lib-a", "A")
    make_library(db, "lib-b", "B")
    ws = db.create_workspace("Research")
    db.update_workspace(ws["id"], library_ids=["lib-a"])
    conv = db.create_conversation("everyday", workspace_id=ws["id"])

    db.update_workspace(ws["id"], library_ids=["lib-a", "lib-b"])

    assert db.get_conversation(conv["id"], with_messages=False)["library_ids"] == ["lib-a", "lib-b"]
    assert db.list_conversations()[0]["library_ids"] == ["lib-a", "lib-b"]

    db.update_workspace(ws["id"], library_ids=[])
    assert db.get_conversation(conv["id"], with_messages=False)["library_ids"] == []
    assert db.list_conversations()[0]["library_ids"] == []


def test_a_conversation_in_a_workspace_cannot_have_its_own_libraries(db):
    make_library(db, "lib-a", "A")
    ws = db.create_workspace("Research")

    with pytest.raises(WorkspaceLibraries):
        db.create_conversation("everyday", workspace_id=ws["id"], library_ids=["lib-a"])

    conv = db.create_conversation("everyday", workspace_id=ws["id"])
    with pytest.raises(WorkspaceLibraries):
        db.update_conversation(conv["id"], library_ids=["lib-a"])


def test_a_conversation_outside_a_workspace_keeps_its_own_libraries(db):
    make_library(db, "lib-a", "A")
    make_library(db, "lib-b", "B")
    ws = db.create_workspace("Research")
    db.update_workspace(ws["id"], library_ids=["lib-b"])

    conv = db.create_conversation("everyday", library_ids=["lib-a"])

    assert db.get_conversation(conv["id"], with_messages=False)["library_ids"] == ["lib-a"]
    assert db.list_conversations()[0]["library_ids"] == ["lib-a"]

"""
The three hardest branches of api.py's _stream_turn, exercised through the
real send_message/retry_message endpoints over a real ChatService (fake
Ollama + retriever, like test_chat.py) rather than a hand-built TurnResult:
the `done` SSE payload's shape, the stopped path storing NUMBERED source
dicts (the CHECK constraint in db.py would reject a bug there outright),
cross-turn citation returning the same [doc:chunk] a second time, and retry
freeing a document number for reuse. Nothing below this exercises any one
layer in isolation -- test_chat.py/test_db.py/test_citations.py already do
that -- this is what's actually wrong if those three all pass but the whole
pipeline still isn't.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import CHAT_ENV, FakeOllama, FakeRetriever, answer, make_hit

import api
from accounts import Accounts
from chat import ChatConfig, ChatService
from citations import handle_for
from db import ChatDB
from limits import Gate, GateConfig


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = ChatDB(tmp_path / "chat.db")
    accounts = Accounts(db)
    user = accounts.create_user("member@example.com", display_name="Member")
    monkeypatch.setattr(api.state, "db", db, raising=False)
    monkeypatch.setattr(api.state, "accounts", accounts, raising=False)
    monkeypatch.setattr(api.state, "gate", Gate(GateConfig()), raising=False)
    return user


def wire_chat(monkeypatch, hits: dict, streams: list, plan: str = '{"search": true, "queries": ["q"]}') -> None:
    cfg = ChatConfig.from_env({**CHAT_ENV, "CHAT_MAX_TOOL_ROUNDS": "0"})  # one streamed call per turn, no tool rounds
    ollama = FakeOllama(plan=plan, streams=streams)
    monkeypatch.setattr(api.state, "chat", ChatService(FakeRetriever(hits), cfg, client=ollama.client()),
                        raising=False)


def drain(response) -> list[tuple[str, dict]]:
    async def collect():
        return [c async for c in response.body_iterator]
    lines = asyncio.run(collect())
    out = []
    for block in lines:
        head, _, rest = block.partition("\n")
        kind = head.removeprefix("event: ")
        data_line = rest.strip().removeprefix("data: ")
        out.append((kind, json.loads(data_line)))
    return out


def event(events: list[tuple[str, dict]], kind: str) -> dict:
    return next(data for k, data in events if k == kind)


# ------------------------------------------------------------- done payload --

def test_the_done_payload_carries_handles_refs_and_per_source_cited(env, monkeypatch):
    user = env
    conv = api.state.db.create_conversation("everyday", user_id=user.id)
    hit = make_hit("a", ["Sec"], text="passage text", doc_sha="docA", chunk_index=0)
    handle = handle_for("docA", 0)
    wire_chat(monkeypatch, {"q": [hit]}, [answer(f"Cites it [{handle}].")])

    events = drain(api.send_message(conv["id"], api.NewMessage(content="q"), user))

    done = event(events, "done")
    assert done["cited"] == [handle]
    assert done["refs"] == {handle: [1, 1]}
    assert done["invalid_citations"] == []
    [src] = [s for s in done["sources"] if s["handle"] == handle]
    assert src["cited"] is True and src["doc_number"] == 1 and src["chunk_number"] == 1

    stored = api.state.db.get_conversation(conv["id"], user_id=user.id)["messages"][-1]
    assert stored["sources"][0]["doc_number"] == 1  # the stored copy agrees with the SSE payload


def test_the_turn_cost_is_sent_live_but_never_stored(env, monkeypatch):
    user = env
    conv = api.state.db.create_conversation("everyday", user_id=user.id)
    wire_chat(monkeypatch, {"q": [make_hit("a", ["Sec"])]}, [answer("ok")])
    real_run_turn = api.state.chat.run_turn

    def priced(*args, **kw):
        for ev in real_run_turn(*args, **kw):
            if ev["type"] == "done":
                ev["result"].cost = 0.0042
            yield ev
    monkeypatch.setattr(api.state.chat, "run_turn", priced)

    events = drain(api.send_message(conv["id"], api.NewMessage(content="q"), user))

    assert event(events, "done")["cost"] == 0.0042
    stored = api.state.db.get_conversation(conv["id"], user_id=user.id)["messages"][-1]
    assert "cost" not in stored["stats"]


# ------------------------------------------------------------------ stopped --

def test_the_stopped_path_stores_numbered_source_dicts(env, monkeypatch):
    """The branch most likely to be forgotten: a stop mid-stream never
    reaches run_turn's own citation step, so _stream_turn must redo it and
    store NUMBERED sources -- message_sources' own CHECK constraint would
    reject an unnumbered cited row outright if this regressed."""
    user = env
    conv = api.state.db.create_conversation("everyday", user_id=user.id)
    handle = handle_for("docA", 0)

    def long_answer(self, question, history, preset, **kw):
        yield {"type": "sources", "sources": [{
            "number": 1, "found_by": "auto", "query": "q", "point_id": "p", "doc_sha": "docA", "filename": "f.pdf",
            "chunk_index": 0, "pages": [1], "headings": [], "text": "t", "rerank_score": 0.9, "handle": handle,
            "doc_number": None, "chunk_number": None}]}
        yield {"type": "token", "text": f"It says [{handle}]"}
        assert api.stop_generation(conv["id"], user) == {"stopped": 1}
        yield {"type": "token", "text": " and more, never seen"}
        yield {"type": "done", "result": None}

    wire_chat(monkeypatch, {}, [])
    monkeypatch.setattr(type(api.state.chat), "run_turn", long_answer)

    events = drain(api.send_message(conv["id"], api.NewMessage(content="q"), user))

    stopped = event(events, "stopped")
    assert stopped["cited"] == [handle]
    assert stopped["refs"] == {handle: [1, 1]}
    assert not any(k == "done" for k, _ in events)

    stored = api.state.db.get_conversation(conv["id"], user_id=user.id)["messages"][-1]
    assert stored["status"] == "stopped" and stored["content"] == f"It says [{handle}]"
    [src] = stored["sources"]
    assert src["cited"] is True and (src["doc_number"], src["chunk_number"]) == (1, 1)


# --------------------------------------------------------- cross-turn citation --

def test_a_second_turn_citing_the_first_turns_passage_returns_the_same_ref(env, monkeypatch):
    user = env
    conv = api.state.db.create_conversation("everyday", user_id=user.id)
    hit = make_hit("a", ["Sec"], text="passage text", doc_sha="docA", chunk_index=0)
    handle = handle_for("docA", 0)

    wire_chat(monkeypatch, {"q": [hit]}, [answer(f"First mention [{handle}].")])
    first = drain(api.send_message(conv["id"], api.NewMessage(content="q"), user))
    assert event(first, "done")["refs"] == {handle: [1, 1]}

    # Second turn: no search at all, relying purely on the carry-forward
    # block (db.citation_state) to know the handle and let it cite again.
    wire_chat(monkeypatch, {}, [answer(f"Back to that first point [{handle}].")],
             plan='{"search": false, "queries": ["x"]}')
    second = drain(api.send_message(conv["id"], api.NewMessage(content="and that first thing?"), user))

    done = event(second, "done")
    assert done["cited"] == [handle]
    assert done["refs"] == {handle: [1, 1]}  # unchanged: recognized, not renumbered
    assert done["invalid_citations"] == []


# -------------------------------------------------------------------- retry --

def test_retrying_frees_its_document_number_for_the_next_new_document(env, monkeypatch):
    user = env
    conv = api.state.db.create_conversation("everyday", user_id=user.id)
    hit_a = make_hit("a", ["A"], text="a text", doc_sha="docA", chunk_index=0)
    hit_b = make_hit("b", ["B"], text="b text", doc_sha="docB", chunk_index=0)
    handle_a, handle_b = handle_for("docA", 0), handle_for("docB", 0)

    wire_chat(monkeypatch, {"first": [hit_a]}, [answer(f"Cites A [{handle_a}].")],
             plan='{"search": true, "queries": ["first"]}')
    drain(api.send_message(conv["id"], api.NewMessage(content="q1"), user))

    wire_chat(monkeypatch, {"second": [hit_b]}, [answer(f"Cites B [{handle_b}].")],
             plan='{"search": true, "queries": ["second"]}')
    second = drain(api.send_message(conv["id"], api.NewMessage(content="q2"), user))
    assert event(second, "done")["refs"] == {handle_b: [2, 1]}

    # Retry q2's answer with a brand new document C instead.
    hit_c = make_hit("c", ["C"], text="c text", doc_sha="docC", chunk_index=0)
    handle_c = handle_for("docC", 0)
    wire_chat(monkeypatch, {"third": [hit_c]}, [answer(f"Cites C instead [{handle_c}].")],
             plan='{"search": true, "queries": ["third"]}')
    retried = drain(api.retry_message(conv["id"], user))

    done = event(retried, "done")
    assert done["refs"] == {handle_c: [2, 1]}  # B's freed number 2, recycled -- not 3

    numbers, cited = api.state.db.citation_state(conv["id"], user_id=user.id)
    assert numbers.ref(handle_a) == (1, 1)  # untouched
    assert numbers.ref(handle_b) is None  # gone
    assert numbers.ref(handle_c) == (2, 1)
    assert {c["doc_sha"] for c in cited} == {"docA", "docC"}

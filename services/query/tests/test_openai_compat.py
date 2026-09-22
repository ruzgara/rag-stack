import asyncio

import pytest
from conftest import CHAT_ENV, FakeOllama, FakeRetriever, answer, make_hit

import api
from accounts import Accounts
from chat import ChatConfig, ChatService, Source, TurnResult
from citations import HANDLE_RE, handle_for
from db import ChatDB
from limits import Gate, GateConfig
from openai_compat import ChatCompletionRequest, chat_completions, sources_footer, text_of


def test_message_content_as_string_or_parts():
    assert text_of("hello") == "hello"
    assert text_of([{"type": "text", "text": "a"}, {"type": "image_url", "image_url": {}},
                    {"type": "text", "text": "b"}]) == "a\nb"
    assert text_of(None) == ""


def test_sources_footer_lists_only_cited_sources_sorted_by_doc_and_chunk():
    sources = [Source(number=n, found_by="auto", query="q", point_id=f"p{n}", doc_sha="d", filename=f"doc{n}.pdf",
                      chunk_index=0, pages=[n], headings=[], text="t", rerank_score=None,
                      handle=f"S{n}abc2") for n in (1, 2, 3)]
    result = TurnResult(sources=sources, cited=["S3abc2", "S1abc2"],
                        refs={"S3abc2": (2, 1), "S1abc2": (1, 1)})
    assert sources_footer(result) == "\n\nSources:\n[1:1] doc1.pdf, p. 1\n[2:1] doc3.pdf, p. 3"
    assert sources_footer(TurnResult(sources=sources, cited=[], refs={})) == ""


# --------------------------------------------------------- the real endpoint --
# Drives chat_completions (both streamed and non-streamed) through a real
# ChatService over a fake Ollama/retriever, exactly like test_chat.py, so
# these exercise the actual rendering path rather than a hand-built TurnResult.

@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    db = ChatDB(tmp_path / "chat.db")
    accounts = Accounts(db)
    user = accounts.create_user("member@example.com", display_name="Member")
    monkeypatch.setattr(api.state, "accounts", accounts, raising=False)
    monkeypatch.setattr(api.state, "gate", Gate(GateConfig()), raising=False)
    return user


def make_chat(hits: dict, script: list) -> ChatService:
    cfg = ChatConfig.from_env({**CHAT_ENV, **{k: str(v) for k, v in {}.items()}})
    ollama = FakeOllama(plan='{"search": true, "queries": ["q"]}', streams=[script])
    return ChatService(FakeRetriever(hits), cfg, client=ollama.client())


def drain_stream(response) -> str:
    async def collect():
        return [c async for c in response.body_iterator]
    chunks = asyncio.run(collect())
    text = ""
    import json as _json
    for line in chunks:
        for part in line.split("\n\n"):
            if not part.startswith("data: ") or part == "data: [DONE]":
                continue
            payload = _json.loads(part[len("data: "):])
            text += payload["choices"][0]["delta"].get("content", "")
    return text


def test_no_handle_reaches_the_client_streamed_or_not(endpoint, monkeypatch):
    hit = make_hit("a", ["Sec"], text="passage text", chunk_index=0)
    handle = handle_for(hit.doc_sha, hit.chunk_index)
    script = answer(f"Cites [{handle}] here.")
    monkeypatch.setattr(api.state, "chat", make_chat({"q": [hit]}, script), raising=False)

    body = ChatCompletionRequest(model="everyday", messages=[{"role": "user", "content": "q"}], stream=False)
    result = chat_completions(body, endpoint)
    non_streamed = result["choices"][0]["message"]["content"]
    assert not HANDLE_RE.search(non_streamed)
    assert "[1:1]" in non_streamed

    monkeypatch.setattr(api.state, "chat", make_chat({"q": [hit]}, script), raising=False)
    body = ChatCompletionRequest(model="everyday", messages=[{"role": "user", "content": "q"}], stream=True)
    streamed = drain_stream(chat_completions(body, endpoint))
    assert not HANDLE_RE.search(streamed)
    assert non_streamed == streamed

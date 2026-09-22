"""
Fakes for query tests: an in-memory Qdrant (scroll with the filters
retrieval.py uses), a scripted Ollama, a reranker that scores by lookup, and
a retriever with canned results. Nothing touches the network, the GPU, .env
or data/.
"""

from __future__ import annotations

import json

import httpx
import pytest

from retrieval import Hit, SearchConfig

SEARCH_ENV = {"EMBED_MODEL": "bge-m3"}
CHAT_ENV = {"CHAT_MODEL_EVERYDAY": "small-model", "CHAT_MODEL_HARD": "big-model", "CHAT_THINK_HARD": "true"}


def make_hit(point_id: str, headings: list[str], score: float = 0.9, doc_sha: str = "doc1", text: str | None = None,
             chunk_index: int = 0, pages: list[int] | None = None) -> Hit:
    text = text if text is not None else f"text of {point_id}"
    return Hit(rank=0, score=score, fused_rank=1, fused_score=0.5, point_id=point_id, doc_sha=doc_sha,
               filename=f"{doc_sha}.pdf", chunk_index=chunk_index, pages=pages or [1], headings=list(headings),
               text=text, context_text="\n".join([*headings, text]), rerank_score=score)


def point(point_id: str, chunk_index: int, headings: list[str], text: str, doc_sha: str = "doc1",
          pages: list[int] | None = None) -> dict:
    return {"id": point_id, "payload": {"doc_sha": doc_sha, "chunk_index": chunk_index, "headings": headings,
                                        "pages": pages or [chunk_index], "text": text}}


class FakeQdrant:
    """Answers points/scroll with 'must' match filters, like Qdrant."""

    def __init__(self, points: list[dict]):
        self.points = points
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.requests.append({"path": request.url.path, "body": body})
        if not request.url.path.endswith("/points/scroll"):
            return httpx.Response(404, text="not faked")
        found = []
        for p in self.points:
            ok = True
            for cond in (body.get("filter") or {}).get("must", []):
                value = p["payload"].get(cond["key"])
                want = cond["match"]["value"]
                ok &= want in value if isinstance(value, list) else value == want
            if ok:
                found.append(p)
        return httpx.Response(200, json={"result": {"points": found[:body.get("limit", 10)]}})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


class FakeReranker:
    """Scores passages by a substring lookup; unknown passages score 0."""

    def __init__(self, scores: dict[str, float]):
        self.scores = scores
        self.calls: list[tuple[str, list[str]]] = []

    def score(self, question: str, passages: list[str]) -> list[float]:
        self.calls.append((question, passages))
        return [next((s for key, s in self.scores.items() if key in p), 0.0) for p in passages]


@pytest.fixture
def search_cfg():
    def _cfg(**env) -> SearchConfig:
        return SearchConfig.from_env({**SEARCH_ENV, **{k: str(v) for k, v in env.items()}})
    return _cfg


class FakeOllama:
    """Scripted Ollama: /api/show reports capabilities, non-streamed
    /api/chat (the planning step) returns `plan`, and each streamed
    /api/chat plays the next script in `streams` as NDJSON."""

    def __init__(self, plan: str | None = '{"search": true, "queries": []}', streams: list[list[dict]] | None = None,
                 tools: bool = True, context_length: int | None = 8192):
        self.plan = plan
        self.streams = list(streams or [])
        self.tools = tools
        self.context_length = context_length
        self.chat_requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/api/show":
            model_info = {"fake.context_length": self.context_length} if self.context_length else {}
            return httpx.Response(200, json={"capabilities": ["completion"] + (["tools"] if self.tools else []),
                                             "model_info": model_info})
        self.chat_requests.append(body)
        if not body.get("stream"):
            if self.plan is None:
                return httpx.Response(500, text="model crashed")
            return httpx.Response(200, json={"message": {"role": "assistant", "content": self.plan}})
        script = self.streams.pop(0)
        return httpx.Response(200, content="\n".join(json.dumps(c) for c in script).encode())

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def answer(*tokens: str, prompt_tokens: int = 100) -> list[dict]:
    """A streamed answer: one chunk per token, then done."""
    chunks = [{"message": {"role": "assistant", "content": t}, "done": False} for t in tokens]
    return chunks + [{"message": {"role": "assistant", "content": ""}, "done": True,
                      "prompt_eval_count": prompt_tokens, "eval_count": len(tokens)}]


def tool_call(query: str) -> list[dict]:
    """A streamed turn that only calls search_documents."""
    return calls({"name": "search_documents", "arguments": {"query": query}})


def calls(*functions: dict) -> list[dict]:
    """A streamed turn that only makes the given tool calls."""
    return [{"message": {"role": "assistant", "content": "",
                         "tool_calls": [{"function": f} for f in functions]}, "done": False},
            {"message": {"role": "assistant", "content": ""}, "done": True, "prompt_eval_count": 50, "eval_count": 5}]


class FakeLibrary:
    """Stands in for the document list."""

    def __init__(self, text: str = "The knowledge base holds 2 document(s).", error: Exception | None = None):
        self.text, self.error = text, error
        self.calls: list[str | None] = []

    def describe(self, name: str | None = None, doc_shas: set[str] | None = None) -> str:
        self.calls.append(name)
        if self.error:
            raise self.error
        return self.text if name is None else f"{self.text} matching {name}"


class FakeRetriever:
    """Canned results per query, honouring limit and exclude like the real one."""

    def __init__(self, results: dict[str, list[Hit]]):
        self.results = results
        self.calls: list[dict] = []

    def retrieve(self, question: str, limit: int | None = None, exclude: set[str] | None = None,
                doc_shas: set[str] | None = None) -> list[Hit]:
        self.calls.append({"question": question, "limit": limit, "exclude": set(exclude or ()), "doc_shas": doc_shas})
        hits = [h for h in self.results.get(question, []) if not (set(h.ids) & set(exclude or ()))]
        return hits[:limit] if limit else hits

import json

import httpx
import pytest
from conftest import CHAT_ENV, FakeLibrary, FakeRetriever, make_hit

from chat import ChatConfig, ChatError, ChatService
from openrouter import OpenRouter, OpenRouterError, floor, stream_to_chunks, to_openai_messages

OR_ENV = {**CHAT_ENV, "CHAT_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "sk-test",
          "CHAT_MODEL_EVERYDAY": "vendor/small", "CHAT_MODEL_HARD": "vendor/big"}

MODELS = [
    {"id": "vendor/small", "context_length": 32000, "top_provider": {"context_length": 16000},
     "supported_parameters": ["tools", "temperature", "structured_outputs", "response_format"]},
    {"id": "vendor/big", "context_length": 200000, "top_provider": {"context_length": None},
     "supported_parameters": ["temperature", "reasoning"]},
]


def sse(*chunks: dict, done: bool = True) -> bytes:
    lines = [": OPENROUTER PROCESSING", ""]
    for c in chunks:
        lines += [f"data: {json.dumps(c)}", ""]
    if done:
        lines += ["data: [DONE]", ""]
    return "\n".join(lines).encode()


def delta(**fields) -> dict:
    return {"choices": [{"index": 0, "delta": fields}]}


def usage(prompt: int, completion: int, cost: float | None = None) -> dict:
    u = {"prompt_tokens": prompt, "completion_tokens": completion}
    if cost is not None:
        u["cost"] = cost
    return {"choices": [], "usage": u}


class FakeOpenRouter:
    """Scripted OpenRouter: /models lists MODELS, a non-streamed
    /chat/completions returns `plan`, and each streamed one plays the next
    SSE body in `streams`."""

    def __init__(self, plan: str = '{"search": true, "queries": []}', streams: list[bytes] | None = None,
                 models_status: int = 200, plan_cost: float | None = None):
        self.plan = plan
        self.plan_cost = plan_cost
        self.streams = list(streams or [])
        self.models_status = models_status
        self.requests: list[dict] = []
        self.model_list_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sk-test"
        if request.url.path.endswith("/models"):
            self.model_list_calls += 1
            return httpx.Response(self.models_status, json={"data": MODELS})
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json={"data": {}})
        body = json.loads(request.content)
        self.requests.append(body)
        if not body.get("stream"):
            reply = {"choices": [{"message": {"role": "assistant", "content": self.plan}}]}
            if self.plan_cost is not None:
                reply["usage"] = {"prompt_tokens": 50, "completion_tokens": 5, "cost": self.plan_cost}
            return httpx.Response(200, json=reply)
        return httpx.Response(200, content=self.streams.pop(0))

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def service(fake: FakeOpenRouter, results=None, **env) -> ChatService:
    cfg = ChatConfig.from_env({**OR_ENV, **env})
    return ChatService(FakeRetriever(results or {}), cfg, client=fake.client(), library=FakeLibrary())


# ---------------------------------------------------------------- config --

def test_provider_defaults_to_ollama():
    assert ChatConfig.from_env(CHAT_ENV).provider == "ollama"


@pytest.mark.parametrize("env, match", [({"CHAT_PROVIDER": "openai"}, "CHAT_PROVIDER"),
                                        ({"OPENROUTER_API_KEY": ""}, "OPENROUTER_API_KEY")])
def test_bad_provider_settings_are_rejected(env, match):
    with pytest.raises(ValueError, match=match):
        ChatConfig.from_env({**OR_ENV, **env})


def test_api_key_stays_out_of_the_config_repr():
    assert "sk-test" not in repr(ChatConfig.from_env(OR_ENV))


# --------------------------------------------------------------- routing --

@pytest.mark.parametrize("model, routed", [("anthropic/claude-sonnet-5", "anthropic/claude-sonnet-5:floor"),
                                           ("openai/gpt-oss-20b:free", "openai/gpt-oss-20b:free"),
                                           ("vendor/small:floor", "vendor/small:floor")])
def test_floor_routing_suffix(model, routed):
    assert floor(model) == routed


def test_every_chat_request_uses_floor_routing_but_lookups_use_the_plain_id():
    fake = FakeOpenRouter(streams=[sse(delta(content="ok"), usage(10, 1))])
    svc = service(fake)
    events = list(svc.run_turn("q", [], "everyday"))
    assert [r["model"] for r in fake.requests] == ["vendor/small:floor", "vendor/small:floor"]
    assert events[-1]["result"].stats["context_limit"] == 16000  # /models found vendor/small
    assert events[-1]["result"].model == "vendor/small"


# -------------------------------------------------------------- messages --

def test_messages_translate_tool_calls_and_replies():
    out = to_openai_messages([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "reasoning_details": [{"type": "reasoning.text", "text": "hm"}],
         "tool_calls": [{"id": "call_1", "function": {"name": "search_documents", "arguments": {"query": "x"}}}]},
        {"role": "tool", "tool_name": "search_documents", "tool_call_id": "call_1", "content": "passages"},
    ])
    assert out[:2] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}]
    assert out[2] == {"role": "assistant", "content": None,
                      "reasoning_details": [{"type": "reasoning.text", "text": "hm"}],
                      "tool_calls": [{"id": "call_1", "type": "function",
                                      "function": {"name": "search_documents", "arguments": '{"query": "x"}'}}]}
    assert out[3] == {"role": "tool", "tool_call_id": "call_1", "content": "passages"}


# ------------------------------------------------------------- streaming --

def chunks_of(body: bytes) -> list[dict]:
    return list(stream_to_chunks(iter(body.decode().split("\n"))))


def test_stream_becomes_ollama_chunks():
    chunks = chunks_of(sse(delta(reasoning="Let me"), delta(reasoning=" think"), delta(content="Hel"),
                           delta(content="lo"), usage(120, 7)))
    assert [c["message"] for c in chunks[:-1]] == [
        {"role": "assistant", "thinking": "Let me"}, {"role": "assistant", "thinking": " think"},
        {"role": "assistant", "content": "Hel"}, {"role": "assistant", "content": "lo"}]
    assert chunks[-1] == {"message": {"role": "assistant", "content": ""}, "done": True,
                          "prompt_eval_count": 120, "eval_count": 7}


def test_fragmented_tool_calls_are_assembled():
    chunks = chunks_of(sse(
        delta(tool_calls=[{"index": 0, "id": "call_a", "function": {"name": "search_documents", "arguments": ""}}]),
        delta(tool_calls=[{"index": 0, "function": {"arguments": '{"que'}}]),
        delta(tool_calls=[{"index": 1, "id": "call_b", "function": {"name": "list_documents", "arguments": ""}}]),
        delta(tool_calls=[{"index": 0, "function": {"arguments": 'ry": "loans"}'}}]),
    ))
    assert len(chunks) == 1 and chunks[0]["done"]
    assert chunks[0]["message"]["tool_calls"] == [
        {"id": "call_a", "function": {"name": "search_documents", "arguments": {"query": "loans"}}},
        {"id": "call_b", "function": {"name": "list_documents", "arguments": {}}}]


def test_reasoning_details_fragments_merge_by_index():
    chunks = chunks_of(sse(
        delta(reasoning_details=[{"type": "reasoning.text", "index": 0, "text": "Fir", "format": "anthropic"}]),
        delta(reasoning_details=[{"type": "reasoning.text", "index": 0, "text": "st", "signature": None}]),
        delta(reasoning_details=[{"type": "reasoning.text", "index": 0, "text": "", "signature": "sig"}]),
    ))
    assert chunks[-1]["message"]["reasoning_details"] == [
        {"type": "reasoning.text", "index": 0, "text": "First", "format": "anthropic", "signature": "sig"}]


def test_mid_stream_error_raises():
    body = sse(delta(content="partial"), {"error": {"code": "server_error", "message": "Provider disconnected"},
                                          "choices": [{"index": 0, "delta": {"content": ""},
                                                       "finish_reason": "error"}]})
    with pytest.raises(OpenRouterError, match="Provider disconnected"):
        chunks_of(body)


def test_done_chunk_carries_the_cost():
    assert chunks_of(sse(delta(content="hi"), usage(10, 1, cost=0.0021)))[-1]["cost"] == 0.0021
    assert "cost" not in chunks_of(sse(delta(content="hi"), usage(10, 1)))[-1]


def test_stream_cut_off_before_done_raises():
    with pytest.raises(OpenRouterError, match="before the answer was complete"):
        chunks_of(sse(delta(content="partial"), done=False))


# -------------------------------------------------------------- metadata --

def test_show_reports_tools_and_the_providers_context_length():
    fake = FakeOpenRouter()
    router = OpenRouter(fake.client(), "sk-test")
    assert router.show("vendor/small") == {"capabilities": ["completion", "tools"],
                                           "model_info": {"openrouter.context_length": 16000}}
    # No top_provider limit: falls back to the model's own.
    assert router.show("vendor/big") == {"capabilities": ["completion"],
                                         "model_info": {"openrouter.context_length": 200000}}
    assert router.show("vendor/missing") is None
    assert fake.model_list_calls == 1


def test_a_routing_suffix_in_env_still_finds_the_model():
    router = OpenRouter(FakeOpenRouter().client(), "sk-test")
    assert router.model("vendor/small:floor")["id"] == "vendor/small"
    assert router.model("vendor/small:nitro")["id"] == "vendor/small"
    assert router.model("vendor/small:free") is None  # a real variant, and not listed here


def test_a_failed_model_list_is_not_cached():
    fake = FakeOpenRouter(models_status=503)
    svc = service(fake)
    assert svc.supports_tools("vendor/small") is False
    fake.models_status = 200
    assert svc.supports_tools("vendor/small") is True


def test_ollamas_context_cap_does_not_apply():
    svc = service(FakeOpenRouter(), OLLAMA_CONTEXT_LENGTH="8192")
    assert svc.context_length("vendor/small") == 16000


# ------------------------------------------------------------------ chat --

def test_planner_gets_a_strict_json_schema_where_the_model_supports_one():
    fake = FakeOpenRouter(plan='{"search": true, "queries": ["loans"]}')
    svc = service(fake)
    plan = svc.plan_search(svc.cfg.preset("everyday"), [], "tell me about loans")
    assert plan.queries == ["loans"] and not plan.failed
    fmt = fake.requests[0]["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"]
    assert fmt["json_schema"]["schema"]["additionalProperties"] is False
    assert fake.requests[0]["temperature"] == 0 and "reasoning" not in fake.requests[0]


def test_model_without_structured_outputs_gets_no_response_format():
    fake = FakeOpenRouter()
    svc = service(fake)
    svc.plan_search(svc.cfg.preset("hard"), [], "q")
    assert "response_format" not in fake.requests[0]


def test_http_error_becomes_chat_error():
    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": MODELS})
        return httpx.Response(402, text="Insufficient credits")
    svc = ChatService(FakeRetriever({}), ChatConfig.from_env(OR_ENV),
                      client=httpx.Client(transport=httpx.MockTransport(handler)), library=FakeLibrary())
    with pytest.raises(ChatError, match="402"):
        list(svc._chat_stream("vendor/small", [{"role": "user", "content": "q"}], think=False, tools=None))


def test_a_turn_with_a_tool_round_runs_end_to_end():
    reasoning = [{"type": "reasoning.text", "index": 0, "text": "need more", "signature": "sig"}]
    fake = FakeOpenRouter(
        plan='{"search": true, "queries": ["loans"]}',
        streams=[
            sse(delta(reasoning="need more", reasoning_details=reasoning),
                delta(tool_calls=[{"index": 0, "id": "call_1",
                                   "function": {"name": "search_documents", "arguments": '{"query": "rates"}'}}]),
                usage(300, 10)),
            sse(delta(content="Answer."), usage(500, 2)),
        ])
    svc = service(fake, results={"loans": [make_hit("a", ["Loans"])], "rates": [make_hit("b", ["Rates"])]})
    events = list(svc.run_turn("tell me about loans", [], "everyday"))
    result = events[-1]["result"]

    assert result.answer == "Answer."
    assert result.search_queries == ["loans", "rates"]
    assert {"type": "thinking", "text": "need more"} in events
    assert result.stats["context_used"] == 502 and result.stats["context_limit"] == 16000

    first, second = fake.requests[1], fake.requests[2]
    assert first["stream"] and first["tools"]
    assistant, tool = second["messages"][-2:]
    assert assistant["tool_calls"][0]["id"] == "call_1"
    assert assistant["reasoning_details"] == reasoning
    assert tool["role"] == "tool" and tool["tool_call_id"] == "call_1" and "Rates" in tool["content"]


def test_thinking_preset_enables_reasoning_and_drops_temperature():
    fake = FakeOpenRouter(streams=[sse(delta(content="ok"), usage(10, 1))])
    svc = service(fake, CHAT_PLAN="false")
    list(svc.run_turn("q", [], "hard"))
    body = fake.requests[0]
    assert body["reasoning"] == {"enabled": True} and "temperature" not in body
    # vendor/big has no tool support, so none are offered.
    assert "tools" not in body


def test_turn_cost_sums_the_planner_and_every_answer_round():
    fake = FakeOpenRouter(
        plan='{"search": true, "queries": ["loans"]}', plan_cost=0.0005,
        streams=[
            sse(delta(tool_calls=[{"index": 0, "id": "call_1",
                                   "function": {"name": "search_documents", "arguments": '{"query": "rates"}'}}]),
                usage(300, 10, cost=0.002)),
            sse(delta(content="Answer."), usage(500, 2, cost=0.003)),
        ])
    svc = service(fake, results={"loans": [make_hit("a", ["Loans"])], "rates": [make_hit("b", ["Rates"])]})
    result = list(svc.run_turn("tell me about loans", [], "everyday"))[-1]["result"]
    assert result.cost == 0.0055
    assert "cost" not in result.stats  # stats are stored with the message; cost is live-only


def test_no_cost_reported_means_no_cost():
    fake = FakeOpenRouter(streams=[sse(delta(content="ok"), usage(10, 1))])
    result = list(service(fake).run_turn("q", [], "everyday"))[-1]["result"]
    assert result.cost is None

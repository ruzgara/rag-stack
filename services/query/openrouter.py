"""
OpenRouter as the chat model backend (CHAT_PROVIDER=openrouter).

chat.py is written against Ollama's native API: request messages, streamed
chunks and /api/show metadata are all in Ollama's shapes. This module speaks
OpenRouter's OpenAI-style API and translates both ways, so chat.py's pipeline
runs unchanged on either backend:

  messages   Ollama's assistant tool_calls (arguments as an object) and tool
             replies become OpenAI's (arguments as a JSON string, replies
             linked by tool_call_id).
  streaming  SSE deltas become Ollama NDJSON-style chunks: delta.content ->
             message.content, delta.reasoning -> message.thinking. Tool calls
             arrive in fragments, so they're assembled and handed over whole
             in the final (done) chunk, with usage as prompt_eval_count /
             eval_count.
  cost       OpenRouter reports what each call cost (usage.cost, in credits,
             i.e. US dollars); it rides along as `cost` on the done chunk and
             the non-streamed reply, which Ollama never sets.
  routing    every chat request uses floor routing (the ":floor" suffix):
             the cheapest provider serving the model first, flex-tier
             endpoints included, instead of OpenRouter's load balancing.
  metadata   the /models list stands in for /api/show: "tools" in
             capabilities, and the context length as model_info.

Only chat goes here. Embeddings, the VLM fallback and the reranker stay local:
the vectors in Qdrant belong to the embedding model that made them.

Reasoning that a model returns in a tool round (reasoning_details) is passed
back with that round's assistant message. Some providers (Anthropic, with
thinking on) refuse a tool result whose preceding reasoning was dropped.
"""

from __future__ import annotations

import json
from typing import Iterator

import httpx

DEFAULT_URL = "https://openrouter.ai/api/v1"
TIMEOUT = httpx.Timeout(300, connect=10)
ROUTING_SUFFIXES = ("floor", "nitro")


class OpenRouterError(Exception):
    pass


def floor(model: str) -> str:
    """The model id with floor routing. An id that already names a variant
    (":free", ":nitro", ":floor", ...) is left as it is; those are routing
    choices of their own and don't combine."""
    return model if ":" in model.rsplit("/", 1)[-1] else f"{model}:floor"


def to_openai_messages(messages: list[dict]) -> list[dict]:
    """Ollama-shaped chat messages as OpenAI-shaped ones."""
    out = []
    for m in messages:
        if m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m.get("tool_call_id") or "", "content": m["content"]})
        elif m["role"] == "assistant" and m.get("tool_calls"):
            calls = []
            for call in m["tool_calls"]:
                fn = call.get("function") or {}
                args = fn.get("arguments")
                calls.append({"id": call.get("id") or "", "type": "function",
                              "function": {"name": fn.get("name") or "",
                                           "arguments": args if isinstance(args, str) else json.dumps(args or {})}})
            msg = {"role": "assistant", "content": m.get("content") or None, "tool_calls": calls}
            if m.get("reasoning_details"):
                msg["reasoning_details"] = m["reasoning_details"]
            out.append(msg)
        else:
            out.append({"role": m["role"], "content": m["content"]})
    return out


def _merge_reasoning(merged: list[dict], fragments: list[dict]) -> None:
    """Accumulates streamed reasoning_details. A block arrives as several
    fragments sharing an index; their text-like fields concatenate, and
    anything else (signature, id, format) is taken as it arrives."""
    for frag in fragments:
        index = frag.get("index", len(merged))
        block = next((b for b in merged if b.get("index") == index), None)
        if block is None:
            merged.append(dict(frag))
            continue
        for key, value in frag.items():
            if key in ("text", "summary", "data") and isinstance(value, str) and isinstance(block.get(key), str):
                block[key] += value
            elif value is not None:
                block[key] = value


def stream_to_chunks(lines: Iterator[str]) -> Iterator[dict]:
    """OpenRouter's SSE lines as Ollama-style streamed chat chunks."""
    calls: dict[int, dict] = {}
    reasoning: list[dict] = []
    usage: dict = {}
    for line in lines:
        # Blank lines separate events; ": OPENROUTER PROCESSING" is a keep-alive.
        if not line or line.startswith(":") or not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            tool_calls = []
            for index in sorted(calls):
                call = calls[index]
                try:
                    args = json.loads(call["arguments"]) if call["arguments"].strip() else {}
                except json.JSONDecodeError:
                    args = call["arguments"]  # chat.py treats unreadable arguments as none
                tool_calls.append({"id": call["id"] or f"call_{index}",
                                   "function": {"name": call["name"], "arguments": args}})
            message = {"role": "assistant", "content": ""}
            if tool_calls:
                message["tool_calls"] = tool_calls
            if reasoning:
                message["reasoning_details"] = reasoning
            done = {"message": message, "done": True, "prompt_eval_count": usage.get("prompt_tokens", 0),
                    "eval_count": usage.get("completion_tokens", 0)}
            if usage.get("cost") is not None:
                done["cost"] = usage["cost"]
            yield done
            return
        chunk = json.loads(data)
        if chunk.get("error"):
            err = chunk["error"]
            raise OpenRouterError(err.get("message") if isinstance(err, dict) else str(err))
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            for tc in delta.get("tool_calls") or []:
                call = calls.setdefault(tc.get("index", len(calls)), {"id": "", "name": "", "arguments": ""})
                fn = tc.get("function") or {}
                call["id"] = tc.get("id") or call["id"]
                call["name"] += fn.get("name") or ""
                call["arguments"] += fn.get("arguments") or ""
            if delta.get("reasoning_details"):
                _merge_reasoning(reasoning, delta["reasoning_details"])
            message = {"role": "assistant"}
            if delta.get("content"):
                message["content"] = delta["content"]
            if delta.get("reasoning"):
                message["thinking"] = delta["reasoning"]
            if len(message) > 1:
                yield {"message": message, "done": False}
    # Without [DONE] the connection dropped mid-answer; storing what arrived
    # as if it were the whole answer would be worse than failing the turn.
    raise OpenRouterError("stream ended before the answer was complete")


class OpenRouter:
    def __init__(self, client: httpx.Client, api_key: str, url: str = DEFAULT_URL):
        self.client = client
        self.url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._models: dict[str, dict] | None = None

    def model(self, model: str) -> dict | None:
        """The model's entry in OpenRouter's /models list, which is fetched
        once and cached. Raises httpx.HTTPError when the list can't be
        fetched, so a transient failure isn't cached; an unknown model is
        None."""
        if self._models is None:
            resp = self.client.get(f"{self.url}/models", headers=self._headers, timeout=30)
            resp.raise_for_status()
            self._models = {m["id"]: m for m in resp.json().get("data") or []}
        # Routing shortcuts aren't listed as models of their own (":free" is).
        base, _, variant = model.rpartition(":")
        if model not in self._models and base and variant in ROUTING_SUFFIXES:
            return self._models.get(base)
        return self._models.get(model)

    def show(self, model: str) -> dict | None:
        """The model's metadata in the shape of Ollama's /api/show, as much
        of it as chat.py reads."""
        info = self.model(model)
        if info is None:
            return None
        params = info.get("supported_parameters") or []
        context = (info.get("top_provider") or {}).get("context_length") or info.get("context_length")
        return {"capabilities": ["completion"] + (["tools"] if "tools" in params else []),
                "model_info": {"openrouter.context_length": context} if context else {}}

    def _body(self, model: str, messages: list[dict], think: bool, temperature: float) -> dict:
        body: dict = {"model": floor(model), "messages": to_openai_messages(messages)}
        if think:
            body["reasoning"] = {"enabled": True}
        else:
            # Providers that support extended reasoning, such as Anthropic, reject
            # temperature when reasoning is on, so it's only sent with reasoning off.
            body["temperature"] = temperature
        return body

    def chat(self, model: str, messages: list[dict], *, think: bool, temperature: float,
             fmt: dict | None = None) -> dict:
        body = self._body(model, messages, think, temperature)
        if fmt:
            try:
                params = (self.model(model) or {}).get("supported_parameters") or []
            except httpx.HTTPError:
                params = []
            # A JSON schema where the model enforces one, plain JSON mode where
            # that's all it has; otherwise the prompt's own instructions have to do.
            # Strict mode (OpenAI's) also requires additionalProperties: false
            # on the object; chat.py's schemas already mark every field required.
            if "structured_outputs" in params:
                schema = {**fmt, "additionalProperties": False}
                body["response_format"] = {"type": "json_schema",
                                           "json_schema": {"name": "reply", "strict": True, "schema": schema}}
            elif "response_format" in params:
                body["response_format"] = {"type": "json_object"}
        resp = self.client.post(f"{self.url}/chat/completions", json=body, headers=self._headers, timeout=TIMEOUT)
        if resp.is_error:
            raise OpenRouterError(f"openrouter chat failed ({resp.status_code}): {resp.text[:500]}")
        data = resp.json()
        if data.get("error"):
            err = data["error"]
            raise OpenRouterError(err.get("message") if isinstance(err, dict) else str(err))
        message = (data.get("choices") or [{}])[0].get("message") or {}
        reply = {"message": {"role": "assistant", "content": message.get("content") or ""}}
        cost = (data.get("usage") or {}).get("cost")
        if cost is not None:
            reply["cost"] = cost
        return reply

    def chat_stream(self, model: str, messages: list[dict], *, think: bool, temperature: float,
                    tools: list | None) -> Iterator[dict]:
        body = self._body(model, messages, think, temperature)
        body["stream"] = True
        if tools:
            body["tools"] = tools
        with self.client.stream("POST", f"{self.url}/chat/completions", json=body, headers=self._headers,
                                timeout=TIMEOUT) as resp:
            if resp.is_error:
                resp.read()
                raise OpenRouterError(f"openrouter chat failed ({resp.status_code}): {resp.text[:500]}")
            yield from stream_to_chunks(resp.iter_lines())

    def check(self) -> None:
        """Raises httpx.HTTPError unless OpenRouter accepts the API key."""
        self.client.get(f"{self.url}/key", headers=self._headers, timeout=5).raise_for_status()

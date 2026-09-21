"""
OpenAI-compatible endpoints, so off-the-shelf chat clients and the openai SDK
can use rag-stack: point them at http://127.0.0.1:$QUERY_PORT/v1.

  GET  /v1/models            the presets, as model ids "everyday" and "hard"
  POST /v1/chat/completions  the same pipeline as the native API, streamed or not

The OpenAI protocol is stateless -- clients send the whole conversation each
time -- so nothing here is stored. It has no field for citations either, so
the cited sources are appended to the answer as a "Sources" list.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Iterator

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from accounts import User
from chat import ChatError, TurnResult
from limits import Busy

router = APIRouter(prefix="/v1")


def _current_user(request: Request) -> User:
    """The same identity check as the native API: a session cookie, a proxy
    header, a personal API token, or the QUERY_API_KEY service account. Deferred imports keep api.py's import of
    this module from turning circular."""
    from api import current_user

    return current_user(request)


# Options with no meaning here; a client sending them would silently get
# something other than what it asked for, so they're rejected.
UNSUPPORTED = ("tools", "functions", "tool_choice", "function_call", "response_format", "logprobs", "audio")


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[dict[str, Any]]
    stream: bool = False
    n: int | None = None

    model_config = {"extra": "allow"}  # temperature, max_tokens, user... are accepted and ignored


def error(status: int, message: str, code: str, param: str | None = None) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": "invalid_request_error", "param": param, "code": code}},
                        status_code=status)


def text_of(content: Any) -> str:
    """Message content is a string, or a list of parts (text, image...)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def sources_footer(result: TurnResult) -> str:
    cited = [s for s in result.sources if s.number in result.cited]
    if not cited:
        return ""
    return "\n\nSources:\n" + "\n".join(f"[{s.number}] {s.label()}" for s in cited)


@router.get("/models")
def models(user: "User" = Depends(_current_user)) -> dict:
    from api import state

    now = int(time.time())
    return {"object": "list", "data": [
        {"id": name, "object": "model", "created": now, "owned_by": f"rag-stack ({p.model})"}
        for name, p in state.chat.cfg.presets.items()]}


@router.post("/chat/completions")
def chat_completions(body: ChatCompletionRequest, user: "User" = Depends(_current_user)):
    from api import state

    extras = body.model_extra or {}
    for key in UNSUPPORTED:
        if extras.get(key):
            return error(400, f"'{key}' is not supported: this endpoint runs its own document search tools",
                         "unsupported_parameter", key)
    if body.n not in (None, 1):
        return error(400, "only n=1 is supported", "unsupported_parameter", "n")
    preset = body.model.strip().lower()
    if preset not in state.chat.cfg.presets:
        return error(404, f"model '{body.model}' not found; available: {', '.join(state.chat.cfg.presets)}",
                     "model_not_found", "model")

    system_parts, history = [], []
    for m in body.messages:
        role, content = m.get("role"), text_of(m.get("content"))
        if role in ("system", "developer"):
            system_parts.append(content)
        elif role in ("user", "assistant") and content:
            history.append({"role": role, "content": content})
    if not history or history[-1]["role"] != "user":
        return error(400, "the last message must be from the user", "invalid_messages", "messages")
    question = history.pop()["content"]
    # A client's own system prompt is added to ours, not substituted for it.
    extra_system = "\n\n".join(p for p in system_parts if p.strip()) or None

    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    # Stateless here -- there's no conversation to attach libraries to -- so
    # every library the caller can read applies, same as a fresh /api chat.
    doc_shas = state.accounts.document_scope(user.id)
    library_overview = state.accounts.library_overview(user.id)
    turn = state.chat.run_turn(question, history, preset, extra_system=extra_system, doc_shas=doc_shas,
                               library_overview=library_overview)

    def usage(r: TurnResult) -> dict:
        p, c = r.stats.get("prompt_tokens", 0), r.stats.get("completion_tokens", 0)
        return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}

    if not body.stream:
        try:
            # Same queue as /api/conversations: this endpoint is another way in
            # to the one GPU, not a way around its limits.
            with state.gate.admit(user.id):
                result = next(ev["result"] for ev in turn if ev["type"] == "done")
        except Busy as exc:
            return error(503, str(exc), "server_busy")
        except (ChatError, httpx.HTTPError, RuntimeError) as exc:
            return error(502, str(exc), "upstream_error")
        return {
            "id": completion_id, "object": "chat.completion", "created": created, "model": preset,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": result.answer + sources_footer(result)}}],
            "usage": usage(result),
        }

    def chunk(delta: dict, finish: str | None = None, **extra) -> str:
        payload = {"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": preset,
                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra}
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def stream() -> Iterator[str]:
        yield chunk({"role": "assistant", "content": ""})
        try:
            with state.gate.admit(user.id):
                for ev in turn:
                    if ev["type"] == "token":
                        yield chunk({"content": ev["text"]})
                    elif ev["type"] == "done":
                        result = ev["result"]
                        footer = sources_footer(result)
                        if footer:
                            yield chunk({"content": footer})
                        yield chunk({}, "stop", usage=usage(result))
        except Busy as exc:
            yield f"data: {json.dumps({'error': {'message': str(exc), 'type': 'server_busy'}})}\n\n"
        except (ChatError, httpx.HTTPError, RuntimeError) as exc:
            yield f"data: {json.dumps({'error': {'message': str(exc), 'type': 'upstream_error'}})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

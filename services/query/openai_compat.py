"""
OpenAI-compatible endpoints, so off-the-shelf chat clients and the openai SDK
can use rag-stack: point them at http://127.0.0.1:$QUERY_PORT/v1.

  GET  /v1/models            the presets, as model ids "everyday" and "hard"
  POST /v1/chat/completions  the same pipeline as the native API, streamed or not

The OpenAI protocol is stateless -- clients send the whole conversation each
time -- so nothing here is stored, and neither is any citation numbering:
every request starts a fresh CitationNumbers and numbers whatever it cites
starting from [1:1], even if an identical question was just answered the
same way. There's no conversation to carry a number forward *to*, so
cross-turn citation (a later message citing an earlier one's passage by
number) doesn't exist on this endpoint -- only on the native /api one.

The protocol also has no field for citations, so the cited sources are
appended to the answer as a "Sources" list, and the handles chat.py's answer
carries are rendered to reader-facing [doc:chunk] before anything leaves
this module -- render_answer for the non-streamed reply, StreamingRenderer
token by token for the streamed one. A raw handle must never reach a client
here: unlike the native UI, this protocol was never taught what one is.
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
from citations import CitationNumbers, StreamingRenderer, render_answer
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
    """result.refs is already exactly the [doc:chunk] chat.py assigned each
    cited handle (a fresh CitationNumbers, since this endpoint never passes
    one in) -- reused here rather than renumbered, so the footer can't
    disagree with what render_answer put in the body above it."""
    by_handle = {s.handle: s for s in result.sources}
    cited = sorted(((ref, by_handle[handle]) for handle, ref in result.refs.items() if handle in by_handle))
    if not cited:
        return ""
    return "\n\nSources:\n" + "\n".join(f"[{doc}:{chunk}] {s.label()}" for (doc, chunk), s in cited)


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
        content = render_answer(result.answer, result.refs.get) + sources_footer(result)
        return {
            "id": completion_id, "object": "chat.completion", "created": created, "model": preset,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": usage(result),
        }

    def chunk(delta: dict, finish: str | None = None, **extra) -> str:
        payload = {"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": preset,
                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra}
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def stream() -> Iterator[str]:
        yield chunk({"role": "assistant", "content": ""})
        # sha_of is filled as `sources` events arrive and handed to the
        # renderer by reference (StreamingRenderer keeps the same dict, not
        # a copy) -- by the time a handle can appear in a token, the
        # `sources` event that introduced it has already come through.
        sha_of: dict[str, str] = {}
        renderer = StreamingRenderer(sha_of)
        try:
            with state.gate.admit(user.id):
                for ev in turn:
                    if ev["type"] == "sources":
                        sha_of.update({s["handle"]: s["doc_sha"] for s in ev["sources"]})
                    elif ev["type"] == "token":
                        out = renderer.feed(ev["text"])
                        if out:
                            yield chunk({"content": out})
                    elif ev["type"] == "done":
                        result = ev["result"]
                        # Flush before the footer: any '[' the renderer was
                        # still holding back at the last token has to be
                        # resolved (or released as plain text) before
                        # anything else is appended.
                        tail = renderer.flush()
                        content = tail + sources_footer(result)
                        if content:
                            yield chunk({"content": content})
                        yield chunk({}, "stop", usage=usage(result))
        except Busy as exc:
            yield f"data: {json.dumps({'error': {'message': str(exc), 'type': 'server_busy'}})}\n\n"
        except (ChatError, httpx.HTTPError, RuntimeError) as exc:
            yield f"data: {json.dumps({'error': {'message': str(exc), 'type': 'upstream_error'}})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

"""
The chat pipeline behind both APIs (native and OpenAI-compatible).

One turn:
  1. Rewrite: the chat model decides whether the message needs a search at
     all ("hello", "thanks", "make that shorter" don't) and, with earlier
     history, turns it into a standalone search query ("and for Level 1?" ->
     "RSF factor for Level 1 assets").
  2. Auto-search: retrieve() returns the passages the reranker scores
     relevant (RAG_RERANK_MIN_SCORE), whole sections where several chunks of
     one section match; they're taken best first until
     CHAT_AUTO_SOURCE_TOKENS is used, and numbered [1]..[n]. How many that is
     depends on the question -- one passage for a narrow one, many for a broad
     one -- rather than a fixed count (CHAT_INITIAL_SOURCES only caps it).
  3. Answer: the chat model streams its answer from Ollama. If the preset has
     tools on, it may call search_documents for more passages, or
     list_documents to see what the knowledge base holds -- at most
     CHAT_MAX_TOOL_ROUNDS times, within CHAT_MAX_SOURCE_TOKENS. Passages
     already given are never returned again, and numbering continues, so
     earlier citations stay valid.
  4. Citations: [n] references in the answer are resolved to their sources.

run_turn() yields events as they happen; the caller streams and stores them.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator

import httpx

from library import Library
from retrieval import Hit, Retriever, _bool, _float, _int, estimate_tokens, load_env

PROMPTS = Path(__file__).parent / "prompts"
PRESETS = ("everyday", "hard")

SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search_documents",
        "description": (
            "Search the document library for more passages. Returns passages not already provided, "
            "numbered for citation. Repeating an earlier query returns further passages for it."
        ),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "What to search for, as a focused query"}},
            "required": ["query"],
        },
    },
}

DOCUMENTS_TOOL = {
    "type": "function",
    "function": {
        "name": "list_documents",
        "description": (
            "List the documents in the knowledge base: filenames, page counts and main sections. "
            "Use it when the user asks which documents or sources are available, whether a particular "
            "document is there, or what one of them covers -- searching can't answer those. "
            "Returns no passage text, so nothing it returns can be cited."
        ),
        "parameters": {
            "type": "object",
            "properties": {"document": {"type": "string",
                                        "description": "Optional: part of a filename, for that document's sections in full"}},
        },
    },
}

LIBRARIES_TOOL = {
    "type": "function",
    "function": {
        "name": "list_libraries",
        "description": (
            "List the libraries (document collections) the user has access to, with their document "
            "counts and whether each is attached to this conversation. Use it when the user asks what "
            "libraries or collections exist, or why a search or list_documents didn't cover something "
            "that might be in a library they haven't attached here."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

# [2], [1, 3], [1; 3], and ranges [1-4] / [1–4]
_CITE_ITEM = r"\d+(?:\s*[-–]\s*\d+)?"
CITATION_RE = re.compile(rf"\[({_CITE_ITEM}(?:\s*[,;]\s*{_CITE_ITEM})*)\]")


class ChatError(Exception):
    pass


@dataclass(frozen=True)
class Preset:
    name: str
    model: str
    tools: bool
    think: bool


@dataclass(frozen=True)
class ChatConfig:
    ollama_url: str
    presets: dict[str, Preset]
    default_preset: str
    temperature: float
    history_max_tokens: int
    rewrite: bool
    initial_sources: int
    auto_source_tokens: int
    tool_results: int
    max_tool_rounds: int
    max_source_tokens: int
    ollama_context_length: int | None

    @classmethod
    def from_env(cls, env: dict | None = None) -> "ChatConfig":
        env = load_env() if env is None else env
        presets = {}
        for name in PRESETS:
            model = env.get(f"CHAT_MODEL_{name.upper()}")
            if not model:
                raise ValueError(f"CHAT_MODEL_{name.upper()} is not set in .env")
            presets[name] = Preset(
                name=name,
                model=model,
                tools=_bool(env, f"CHAT_TOOLS_{name.upper()}", "true"),
                think=_bool(env, f"CHAT_THINK_{name.upper()}", "false"),
            )
        default = (env.get("CHAT_DEFAULT_PRESET") or "everyday").strip().lower()
        if default not in presets:
            raise ValueError(f"CHAT_DEFAULT_PRESET must be one of {', '.join(PRESETS)}, got {default!r}")
        rounds = int(env.get("CHAT_MAX_TOOL_ROUNDS") or 2)
        if rounds < 0:
            raise ValueError(f"CHAT_MAX_TOOL_ROUNDS must be >= 0, got {rounds}")
        return cls(
            ollama_url=f"http://127.0.0.1:{env.get('OLLAMA_PORT', '11434')}",
            presets=presets,
            default_preset=default,
            temperature=_float(env, "CHAT_TEMPERATURE", "0.3"),
            history_max_tokens=_int(env, "CHAT_HISTORY_MAX_TOKENS", "8000"),
            rewrite=_bool(env, "CHAT_REWRITE", "true"),
            initial_sources=_int(env, "CHAT_INITIAL_SOURCES", "10"),
            auto_source_tokens=_int(env, "CHAT_AUTO_SOURCE_TOKENS", "3500"),
            tool_results=_int(env, "CHAT_TOOL_RESULTS", "6"),
            max_tool_rounds=rounds,
            max_source_tokens=_int(env, "CHAT_MAX_SOURCE_TOKENS", "6000"),
            # Ollama truncates every model to this regardless of what the
            # model itself supports (unset means Ollama's own built-in
            # default, which we don't try to guess). context_length() uses
            # it to cap the reported limit -- otherwise a model advertising
            # a bigger window than Ollama is actually configured to serve
            # would make "context used" read far too low.
            ollama_context_length=int(env["OLLAMA_CONTEXT_LENGTH"]) if env.get("OLLAMA_CONTEXT_LENGTH") else None,
        )

    def preset(self, name: str | None) -> Preset:
        name = (name or self.default_preset).strip().lower()
        if name not in self.presets:
            raise ChatError(f"unknown preset {name!r}; available: {', '.join(self.presets)}")
        return self.presets[name]


@dataclass
class Source:
    number: int
    found_by: str  # auto | tool
    query: str
    point_id: str
    doc_sha: str
    filename: str
    chunk_index: int
    pages: list[int]
    headings: list[str]
    text: str
    rerank_score: float | None

    @classmethod
    def from_hit(cls, number: int, hit: Hit, found_by: str, query: str) -> "Source":
        return cls(number=number, found_by=found_by, query=query, point_id=hit.point_id, doc_sha=hit.doc_sha,
                   filename=hit.filename, chunk_index=hit.chunk_index, pages=hit.pages, headings=hit.headings,
                   text=hit.text, rerank_score=hit.rerank_score)

    def label(self) -> str:
        pages = ""
        if self.pages:
            first, last = self.pages[0], self.pages[-1]
            pages = f", p. {first}" if first == last else f", pp. {first}-{last}"
        return f"{self.filename}{pages}"

    def for_prompt(self) -> str:
        section = f" — {' > '.join(self.headings)}" if self.headings else ""
        return f"[{self.number}] {self.label()}{section}\n{self.text.strip()}"


@dataclass
class TurnResult:
    answer: str = ""
    model: str = ""
    preset: str = ""
    search_queries: list[str] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    cited: list[int] = field(default_factory=list)
    invalid_citations: list[int] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def resolve_citations(answer: str, sources: list[Source]) -> tuple[list[int], list[int]]:
    """Numbers the answer cites, split into ones that exist and ones that don't."""
    known = {s.number for s in sources}
    seen: list[int] = []
    for m in CITATION_RE.finditer(answer):
        for item in re.split(r"\s*[,;]\s*", m.group(1)):
            bounds = [int(x) for x in re.split(r"\s*[-–]\s*", item)]
            lo, hi = bounds[0], bounds[-1]
            for n in range(lo, min(hi, lo + 50) + 1):  # cap absurd ranges
                if n not in seen:
                    seen.append(n)
    return [n for n in seen if n in known], [n for n in seen if n not in known]


def trim_history(history: list[dict], max_tokens: int) -> list[dict]:
    """Most recent turns that fit the budget, never starting on an answer."""
    kept, used = [], 0
    for msg in reversed(history):
        cost = estimate_tokens(msg["content"])
        if used + cost > max_tokens:
            break
        kept.append(msg)
        used += cost
    kept.reverse()
    while kept and kept[0]["role"] != "user":
        kept.pop(0)
    return kept


class ChatService:
    def __init__(self, retriever: Retriever, cfg: ChatConfig | None = None, client: httpx.Client | None = None,
                 library: Library | None = None):
        self.cfg = cfg or ChatConfig.from_env()
        self.retriever = retriever
        self.library = library or Library(retriever)
        self.client = client or httpx.Client()
        self._model_info: dict[str, dict] = {}

    # --------------------------------------------------------------- ollama --

    def _chat(self, model: str, messages: list[dict], *, think: bool = False, fmt: dict | None = None,
              temperature: float | None = None) -> dict:
        body = {"model": model, "messages": messages, "stream": False, "think": think, "keep_alive": "30m",
                "options": {"temperature": self.cfg.temperature if temperature is None else temperature}}
        if fmt:
            body["format"] = fmt
        resp = self.client.post(f"{self.cfg.ollama_url}/api/chat", json=body, timeout=httpx.Timeout(300, connect=10))
        if resp.is_error:
            raise ChatError(f"ollama chat failed ({resp.status_code}): {resp.text[:500]}")
        return resp.json()

    def _chat_stream(self, model: str, messages: list[dict], *, think: bool, tools: list | None) -> Iterator[dict]:
        body = {"model": model, "messages": messages, "stream": True, "think": think, "keep_alive": "30m",
                "options": {"temperature": self.cfg.temperature}}
        if tools:
            body["tools"] = tools
        with self.client.stream("POST", f"{self.cfg.ollama_url}/api/chat", json=body,
                                timeout=httpx.Timeout(300, connect=10)) as resp:
            if resp.is_error:
                resp.read()
                raise ChatError(f"ollama chat failed ({resp.status_code}): {resp.text[:500]}")
            for line in resp.iter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                if chunk.get("error"):
                    raise ChatError(f"ollama: {chunk['error']}")
                yield chunk

    def _show(self, model: str) -> dict | None:
        """Ollama's model metadata (capabilities, context length, ...),
        cached per model. Returns None on a transient failure without
        caching it, so the next call tries again."""
        if model not in self._model_info:
            try:
                resp = self.client.post(f"{self.cfg.ollama_url}/api/show", json={"model": model}, timeout=30)
                resp.raise_for_status()
                self._model_info[model] = resp.json()
            except httpx.HTTPError:
                return None
        return self._model_info[model]

    def supports_tools(self, model: str) -> bool:
        """Ollama reports each model's capabilities; a model without tool
        support gets the auto-search only instead of a failed request."""
        info = self._show(model)
        return "tools" in (info.get("capabilities") or []) if info else False

    def context_length(self, model: str) -> int | None:
        """The context window this model actually gets to use: its own
        maximum, from Ollama's model_info (e.g. `qwen3vl.context_length`),
        capped by OLLAMA_CONTEXT_LENGTH if that's set lower -- Ollama itself
        truncates to it, so reporting the model's uncapped max would read
        "context used" far too low once the model exceeds what Ollama is
        actually configured to serve. None if the model's max can't be
        determined."""
        info = self._show(model)
        if not info:
            return None
        for key, value in (info.get("model_info") or {}).items():
            if key.endswith(".context_length"):
                if self.cfg.ollama_context_length is not None:
                    return min(value, self.cfg.ollama_context_length)
                return value
        return None

    # ------------------------------------------------------------ pipeline --

    def rewrite_query(self, preset: Preset, history: list[dict], question: str) -> tuple[bool, str]:
        """Whether the message needs a document search, and the standalone
        search query. Falls back to searching the message as-is."""
        recent = history[-6:]
        transcript = ("\n".join(f"{m['role'].upper()}: {m['content'][:1500]}" for m in recent)
                      or "(none — this is the first message)")
        prompt = (
            "You prepare the user's latest message for a document search.\n\n"
            "1. search: false only if the message can be answered from the conversation alone without new "
            "information from the documents — greetings, thanks, small talk, or requests to reformat, shorten, "
            "translate or explain the previous answer. Otherwise true. When unsure, true.\n"
            "2. query: the latest message rewritten as a standalone search query. Resolve references to earlier "
            "messages (\"it\", \"that\", \"and for X?\") using the conversation. Keep names, numbers and "
            "technical terms exactly. Keep the language of the latest message. If it is already standalone, "
            "return it unchanged.\n\n"
            f"Conversation:\n{transcript}\n\nLatest message: {question}"
        )
        schema = {"type": "object", "properties": {"search": {"type": "boolean"}, "query": {"type": "string"}},
                  "required": ["search", "query"]}
        try:
            resp = self._chat(preset.model, [{"role": "user", "content": prompt}], temperature=0, fmt=schema)
            out = json.loads(resp["message"]["content"])
            search, query = out.get("search") is not False, str(out.get("query") or "").strip()
        except (ChatError, httpx.HTTPError, json.JSONDecodeError, KeyError, AttributeError):
            search, query = True, ""
        return search, query or question

    def _search(self, query: str, limit: int, seen: set[str], sources: list[Source], found_by: str,
                budget: list[int], max_tokens: int | None = None, doc_shas: set[str] | None = None) -> list[Source]:
        """New sources for a query, best first, numbered after the existing
        ones: every relevant result that fits this search's token allowance
        (max_tokens) and the turn's remaining budget (budget[0], mutated).
        A result too big for what's left is skipped in favour of smaller,
        less relevant ones. `limit` only caps the count."""
        new = []
        allowance = min(budget[0], max_tokens) if max_tokens else budget[0]
        for hit in self.retriever.retrieve(query, limit=limit, exclude=seen, doc_shas=doc_shas):
            src = Source.from_hit(len(sources) + 1, hit, found_by, query)
            cost = estimate_tokens(src.for_prompt())
            if cost > allowance and (new or sources):  # always allow at least one source
                continue
            allowance -= cost
            budget[0] -= cost
            seen.update(hit.ids)
            sources.append(src)
            new.append(src)
        return new

    def run_turn(self, question: str, history: list[dict], preset_name: str | None = None,
                 extra_system: str | None = None, doc_shas: set[str] | None = None,
                 library_overview: list[dict] | None = None) -> Iterator[dict]:
        """Yields events: rewrite, searching, sources, listing, thinking, token, done.
        The done event carries the TurnResult. `doc_shas` restricts every
        search and the document listing to a library scope (None: every
        indexed document; empty: none) -- see Retriever.search. `library_overview`
        is Accounts.library_overview()'s output, offered back to the model
        verbatim through list_libraries -- already filtered to libraries the
        caller can read, so the model never learns about ones it can't."""
        preset = self.cfg.preset(preset_name)
        result = TurnResult(model=preset.model, preset=preset.name)
        t_start = time.monotonic()
        history = trim_history(history, self.cfg.history_max_tokens)

        # 1. Decide whether the message needs a search at all, and rewrite a
        #    follow-up into a standalone search query.
        query, do_search = question, True
        if self.cfg.rewrite:
            t0 = time.monotonic()
            do_search, query = self.rewrite_query(preset, history, question)
            result.stats["rewrite_s"] = round(time.monotonic() - t0, 2)
            yield {"type": "rewrite", "query": query, "search": do_search}

        # 2. Auto-search.
        seen: set[str] = set()
        budget = [self.cfg.max_source_tokens]
        if do_search:
            yield {"type": "searching", "query": query, "found_by": "auto"}
            t0 = time.monotonic()
            new = self._search(query, self.cfg.initial_sources, seen, result.sources, "auto", budget,
                               self.cfg.auto_source_tokens, doc_shas)
            result.stats["search_s"] = round(time.monotonic() - t0, 2)
            result.search_queries.append(query)
            yield {"type": "sources", "sources": [asdict(s) for s in new]}
        result.stats["auto_search"] = do_search

        # 3. Answer, with optional tool rounds.
        system = (PROMPTS / "system.md").read_text(encoding="utf-8")
        tools_on = preset.tools and self.cfg.max_tool_rounds > 0 and self.supports_tools(preset.model)
        if tools_on:
            system += "\n" + (PROMPTS / "tools.md").read_text(encoding="utf-8")
        if extra_system:
            system += f"\n\nAdditional instructions:\n{extra_system}"
        if not do_search:
            context = ("Sources: none — no document search was run, because this message doesn't need the "
                       "documents. Reply to it directly and briefly; don't add citations, and don't mention "
                       "sources or documents unless asked.")
        elif result.sources:
            context = "Sources:\n\n" + "\n\n".join(s.for_prompt() for s in result.sources)
        else:
            context = ("Sources: none — the document search found no relevant passages for this question. "
                       "Tell the user briefly that the documents don't cover it; don't answer from general "
                       "knowledge unless you say so, and don't suggest searches to the user.")
        user_turn = f"{context}\n\n---\n\nQuestion: {question}"
        if tools_on and not do_search:
            user_turn += ("\n\n(If answering this does need information from the documents after all, "
                          "call search_documents.)")
        elif tools_on:
            # Small models weigh the end of the prompt most; the system
            # prompt's tool guidance alone gets ignored in favour of refusing.
            user_turn += ("\n\n(If the sources above don't cover every part of the question, "
                          "call search_documents for the missing part before answering.)")
        messages = [{"role": "system", "content": system}, *history,
                    # Sources ride with the question rather than in the system
                    # prompt, so the system + history prefix stays identical
                    # between turns and Ollama can reuse its cache for it.
                    {"role": "user", "content": user_turn}]

        available_tools = [SEARCH_TOOL, DOCUMENTS_TOOL] + ([LIBRARIES_TOOL] if library_overview is not None else [])
        rounds = 0
        prompt_tokens = eval_tokens = 0
        round_prompt_tokens = round_eval_tokens = 0
        t_first_token = None
        parts: list[str] = []
        while True:
            offer_tools = tools_on and rounds < self.cfg.max_tool_rounds and budget[0] > 0
            content, tool_calls = "", []
            for chunk in self._chat_stream(preset.model, messages, think=preset.think,
                                           tools=available_tools if offer_tools else None):
                msg = chunk.get("message") or {}
                if msg.get("thinking"):
                    yield {"type": "thinking", "text": msg["thinking"]}
                if msg.get("content"):
                    if t_first_token is None:
                        t_first_token = time.monotonic()
                    content += msg["content"]
                    yield {"type": "token", "text": msg["content"]}
                tool_calls.extend(msg.get("tool_calls") or [])
                if chunk.get("done"):
                    round_prompt_tokens = chunk.get("prompt_eval_count", 0)
                    round_eval_tokens = chunk.get("eval_count", 0)
                    prompt_tokens += round_prompt_tokens
                    eval_tokens += round_eval_tokens
            if content:
                parts.append(content)
            if not (tool_calls and offer_tools):
                break

            rounds += 1
            messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
            for call in tool_calls:
                fn = call.get("function") or {}
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                name = fn.get("name")
                tool_query = str(args.get("query") or "").strip()
                if name == DOCUMENTS_TOOL["function"]["name"]:
                    wanted = str(args.get("document") or "").strip()
                    yield {"type": "listing", "document": wanted}
                    try:
                        reply = self.library.describe(wanted or None, doc_shas)
                    except (RuntimeError, httpx.HTTPError) as exc:
                        reply = f"The document list is unavailable ({exc})."
                    # Not sources, but it shares the prompt with them.
                    budget[0] -= estimate_tokens(reply)
                    result.stats["listed_documents"] = result.stats.get("listed_documents", 0) + 1
                elif name == LIBRARIES_TOOL["function"]["name"]:
                    yield {"type": "listing_libraries"}
                    if library_overview:
                        lines = []
                        for lib in library_overview:
                            note = (f"searchable now ({lib['document_count']} documents)" if lib["in_scope"] else
                                    f"not attached to this conversation ({lib['document_count']} documents)")
                            desc = f" -- {lib['description']}" if lib["description"] else ""
                            lines.append(f"- {lib['name']}: {note}{desc}")
                        reply = "\n".join(lines)
                    else:
                        reply = "No libraries available."
                    budget[0] -= estimate_tokens(reply)
                elif name != SEARCH_TOOL["function"]["name"] or not tool_query:
                    reply = "Error: call search_documents with a non-empty 'query'."
                else:
                    yield {"type": "searching", "query": tool_query, "found_by": "tool"}
                    new = self._search(tool_query, self.cfg.tool_results, seen, result.sources, "tool", budget,
                                       doc_shas=doc_shas)
                    result.search_queries.append(tool_query)
                    yield {"type": "sources", "sources": [asdict(s) for s in new]}
                    reply = ("\n\n".join(s.for_prompt() for s in new) if new
                             else "No further relevant passages found for this query.")
                messages.append({"role": "tool", "tool_name": name or "", "content": reply})

        # 4. Resolve citations.
        result.answer = "\n\n".join(p.strip() for p in parts if p.strip())
        result.cited, result.invalid_citations = resolve_citations(result.answer, result.sources)
        context_limit = self.context_length(preset.model)
        context_used = round_prompt_tokens + round_eval_tokens
        result.stats.update(
            tool_rounds=rounds,
            prompt_tokens=prompt_tokens,
            completion_tokens=eval_tokens,
            first_token_s=round(t_first_token - t_start, 2) if t_first_token else None,
            total_s=round(time.monotonic() - t_start, 2),
            source_tokens=self.cfg.max_source_tokens - budget[0],
            # The final round's own prompt + completion tokens -- how much of
            # the model's context window this turn actually occupied, as
            # opposed to prompt_tokens/completion_tokens above which sum
            # every tool round in the turn.
            context_used=context_used,
            context_limit=context_limit,
            context_used_pct=round(100 * context_used / context_limit, 1) if context_limit else None,
        )
        yield {"type": "done", "result": result}

    def generate_title(self, preset_name: str | None, question: str, answer: str) -> str | None:
        preset = self.cfg.preset(preset_name)
        prompt = ("Write a short title (at most 6 words, no quotes, no final period) for a conversation that "
                  f"starts with this exchange. Use the language of the question.\n\nQuestion: {question}\n\n"
                  f"Answer: {answer[:1500]}")
        try:
            resp = self._chat(preset.model, [{"role": "user", "content": prompt}], temperature=0,
                              fmt={"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]})
            title = json.loads(resp["message"]["content"]).get("title", "").strip().strip('"').rstrip(".")
        except (ChatError, httpx.HTTPError, json.JSONDecodeError, KeyError):
            return None
        return title[:120] or None

"""
The chat pipeline behind both APIs (native and OpenAI-compatible).

One turn:
  1. Plan (prompts/planner.md): the chat model decides whether the message
     needs a search at all ("hello", "thanks", "make that shorter" don't)
     and, with earlier history, writes the standalone search queries ("and
     for Level 1?" -> "RSF factor for Level 1 assets"). Usually one; up to
     CHAT_MAX_QUERIES when the message asks about several distinct things,
     which share the auto-search token budget between them.
  2. Auto-search: retrieve() returns the passages the reranker scores
     relevant (RAG_RERANK_MIN_SCORE), whole sections where several chunks of
     one section match; they're taken best first until
     CHAT_AUTO_SOURCE_TOKENS is used. Each gets a citation handle (see
     citations.py) -- opaque, stable forever, and never shown to the reader.
     How many passages that is depends on the question -- one for a narrow
     one, many for a broad one -- rather than a fixed count
     (CHAT_INITIAL_SOURCES only caps it).
  3. Answer: the chat model streams its answer from Ollama (or OpenRouter,
     CHAT_PROVIDER -- see openrouter.py). If the preset has
     tools on, it may call search_documents for more passages, or
     list_documents to see what the knowledge base holds -- at most
     CHAT_MAX_TOOL_ROUNDS times, within CHAT_MAX_SOURCE_TOKENS. Passages
     already given are never returned again. Passages the conversation cited
     in an earlier turn are offered too, by identifier and location only
     (no text), so the model can cite them again without re-searching.
  4. Citations: the handles in the answer are resolved against this turn's
     sources and the conversation's numbering (citations.py), which also
     assigns each cited passage its reader-facing [doc:chunk].

run_turn() yields events as they happen; the caller streams and stores them.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator, NamedTuple

import httpx

from citations import (CitationNumbers, CitationScanner, assign_numbers, handle_for, parse_citations,
                       resolve)
from library import Library
from openrouter import DEFAULT_URL as OPENROUTER_URL, OpenRouter, OpenRouterError
from retrieval import Hit, Retriever, _bool, _float, _int, estimate_tokens, load_env

log = logging.getLogger(__name__)

PROMPTS = Path(__file__).parent / "prompts"
PRESETS = ("everyday", "hard")
PROVIDERS = ("ollama", "openrouter")

SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search_documents",
        "description": (
            "Search the document library for more passages. Returns passages not already provided, "
            "each with its own citation identifier. Repeating an earlier query returns further passages for it."
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

def prompt_text(name: str, **fields: object) -> str:
    """One of the files in prompts/, ready to drop into a message. Read on
    every use, like the system prompts, so edits take effect without a restart.
    `fields` fill {placeholders}; plain .format() isn't used, so a prompt can
    contain braces of its own (a JSON example, say) without escaping them."""
    text = (PROMPTS / name).read_text(encoding="utf-8").strip()
    for key, value in fields.items():
        text = text.replace("{" + key + "}", str(value))
    return text


class ChatError(Exception):
    pass


class Plan(NamedTuple):
    """plan_search's result. `queries` holds at least one query whenever
    `search` is set. `failed` means the model never answered -- the one query
    is the user's message verbatim, not a considered decision to leave it
    alone, so a follow-up like "what are some challenges" goes to search with
    its subject still missing. `cost` is what the call cost, when the
    provider reports it (OpenRouter)."""
    search: bool
    queries: list[str]
    failed: bool
    cost: float | None = None


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
    plan: bool
    max_queries: int
    initial_sources: int
    auto_source_tokens: int
    tool_results: int
    max_tool_rounds: int
    max_source_tokens: int
    carry_sources: int
    ollama_context_length: int | None
    provider: str = "ollama"
    openrouter_url: str = OPENROUTER_URL
    openrouter_api_key: str = field(default="", repr=False)

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
        max_queries = int(env.get("CHAT_MAX_QUERIES") or 2)
        if max_queries < 1:
            raise ValueError(f"CHAT_MAX_QUERIES must be >= 1, got {max_queries}")
        provider = (env.get("CHAT_PROVIDER") or "ollama").strip().lower()
        if provider not in PROVIDERS:
            raise ValueError(f"CHAT_PROVIDER must be one of {', '.join(PROVIDERS)}, got {provider!r}")
        api_key = (env.get("OPENROUTER_API_KEY") or "").strip()
        if provider == "openrouter" and not api_key:
            raise ValueError("CHAT_PROVIDER=openrouter needs OPENROUTER_API_KEY set in .env")
        return cls(
            ollama_url=f"http://127.0.0.1:{env.get('OLLAMA_PORT', '11434')}",
            presets=presets,
            default_preset=default,
            temperature=_float(env, "CHAT_TEMPERATURE", "0.3"),
            history_max_tokens=_int(env, "CHAT_HISTORY_MAX_TOKENS", "8000"),
            plan=_bool(env, "CHAT_PLAN", "true"),
            max_queries=max_queries,
            initial_sources=_int(env, "CHAT_INITIAL_SOURCES", "10"),
            auto_source_tokens=_int(env, "CHAT_AUTO_SOURCE_TOKENS", "3500"),
            tool_results=_int(env, "CHAT_TOOL_RESULTS", "6"),
            max_tool_rounds=rounds,
            max_source_tokens=_int(env, "CHAT_MAX_SOURCE_TOKENS", "6000"),
            # How many earlier-cited passages the carry-forward block offers
            # by label (identifier and location, no text) -- a cap on the
            # PROMPT, not on what a citation can resolve to: citation_state
            # itself is read uncapped, so a passage evicted from this list
            # can still be cited (and re-attached) if the model remembers
            # its handle from an earlier answer.
            carry_sources=_int(env, "CHAT_CARRY_SOURCES", "40"),
            # Ollama truncates every model to this regardless of what the
            # model itself supports (unset means Ollama's own built-in
            # default, which we don't try to guess). context_length() uses
            # it to cap the reported limit -- otherwise a model advertising
            # a bigger window than Ollama is actually configured to serve
            # would make "context used" read far too low.
            ollama_context_length=int(env["OLLAMA_CONTEXT_LENGTH"]) if env.get("OLLAMA_CONTEXT_LENGTH") else None,
            provider=provider,
            openrouter_url=(env.get("OPENROUTER_URL") or OPENROUTER_URL).strip(),
            openrouter_api_key=api_key,
        )

    def preset(self, name: str | None) -> Preset:
        name = (name or self.default_preset).strip().lower()
        if name not in self.presets:
            raise ChatError(f"unknown preset {name!r}; available: {', '.join(self.presets)}")
        return self.presets[name]


@dataclass
class Source:
    # This message's own retrieval index -- what fixes its row in
    # message_sources (db.py's PRIMARY KEY (message_id, number)). Never
    # shown to the model or the reader; the citation currency is `handle`.
    number: int
    found_by: str  # auto | tool | earlier (carried forward, not retrieved this turn)
    query: str
    point_id: str
    doc_sha: str
    filename: str
    chunk_index: int
    pages: list[int]
    headings: list[str]
    text: str
    rerank_score: float | None
    handle: str = ""
    doc_number: int | None = None
    chunk_number: int | None = None

    @classmethod
    def from_hit(cls, number: int, hit: Hit, found_by: str, query: str) -> "Source":
        return cls(number=number, found_by=found_by, query=query, point_id=hit.point_id, doc_sha=hit.doc_sha,
                   filename=hit.filename, chunk_index=hit.chunk_index, pages=hit.pages, headings=hit.headings,
                   text=hit.text, rerank_score=hit.rerank_score, handle=handle_for(hit.doc_sha, hit.chunk_index))

    @classmethod
    def from_earlier(cls, number: int, row: dict) -> "Source":
        """A passage cited in an earlier turn (db.citation_state), carried
        into this one so the model can cite it again by the same handle
        without re-searching. Keeps its already-assigned [doc:chunk] --
        run_turn never reassigns a live number."""
        return cls(number=number, found_by="earlier", query=row["query"], point_id=row["point_id"],
                   doc_sha=row["doc_sha"], filename=row["filename"], chunk_index=row["chunk_index"],
                   pages=row["pages"], headings=row["headings"], text=row["text"], rerank_score=row["rerank_score"],
                   handle=row["handle"], doc_number=row["doc_number"], chunk_number=row["chunk_number"])

    def label(self) -> str:
        pages = ""
        if self.pages:
            first, last = self.pages[0], self.pages[-1]
            pages = f", p. {first}" if first == last else f", pp. {first}-{last}"
        return f"{self.filename}{pages}"

    def for_prompt(self, *, text: bool = True) -> str:
        """The full `[handle] label\\npassage text` form for the Sources:
        block, or (text=False) just the identifier and location -- what the
        carry-forward block offers for a passage cited in an earlier turn,
        so the model can cite it again without paying for its text twice."""
        section = f" — {' > '.join(self.headings)}" if self.headings else ""
        head = f"[{self.handle}] {self.label()}{section}"
        return head if not text else f"{head}\n{self.text.strip()}"


@dataclass
class TurnResult:
    answer: str = ""
    model: str = ""
    preset: str = ""
    search_queries: list[str] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    cited: list[str] = field(default_factory=list)  # handles, first-citation order
    invalid_citations: list[str] = field(default_factory=list)  # raw text that didn't resolve
    refs: dict[str, tuple[int, int]] = field(default_factory=dict)  # handle -> (doc_number, chunk_number)
    stats: dict = field(default_factory=dict)
    # US dollars, when the provider reports a price (OpenRouter). Kept out of
    # `stats` because stats are stored with the message; this is only shown
    # live, on the done event.
    cost: float | None = None


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
        # Chat models only: embeddings (retrieval.py) always go to Ollama.
        self.openrouter = (OpenRouter(self.client, self.cfg.openrouter_api_key, self.cfg.openrouter_url)
                           if self.cfg.provider == "openrouter" else None)

    # ------------------------------------------------------- chat backends --
    # Everything below speaks Ollama's shapes; with CHAT_PROVIDER=openrouter,
    # openrouter.py translates to and from them.

    def _chat(self, model: str, messages: list[dict], *, think: bool = False, fmt: dict | None = None,
              temperature: float | None = None) -> dict:
        temperature = self.cfg.temperature if temperature is None else temperature
        if self.openrouter:
            try:
                return self.openrouter.chat(model, messages, think=think, temperature=temperature, fmt=fmt)
            except OpenRouterError as exc:
                raise ChatError(str(exc)) from None
        body = {"model": model, "messages": messages, "stream": False, "think": think, "keep_alive": "30m",
                "options": {"temperature": temperature}}
        if fmt:
            body["format"] = fmt
        resp = self.client.post(f"{self.cfg.ollama_url}/api/chat", json=body, timeout=httpx.Timeout(300, connect=10))
        if resp.is_error:
            raise ChatError(f"ollama chat failed ({resp.status_code}): {resp.text[:500]}")
        return resp.json()

    def _chat_stream(self, model: str, messages: list[dict], *, think: bool, tools: list | None) -> Iterator[dict]:
        if self.openrouter:
            try:
                yield from self.openrouter.chat_stream(model, messages, think=think,
                                                       temperature=self.cfg.temperature, tools=tools)
            except OpenRouterError as exc:
                raise ChatError(str(exc)) from None
            return
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
                if self.openrouter:
                    info = self.openrouter.show(model)
                    if info is None:
                        return None
                    self._model_info[model] = info
                else:
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
                # OLLAMA_CONTEXT_LENGTH is Ollama's own truncation; it
                # doesn't limit a model served by OpenRouter.
                if self.cfg.ollama_context_length is not None and not self.openrouter:
                    return min(value, self.cfg.ollama_context_length)
                return value
        return None

    # ------------------------------------------------------------ pipeline --

    def plan_search(self, preset: Preset, history: list[dict], question: str) -> Plan:
        """Whether the message needs a document search, and the standalone
        queries to run. Falls back to searching the message as-is, with
        failed=True so the caller can say so rather than leaving a bad
        search looking like a deliberate one."""
        recent = history[-6:]
        transcript = ("\n".join(f"{m['role'].upper()}: {m['content'][:1500]}" for m in recent)
                      or "(none — this is the first message)")
        prompt = (f"{prompt_text('planner.md', max_queries=self.cfg.max_queries)}\n\n"
                  f"Conversation:\n{transcript}\n\nLatest message: {question}")
        schema = {"type": "object",
                  "properties": {"search": {"type": "boolean"},
                                 "queries": {"type": "array", "items": {"type": "string"}}},
                  "required": ["search", "queries"]}
        failed, queries, cost = False, [], None
        try:
            resp = self._chat(preset.model, [{"role": "user", "content": prompt}], temperature=0, fmt=schema)
            cost = resp.get("cost")
            out = json.loads(resp["message"]["content"])
            search = out.get("search") is not False
            raw = out.get("queries")
            # A model that ignores the schema and answers with a bare string
            # is still telling us what to search for.
            for item in ([raw] if isinstance(raw, str) else raw or []):
                q = str(item).strip()
                # The planner is told not to repeat itself; this enforces
                # it, since the same search twice only costs time.
                if q and q.casefold() not in {seen.casefold() for seen in queries}:
                    queries.append(q)
        except (ChatError, httpx.HTTPError, json.JSONDecodeError, KeyError, AttributeError, TypeError):
            search, queries, failed = True, [], True
        return Plan(search, queries[:self.cfg.max_queries] or [question], failed, cost)

    def _search(self, query: str, limit: int, seen: set[str], sources: list[Source], found_by: str,
                budget: list[int], max_tokens: int | None = None, doc_shas: set[str] | None = None,
                by_handle: dict[str, Source] | None = None) -> list[Source]:
        """New sources for a query, best first, numbered after the existing
        ones: every relevant result that fits this search's token allowance
        (max_tokens) and the turn's remaining budget (budget[0], mutated).
        A result too big for what's left is skipped in favour of smaller,
        less relevant ones. `limit` only caps the count.

        `by_handle` is run_turn's map of this turn's carried-forward sources
        (see Source.from_earlier). A hit whose handle is already in it
        upgrades that Source in place -- a fresh retrieval has a real
        rerank score and the passage's full text, strictly better than the
        label-only carried copy -- rather than appending a second row for
        the same passage. Appending one would make citation_state's dedupe
        and the UI's handle map ambiguous: this is the single easiest thing
        here to get wrong, and it fails silently if you do."""
        new = []
        allowance = min(budget[0], max_tokens) if max_tokens else budget[0]
        for hit in self.retriever.retrieve(query, limit=limit, exclude=seen, doc_shas=doc_shas):
            handle = handle_for(hit.doc_sha, hit.chunk_index)
            carried = by_handle.get(handle) if by_handle else None
            if carried is not None and (carried.doc_sha, carried.chunk_index) != (hit.doc_sha, hit.chunk_index):
                # A blake2b collision -- astronomically unlikely (~7.4M handle
                # space), but silent misattribution is exactly what handles
                # exist to avoid, so this hit is dropped rather than risked.
                log.warning("citation handle collision on %s: %s:%s vs %s:%s", handle, carried.doc_sha,
                           carried.chunk_index, hit.doc_sha, hit.chunk_index)
                continue
            candidate = Source.from_hit(carried.number if carried else len(sources) + 1, hit, found_by, query)
            cost = estimate_tokens(candidate.for_prompt())
            if cost > allowance and (new or sources):  # always allow at least one source
                continue
            allowance -= cost
            budget[0] -= cost
            seen.update(hit.ids)
            if carried is not None:
                carried.found_by, carried.query, carried.point_id = found_by, query, hit.point_id
                carried.pages, carried.headings, carried.text = hit.pages, hit.headings, hit.text
                carried.rerank_score = hit.rerank_score
                new.append(carried)
            else:
                sources.append(candidate)
                new.append(candidate)
        return new

    def run_turn(self, question: str, history: list[dict], preset_name: str | None = None,
                 extra_system: str | None = None, doc_shas: set[str] | None = None,
                 library_overview: list[dict] | None = None, numbers: CitationNumbers | None = None,
                 earlier: list[dict] | None = None) -> Iterator[dict]:
        """Yields events: plan, searching, sources, listing, thinking, citation,
        token, done.
        The done event carries the TurnResult. `doc_shas` restricts every
        search and the document listing to a library scope (None: every
        indexed document; empty: none) -- see Retriever.search. `library_overview`
        is Accounts.library_overview()'s output, offered back to the model
        verbatim through list_libraries -- already filtered to libraries the
        caller can read, so the model never learns about ones it can't.

        `numbers` is the conversation's citation numbering so far (db.citation_state);
        default-empty is exactly what a caller with no conversation (openai_compat)
        wants. Copied immediately, so a turn that fails part-way leaves the
        caller's copy untouched -- nothing here is numbered until the turn
        that numbered it actually finishes. `earlier` is citation_state's list
        of passages already cited in this conversation (uncapped -- see
        CHAT_CARRY_SOURCES below), offered back so the model can cite one
        again without re-searching for it."""
        preset = self.cfg.preset(preset_name)
        result = TurnResult(model=preset.model, preset=preset.name)
        numbers = numbers.copy() if numbers is not None else CitationNumbers()
        t_start = time.monotonic()
        history = trim_history(history, self.cfg.history_max_tokens)
        # Summed over every model call in the turn; stays None on Ollama,
        # which has no price to report.
        cost: float | None = None

        # 0. Carry forward what this conversation has already cited: put
        # these sources in first, so _search's len(sources) + 1 numbering
        # continues over them unchanged, and index them by handle so a hit
        # that re-finds one of them upgrades it in place (_search) instead
        # of appearing twice.
        by_handle: dict[str, Source] = {}
        if earlier:
            carried = [Source.from_earlier(i + 1, row) for i, row in enumerate(earlier)]
            result.sources.extend(carried)
            by_handle = {s.handle: s for s in carried}
            yield {"type": "sources", "sources": [asdict(s) for s in carried]}

        # 1. Decide whether the message needs a search at all, and write the
        #    standalone queries for it.
        queries, do_search = [question], True
        if self.cfg.plan:
            t0 = time.monotonic()
            do_search, queries, failed, cost = self.plan_search(preset, history, question)
            result.stats["plan_s"] = round(time.monotonic() - t0, 2)
            # Only recorded when it went wrong, so a good turn stays unmarked.
            if failed:
                result.stats["plan_failed"] = True
            yield {"type": "plan", "queries": queries, "search": do_search, "failed": failed}

        # 2. Auto-search.
        seen: set[str] = set()
        budget = [self.cfg.max_source_tokens]
        if do_search:
            t0 = time.monotonic()
            # Planned queries share one turn's worth of sources rather than
            # getting CHAT_AUTO_SOURCE_TOKENS each, so asking about two things
            # doesn't crowd out the tool rounds that follow. The cost is a
            # smaller allowance per query: a long passage that would have
            # fitted a one-query turn can be skipped on a two-query one.
            each = max(1, self.cfg.auto_source_tokens // len(queries))
            for q in queries:
                yield {"type": "searching", "query": q, "found_by": "auto"}
                new = self._search(q, self.cfg.initial_sources, seen, result.sources, "auto", budget,
                                   each, doc_shas, by_handle)
                result.search_queries.append(q)
                yield {"type": "sources", "sources": [asdict(s) for s in new]}
            result.stats["search_s"] = round(time.monotonic() - t0, 2)
            # How many of search_queries were the planner's, so a stored turn
            # can tell them from the tool's.
            result.stats["auto_queries"] = len(queries)
        result.stats["auto_search"] = do_search

        # 3. Answer, with optional tool rounds.
        system = (PROMPTS / "system.md").read_text(encoding="utf-8")
        tools_on = preset.tools and self.cfg.max_tool_rounds > 0 and self.supports_tools(preset.model)
        if tools_on:
            system += "\n" + (PROMPTS / "tools.md").read_text(encoding="utf-8")
        if extra_system:
            system += f"\n\nAdditional instructions:\n{extra_system}"
        # Retrieved-this-turn sources go in the normal Sources: block with
        # their full text; carried-forward ones that never got re-found (an
        # upgraded one is found_by "auto"/"tool" by now, and belongs above)
        # get their own label-only block below instead.
        fresh_sources = [s for s in result.sources if s.found_by != "earlier"]
        earlier_sources = [s for s in result.sources if s.found_by == "earlier"]
        if not do_search:
            context = prompt_text("no_search.md")
        elif fresh_sources:
            context = "Sources:\n\n" + "\n\n".join(s.for_prompt() for s in fresh_sources)
        else:
            context = prompt_text("no_sources.md")
        if earlier_sources:
            # Most recent first-cited, then sorted by [doc:chunk] for
            # readability -- capped so a long conversation's prompt doesn't
            # grow without bound; citation_state itself was read uncapped
            # (`earlier` above), so a passage evicted from this list can
            # still be cited and reattached in step 4 if the model remembers
            # its handle from an earlier answer.
            kept = earlier_sources[-self.cfg.carry_sources:]
            kept.sort(key=lambda s: (s.doc_number, s.chunk_number))
            carry_block = ("Cited earlier in this conversation (identifier and location only "
                           "-- search if you need the text):\n\n" +
                           "\n".join(s.for_prompt(text=False) for s in kept))
            budget[0] -= estimate_tokens(carry_block)
            context = f"{context}\n\n{carry_block}"
        user_turn = f"{context}\n\n---\n\nQuestion: {question}"
        if tools_on and not do_search:
            # The planner's call not to search is a guess made before seeing
            # any passages, so the answering model gets to overrule it.
            user_turn += ("\n\n(That judgement can be wrong. If answering this does need the documents "
                          "after all, call search_documents instead of answering without them.)")
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
        # Numbers each citation as its bracket closes, so the reader sees
        # [1:3] while the answer is still being written rather than a
        # placeholder that only resolves at `done`. Step 4's assign_numbers
        # then only confirms what this handed out -- see CitationScanner.
        scanner = CitationScanner(numbers, lambda h: next((s.doc_sha for s in result.sources if s.handle == h), None))
        while True:
            offer_tools = tools_on and rounds < self.cfg.max_tool_rounds and budget[0] > 0
            content, tool_calls, reasoning_details = "", [], None
            for chunk in self._chat_stream(preset.model, messages, think=preset.think,
                                           tools=available_tools if offer_tools else None):
                msg = chunk.get("message") or {}
                if msg.get("thinking"):
                    yield {"type": "thinking", "text": msg["thinking"]}
                if msg.get("content"):
                    if t_first_token is None:
                        t_first_token = time.monotonic()
                    content += msg["content"]
                    # Before the token, so the number is in the reader's map
                    # by the time the text citing it arrives.
                    for handle, doc_n, chunk_n in scanner.feed(msg["content"]):
                        yield {"type": "citation", "handle": handle, "doc": doc_n, "chunk": chunk_n}
                    yield {"type": "token", "text": msg["content"]}
                tool_calls.extend(msg.get("tool_calls") or [])
                # Provider-specific reasoning state (OpenRouter), handed back
                # with this round's tool calls -- see openrouter.py.
                reasoning_details = msg.get("reasoning_details") or reasoning_details
                if chunk.get("done"):
                    round_prompt_tokens = chunk.get("prompt_eval_count", 0)
                    round_eval_tokens = chunk.get("eval_count", 0)
                    prompt_tokens += round_prompt_tokens
                    eval_tokens += round_eval_tokens
                    if chunk.get("cost") is not None:
                        cost = (cost or 0) + chunk["cost"]
            if content:
                parts.append(content)
            if not (tool_calls and offer_tools):
                break

            rounds += 1
            assistant = {"role": "assistant", "content": content, "tool_calls": tool_calls}
            if reasoning_details:
                assistant["reasoning_details"] = reasoning_details
            messages.append(assistant)
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
                                       doc_shas=doc_shas, by_handle=by_handle)
                    result.search_queries.append(tool_query)
                    yield {"type": "sources", "sources": [asdict(s) for s in new]}
                    reply = ("\n\n".join(s.for_prompt() for s in new) if new
                             else "No further relevant passages found for this query.")
                tool_reply = {"role": "tool", "tool_name": name or "", "content": reply}
                if call.get("id"):
                    # OpenAI-style APIs link each reply to its call by id.
                    tool_reply["tool_call_id"] = call["id"]
                messages.append(tool_reply)

        # 4. Resolve citations: handles the answer cites, against this
        # turn's sources plus the conversation's numbering (so a handle
        # cited earlier still resolves even if this turn evicted it from
        # the carry-forward block). Assigning writes each cited Source's
        # [doc:chunk] back onto it, so a caller storing result.sources sees
        # the number without re-deriving it.
        result.answer = "\n\n".join(p.strip() for p in parts if p.strip())
        known = {s.handle: s for s in result.sources}
        citations = parse_citations(result.answer)
        result.cited, result.invalid_citations = resolve(citations, numbers, known)
        result.refs = assign_numbers(result.cited, {h: known[h].doc_sha for h in result.cited}, numbers)
        for handle, (doc_n, chunk_n) in result.refs.items():
            known[handle].doc_number, known[handle].chunk_number = doc_n, chunk_n
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
        # The planner and every answer round; not the conversation title,
        # which is generated after the turn.
        result.cost = round(cost, 6) if cost is not None else None
        yield {"type": "done", "result": result}

    def generate_title(self, preset_name: str | None, question: str, answer: str) -> str | None:
        preset = self.cfg.preset(preset_name)
        prompt = f'{prompt_text("title.md")}\n\nQuestion: {question}\n\nAnswer: {answer[:1500]}'
        try:
            resp = self._chat(preset.model, [{"role": "user", "content": prompt}], temperature=0,
                              fmt={"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]})
            title = json.loads(resp["message"]["content"]).get("title", "").strip().strip('"').rstrip(".")
        except (ChatError, httpx.HTTPError, json.JSONDecodeError, KeyError):
            return None
        return title[:120] or None

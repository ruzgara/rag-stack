import httpx
import pytest
from conftest import CHAT_ENV, FakeLibrary, FakeOllama, FakeRetriever, answer, calls, make_hit, tool_call

from chat import ChatConfig, ChatError, ChatService, Source, resolve_citations, trim_history


def config(**env) -> ChatConfig:
    return ChatConfig.from_env({**CHAT_ENV, **{k: str(v) for k, v in env.items()}})


def service(results=None, ollama: FakeOllama | None = None, library=None,
            **env) -> tuple[ChatService, FakeRetriever, FakeOllama]:
    retriever = FakeRetriever(results or {})
    ollama = ollama or FakeOllama(streams=[answer("ok")])
    return (ChatService(retriever, config(**env), client=ollama.client(), library=library or FakeLibrary()),
            retriever, ollama)


def run(svc: ChatService, question: str, history=None, preset=None, library_overview=None) -> tuple[list[dict], object]:
    events = list(svc.run_turn(question, history or [], preset, library_overview=library_overview))
    return events, events[-1]["result"]


def source(number: int, pages=(3,), headings=("Ch 1", "Risks")) -> Source:
    return Source(number=number, found_by="auto", query="q", point_id=f"p{number}", doc_sha="d", filename="book.pdf",
                  chunk_index=number, pages=list(pages), headings=list(headings), text=" body ", rerank_score=0.9)


# ------------------------------------------------------------- citations --

def test_citations_in_every_form():
    sources = [source(n) for n in range(1, 9)]
    cited, invalid = resolve_citations("A [1]. B [2, 3]. C [4-5] and [6–6]. D [7; 8]. E [3]. F [99].", sources)
    assert cited == [1, 2, 3, 4, 5, 6, 7, 8]
    assert invalid == [99]


def test_absurd_citation_ranges_are_capped():
    cited, invalid = resolve_citations("[1-100000]", [source(1)])
    assert cited == [1] and len(invalid) == 50


def test_text_in_brackets_is_not_a_citation():
    assert resolve_citations("see [a] and [1a] and [ 2 ]", [source(1), source(2)]) == ([], [])


# --------------------------------------------------------------- history --

def test_history_keeps_the_most_recent_turns_that_fit():
    history = [{"role": "user", "content": "a" * 400}, {"role": "assistant", "content": "b" * 400},
               {"role": "user", "content": "c" * 40}, {"role": "assistant", "content": "d" * 40}]
    assert trim_history(history, 50) == history[2:]
    assert trim_history(history, 10_000) == history


def test_history_never_starts_with_an_answer():
    history = [{"role": "user", "content": "a" * 400}, {"role": "assistant", "content": "b" * 40},
               {"role": "user", "content": "c" * 40}]
    assert trim_history(history, 30) == history[2:]


# --------------------------------------------------------------- sources --

@pytest.mark.parametrize("pages, label", [([3], "book.pdf, p. 3"), ([3, 4, 7], "book.pdf, pp. 3-7"), ([], "book.pdf")])
def test_source_label(pages, label):
    assert source(1, pages=pages).label() == label


def test_source_in_the_prompt_shows_number_place_and_section():
    assert source(2).for_prompt() == "[2] book.pdf, p. 3 — Ch 1 > Risks\nbody"


# ---------------------------------------------------------------- config --

def test_chat_config_defaults():
    cfg = config()
    assert cfg.presets["everyday"].model == "small-model" and not cfg.presets["everyday"].think
    assert cfg.presets["hard"].think
    assert (cfg.initial_sources, cfg.auto_source_tokens, cfg.max_source_tokens) == (10, 3500, 6000)


@pytest.mark.parametrize("env", [{"CHAT_MODEL_HARD": ""}, {"CHAT_DEFAULT_PRESET": "turbo"},
                                 {"CHAT_MAX_TOOL_ROUNDS": "-1"}])
def test_bad_chat_settings_are_rejected(env):
    with pytest.raises(ValueError):
        config(**env)


def test_unknown_preset():
    with pytest.raises(ChatError, match="unknown preset"):
        config().preset("turbo")
    assert config().preset(None).name == "everyday"
    assert config().preset(" HARD ").name == "hard"


# ---------------------------------------------------------------- planner --

@pytest.mark.parametrize("reply, expected", [
    ('{"search": false, "queries": ["hello"]}', (False, ["hello"], False)),
    ('{"search": true, "queries": ["RSF factor for Level 1 assets"]}',
     (True, ["RSF factor for Level 1 assets"], False)),
    ('{"search": true, "queries": ["deposits", "withdrawals"]}', (True, ["deposits", "withdrawals"], False)),
    # Empty queries are the model answering, so they aren't a failure -- but a
    # reply we can't read at all is, and only then is the turn marked.
    ('{"search": true, "queries": [""]}', (True, ["and for Level 1?"], False)),
    ('{"search": true, "queries": []}', (True, ["and for Level 1?"], False)),
    ('{"queries": ["x"]}', (True, ["x"], False)),                       # missing decision: search
    ('{"search": true, "queries": "x"}', (True, ["x"], False)),         # schema ignored: bare string
    # Two searches for one thing would only cost time, so the repeat is dropped.
    ('{"search": true, "queries": ["Level 1", "level 1", "Level 2"]}', (True, ["Level 1", "Level 2"], False)),
    ("not json", (True, ["and for Level 1?"], True)),
    (None, (True, ["and for Level 1?"], True)),                        # Ollama error
])
def test_the_planner_falls_back_to_searching_the_message(reply, expected):
    svc, _, _ = service(ollama=FakeOllama(plan=reply))
    assert svc.plan_search(svc.cfg.preset(None), [], "and for Level 1?") == expected


def test_the_planner_keeps_at_most_max_queries():
    svc, _, _ = service(ollama=FakeOllama(plan='{"search": true, "queries": ["a", "b", "c", "d"]}'))
    assert svc.cfg.max_queries == 2
    assert svc.plan_search(svc.cfg.preset(None), [], "a and b and c and d").queries == ["a", "b"]


def test_the_planner_sends_the_recent_conversation_and_its_own_prompt():
    svc, _, ollama = service(ollama=FakeOllama(plan='{"search": true, "queries": ["q"]}'))
    history = [{"role": "user", "content": f"message {i}"} for i in range(10)]
    svc.plan_search(svc.cfg.preset(None), history, "latest")
    prompt = ollama.chat_requests[0]["messages"][0]["content"]
    assert "message 9" in prompt and "message 3" not in prompt  # last 6 only
    assert "You prepare searches over a document library" in prompt  # prompts/planner.md
    assert "{max_queries}" not in prompt and "At most 2." in prompt  # filled in
    assert ollama.chat_requests[0]["format"]["required"] == ["search", "queries"]


# ----------------------------------------------------- source selection --

def sized(point_id: str, tokens: int, headings=("Ch",)):
    return make_hit(point_id, list(headings), text="x" * (tokens * 4))


def test_sources_fill_the_budget_best_first_skipping_what_doesnt_fit():
    svc, _, _ = service({"q": [sized("a", 300), sized("big", 900), sized("b", 300), sized("c", 300)]})
    sources, budget = [], [10_000]
    new = svc._search("q", 10, set(), sources, "auto", budget, max_tokens=1000)
    assert [s.point_id for s in new] == ["a", "b", "c"]  # "big" didn't fit after "a"
    assert [s.number for s in new] == [1, 2, 3]
    assert 10_000 - budget[0] == sum(len(s.for_prompt()) // 4 + 1 for s in new)


def test_the_first_source_is_always_allowed():
    svc, _, _ = service({"q": [sized("huge", 5000)]})
    assert len(svc._search("q", 10, set(), [], "auto", [100], max_tokens=100)) == 1


def test_the_turn_budget_limits_the_search_allowance():
    svc, _, _ = service({"q": [sized("a", 300), sized("b", 300)]})
    new = svc._search("q", 10, set(), [source(1)], "tool", [400], max_tokens=5000)
    assert [s.point_id for s in new] == ["a"]
    assert new[0].number == 2  # numbering continues


def test_a_section_marks_all_its_chunks_as_seen():
    section = make_hit("p10", ["Ch"])
    section.point_ids = ["p10", "p11", "p12"]
    svc, _, _ = service({"q": [section]})
    seen = set()
    svc._search("q", 10, seen, [], "auto", [10_000])
    assert seen == {"p10", "p11", "p12"}


# -------------------------------------------------------------- run_turn --

def user_turn(ollama: FakeOllama, n: int = 0) -> str:
    streamed = [r for r in ollama.chat_requests if r.get("stream")]
    return streamed[n]["messages"][-1]["content"]


def test_a_failed_plan_is_announced_and_recorded():
    """The turn still runs on the raw message, but the stream and the stored
    stats both say the planning didn't happen, so the UI can show it."""
    ollama = FakeOllama(plan=None, streams=[answer("ok")])
    svc, retriever, _ = service({"what are some challenges": []}, ollama)
    events, result = run(svc, "what are some challenges")
    plan = next(e for e in events if e["type"] == "plan")
    assert plan == {"type": "plan", "queries": ["what are some challenges"],
                    "search": True, "failed": True}
    assert result.stats["plan_failed"] is True
    assert retriever.calls[0]["question"] == "what are some challenges"  # searched as-is


def test_a_working_plan_leaves_no_failure_mark():
    ollama = FakeOllama(plan='{"search": true, "queries": ["bond parties"]}', streams=[answer("ok")])
    svc, _, _ = service({"bond parties": []}, ollama)
    events, result = run(svc, "who are they?")
    assert next(e for e in events if e["type"] == "plan")["failed"] is False
    assert "plan_failed" not in result.stats


def test_a_greeting_skips_the_search():
    ollama = FakeOllama(plan='{"search": false, "queries": ["hello"]}', streams=[answer("Hi", "!")])
    svc, retriever, _ = service(ollama=ollama)
    events, result = run(svc, "hello")
    assert [e["type"] for e in events] == ["plan", "token", "token", "done"]
    assert retriever.calls == []
    assert result.answer == "Hi!" and result.sources == [] and result.stats["auto_search"] is False
    assert "no document search was run" in user_turn(ollama)


def test_a_question_is_answered_from_its_sources_with_citations():
    ollama = FakeOllama(plan='{"search": true, "queries": ["bond parties"]}', streams=[answer("Investors [1]", " and [3].")])
    hits = [make_hit("a", ["BOND ISSUES", "The parties"], text="Investors and issuers."), make_hit("b", ["Timetable"])]
    svc, retriever, _ = service({"bond parties": hits}, ollama)
    events, result = run(svc, "who are the parties?")

    assert [e["type"] for e in events[:3]] == ["plan", "searching", "sources"]
    assert retriever.calls[0]["question"] == "bond parties"  # the planned query is searched
    assert result.cited == [1] and result.invalid_citations == [3]
    assert result.search_queries == ["bond parties"]
    turn = user_turn(ollama)
    assert turn.startswith("Sources:\n\n[1] doc1.pdf, p. 1 — BOND ISSUES > The parties\nInvestors and issuers.")
    assert "Question: who are the parties?" in turn  # the model sees the user's own words
    assert result.stats["source_tokens"] > 0


def test_a_skipped_search_can_still_be_overruled_by_the_tool():
    """The planner decides before seeing any passage, so a tools-on preset is
    told it may search anyway rather than being told the message needs nothing."""
    ollama = FakeOllama(plan='{"search": false, "queries": ["hello"]}',
                        streams=[[*tool_call("bond parties")], answer("Found it [1].")])
    svc, retriever, _ = service({"bond parties": [make_hit("a", ["Parties"])]}, ollama, preset="hard")
    events, result = run(svc, "hi -- and who are the parties?", preset="hard")

    turn = user_turn(ollama)
    assert "no document search was run" in turn and "looked like one that doesn't need" in turn
    assert "That judgement can be wrong" in turn  # the override is offered
    assert [e["type"] for e in events if e["type"] == "searching"] == ["searching"]
    assert [c["question"] for c in retriever.calls] == ["bond parties"]
    assert result.search_queries == ["bond parties"]  # found by the tool, not the planner
    assert result.stats["auto_search"] is False and result.cited == [1]


def test_a_two_part_question_searches_each_part_and_records_both():
    """Every planned query gets its own search and its own pair of events, so
    the trace shows what was looked for; the sources are numbered across all
    of them, so citations stay unambiguous."""
    ollama = FakeOllama(plan='{"search": true, "queries": ["deposit rules", "withdrawal rules"]}',
                        streams=[answer("Both [1] and [2].")])
    svc, retriever, _ = service({"deposit rules": [make_hit("a", ["Deposits"])],
                                 "withdrawal rules": [make_hit("b", ["Withdrawals"])]}, ollama)
    events, result = run(svc, "how do deposits and withdrawals differ?")

    assert [e["type"] for e in events[:5]] == ["plan", "searching", "sources", "searching", "sources"]
    assert [e["query"] for e in events if e["type"] == "searching"] == ["deposit rules", "withdrawal rules"]
    assert [c["question"] for c in retriever.calls] == ["deposit rules", "withdrawal rules"]
    assert result.search_queries == ["deposit rules", "withdrawal rules"]
    assert result.stats["auto_queries"] == 2
    assert [s.number for s in result.sources] == [1, 2]
    assert result.cited == [1, 2]


def test_planned_queries_share_one_turns_worth_of_sources():
    """Two queries don't get CHAT_AUTO_SOURCE_TOKENS each -- they halve it, so
    a two-part question leaves the tool rounds as much room as a one-part one.
    The flip side is that each query's allowance is smaller, so a passage that
    would have fitted a single-query turn can be skipped here."""
    ollama = FakeOllama(plan='{"search": true, "queries": ["one", "two"]}', streams=[answer("ok")])
    svc, _, _ = service({"one": [sized("a", 150), sized("b", 150)], "two": [sized("c", 150)]}, ollama,
                        CHAT_AUTO_SOURCE_TOKENS=400)
    _, result = run(svc, "one and two")
    # 400 // 2 = 200 each: "b" no longer fits the first query, but the second
    # query still gets its own 200 rather than an exhausted budget.
    assert [s.point_id for s in result.sources] == ["a", "c"]
    assert result.stats["source_tokens"] < 400  # the turn kept room for the tool rounds


def test_one_planned_query_keeps_the_whole_auto_allowance():
    ollama = FakeOllama(plan='{"search": true, "queries": ["one"]}', streams=[answer("ok")])
    svc, _, _ = service({"one": [sized("a", 150), sized("b", 150)]}, ollama, CHAT_AUTO_SOURCE_TOKENS=400)
    _, result = run(svc, "one")
    assert [s.point_id for s in result.sources] == ["a", "b"]  # both fit when nothing shares the budget


def test_context_usage_is_reported_against_the_models_window():
    ollama = FakeOllama(plan='{"search": false, "queries": ["hi"]}', streams=[answer("Hi", "!", prompt_tokens=200)],
                        context_length=8192)
    svc, _, _ = service(ollama=ollama)
    _, result = run(svc, "hi")
    assert result.stats["context_used"] == 200 + 2  # prompt + the two answer tokens
    assert result.stats["context_limit"] == 8192
    assert result.stats["context_used_pct"] == round(100 * 202 / 8192, 1)


def test_context_limit_is_none_when_ollama_cant_report_it():
    ollama = FakeOllama(plan='{"search": false, "queries": ["hi"]}', streams=[answer("Hi")], context_length=None)
    svc, _, _ = service(ollama=ollama)
    _, result = run(svc, "hi")
    assert result.stats["context_limit"] is None and result.stats["context_used_pct"] is None


def test_context_limit_is_capped_by_ollamas_configured_context_length():
    """A model can advertise a bigger window (e.g. 128k) than Ollama is
    actually configured to serve (OLLAMA_CONTEXT_LENGTH) -- the reported
    limit has to be whichever is smaller, or "context used" reads far too
    low against a window the model never really gets."""
    ollama = FakeOllama(plan='{"search": false, "queries": ["hi"]}', streams=[answer("Hi", "!", prompt_tokens=200)],
                        context_length=131072)
    svc, _, _ = service(ollama=ollama, OLLAMA_CONTEXT_LENGTH=32768)
    _, result = run(svc, "hi")
    assert result.stats["context_limit"] == 32768


def test_context_limit_is_the_models_own_when_it_is_smaller_than_ollamas_configured_length():
    ollama = FakeOllama(plan='{"search": false, "queries": ["hi"]}', streams=[answer("Hi", "!", prompt_tokens=200)],
                        context_length=8192)
    svc, _, _ = service(ollama=ollama, OLLAMA_CONTEXT_LENGTH=32768)
    _, result = run(svc, "hi")
    assert result.stats["context_limit"] == 8192


def test_no_relevant_sources_is_said_plainly():
    ollama = FakeOllama(streams=[answer("Not covered.")])
    svc, _, _ = service({}, ollama)
    run(svc, "how do I bake bread?")
    assert "found no relevant passages" in user_turn(ollama)


def test_the_model_can_search_for_more_with_the_tool():
    ollama = FakeOllama(plan='{"search": true, "queries": ["loans"]}',
                        streams=[tool_call("bond parties"), answer("Loans [1], bonds [2].")])
    svc, retriever, _ = service({"loans": [make_hit("a", ["LOANS"])], "bond parties": [make_hit("b", ["BONDS"])]},
                                ollama)
    events, result = run(svc, "compare loans and bonds")

    searches = [e for e in events if e["type"] == "searching"]
    assert [(e["query"], e["found_by"]) for e in searches] == [("loans", "auto"), ("bond parties", "tool")]
    assert [(s.number, s.point_id, s.found_by) for s in result.sources] == [(1, "a", "auto"), (2, "b", "tool")]
    assert retriever.calls[1]["exclude"] == {"a"}  # already-given passages aren't returned again
    assert result.cited == [1, 2] and result.stats["tool_rounds"] == 1
    # The tool result goes back to the model as a tool message.
    second = [r for r in ollama.chat_requests if r.get("stream")][1]["messages"]
    assert second[-1]["role"] == "tool" and second[-1]["content"].startswith("[2] doc1.pdf")
    assert second[-2]["tool_calls"][0]["function"]["arguments"] == {"query": "bond parties"}


def test_tool_rounds_are_limited():
    ollama = FakeOllama(streams=[tool_call("more"), answer("done")])
    svc, _, _ = service({"q": [make_hit("a", ["A"])]}, ollama, CHAT_MAX_TOOL_ROUNDS=1)
    run(svc, "q")
    streamed = [r for r in ollama.chat_requests if r.get("stream")]
    assert "tools" in streamed[0] and "tools" not in streamed[1]


def test_models_without_tool_support_get_no_tools():
    ollama = FakeOllama(tools=False, streams=[answer("ok")])
    svc, _, _ = service({}, ollama)
    run(svc, "q")
    request = [r for r in ollama.chat_requests if r.get("stream")][0]
    assert "tools" not in request
    assert "search_documents" not in request["messages"][0]["content"]


def test_presets_choose_model_and_thinking():
    ollama = FakeOllama(streams=[answer("ok")])
    svc, _, _ = service({}, ollama)
    run(svc, "q", preset="hard")
    request = [r for r in ollama.chat_requests if r.get("stream")][0]
    assert (request["model"], request["think"]) == ("big-model", True)


def test_without_the_planning_step_the_message_is_searched_as_is():
    ollama = FakeOllama(streams=[answer("ok")])
    svc, retriever, _ = service({}, ollama, CHAT_PLAN="false")
    events, _ = run(svc, "and for Level 1?")
    assert events[0]["type"] == "searching" and retriever.calls[0]["question"] == "and for Level 1?"


def test_an_ollama_error_mid_stream_is_raised():
    ollama = FakeOllama(streams=[[{"error": "model runner crashed"}]])
    svc, _, _ = service({}, ollama)
    with pytest.raises(ChatError, match="model runner crashed"):
        run(svc, "q")


# ------------------------------------------------------- the document list --

def test_the_model_can_ask_what_the_knowledge_base_holds():
    ollama = FakeOllama(streams=[calls({"name": "list_documents", "arguments": {}}), answer("Two books.")])
    lib = FakeLibrary()
    svc, _, _ = service({}, ollama, library=lib)
    events, result = run(svc, "what documents do you have?")

    assert [e for e in events if e["type"] == "listing"] == [{"type": "listing", "document": ""}]
    assert lib.calls == [None]
    second = [r for r in ollama.chat_requests if r.get("stream")][1]["messages"]
    assert second[-1] == {"role": "tool", "tool_name": "list_documents",
                          "content": "The knowledge base holds 2 document(s)."}
    assert result.answer == "Two books."
    assert result.sources == [] and result.cited == []   # a listing is not a citable source
    assert result.stats["listed_documents"] == 1


def test_one_document_can_be_looked_up_by_name():
    ollama = FakeOllama(streams=[calls({"name": "list_documents", "arguments": {"document": "basel"}}),
                                 answer("It covers the NSFR.")])
    lib = FakeLibrary()
    svc, _, _ = service({}, ollama, library=lib)
    events, _ = run(svc, "what does the basel paper cover?")
    assert lib.calls == ["basel"]
    assert [e for e in events if e["type"] == "listing"] == [{"type": "listing", "document": "basel"}]


def test_the_document_list_is_offered_as_a_tool_beside_search():
    ollama = FakeOllama(streams=[answer("ok")])
    svc, _, _ = service({}, ollama)
    run(svc, "q")
    tools = [r for r in ollama.chat_requests if r.get("stream")][0]["tools"]
    assert [t["function"]["name"] for t in tools] == ["search_documents", "list_documents"]


def test_a_broken_document_list_doesnt_fail_the_answer():
    ollama = FakeOllama(streams=[calls({"name": "list_documents", "arguments": {}}), answer("Can't tell just now.")])
    svc, _, _ = service({}, ollama, library=FakeLibrary(error=httpx.ConnectError("qdrant down")))
    _, result = run(svc, "what do you have?")
    second = [r for r in ollama.chat_requests if r.get("stream")][1]["messages"]
    assert "The document list is unavailable" in second[-1]["content"]
    assert result.answer == "Can't tell just now."


def test_the_listing_counts_against_the_source_budget():
    listing = "x" * 4000
    ollama = FakeOllama(streams=[calls({"name": "list_documents", "arguments": {}}), answer("ok")])
    svc, _, _ = service({}, ollama, library=FakeLibrary(text=listing))
    _, result = run(svc, "q")
    assert result.stats["source_tokens"] >= len(listing) // 4


def test_both_tools_can_be_called_in_one_round():
    ollama = FakeOllama(streams=[calls({"name": "list_documents", "arguments": {}},
                                       {"name": "search_documents", "arguments": {"query": "bonds"}}),
                                 answer("Answer [1].")])
    svc, retriever, _ = service({"bonds": [make_hit("a", ["BONDS"])]}, ollama)
    _, result = run(svc, "q")
    assert [s.point_id for s in result.sources] == ["a"]
    tool_messages = [m for m in [r for r in ollama.chat_requests if r.get("stream")][1]["messages"]
                     if m["role"] == "tool"]
    assert [m["tool_name"] for m in tool_messages] == ["list_documents", "search_documents"]


# --------------------------------------------------------------- libraries --

def test_list_libraries_is_not_offered_without_an_overview():
    ollama = FakeOllama(streams=[answer("ok")])
    svc, _, _ = service({}, ollama)
    run(svc, "q")   # no library_overview passed
    tools = [r for r in ollama.chat_requests if r.get("stream")][0]["tools"]
    assert [t["function"]["name"] for t in tools] == ["search_documents", "list_documents"]


def test_list_libraries_is_offered_alongside_the_other_tools():
    ollama = FakeOllama(streams=[answer("ok")])
    svc, _, _ = service({}, ollama)
    run(svc, "q", library_overview=[])
    tools = [r for r in ollama.chat_requests if r.get("stream")][0]["tools"]
    assert [t["function"]["name"] for t in tools] == ["search_documents", "list_documents", "list_libraries"]


def test_list_libraries_reports_scope_for_the_readable_libraries_only():
    overview = [
        {"name": "Attached", "description": "In scope", "in_scope": True, "document_count": 3},
        {"name": "Readable only", "description": None, "in_scope": False, "document_count": 1},
    ]
    ollama = FakeOllama(streams=[calls({"name": "list_libraries", "arguments": {}}), answer("Here's what exists.")])
    svc, _, _ = service({}, ollama)
    events, _ = run(svc, "what libraries are there?", library_overview=overview)

    assert [e for e in events if e["type"] == "listing_libraries"] == [{"type": "listing_libraries"}]
    second = [r for r in ollama.chat_requests if r.get("stream")][1]["messages"]
    reply = second[-1]["content"]
    assert reply == (
        "- Attached: searchable now (3 documents) -- In scope\n"
        "- Readable only: not attached to this conversation (1 documents)"
    )


def test_list_libraries_with_none_readable_says_so():
    ollama = FakeOllama(streams=[calls({"name": "list_libraries", "arguments": {}}), answer("None yet.")])
    svc, _, _ = service({}, ollama)
    _, _ = run(svc, "what libraries are there?", library_overview=[])
    second = [r for r in ollama.chat_requests if r.get("stream")][1]["messages"]
    assert second[-1]["content"] == "No libraries available."

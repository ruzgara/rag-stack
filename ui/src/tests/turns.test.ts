import { describe, expect, it } from "vitest";
import { toTurns, deriveInvalid, applyEvent, newTurn, traceFromStored, processSummary, conversationDocuments, citedDocuments, passageMarks, documentMarks } from "../lib/turns";
import type { DoneSource, Message } from "../lib/types";
import { usd } from "../lib/format";

// A deterministic, HANDLE_RE-shaped handle per small integer -- distinct
// values for distinct `number`s, so tests can still key off a plain int like
// the old `number` field did, without hand-picking a real handle each time.
const ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789";
function handleFor(n: number): string {
  const c = (i: number) => ALPHABET[(n + i) % ALPHABET.length];
  return `S${c(0)}2${c(1)}${c(2)}${c(3)}`;
}

function src(number: number, query = "q", found_by: "auto" | "tool" = "auto"): DoneSource {
  return {
    number,
    found_by,
    query,
    point_id: "p",
    doc_sha: "sha",
    filename: "f.pdf",
    chunk_index: 0,
    pages: [1],
    headings: [],
    text: "text",
    rerank_score: null,
    handle: handleFor(number),
    doc_number: number,
    chunk_number: 1,
    cited: true,
  };
}

describe("toTurns", () => {
  it("pairs a stored user+assistant message into one Turn", () => {
    const messages: Message[] = [
      { id: "u1", role: "user", content: "hello", created_at: 1 },
      {
        id: "a1",
        role: "assistant",
        content: "hi there",
        created_at: 2,
        model: "m",
        preset: "everyday",
        search_queries: ["q1"],
        status: "complete",
        error: null,
        stats: { auto_search: false, tool_rounds: 0, prompt_tokens: 1, completion_tokens: 1, first_token_s: 0.1, total_s: 1, source_tokens: 0 },
        sources: [src(1)],
      },
    ];
    const turns = toTurns(messages);
    expect(turns).toHaveLength(1);
    expect(turns[0]).toMatchObject({
      key: "a1", // the answer's id: a retry replaces the answer, so the key must follow it
      question: "hello",
      questionId: "u1",
      answer: "hi there",
      status: "complete",
      searchQueries: ["q1"],
    });
    expect(turns[0].sources).toHaveLength(1);
  });

  it("handles an orphan user message with no persisted reply", () => {
    const messages: Message[] = [{ id: "u1", role: "user", content: "hello?", created_at: 1 }];
    const turns = toTurns(messages);
    expect(turns).toHaveLength(1);
    expect(turns[0].answer).toBe("");
    expect(turns[0].status).toBe("streaming");
  });

  it("pairs multiple turns in order", () => {
    const messages: Message[] = [
      { id: "u1", role: "user", content: "q1", created_at: 1 },
      {
        id: "a1", role: "assistant", content: "a1", created_at: 2, model: "m", preset: "everyday",
        search_queries: [], status: "complete", error: null,
        stats: { auto_search: false, tool_rounds: 0, prompt_tokens: 0, completion_tokens: 0, first_token_s: null, total_s: 0, source_tokens: 0 },
        sources: [],
      },
      { id: "u2", role: "user", content: "q2", created_at: 3 },
      {
        id: "a2", role: "assistant", content: "a2", created_at: 4, model: "m", preset: "everyday",
        search_queries: [], status: "error", error: "boom",
        stats: { auto_search: false, tool_rounds: 0, prompt_tokens: 0, completion_tokens: 0, first_token_s: null, total_s: 0, source_tokens: 0 },
        sources: [],
      },
    ];
    const turns = toTurns(messages);
    expect(turns.map((t) => t.question)).toEqual(["q1", "q2"]);
    expect(turns[1].status).toBe("error");
    expect(turns[1].error).toBe("boom");
  });
});

describe("deriveInvalid", () => {
  // Mirrors citations.py's resolve(): the invalid half of it. Entries are
  // handles, "d:c" ref strings, or malformed raw tokens -- never printed
  // verbatim by a caller (TurnView.svelte only reports a count).
  it("a known handle resolves, an unknown one is invalid", () => {
    const known = src(1); // handle = handleFor(1)
    const bogus = handleFor(99);
    expect(deriveInvalid(`Answer with [${known.handle}] and [${bogus}].`, [known])).toEqual([bogus]);
  });

  it("a ref range only partly covered by known sources reports the rest", () => {
    const known = { ...src(1), doc_number: 1, chunk_number: 1 };
    expect(deriveInvalid("Uses [1:1-3] but only chunk 1 exists.", [known])).toEqual(["1:2", "1:3"]);
  });

  it("no citations here", () => {
    expect(deriveInvalid("No citations here.", [src(1)])).toEqual([]);
  });

  it("citing the same known handle twice is not invalid, and not duplicated", () => {
    const known = src(1);
    expect(deriveInvalid(`Cites [${known.handle}] twice, [${known.handle}] again.`, [known])).toEqual([]);
  });
});

describe("applyEvent", () => {
  it("builds the trace and attaches each sources event to its search", () => {
    const t = newTurn("k", "q?");
    applyEvent(t, { type: "queued", position: 2 });
    expect(t.queuePosition).toBe(2);
    applyEvent(t, { type: "user", id: "u1" });
    expect(t.queuePosition).toBeNull();
    applyEvent(t, { type: "plan", queries: ["standalone q"], search: true });
    applyEvent(t, { type: "searching", query: "standalone q", found_by: "auto" });
    applyEvent(t, { type: "sources", sources: [src(1), src(2)] });
    applyEvent(t, { type: "listing", document: "" });
    applyEvent(t, { type: "searching", query: "more", found_by: "tool" });
    applyEvent(t, { type: "sources", sources: [src(2), src(3, "more", "tool")] }); // 2 already known
    expect(t.trace.map((s) => s.verb)).toEqual(["planned", "search", "list", "search"]);
    expect(t.trace[1].sources).toEqual([handleFor(1), handleFor(2)]);
    expect(t.trace[3].sources).toEqual([handleFor(2), handleFor(3)]);
    expect(t.sources.map((s) => s.handle)).toEqual([handleFor(1), handleFor(2), handleFor(3)]);
  });

  it("replaces, not duplicates, a source whose handle is re-emitted (the carry-forward upgrade case)", () => {
    const t = newTurn("k", "q?");
    applyEvent(t, { type: "sources", sources: [{ ...src(1), found_by: "auto", rerank_score: null }] });
    applyEvent(t, { type: "sources", sources: [{ ...src(1), found_by: "tool", rerank_score: 0.9 }] });
    expect(t.sources).toHaveLength(1);
    expect(t.sources[0]).toMatchObject({ found_by: "tool", rerank_score: 0.9 });
  });

  it("shows several planned queries as one plan line, then a search each", () => {
    const t = newTurn("k", "how do deposits and withdrawals differ?");
    applyEvent(t, { type: "plan", queries: ["deposit rules", "withdrawal rules"], search: true });
    applyEvent(t, { type: "searching", query: "deposit rules", found_by: "auto" });
    applyEvent(t, { type: "sources", sources: [src(1, "deposit rules")] });
    applyEvent(t, { type: "searching", query: "withdrawal rules", found_by: "auto" });
    applyEvent(t, { type: "sources", sources: [src(2, "withdrawal rules")] });
    expect(t.trace[0]).toEqual({ verb: "planned", text: "deposit rules · withdrawal rules", sources: [] });
    expect(t.trace.map((s) => s.verb)).toEqual(["planned", "search", "search"]);
    expect(t.searchQueries).toEqual(["deposit rules", "withdrawal rules"]);
  });

  it("marks a failed plan, live and in the summary", () => {
    const t = newTurn("k", "what are some challenges");
    applyEvent(t, { type: "plan", queries: ["what are some challenges"], search: true, failed: true });
    expect(t.trace[0]).toEqual({ verb: "planned", text: "what are some challenges", sources: [], failed: true });
    expect(processSummary(t)).toContain("plan failed");
  });

  it("leaves a working plan unmarked", () => {
    const t = newTurn("k", "and for 2021?");
    applyEvent(t, { type: "plan", queries: ["the 2021 indenture parties"], search: true, failed: false });
    expect(t.trace[0].failed).toBeUndefined();
    expect(processSummary(t)).not.toContain("plan failed");
  });

  it("counts thinking and answer tokens and stamps the first token", () => {
    const t = newTurn("k", "q?");
    applyEvent(t, { type: "thinking", text: "hm" });
    applyEvent(t, { type: "thinking", text: "m" });
    expect(t.firstTokenAt).toBeNull();
    applyEvent(t, { type: "token", text: "Hi" });
    applyEvent(t, { type: "token", text: "!" });
    expect(t).toMatchObject({ thinking: "hmm", thinkingTokens: 2, answer: "Hi!", answerTokens: 2, hasLiveThinking: true });
    expect(t.firstTokenAt).not.toBeNull();
  });

  it("takes the turn's cost from done, and has none without it (Ollama)", () => {
    const done = { type: "done" as const, message_id: "m", answer: "a", model: "x", preset: "everyday",
                   search_queries: [], cited: [], refs: {}, invalid_citations: [], stats: {}, sources: [] };
    const priced = newTurn("k", "q?");
    applyEvent(priced, { ...done, cost: 0.0042 });
    expect(priced.cost).toBe(0.0042);
    const free = newTurn("k", "q?");
    applyEvent(free, { ...done, cost: null });
    expect(free.cost).toBeNull();
  });

  it("splits reasoning that resumes after a search into its own run", () => {
    const t = newTurn("k", "q?");
    applyEvent(t, { type: "thinking", text: "first " });
    applyEvent(t, { type: "thinking", text: "thought" });
    applyEvent(t, { type: "searching", query: "northgate", found_by: "tool" });
    applyEvent(t, { type: "thinking", text: "second" });
    expect(t.trace.map((s) => s.verb)).toEqual(["thinking", "search", "thinking"]);
    expect(t.trace[0]).toMatchObject({ text: "first thought", tokens: 2 });
    expect(t.trace[2]).toMatchObject({ text: "second", tokens: 1 });
    expect(t.thinking).toBe("first thoughtsecond");
    expect(t.thinkingTokens).toBe(3);
  });

  it("marks cited sources on a stop, and applies their refs", () => {
    const t = newTurn("k", "q?");
    const s1 = { ...src(1), doc_number: null, chunk_number: null };
    const s2 = { ...src(2), doc_number: null, chunk_number: null };
    applyEvent(t, { type: "sources", sources: [s1, s2] });
    applyEvent(t, { type: "token", text: `partial [${s2.handle}]` });
    applyEvent(t, { type: "stopped", message_id: "a1", answer: `partial [${s2.handle}]`,
                    cited: [s2.handle], refs: { [s2.handle]: [1, 1] } });
    expect(t.status).toBe("stopped");
    expect(t.sources.map((s) => s.cited)).toEqual([false, true]);
    expect(t.sources[1]).toMatchObject({ doc_number: 1, chunk_number: 1 });
  });

  it("a stop while queued carries nothing and keeps the turn empty", () => {
    const t = newTurn("k", "q?");
    applyEvent(t, { type: "stopped" });
    expect(t).toMatchObject({ status: "stopped", answer: "", questionId: null });
  });
});

describe("traceFromStored", () => {
  it("rebuilds the plan, auto search, tool searches and listings from what's stored", () => {
    const sources = [src(1, "auto q"), src(2, "auto q"), src(3, "tool q", "tool")];
    const steps = traceFromStored(["auto q", "tool q"], sources,
      { auto_search: true, plan_s: 0.3, search_s: 0.8, listed_documents: 2 });
    expect(steps).toEqual([
      { verb: "planned", text: "auto q", sources: [] },
      { verb: "search", text: "auto q", sources: [handleFor(1), handleFor(2)], foundBy: "auto", seconds: 0.8 },
      { verb: "search", text: "tool q", sources: [handleFor(3)], foundBy: "tool", seconds: undefined },
      { verb: "list", text: "the documents, 2 times", sources: [] },
    ]);
  });

  it("uses auto_queries to tell planned queries from tool queries", () => {
    const sources = [src(1, "a"), src(2, "b"), src(3, "tool q", "tool")];
    const steps = traceFromStored(["a", "b", "tool q"], sources,
      { auto_search: true, plan_s: 0.3, search_s: 0.8, auto_queries: 2 });
    expect(steps[0]).toEqual({ verb: "planned", text: "a · b", sources: [] });
    expect(steps.slice(1)).toEqual([
      { verb: "search", text: "a", sources: [handleFor(1)], foundBy: "auto", seconds: 0.8 },
      { verb: "search", text: "b", sources: [handleFor(2)], foundBy: "auto", seconds: 0.8 },
      { verb: "search", text: "tool q", sources: [handleFor(3)], foundBy: "tool", seconds: undefined },
    ]);
  });

  it("treats a turn stored without auto_queries as having planned one", () => {
    const steps = traceFromStored(["a", "tool q"], [src(1, "a"), src(2, "tool q", "tool")],
      { auto_search: true, plan_s: 0.3, auto_queries: undefined });
    expect(steps[0]).toEqual({ verb: "planned", text: "a", sources: [] });
    expect(steps[2]).toMatchObject({ verb: "search", text: "tool q", foundBy: "tool" });
  });

  it("with no auto search, every query is a tool query", () => {
    const steps = traceFromStored(["x"], [src(1, "x", "tool")], { auto_search: false, plan_s: 0.2 });
    expect(steps[0]).toMatchObject({ verb: "note" });
    expect(steps[1]).toMatchObject({ verb: "search", foundBy: "tool", sources: [handleFor(1)] });
  });

  it("rebuilds a failed plan from the stored stats", () => {
    const steps = traceFromStored(["what are some challenges"], [],
      { auto_search: true, plan_s: 3.3, plan_failed: true });
    expect(steps[0]).toEqual({ verb: "planned", text: "what are some challenges", sources: [], failed: true });
  });

  it("gives a repeated query's passages to each occurrence once", () => {
    const sources = [src(1, "x", "tool"), src(2, "x", "tool")];
    const steps = traceFromStored(["x", "x"], sources, { auto_search: false });
    expect(steps.map((s) => s.sources)).toEqual([[handleFor(1), handleFor(2)], []]);
  });

  it("excludes carried-forward (\"earlier\") sources from the trace -- they weren't retrieved this turn", () => {
    const sources = [{ ...src(1, "x", "auto"), found_by: "earlier" as const }];
    const steps = traceFromStored(["x"], sources, { auto_search: true, plan_s: 0.1 });
    expect(steps[1]).toMatchObject({ sources: [] }); // found_by "earlier" never matches "auto"/"tool"
  });
});

describe("processSummary", () => {
  it("reads like the design", () => {
    const t = newTurn("k", "q");
    applyEvent(t, { type: "searching", query: "a", found_by: "auto" });
    applyEvent(t, { type: "searching", query: "b", found_by: "tool" });
    t.sources = [src(1), { ...src(2), cited: false }];
    t.stats = { total_s: 4.2 };
    expect(processSummary(t)).toBe("2 searches · 1 source cited · 4.2s");
  });
});

describe("applyEvent: citation", () => {
  it("numbers the source it names, so the streaming answer can render it", () => {
    const t = newTurn("k", "q");
    // Mid-stream a newly retrieved source has no number yet.
    applyEvent(t, { type: "sources", sources: [{ ...src(1), doc_number: null, chunk_number: null }] });
    applyEvent(t, { type: "citation", handle: handleFor(1), doc: 2, chunk: 5 });
    expect([t.sources[0].doc_number, t.sources[0].chunk_number]).toEqual([2, 5]);
  });

  it("ignores a handle it has no source for rather than inventing one", () => {
    const t = newTurn("k", "q");
    applyEvent(t, { type: "sources", sources: [src(1)] });
    applyEvent(t, { type: "citation", handle: handleFor(9), doc: 1, chunk: 1 });
    expect(t.sources).toHaveLength(1);
  });
});

describe("conversationDocuments", () => {
  function turnWith(key: string, sources: DoneSource[]) {
    const t = newTurn(key, "q?");
    t.sources = sources;
    t.status = "complete";
    return t;
  }

  it("folds a handle seen in several turns into one passage, opened at the turn that cited it", () => {
    const retrieved = { ...src(1), cited: false, doc_number: null, chunk_number: null };
    const carried = { ...src(1), found_by: "earlier" as const, cited: true, doc_number: 1, chunk_number: 1 };
    const docs = conversationDocuments([turnWith("a1", [retrieved]), turnWith("a2", [carried])]);
    expect(docs).toHaveLength(1);
    expect(docs[0].passages).toHaveLength(1);
    expect(docs[0].passages[0]).toMatchObject({ turnKey: "a2", cited: true, docNumber: 1, chunkNumber: 1 });
    expect(docs[0].citedCount).toBe(1);
    expect(docs[0].turnKeys).toEqual(["a2"]);
  });

  it("groups passages by document and counts the answers that cited it", () => {
    const a = { ...src(1), doc_sha: "shaA", filename: "A.pdf", doc_number: 1, chunk_number: 1 };
    const b = { ...src(2), doc_sha: "shaA", filename: "A.pdf", doc_number: 1, chunk_number: 2 };
    const c = { ...src(3), doc_sha: "shaB", filename: "B.pdf", doc_number: 2, chunk_number: 1 };
    const docs = conversationDocuments([turnWith("a1", [a]), turnWith("a2", [b, c])]);
    expect(docs.map((d) => d.filename)).toEqual(["A.pdf", "B.pdf"]);
    expect(docs[0]).toMatchObject({ docNumber: 1, citedCount: 2 });
    expect(docs[0].turnKeys).toEqual(["a1", "a2"]);
    expect(docs[1]).toMatchObject({ docNumber: 2, citedCount: 1 });
  });

  it("sorts cited documents by [doc], with never-cited ones after them", () => {
    const second = { ...src(1), doc_sha: "shaA", filename: "A.pdf", doc_number: 2, chunk_number: 1 };
    const first = { ...src(2), doc_sha: "shaB", filename: "B.pdf", doc_number: 1, chunk_number: 1 };
    const never = { ...src(3), doc_sha: "shaC", filename: "C.pdf", cited: false, doc_number: null, chunk_number: null };
    const docs = conversationDocuments([turnWith("a1", [second, never, first])]);
    expect(docs.map((d) => d.filename)).toEqual(["B.pdf", "A.pdf", "C.pdf"]);
    expect(docs[2]).toMatchObject({ citedCount: 0, docNumber: null, turnKeys: [] });
  });

  it("puts a document's cited passages ahead of its uncited ones", () => {
    const uncited = { ...src(1), doc_sha: "shaA", chunk_index: 0, cited: false, doc_number: null, chunk_number: null };
    const cited = { ...src(2), doc_sha: "shaA", chunk_index: 9, doc_number: 1, chunk_number: 1 };
    const docs = conversationDocuments([turnWith("a1", [uncited, cited])]);
    expect(docs[0].passages.map((p) => p.cited)).toEqual([true, false]);
  });

  it("is empty for a conversation that never retrieved anything", () => {
    expect(conversationDocuments([turnWith("a1", [])])).toEqual([]);
  });
});

describe("citation-ordered marks", () => {
  function turnWith(key: string, sources: DoneSource[]) {
    const t = newTurn(key, "q?");
    t.sources = sources;
    return t;
  }
  // [1:1] is chunk 9 and [1:2] chunk 3: citation order runs against the document.
  const late = { ...src(1), doc_sha: "shaA", chunk_index: 9, doc_number: 1, chunk_number: 1 };
  const early = { ...src(2), doc_sha: "shaA", chunk_index: 3, doc_number: 1, chunk_number: 2 };
  const uncited = { ...src(3), doc_sha: "shaA", chunk_index: 5, cited: false, doc_number: null, chunk_number: null };

  it("passageMarks keeps citation order, not document order", () => {
    const [doc] = citedDocuments([turnWith("a1", [early, late])]);
    expect(passageMarks(doc.passages)).toEqual([{ chunkIndex: 9, label: "[1:1]" }, { chunkIndex: 3, label: "[1:2]" }]);
  });

  it("citedDocuments drops uncited passages and never-cited documents", () => {
    const other = { ...uncited, handle: handleFor(4), doc_sha: "shaB" };
    const docs = citedDocuments([turnWith("a1", [late, uncited, other])]);
    expect(docs.map((d) => d.docSha)).toEqual(["shaA"]);
    expect(docs[0].passages.map((p) => p.chunkIndex)).toEqual([9]);
  });

  it("documentMarks adds the passage it was opened on, last, when it isn't cited", () => {
    const turns = [turnWith("a1", [late, early, uncited])];
    expect(documentMarks(turns, "shaA", 5).map((m) => m.chunkIndex)).toEqual([9, 3, 5]);
    expect(documentMarks(turns, "shaA", 3).map((m) => m.chunkIndex)).toEqual([9, 3]);
  });
});

describe("usd", () => {
  it("keeps two significant digits below a cent, cents above", () => {
    expect(usd(0)).toBe("$0");
    expect(usd(0.00000042)).toBe("<$0.0001");
    expect(usd(0.004213)).toBe("$0.0042");
    expect(usd(0.0567)).toBe("$0.06");
    expect(usd(1.5)).toBe("$1.50");
  });
});

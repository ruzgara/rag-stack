import { describe, expect, it } from "vitest";
import { toTurns, deriveInvalid, applyEvent, newTurn, traceFromStored, processSummary } from "../lib/turns";
import type { DoneSource, Message } from "../lib/types";

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
  // Mirrors chat.py:resolve_citations' split into known/unknown citation numbers.
  it.each([
    ["Answer with [1] and [2].", [1], [2]],
    ["Uses [1-3] but only source 1 exists.", [1], [2, 3]],
    ["No citations here.", [1], []],
    ["Cites [1] twice, [1] again.", [1], []],
  ])("%s", (answer, knownNumbers, expectedInvalid) => {
    const sources = knownNumbers.map((n) => src(n));
    expect(deriveInvalid(answer, sources)).toEqual(expectedInvalid);
  });
});

describe("applyEvent", () => {
  it("builds the trace and attaches each sources event to its search", () => {
    const t = newTurn("k", "q?");
    applyEvent(t, { type: "queued", position: 2 });
    expect(t.queuePosition).toBe(2);
    applyEvent(t, { type: "user", id: "u1" });
    expect(t.queuePosition).toBeNull();
    applyEvent(t, { type: "rewrite", query: "standalone q", search: true });
    applyEvent(t, { type: "searching", query: "standalone q", found_by: "auto" });
    applyEvent(t, { type: "sources", sources: [src(1), src(2)] });
    applyEvent(t, { type: "listing", document: "" });
    applyEvent(t, { type: "searching", query: "more", found_by: "tool" });
    applyEvent(t, { type: "sources", sources: [src(2), src(3, "more", "tool")] }); // 2 already known
    expect(t.trace.map((s) => s.verb)).toEqual(["rewrote", "search", "list", "search"]);
    expect(t.trace[1].sources).toEqual([1, 2]);
    expect(t.trace[3].sources).toEqual([2, 3]);
    expect(t.sources.map((s) => s.number)).toEqual([1, 2, 3]);
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

  it("marks cited sources on a stop", () => {
    const t = newTurn("k", "q?");
    applyEvent(t, { type: "sources", sources: [src(1), src(2)] });
    applyEvent(t, { type: "token", text: "partial [2]" });
    applyEvent(t, { type: "stopped", message_id: "a1", answer: "partial [2]", cited: [2] });
    expect(t.status).toBe("stopped");
    expect(t.sources.map((s) => s.cited)).toEqual([false, true]);
  });

  it("a stop while queued carries nothing and keeps the turn empty", () => {
    const t = newTurn("k", "q?");
    applyEvent(t, { type: "stopped" });
    expect(t).toMatchObject({ status: "stopped", answer: "", questionId: null });
  });
});

describe("traceFromStored", () => {
  it("rebuilds rewrite, auto search, tool searches and listings from what's stored", () => {
    const sources = [src(1, "auto q"), src(2, "auto q"), src(3, "tool q", "tool")];
    const steps = traceFromStored(["auto q", "tool q"], sources,
      { auto_search: true, rewrite_s: 0.3, search_s: 0.8, listed_documents: 2 });
    expect(steps).toEqual([
      { verb: "rewrote", text: "auto q", sources: [] },
      { verb: "search", text: "auto q", sources: [1, 2], foundBy: "auto", seconds: 0.8 },
      { verb: "search", text: "tool q", sources: [3], foundBy: "tool", seconds: undefined },
      { verb: "list", text: "the documents, 2 times", sources: [] },
    ]);
  });

  it("with no auto search, every query is a tool query", () => {
    const steps = traceFromStored(["x"], [src(1, "x", "tool")], { auto_search: false, rewrite_s: 0.2 });
    expect(steps[0]).toMatchObject({ verb: "note" });
    expect(steps[1]).toMatchObject({ verb: "search", foundBy: "tool", sources: [1] });
  });

  it("gives a repeated query's passages to each occurrence once", () => {
    const sources = [src(1, "x", "tool"), src(2, "x", "tool")];
    const steps = traceFromStored(["x", "x"], sources, { auto_search: false });
    expect(steps.map((s) => s.sources)).toEqual([[1, 2], []]);
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

import { describe, expect, it } from "vitest";
import { toTurns, deriveInvalid } from "../lib/turns";
import type { DoneSource, Message } from "../lib/types";

function src(number: number): DoneSource {
  return {
    number,
    found_by: "auto",
    query: "q",
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
      key: "u1",
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
    const sources = knownNumbers.map(src);
    expect(deriveInvalid(answer, sources)).toEqual(expectedInvalid);
  });
});

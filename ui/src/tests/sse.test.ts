import { describe, expect, it } from "vitest";
import { SseParser, type SseFrame } from "../lib/sse";

// One recorded byte stream covering: all 12 rag-stack event types, a
// :keep-alive comment line, a synthetic multi-line `data:` frame, CRLF line
// endings on some frames and LF on others, and a final frame with NO
// trailing blank line (which flush() must still recover). Fed through the
// parser chunked at 1, 3, 7 bytes and whole-frame; all four chunkings must
// produce an identical event sequence. This is the highest-value test in
// the suite -- it catches every chunk-boundary bug in one shot, including
// the lone-\r hold-back.

const FRAMES: [string, string][] = [
  ["queued", '{"position":1}'],
  ["user", '{"id":"m1"}'],
  ["plan", '{"queries":["q"],"search":true}'],
  ["searching", '{"query":"q","found_by":"auto"}'],
  ["sources", '{"sources":[]}'],
  ["listing", '{"document":""}'],
  ["listing_libraries", "{}"],
  ["thinking", '{"text":"hmm"}'],
  ["token", '{"text":"ab"}'],
  ["done", '{"message_id":"m2","answer":"ans","model":"llama","preset":"everyday","search_queries":[],"cited":[1],"invalid_citations":[],"stats":{},"sources":[]}'],
  ["title", '{"title":"t"}'],
  ["error", '{"error":"boom"}'],
];

function buildStream(): string {
  let out = "";
  // First half with CRLF endings, second half with LF -- both must parse
  // identically after normalization.
  FRAMES.forEach(([event, data], i) => {
    const nl = i < FRAMES.length / 2 ? "\r\n" : "\n";
    out += `event: ${event}${nl}data: ${data}${nl}${nl}`;
  });
  // A keep-alive comment line, alone, is not a frame.
  out += ":keep-alive\n\n";
  // A synthetic multi-line `data:` frame: two data: lines join with \n.
  out += "event: note\ndata: line one\ndata: line two\n\n";
  // Final frame with NO trailing blank line -- only flush() recovers this.
  out += "event: trailing\ndata: {\"x\":1}";
  return out;
}

function chunk(text: string, size: number): string[] {
  if (size <= 0) return [text];
  const out: string[] = [];
  for (let i = 0; i < text.length; i += size) out.push(text.slice(i, i + size));
  return out;
}

function parseAll(text: string, chunkSize: number): SseFrame[] {
  const parser = new SseParser();
  const frames: SseFrame[] = [];
  for (const c of chunk(text, chunkSize)) frames.push(...parser.push(c));
  frames.push(...parser.flush());
  return frames;
}

describe("SseParser", () => {
  const stream = buildStream();
  const expected = parseAll(stream, 0); // whole-frame, single push

  it("produces the expected 14 frames for a whole-frame push", () => {
    expect(expected).toHaveLength(14); // 12 named + note + trailing (keep-alive is not a frame)
    expect(expected.map((f) => f.event)).toEqual([
      "queued", "user", "plan", "searching", "sources", "listing",
      "listing_libraries", "thinking", "token", "done", "title", "error",
      "note", "trailing",
    ]);
  });

  it("joins multi-line data with \\n", () => {
    const note = expected.find((f) => f.event === "note");
    expect(note?.data).toBe("line one\nline two");
  });

  it("recovers the final frame with no trailing blank line via flush()", () => {
    const trailing = expected.find((f) => f.event === "trailing");
    expect(trailing?.data).toBe('{"x":1}');
  });

  it("ignores a :keep-alive comment line", () => {
    expect(expected.some((f) => f.data.includes("keep-alive"))).toBe(false);
  });

  for (const size of [1, 3, 7]) {
    it(`is identical when chunked at ${size} byte(s)`, () => {
      expect(parseAll(stream, size)).toEqual(expected);
    });
  }

  it("holds back a lone trailing \\r across a chunk boundary", () => {
    // "a\r" | "\nb\r\n\r\n" -- the \r\n blank line must not be missed, and
    // must not be double-counted, because of where the chunk was cut.
    const parser = new SseParser();
    const f1 = parser.push("event: x\r");
    expect(f1).toHaveLength(0);
    const f2 = parser.push("\ndata: y\r\n\r\n");
    expect(f2).toHaveLength(1);
    expect(f2[0]).toEqual({ event: "x", data: "y" });
  });

  it("defaults event to 'message' when omitted", () => {
    const parser = new SseParser();
    const frames = parser.push("data: no-event-field\n\n");
    expect(frames).toEqual([{ event: "message", data: "no-event-field" }]);
  });

  it("treats a UTF-8 multi-byte character split across chunks correctly upstream", () => {
    // SseParser itself operates on already-decoded strings; the
    // TextDecoder({stream:true}) split-handling lives in stream.ts. This
    // test just confirms a decoded multi-byte string round-trips as data.
    const parser = new SseParser();
    const frames = parser.push('event: token\ndata: {"text":"café"}\n\n');
    expect(frames[0].data).toBe('{"text":"café"}');
  });
});

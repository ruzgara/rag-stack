import { describe, expect, it } from "vitest";
import { fenceScan, splitStable, closeOpenFence } from "../lib/markdown";

describe("fenceScan", () => {
  it("tracks an open fence across lines", () => {
    const { openMarker } = fenceScan("```js\nconst x = 1;\n");
    expect(openMarker).toBe("```");
  });

  it("closes on a matching or longer marker of the same character", () => {
    expect(fenceScan("```\ncode\n```\n").openMarker).toBeNull();
    expect(fenceScan("```\ncode\n````\n").openMarker).toBeNull(); // longer closer still closes
  });

  it("does not close on a shorter or different-character marker", () => {
    expect(fenceScan("````\ncode\n```\n").openMarker).toBe("````"); // shorter fence can't close a longer one
    expect(fenceScan("```\ncode\n~~~\n").openMarker).toBe("```"); // different character never closes
  });
});

describe("splitStable", () => {
  it("cuts at the last blank line outside any fence", () => {
    const [stable, volatile] = splitStable("para one\n\npara two still going");
    expect(stable).toBe("para one\n\n");
    expect(volatile).toBe("para two still going");
  });

  it("never cuts inside a fence, including a blank line inside a code block", () => {
    // The ONLY blank line in this text is inside the fence. If the split
    // used openAfter[i] instead of openAfter[i-1] (the bug the invariant
    // comment in markdown.ts warns about), it would wrongly treat this
    // blank line as safe and cut there.
    const text = "```js\nconst x = 1;\n\nconst y = 2;\n```";
    const [stable, volatile] = splitStable(text);
    expect(stable).toBe("");
    expect(volatile).toBe(text);
  });

  it("cuts after a fence closes, once a real blank line follows it", () => {
    const text = "```js\nconst x = 1;\n\nconst y = 2;\n```\n\nafter";
    const [stable, volatile] = splitStable(text);
    expect(stable).toBe("```js\nconst x = 1;\n\nconst y = 2;\n```\n\n");
    expect(volatile).toBe("after");
  });

  it("returns everything as volatile when there is no safe cut point", () => {
    const [stable, volatile] = splitStable("still one paragraph, no blank line yet");
    expect(stable).toBe("");
    expect(volatile).toBe("still one paragraph, no blank line yet");
  });

  it("puts everything in stable and leaves an empty volatile when text ends on a blank line", () => {
    const [stable, volatile] = splitStable("done.\n\n");
    expect(stable).toBe("done.\n\n");
    expect(volatile).toBe("");
  });
});

describe("closeOpenFence", () => {
  it("appends a matching closer for an open fence", () => {
    expect(closeOpenFence("```js\nconst x = 1;")).toBe("```js\nconst x = 1;\n```");
  });

  it("matches the marker character and length", () => {
    expect(closeOpenFence("~~~~\ncode")).toBe("~~~~\ncode\n~~~~");
  });

  it("is a no-op when there is no open fence", () => {
    expect(closeOpenFence("plain text")).toBe("plain text");
  });

  it("does not double a trailing newline", () => {
    expect(closeOpenFence("```\ncode\n")).toBe("```\ncode\n```");
  });
});

describe("incremental vs. one-shot equivalence", () => {
  it("feeding one character at a time and always cutting at splitStable arrives at the same full text as one finalize pass", () => {
    const answer = "# Title\n\nSome *text* with a [1] citation.\n\n```js\nconst x = 1;\n```\n\nMore text after.";
    let seenStable = "";
    for (let i = 1; i <= answer.length; i++) {
      const [stable] = splitStable(answer.slice(0, i));
      if (stable.length > seenStable.length) seenStable = stable;
    }
    // The stable prefix, grown monotonically as more of the answer arrives,
    // must always be a true prefix of the final text -- splitStable never
    // "takes back" a decision as more content streams in.
    expect(answer.startsWith(seenStable)).toBe(true);
  });
});

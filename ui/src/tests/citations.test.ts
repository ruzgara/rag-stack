import { describe, expect, it } from "vitest";
import { parseBracket, parseCitations, buildCiteMap, linkifyCitations, trimPartialCitation } from "../lib/citations";

// This table is duplicated verbatim in services/query/tests/test_citations.py
// (HANDLE_GRAMMAR + REF_GRAMMAR) -- a mirror divergence between the two
// grammars should fail a test here or there, not wait for review to notice.
const HANDLE_GRAMMAR: [string, ["handle", string][]][] = [
  ["[Sk29fp]", [["handle", "Sk29fp"]]],
  ["[Sk29fp][Sm4p2q]", [["handle", "Sk29fp"], ["handle", "Sm4p2q"]]],
  ["[Sk29fp, Sm4p2q]", [["handle", "Sk29fp"], ["handle", "Sm4p2q"]]],
  ["[Sk29fp; Sm4p2q]", [["handle", "Sk29fp"], ["handle", "Sm4p2q"]]],
];

const REF_GRAMMAR: [string, ["ref", [number, number]][]][] = [
  ["[1:3]", [["ref", [1, 3]]]],
  ["[1:3][2:4]", [["ref", [1, 3]], ["ref", [2, 4]]]],
  ["[1:3, 2:4]", [["ref", [1, 3]], ["ref", [2, 4]]]],
  ["[1:3; 2:4]", [["ref", [1, 3]], ["ref", [2, 4]]]],
  ["[1:3,4]", [["ref", [1, 3]], ["ref", [1, 4]]]],
  ["[1:3-5]", [["ref", [1, 3]], ["ref", [1, 4]], ["ref", [1, 5]]]],
  ["[2;4]", [["ref", [2, 4]]]],
  ["[2:4;3,5]", [["ref", [2, 4]], ["ref", [3, 5]]]],
];

function flatten(text: string): unknown[] {
  const out: unknown[] = [];
  for (const m of text.matchAll(/\[([^[\]]*)\]/g)) {
    const items = parseBracket(m[1]);
    expect(items, `${text} did not parse as a citation`).not.toBeNull();
    for (const item of items ?? []) {
      if (item.handle !== undefined) out.push(["handle", item.handle]);
      else if (item.ref !== undefined) out.push(["ref", item.ref]);
      else out.push(["raw", item.raw]);
    }
  }
  return out;
}

describe("grammar table", () => {
  it.each([...HANDLE_GRAMMAR, ...REF_GRAMMAR])("%s", (text, expected) => {
    expect(flatten(text)).toEqual(expected);
  });

  it("[1:3; 4]: the orphan bare number is an invalid marker, not dropped", () => {
    expect(flatten("[1:3; 4]")).toEqual([["ref", [1, 3]], ["raw", "4"]]);
  });

  it("[1-2:3]: a doc-side range is invalid -- ranges are chunk-only", () => {
    expect(flatten("[1-2:3]")).toEqual([["raw", "1-2:3"]]);
  });

  it("caps a chunk range at 50 wide (lo + 50)", () => {
    const items = parseBracket("1:3-9999");
    expect(items).toHaveLength(51);
    expect(items?.[50].ref).toEqual([1, 53]);
  });

  it("handles are case-insensitive", () => {
    expect(flatten("[SK29FP]")).toEqual([["handle", "Sk29fp"]]);
  });

  it("Scoped/Sample are not handles -- no digit in position 3", () => {
    expect(parseBracket("Scoped")).toBeNull();
    expect(parseBracket("Sample")).toBeNull();
  });

  it("a lone number is literal text, not a citation", () => {
    expect(parseBracket("2024")).toBeNull();
    expect(parseBracket("3")).toBeNull();
    expect(parseBracket(" 2 ")).toBeNull();
  });

  it("ordinary prose in brackets is not a citation", () => {
    expect(parseBracket("a")).toBeNull();
    expect(parseBracket("see")).toBeNull();
    expect(parseBracket("")).toBeNull();
  });

  it("items are deduped by first appearance", () => {
    const items = parseBracket("1:3, 1:3, 1:4");
    expect(items?.map((i) => i.ref)).toEqual([[1, 3], [1, 4]]);
  });
});

describe("parseCitations", () => {
  it("finds citation spans and skips literal brackets", () => {
    const text = "See [Sk29fp] and also [2024].pdf, plus [1:3].";
    const cites = parseCitations(text);
    expect(cites).toHaveLength(2);
    expect(cites[0].text).toBe("[Sk29fp]");
    expect(cites[0].items[0].handle).toBe("Sk29fp");
    expect(cites[1].text).toBe("[1:3]");
    expect(cites[1].items[0].ref).toEqual([1, 3]);
  });
});

describe("linkifyCitations", () => {
  function render(html: string): HTMLDivElement {
    const div = document.createElement("div");
    div.innerHTML = html;
    return div;
  }

  const known = buildCiteMap([{ handle: "Sk29fp", doc_number: 1, chunk_number: 3 }]);

  it("linkifies a known handle as its [doc:chunk]", () => {
    const root = render("<p>see [Sk29fp] here</p>");
    linkifyCitations(root, known, false);
    const span = root.querySelector(".cite");
    expect(span?.textContent).toBe("[1:3]");
    expect(span?.getAttribute("data-handle")).toBe("Sk29fp");
    expect(span?.classList.contains("invalid")).toBe(false);
  });

  it("linkifies a resolvable [doc:chunk] ref the same way", () => {
    const root = render("<p>see [1:3] here</p>");
    linkifyCitations(root, known, false);
    const span = root.querySelector(".cite");
    expect(span?.textContent).toBe("[1:3]");
    expect(span?.getAttribute("data-handle")).toBe("Sk29fp");
  });

  it("carries every passage in a range, one span each", () => {
    const map = buildCiteMap([
      { handle: "Sk29fp", doc_number: 1, chunk_number: 3 },
      { handle: "Sm4p2q", doc_number: 1, chunk_number: 4 },
    ]);
    const root = render("<p>[1:3-4]</p>");
    linkifyCitations(root, map, false);
    const spans = [...root.querySelectorAll(".cite")];
    expect(spans.map((s) => s.textContent)).toEqual(["[1:3]", "[1:4]"]);
  });

  it("leaves a citation-shaped string inside <code> untouched", () => {
    const root = render("<p>call <code>arr[Sk29fp]</code></p>");
    linkifyCitations(root, known, false);
    expect(root.querySelector("code")?.textContent).toBe("arr[Sk29fp]");
    expect(root.querySelector(".cite")).toBeNull();
  });

  it("leaves a citation-shaped string inside an <a href> untouched", () => {
    const root = render('<p><a href="/x">Report [2024]</a></p>');
    linkifyCitations(root, known, false);
    expect(root.querySelector("a")?.textContent).toBe("Report [2024]");
    expect(root.querySelector(".cite")).toBeNull();
  });

  it("does not corrupt a filename that merely looks like a citation", () => {
    const root = render("<p>Report [2024].pdf was cited</p>");
    linkifyCitations(root, known, false);
    expect(root.textContent).toBe("Report [2024].pdf was cited");
  });

  it("marks an unresolved ref as invalid without a data-handle", () => {
    const root = render("<p>[9:9]</p>");
    linkifyCitations(root, known, false);
    const span = root.querySelector(".cite");
    expect(span?.classList.contains("invalid")).toBe(true);
    expect(span?.hasAttribute("data-handle")).toBe(false);
    expect(span?.textContent).toBe("[9:9]"); // plain digits: safe to show
  });

  it("marks an unresolved handle as invalid WITHOUT ever showing the handle", () => {
    // Sz29zz is handle-shaped (passes HANDLE_RE) but not in `known` -- the
    // garbled-handle case the redesign exists to make visible, not silent.
    const root = render("<p>[Sz29zz]</p>");
    linkifyCitations(root, known, false);
    const span = root.querySelector(".cite");
    expect(span?.classList.contains("invalid")).toBe(true);
    expect(span?.hasAttribute("data-handle")).toBe(false);
    expect(span?.textContent).not.toContain("Sz29zz");
  });

  it("while pending, a handle the server has numbered still renders as its [doc:chunk]", () => {
    // Numbers arrive on their own `citation` events as each bracket closes,
    // so mid-stream is the normal case for a resolvable citation, not a
    // reason to withhold it.
    const root = render("<p>[Sk29fp]</p>");
    linkifyCitations(root, known, true);
    const span = root.querySelector(".cite");
    expect(span?.textContent).toBe("[1:3]");
    expect(span?.classList.contains("pending")).toBe(false);
  });

  it("while pending, an unnumbered handle is a neutral marker, never the handle", () => {
    const root = render("<p>[Sm4p2q]</p>");
    linkifyCitations(root, known, true);
    const span = root.querySelector(".cite.pending");
    expect(span?.textContent).not.toContain("Sm4p2q");
    expect(span?.hasAttribute("data-handle")).toBe(false);
  });

  it("the same unnumbered handle is invalid, not pending, once the answer is done", () => {
    const root = render("<p>[Sm4p2q]</p>");
    linkifyCitations(root, known, false);
    const span = root.querySelector(".cite.invalid");
    expect(span?.textContent).not.toContain("Sm4p2q");
  });

  it("holds back a bracket still being typed so its handle never flashes", () => {
    expect(trimPartialCitation("The floor is 100% [Sk29")).toBe("The floor is 100% ");
    expect(trimPartialCitation("The floor is 100% [Sk29fp]")).toBe("The floor is 100% [Sk29fp]");
    // Past the hold limit a '[' was never a citation, so prose keeps it.
    const prose = `see [${"x".repeat(80)}`;
    expect(trimPartialCitation(prose)).toBe(prose);
  });

  it("never builds markup by string concatenation (no attribute injection)", () => {
    const root = render("<p>[Sk29fp]</p>");
    linkifyCitations(root, known, false);
    const span = root.querySelector(".cite")!;
    expect(span.innerHTML).toBe("[1:3]"); // textContent, not innerHTML, was used to populate it
  });
});

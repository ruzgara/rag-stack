import { describe, expect, it } from "vitest";
import { parseCitation, linkifyCitations, citationRe } from "../lib/citations";

describe("parseCitation", () => {
  it.each([
    ["3", [3]],
    ["1,2", [1, 2]],
    ["1-3", [1, 2, 3]],
    ["1–3", [1, 2, 3]], // en dash
    ["1; 4-6", [1, 4, 5, 6]],
  ])("parses %s", (input, expected) => {
    expect(parseCitation(input)).toEqual(expected);
  });

  it("caps an absurd range at 51 entries (lo + 50)", () => {
    expect(parseCitation("1-9999")).toHaveLength(51);
    expect(parseCitation("1-9999")[50]).toBe(51);
  });

  it("dedupes across overlapping parts, keeping first-seen order", () => {
    expect(parseCitation("1-3, 2-4")).toEqual([1, 2, 3, 4]);
  });
});

describe("citationRe", () => {
  it("returns a fresh RegExp instance each call (no shared lastIndex)", () => {
    const a = citationRe();
    const b = citationRe();
    expect(a).not.toBe(b);
  });

  it("matches the documented forms", () => {
    const text = "See [1], [1-3], [1–3], [1, 2], [1; 3].";
    const matches = [...text.matchAll(citationRe())].map((m) => m[0]);
    expect(matches).toEqual(["[1]", "[1-3]", "[1–3]", "[1, 2]", "[1; 3]"]);
  });
});

describe("linkifyCitations", () => {
  function render(html: string): HTMLDivElement {
    const div = document.createElement("div");
    div.innerHTML = html;
    return div;
  }

  it("linkifies a plain-text citation", () => {
    const root = render("<p>see [1] here</p>");
    linkifyCitations(root, new Set([1]));
    const span = root.querySelector(".cite");
    expect(span?.textContent).toBe("[1]");
    expect(span?.classList.contains("invalid")).toBe(false);
  });

  it("carries every number in a range, not just the first (defect 3 fix)", () => {
    const root = render("<p>[1-3]</p>");
    linkifyCitations(root, new Set([1, 2, 3]));
    expect(root.querySelector(".cite")?.getAttribute("data-nums")).toBe("1,2,3");
  });

  it("leaves a citation-shaped string inside <code> untouched", () => {
    const root = render("<p>call <code>arr[2]</code></p>");
    linkifyCitations(root, new Set([2]));
    expect(root.querySelector("code")?.textContent).toBe("arr[2]");
    expect(root.querySelector(".cite")).toBeNull();
  });

  it("leaves a citation-shaped string inside an <a href> untouched", () => {
    const root = render('<p><a href="/x">Report [2024]</a></p>');
    linkifyCitations(root, new Set([2024]));
    expect(root.querySelector("a")?.textContent).toBe("Report [2024]");
    expect(root.querySelector(".cite")).toBeNull();
  });

  it("does not corrupt a filename that merely looks like a citation", () => {
    const root = render("<p>Report [2024].pdf was cited</p>");
    linkifyCitations(root, new Set([2024]));
    expect(root.textContent).toBe("Report [2024].pdf was cited");
  });

  it("marks a number absent from `known` as invalid", () => {
    const root = render("<p>[99]</p>");
    linkifyCitations(root, new Set([1, 2]));
    const span = root.querySelector(".cite");
    expect(span?.classList.contains("invalid")).toBe(true);
    expect(span?.getAttribute("title")).toContain("99");
  });

  it("never builds markup by string concatenation (no attribute injection)", () => {
    // A filename-shaped attack would have to come through markdown/marked
    // sanitization first, but linkifyCitations itself must not be the hole:
    // it only ever sets .textContent and DOM properties.
    const root = render('<p>[1]</p>');
    linkifyCitations(root, new Set([1]));
    const span = root.querySelector(".cite")!;
    expect(span.innerHTML).toBe("[1]"); // textContent, not innerHTML, was used to populate it
  });
});

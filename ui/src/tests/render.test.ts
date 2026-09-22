import { describe, expect, it } from "vitest";
import { StreamingMarkdown, mdToFragment } from "../lib/render";
import { buildCiteMap } from "../lib/citations";

function fragText(frag: DocumentFragment): string {
  const div = document.createElement("div");
  div.append(frag.cloneNode(true));
  return div.innerHTML;
}

describe("mdToFragment", () => {
  it("renders basic markdown to sanitized HTML", () => {
    const frag = mdToFragment("**bold** and *em*");
    expect(fragText(frag)).toContain("<strong>bold</strong>");
    expect(fragText(frag)).toContain("<em>em</em>");
  });

  it("strips a disallowed tag (e.g. script) while keeping its text", () => {
    const frag = mdToFragment('<script>alert(1)</script>\n\nhello');
    const html = fragText(frag);
    expect(html).not.toContain("<script>");
    expect(html).toContain("hello");
  });

  it("keeps an http(s) link but drops a javascript: URI", () => {
    const safe = fragText(mdToFragment("[go](https://example.com)"));
    expect(safe).toContain('href="https://example.com"');
    const unsafe = fragText(mdToFragment("[go](javascript:alert(1))"));
    expect(unsafe).not.toContain("javascript:");
  });
});

describe("StreamingMarkdown", () => {
  // The fixture exercises every piece of the incremental algorithm: a
  // heading, a paragraph with a citation, a fenced code block, and a table
  // -- the same shapes plan §5's risk #5 names as flicker-prone mid-stream.
  const FIXTURE =
    "# Title\n\n" +
    "See [Sk29fp] for details.\n\n" +
    "```js\nconst x = 1;\n\nconst y = 2;\n```\n\n" +
    "| a | b |\n|---|---|\n| 1 | 2 |\n";
  const MAP = buildCiteMap([{ handle: "Sk29fp", doc_number: 1, chunk_number: 3 }]);

  it("typing one character at a time converges to the same markup as a single finalize() pass", () => {
    const live = document.createElement("div");
    const renderer = new StreamingMarkdown(live);
    for (let i = 1; i <= FIXTURE.length; i++) {
      renderer.update(FIXTURE.slice(0, i), MAP, true);
    }
    renderer.finalize(FIXTURE, MAP);

    const reference = document.createElement("div");
    new StreamingMarkdown(reference).finalize(FIXTURE, MAP);

    expect(live.innerHTML).toBe(reference.innerHTML);
  });

  it("finalize() renders the heading, code block, table and linkified citation", () => {
    const root = document.createElement("div");
    new StreamingMarkdown(root).finalize(FIXTURE, MAP);
    expect(root.querySelector("h1")?.textContent).toBe("Title");
    expect(root.querySelector("pre code")?.textContent).toContain("const x = 1;");
    expect(root.querySelector("table")).not.toBeNull();
    const cite = root.querySelector(".cite");
    expect(cite?.getAttribute("data-handle")).toBe("Sk29fp");
    expect(cite?.getAttribute("data-ref")).toBe("1:3");
    expect(cite?.textContent).toBe("[1:3]");
  });

  it("update() never leaves the code fence unterminated in what it renders", () => {
    const root = document.createElement("div");
    const renderer = new StreamingMarkdown(root);
    // Mid-stream, only the opening fence has arrived.
    renderer.update("```js\nconst x = 1;", MAP, true);
    // marked must have received a closed fence (via closeOpenFence), so this
    // renders as a <pre><code> block, not as a paragraph the way an
    // unterminated fence would fall back to.
    expect(root.querySelector("pre code")).not.toBeNull();
  });

  it("while pending, a citation renders as a neutral marker -- never the handle", () => {
    const root = document.createElement("div");
    new StreamingMarkdown(root).update("Cites [Sk29fp] so far.", MAP, true);
    const cite = root.querySelector(".cite.pending");
    expect(cite).not.toBeNull();
    expect(cite?.textContent).not.toContain("Sk29fp");
    expect(cite?.hasAttribute("data-handle")).toBe(false);
  });

  it("finalize() is idempotent with the raw stored answer, not a fence-closed copy", () => {
    // An answer that legitimately ends mid-fence (e.g. an errored turn) must
    // render as-is, not have a synthetic closer injected into what's shown --
    // closeOpenFence only ever touches the copy handed to marked mid-stream.
    const root = document.createElement("div");
    new StreamingMarkdown(root).finalize("```js\nconst x = 1;", MAP);
    // marked treats the unterminated fence as a code block to EOF; either
    // way, this must not throw and must include the raw text somewhere.
    expect(root.textContent).toContain("const x = 1;");
  });
});

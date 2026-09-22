// marked (parse) + DOMPurify (sanitize to a fragment) + the stable/volatile
// split from markdown.ts. See plan §5 for the full algorithm and why this
// combination was chosen over a purpose-built streaming-markdown library.

import { marked } from "marked";
import DOMPurify from "dompurify";
import { splitStable, closeOpenFence } from "./markdown";
import { linkifyCitations, type CiteMap } from "./citations";

marked.setOptions({ gfm: true, breaks: false });

const PURIFY_CONFIG = {
  ALLOWED_TAGS: [
    "p", "br", "hr", "strong", "em", "del", "code", "pre", "blockquote",
    "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "thead", "tbody", "tr", "th", "td", "a", "span",
  ],
  ALLOWED_ATTR: ["href", "title", "colspan", "rowspan", "align", "start"],
  ALLOWED_URI_REGEXP: /^(?:https?:|mailto:|#)/i,
};

export function mdToFragment(md: string): DocumentFragment {
  const html = marked.parse(md, { async: false }) as string;
  return DOMPurify.sanitize(html, {
    ...PURIFY_CONFIG,
    RETURN_DOM_FRAGMENT: true,
  }) as unknown as DocumentFragment;
}

/** Binds an incremental render to one element. Per animation frame it
 *  re-parses only the paragraph currently being written; the stable prefix
 *  re-parses only when it grows. */
export class StreamingMarkdown {
  private stableLen = -1;
  private stable = document.createElement("div");
  private tail = document.createElement("div");

  constructor(private root: HTMLElement) {
    root.replaceChildren(this.stable, this.tail);
  }

  update(raw: string, map: CiteMap, pending: boolean): void {
    const [stable, volatile] = splitStable(raw);

    if (stable.length !== this.stableLen) {
      // Fires once per completed block, not per token. Re-parsing the whole
      // stable prefix (rather than appending only the new slice) costs one
      // parse per block and avoids artifacts like "1.\n\n2." becoming two
      // separate <ol>s each starting at 1.
      this.stableLen = stable.length;
      const frag = mdToFragment(stable);
      linkifyCitations(frag, map, pending);
      this.stable.replaceChildren(frag);
    }

    if (!volatile) {
      // raw ends on a blank line: splitStable puts everything in `stable`.
      // Parsing "" every frame would be pure waste.
      this.tail.replaceChildren();
      return;
    }

    const frag = mdToFragment(closeOpenFence(volatile));
    linkifyCitations(frag, map, pending);
    this.tail.replaceChildren(frag);
  }

  /** `done` carries the authoritative answer. One clean parse of the whole
   *  thing, so nothing the incremental pass got wrong survives -- a table
   *  that grew a column, a list split across the stable boundary, a late
   *  link reference. Terminal: this instance is one-per-turn (Markdown.svelte
   *  makes a fresh StreamingMarkdown per Turn), so update() is never called
   *  again after finalize() replaces root's children with a flat fragment. */
  finalize(answer: string, map: CiteMap): void {
    this.stableLen = -1;
    const frag = mdToFragment(answer);
    linkifyCitations(frag, map, false);
    this.root.replaceChildren(frag);
  }
}

// Citation parsing and DOM linkification. Verified against
// services/query/chat.py:91-93 (CITATION_RE) and chat.py:207-218
// (resolve_citations). The server's `cited` set is computed with that exact
// regex and that exact 50-wide range cap; a looser client regex would light
// up sources the server never marked cited. MIRRORS chat.py -- change both
// files together or neither.

const ITEM = String.raw`\d+(?:\s*[-–]\s*\d+)?`;
const SOURCE = String.raw`\[(${ITEM}(?:\s*[,;]\s*${ITEM})*)\]`;

/** A FACTORY, never a shared constant. A /g regex carries mutable lastIndex,
 *  and both linkifyCitations and turns.ts:deriveInvalid drive it -- a shared
 *  instance interleaved on one tick would silently skip matches. Unit tests
 *  would never catch that (each test gets a fresh call); only the running
 *  app would. */
export const citationRe = (): RegExp => new RegExp(SOURCE, "g");

/** "1,3" -> [1,3]; "1-3" -> [1,2,3]; "1-3; 7" -> [1,2,3,7].
 *  Mirrors chat.py:211-217, including `min(hi, lo + 50)` and first-seen
 *  ordering. Note: like the Python, there is deliberately NO `lo >= 1` guard
 *  -- "[0]" resolves to [0] on both sides, so both flag it as invalid. */
export function parseCitation(inner: string): number[] {
  const out: number[] = [];
  for (const part of inner.split(/\s*[,;]\s*/)) {
    const bounds = part.split(/\s*[-–]\s*/).map(Number);
    const lo = bounds[0];
    const hi = bounds[bounds.length - 1];
    if (!Number.isFinite(lo) || !Number.isFinite(hi)) continue;
    for (let n = lo; n <= Math.min(hi, lo + 50); n++) {
      if (!out.includes(n)) out.push(n);
    }
  }
  return out;
}

const SKIP = new Set(["CODE", "PRE", "A", "KBD", "SAMP"]);

function skipped(el: Element, root: ParentNode): boolean {
  for (let c: Element | null = el; c && (c as Node) !== root; c = c.parentElement) {
    if (SKIP.has(c.tagName)) return true;
  }
  return false;
}

/** Replaces [n] with <span class="cite" data-nums="1,2,3"> in TEXT NODES
 *  ONLY, so `arr[1]` in a code block and a [2] inside an href are untouched.
 *  Every node is built with createElement/textContent -- nothing is ever
 *  concatenated into markup, so test-ui.html:254's title="" injection hole
 *  cannot recur by construction. */
export function linkifyCitations(root: ParentNode, known: Set<number>): void {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) =>
      n.parentElement && skipped(n.parentElement, root)
        ? NodeFilter.FILTER_REJECT
        : NodeFilter.FILTER_ACCEPT,
  });

  // Collect first: replacing a node mid-traversal invalidates the walker.
  const re = citationRe();
  const targets: Text[] = [];
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    re.lastIndex = 0;
    if (re.test(n.nodeValue ?? "")) targets.push(n as Text);
  }
  for (const node of targets) replaceIn(node, known); // replaceIn calls citationRe() itself
}

function replaceIn(node: Text, known: Set<number>): void {
  const text = node.nodeValue ?? "";
  const frag = document.createDocumentFragment();
  let last = 0;
  const re = citationRe();

  for (let m = re.exec(text); m; m = re.exec(text)) {
    const nums = parseCitation(m[1]);
    if (!nums.length) continue;
    if (m.index > last) frag.append(text.slice(last, m.index));

    const span = document.createElement("span");
    const unknown = nums.every((n) => !known.has(n));
    span.className = unknown ? "cite invalid" : "cite";
    span.dataset.nums = nums.join(",");
    span.tabIndex = 0;
    span.setAttribute("role", "button");
    span.textContent = m[0]; // textContent, never innerHTML
    if (unknown) span.title = `no source ${nums.join(", ")} in this answer`; // property, not markup

    frag.append(span);
    last = m.index + m[0].length;
  }

  if (last === 0) return; // no citation matched; leave the node alone
  if (last < text.length) frag.append(text.slice(last));
  node.replaceWith(frag);
}

// Citation grammar and DOM linkification. MIRRORS services/query/citations.py's
// `parse_bracket`/`parse_citations` ONLY -- change both files together or
// neither, and the grammar table in this file's tests is duplicated verbatim
// from tests/test_citations.py with a comment pointing back. `handle_for`
// (blake2b) is deliberately NOT mirrored: there is no blake2b in the browser,
// and there doesn't need to be -- the server sends a `handle` on every
// source, so the client never has to derive one, only recognize one.
//
// A citation the model writes is always a handle ([Sk29fp]); the [doc:chunk]
// ref form this file also parses is the server's defensive fallback grammar,
// mirrored here so a stored answer that happens to contain one (or a client
// composing a message that echoes one back) classifies identically on both
// sides.

export interface Item {
  handle?: string;
  ref?: [number, number];
  raw?: string;
}

export interface Citation {
  start: number;
  end: number;
  text: string;
  items: Item[];
}

// Mirrors ALPHABET/HANDLE_RE in citations.py exactly: 31 symbols, no 0/o/1/l/i,
// a digit forced at the third position -- see that file for why (it's what
// stops "Scoped"/"Sample" from parsing as a handle).
const HANDLE_RE = /^[Ss][a-hj-km-np-z2-9][2-9][a-hj-km-np-z2-9]{3}$/;

function canonicalHandle(token: string): string {
  return "S" + token.slice(1).toLowerCase();
}

function matchHandle(token: string): string | null {
  if (token.length !== 6 || (token[0] !== "S" && token[0] !== "s")) return null;
  const canonical = canonicalHandle(token);
  return HANDLE_RE.test(canonical) ? canonical : null;
}

/** "3" -> [3]; "3-5" -> [3,4,5]; en dash accepted; capped at 50 wide. */
function chunkRange(text: string): number[] | null {
  const bounds = text.split(/\s*[-–]\s*/);
  if (bounds.length < 1 || bounds.length > 2 || !bounds.every((b) => /^\d+$/.test(b))) return null;
  const lo = Number(bounds[0]);
  const hi = Number(bounds[bounds.length - 1]);
  if (hi < lo) return null;
  const end = Math.min(hi, lo + 50);
  const out: number[] = [];
  for (let n = lo; n <= end; n++) out.push(n);
  return out;
}

/** One `;`-delimited segment (or, in the no-colon case, the whole bracket --
 *  see parseBracket). Mirrors citations.py's _parse_segment left to right,
 *  carrying `currentDoc`: a handle is a handle; `d:c`/`d:c-e` sets currentDoc
 *  and emits that chunk range; a bare number emits against currentDoc, or --
 *  first item of the segment, currentDoc unset -- becomes currentDoc instead.
 *  An orphan (nothing ever claims it as a chunk) becomes a raw invalid marker. */
function parseSegment(segment: string, flat: boolean): Item[] {
  const sep = flat ? /\s*[,;]\s*/ : /\s*,\s*/;
  const tokens = segment.split(sep).map((t) => t.trim()).filter((t) => t.length > 0);
  const items: Item[] = [];
  let currentDoc: number | null = null;
  let pendingDoc: number | null = null;
  const flushPending = (): void => {
    if (pendingDoc !== null) {
      items.push({ raw: String(pendingDoc) });
      pendingDoc = null;
    }
  };

  tokens.forEach((token, i) => {
    const handle = matchHandle(token);
    if (handle) {
      flushPending();
      items.push({ handle });
      return;
    }
    if (token.includes(":")) {
      flushPending();
      const idx = token.indexOf(":");
      const docPart = token.slice(0, idx).trim();
      const chunkPart = token.slice(idx + 1).trim();
      const chunks = /^\d+$/.test(docPart) ? chunkRange(chunkPart) : null;
      if (!chunks) {
        items.push({ raw: token });
        return;
      }
      currentDoc = Number(docPart);
      for (const c of chunks) items.push({ ref: [currentDoc, c] });
      return;
    }
    if (!/^\d+$/.test(token)) {
      // Not a handle, no colon, not even a number -- ordinary prose that
      // happens to sit inside brackets. Silently ignored, not flagged
      // invalid: only something that looks like an attempted citation
      // earns a struck-through marker.
      return;
    }
    const n = Number(token);
    if (currentDoc !== null) {
      items.push({ ref: [currentDoc, n] });
    } else if (i === 0) {
      pendingDoc = n;
    } else if (pendingDoc !== null) {
      currentDoc = pendingDoc;
      items.push({ ref: [currentDoc, n] });
      pendingDoc = null;
    } else {
      items.push({ raw: token });
    }
  });
  flushPending();
  return items;
}

/** The grammar inside one `[...]`. null means "not a citation at all" -- the
 *  caller leaves the brackets as literal text. Mirrors citations.py's
 *  parse_bracket exactly, including both corner cases: a lone number reads
 *  as a filename year / stray legacy citation, so it's literal; everything
 *  else is a citation, even if every item in it is invalid. */
export function parseBracket(inner: string): Item[] | null {
  const trimmed = inner.trim();
  if (!trimmed) return null;
  if (/^\d+$/.test(trimmed)) return null;
  const hasColon = trimmed.includes(":");
  const segments = hasColon ? trimmed.split(/\s*;\s*/) : [trimmed];
  let items: Item[] = [];
  for (const segment of segments) items = items.concat(parseSegment(segment, !hasColon));
  if (!items.length) return null;

  const seen = new Set<string>();
  const deduped: Item[] = [];
  for (const item of items) {
    const key = JSON.stringify([item.handle ?? null, item.ref ?? null, item.raw ?? null]);
    if (!seen.has(key)) {
      seen.add(key);
      deduped.push(item);
    }
  }
  return deduped;
}

/** Drops a trailing unclosed `[...` from a still-streaming answer, so the
 *  few tokens between `[` and `]` don't flash the raw handle at the reader
 *  before the bracket closes and becomes a citation. Mirrors the server's
 *  _BracketStream hold-back, including its 64-character give-up: past that
 *  the `[` was never a citation and is left alone as the prose it is. */
export function trimPartialCitation(text: string): string {
  return text.replace(/\[[^[\]\n]{0,63}$/, "");
}

export function parseCitations(text: string): Citation[] {
  const out: Citation[] = [];
  const re = /\[([^[\]\n]{1,200})\]/g;
  for (let m = re.exec(text); m; m = re.exec(text)) {
    const items = parseBracket(m[1]);
    if (items) out.push({ start: m.index, end: m.index + m[0].length, text: m[0], items });
  }
  return out;
}

/** A turn's sources, indexed for lookup both ways: handle -> its [doc:chunk]
 *  (null if this source hasn't actually been cited -- retrieved-but-uncited
 *  sources carry no number, same as the server) and "doc:chunk" -> handle,
 *  for the defensive ref grammar. */
export interface CiteMap {
  handles: Map<string, [number, number] | null>;
  refs: Map<string, string>;
}

export function buildCiteMap(
  sources: { handle: string; doc_number: number | null; chunk_number: number | null }[],
): CiteMap {
  const handles = new Map<string, [number, number] | null>();
  const refs = new Map<string, string>();
  for (const s of sources) {
    const ref: [number, number] | null =
      s.doc_number !== null && s.chunk_number !== null ? [s.doc_number, s.chunk_number] : null;
    handles.set(s.handle, ref);
    if (ref) refs.set(`${ref[0]}:${ref[1]}`, s.handle);
  }
  return { handles, refs };
}

const SKIP = new Set(["CODE", "PRE", "A", "KBD", "SAMP"]);

function skipped(el: Element, root: ParentNode): boolean {
  for (let c: Element | null = el; c && (c as Node) !== root; c = c.parentElement) {
    if (SKIP.has(c.tagName)) return true;
  }
  return false;
}

/** Replaces `[...]` citations with `<span class="cite" data-handle=...
 *  data-ref="1:3">[1:3]</span>` in TEXT NODES ONLY, so `arr[1]` in a code
 *  block and a `[2]` inside an href are untouched. Every node is built with
 *  createElement/textContent -- nothing is ever concatenated into markup.
 *
 *  `pending`: the answer is still streaming. Numbers arrive on their own
 *  `citation` events as each bracket closes, so nearly every citation is
 *  already resolvable here and renders as the real [doc:chunk]; `pending`
 *  only decides what an UNresolved one looks like -- a neutral placeholder
 *  while there's still a chance it gets numbered, a struck-through invalid
 *  marker once the answer is done. Never the raw handle, either way. */
export function linkifyCitations(root: ParentNode, map: CiteMap, pending: boolean): void {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) =>
      n.parentElement && skipped(n.parentElement, root)
        ? NodeFilter.FILTER_REJECT
        : NodeFilter.FILTER_ACCEPT,
  });

  const targets: Text[] = [];
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (parseCitations(n.nodeValue ?? "").length) targets.push(n as Text);
  }
  for (const node of targets) replaceIn(node, map, pending);
}

function replaceIn(node: Text, map: CiteMap, pending: boolean): void {
  const text = node.nodeValue ?? "";
  const citations = parseCitations(text);
  if (!citations.length) return;

  const frag = document.createDocumentFragment();
  let last = 0;
  for (const citation of citations) {
    if (citation.start > last) frag.append(text.slice(last, citation.start));
    for (const item of citation.items) frag.append(renderItem(item, map, pending));
    last = citation.end;
  }
  if (last < text.length) frag.append(text.slice(last));
  node.replaceWith(frag);
}

function renderItem(item: Item, map: CiteMap, pending: boolean): HTMLSpanElement {
  const span = document.createElement("span");
  span.tabIndex = 0;
  span.setAttribute("role", "button");

  let ref: [number, number] | null = null;
  let handle: string | null = null;
  if (item.handle !== undefined) {
    handle = item.handle;
    ref = map.handles.get(item.handle) ?? null;
  } else if (item.ref !== undefined) {
    ref = item.ref;
    const found = map.refs.get(`${item.ref[0]}:${item.ref[1]}`);
    handle = found && map.handles.has(found) ? found : null;
  }

  if (ref && handle) {
    span.className = "cite";
    span.dataset.handle = handle;
    span.dataset.ref = `${ref[0]}:${ref[1]}`;
    span.textContent = `[${ref[0]}:${ref[1]}]`; // textContent, never innerHTML
  } else if (pending) {
    // Still streaming, so this one may yet be numbered -- a neutral marker
    // rather than calling it invalid before the answer has finished. No
    // data-handle, so it isn't clickable either.
    span.className = "cite pending";
    span.textContent = "[…]";
  } else {
    // Invalid: no data-handle, so click/hover handlers ignore it by
    // construction rather than by a class check. A malformed ref or orphan
    // number is shown as-is (plain digits -- the reader already sees that
    // form everywhere); a handle that didn't resolve is NOT shown as
    // typed -- a raw handle must never reach the reader, garbled or not.
    span.className = "cite invalid";
    span.title = "no source for this citation in this answer";
    span.textContent =
      item.handle !== undefined ? "[invalid citation]"
      : item.raw !== undefined ? `[${item.raw}]`
      : `[${item.ref![0]}:${item.ref![1]}]`;
  }
  return span;
}

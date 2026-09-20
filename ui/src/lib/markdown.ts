// Pure-string helpers for incremental markdown rendering. No DOM, no marked,
// no Svelte -- see render.ts for the piece that turns this into HTML. The
// whole point of this module is letting the renderer re-parse only the part
// of the answer that is still changing.

const FENCE = /^ {0,3}(`{3,}|~{3,})/;

/** One pass over the lines. INVARIANT: openAfter[i] is the fence state
 *  *after* line i, so the state a line was *reached* in is openAfter[i-1].
 *  Both splitStable and closeOpenFence read this same scan, so they cannot
 *  disagree about where a fence is. Note the loop condition is
 *  `pos <= text.length`, so text ending in "\n" yields a final empty-line
 *  entry -- splitStable relies on that to find a trailing blank line. */
export function fenceScan(text: string): { offsets: number[]; openAfter: boolean[]; openMarker: string | null } {
  const offsets: number[] = [];
  const openAfter: boolean[] = [];
  let open: string | null = null;

  for (let pos = 0; pos <= text.length; ) {
    const nl = text.indexOf("\n", pos);
    const line = text.slice(pos, nl < 0 ? text.length : nl);
    offsets.push(pos);
    const m = FENCE.exec(line);
    if (m) {
      const marker = m[1];
      // CommonMark: a fence closes on the same character, at least as long.
      if (open === null) open = marker;
      else if (marker[0] === open[0] && marker.length >= open.length) open = null;
    }
    openAfter.push(open !== null);
    if (nl < 0) break;
    pos = nl + 1;
  }

  return { offsets, openAfter, openMarker: open };
}

/** [stable, volatile]: cut at the last blank line that is not inside a fence.
 *  Markdown block structure never reaches back across a blank line, so
 *  everything before the cut is safe to leave rendered as-is. */
export function splitStable(text: string): [string, string] {
  const { offsets, openAfter } = fenceScan(text);

  for (let i = offsets.length - 1; i > 0; i--) {
    const start = offsets[i];
    const nl = text.indexOf("\n", start);
    const line = nl < 0 ? text.slice(start) : text.slice(start, nl);
    if (line.trim() !== "") continue;
    // openAfter[i-1], not [i]: the state this line was REACHED in. The two
    // happen to be equal for a blank line (it can neither open nor close a
    // fence), but relying on that coincidence is how this drifts later.
    if (openAfter[i - 1]) continue; // the blank line is inside a fence
    const cut = nl < 0 ? text.length : nl + 1;
    return [text.slice(0, cut), text.slice(cut)];
  }

  return ["", text];
}

/** A copy safe to hand to marked mid-stream. An unterminated fence otherwise
 *  makes marked re-flow the rest of the answer as paragraphs, flickering the
 *  whole bubble on every token. Never mutate the stored raw text with this. */
export function closeOpenFence(text: string): string {
  const { openMarker } = fenceScan(text);
  if (!openMarker) return text;
  return text + (text.endsWith("\n") ? "" : "\n") + openMarker;
}

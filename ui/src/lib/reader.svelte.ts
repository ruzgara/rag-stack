// One document as the viewers read it: which chunks are loaded (a passage
// and its neighbours, or the whole thing), which are marked, and which one
// you're on. Shared by SourceModal (a passage) and DocumentModal (a
// document) so both move, highlight and scroll alike; ChunkView renders it.

import { tick } from "svelte";
import { get } from "./api";
import type { Mark } from "./turns";
import type { Chunk, ChunksResponse } from "./types";

/** Px below the view's top edge that decides which chunk you're on. */
const READ_LINE = 24;

export class ChunkReader {
  docSha = $state<string | null>(null);
  /** Marked chunks in citation order -- the order prev/next walks. */
  marks = $state<Mark[]>([]);
  chunks = $state<Chunk[]>([]);
  total = $state<number | null>(null);
  full = $state(false);
  loading = $state(false);
  error = $state<string | null>(null);
  /** The chunk you're on: jumped to, or at the top of the view. */
  focus = $state(0);
  /** The mark you were last on, so prev/next still has a place in the
   *  citation order after scrolling off into plain text. */
  private lastMark = $state<number | null>(null);
  body = $state<HTMLElement | undefined>();

  byChunk = $derived(new Map(this.marks.map((m) => [m.chunkIndex, m])));
  private at = $derived(this.marks.findIndex((m) => m.chunkIndex === this.lastMark));
  /** Index into `marks`, or null when there's none that way. On a mark,
   *  its neighbours; scrolled off one, back to it or on past it. */
  prevMark = $derived.by(() => {
    if (this.at === -1) return null;
    const onIt = this.marks[this.at].chunkIndex === this.focus;
    const i = onIt ? this.at - 1 : this.at;
    return i >= 0 ? i : null;
  });
  nextMark = $derived(this.at + 1 < this.marks.length ? this.at + 1 : null);
  /** The mark the viewer is on, if it's on one. */
  current = $derived(this.byChunk.get(this.focus) ?? null);

  private autoScrolling = false;
  private autoTimer: ReturnType<typeof setTimeout> | undefined;
  private scrollFrame = 0;

  /** Point the reader at a chunk of a document and load around it. */
  open(docSha: string, chunk: number, full: boolean): void {
    this.docSha = docSha;
    this.full = full;
    this.lastMark = null;
    this.setFocus(chunk);
    void this.load(chunk);
  }

  openFull(): void {
    this.full = true;
    void this.load(this.focus);
  }

  /** To marks[i], scrolling there -- the same glide whether the whole
   *  document is loaded or just a passage's neighbours, which first grow to
   *  reach it. */
  jump(i: number | null): void {
    const m = i === null ? undefined : this.marks[i];
    if (!m) return;
    this.setFocus(m.chunkIndex);
    if (this.chunks.some((c) => c.chunk_index === m.chunkIndex)) this.glideTo(m.chunkIndex);
    else void this.extendTo(m.chunkIndex);
  }

  onScroll(): void {
    if (this.autoScrolling || this.scrollFrame) return;
    this.scrollFrame = requestAnimationFrame(() => {
      this.scrollFrame = 0;
      const body = this.body;
      if (!body) return;
      // Just inside the top edge, where a jump puts a chunk -- so a chunk
      // whose last line is only peeking in above doesn't count.
      const line = body.getBoundingClientRect().top + READ_LINE;
      let best: number | null = null;
      let bestGap = Infinity;
      for (const el of body.querySelectorAll<HTMLElement>("[data-i]")) {
        const r = el.getBoundingClientRect();
        const gap = r.top <= line && r.bottom >= line ? 0 : Math.min(Math.abs(r.top - line), Math.abs(r.bottom - line));
        if (gap < bestGap) {
          bestGap = gap;
          best = Number(el.dataset.i);
        }
      }
      if (best !== null) this.setFocus(best);
    });
  }

  onScrollEnd(): void {
    this.autoScrolling = false;
    clearTimeout(this.autoTimer);
  }

  destroy(): void {
    clearTimeout(this.autoTimer);
    cancelAnimationFrame(this.scrollFrame);
  }

  private setFocus(chunk: number): void {
    this.focus = chunk;
    if (this.byChunk.has(chunk)) this.lastMark = chunk;
  }

  /** `count` chunks from `from` (Infinity: to the end), a page at a time. */
  private async fetchRange(sha: string, from: number, count: number): Promise<Chunk[]> {
    const all: Chunk[] = [];
    let offset = from;
    while (all.length < count) {
      const limit = Math.min(200, count - all.length);
      const res = await get<ChunksResponse>(`/api/documents/${sha}/chunks?offset=${offset}&limit=${limit}`);
      all.push(...res.chunks);
      this.total = res.total;
      offset += res.chunks.length;
      if (!res.chunks.length || offset >= res.total) break;
    }
    return all;
  }

  /** Grow the loaded window to take in `target` and a chunk either side,
   *  holding what's on screen still while chunks land above it, then glide. */
  private async extendTo(target: number): Promise<void> {
    const sha = this.docSha;
    const first = this.chunks[0]?.chunk_index;
    const last = this.chunks[this.chunks.length - 1]?.chunk_index;
    if (!sha || first === undefined || last === undefined || this.error) return void this.load(target);
    const lo = Math.min(first, Math.max(0, target - 1));
    const hi = Math.max(last, target + 1);
    this.loading = true;
    let chunks: Chunk[] | null = null;
    try {
      chunks = await this.fetchRange(sha, lo, hi - lo + 1);
    } catch {
      /* handled below */
    }
    this.loading = false;
    if (!chunks) return void this.load(target); // a plain reload around it reports the error
    const before = this.body?.querySelector(`[data-i="${first}"]`)?.getBoundingClientRect().top ?? 0;
    this.chunks = chunks;
    await tick();
    const after = this.body?.querySelector(`[data-i="${first}"]`)?.getBoundingClientRect().top ?? before;
    if (this.body && after !== before) {
      this.holdScroll();
      this.body.scrollTop += after - before;
    }
    this.glideTo(target);
  }

  private glideTo(chunk: number): void {
    this.scrollTo(chunk, { block: "start", behavior: "smooth" });
  }

  private async load(center: number): Promise<void> {
    const sha = this.docSha;
    if (!sha) return;
    const whole = this.full;
    this.loading = true;
    this.error = null;
    try {
      this.chunks = whole
        ? await this.fetchRange(sha, 0, Infinity)
        : await this.fetchRange(sha, Math.max(0, center - 1), 3);
    } catch (e) {
      // The document may have been deleted or re-indexed since the answer;
      // ChunkView falls back to the stored copy of the passage.
      this.chunks = [];
      this.error = e instanceof Error ? e.message : String(e);
    } finally {
      this.loading = false;
    }
    await tick();
    this.scrollTo(center, { block: whole ? "start" : "nearest" });
  }

  // While the viewer scrolls itself somewhere, the chunks it passes on the
  // way aren't where you are: `scrollend` ends that, and the timer covers
  // browsers without it and scrolls that had nowhere to go.
  private scrollTo(chunk: number, opts: ScrollIntoViewOptions): void {
    const el = this.body?.querySelector(`[data-i="${chunk}"]`) ?? this.body?.querySelector(".hit");
    if (!el) return;
    this.holdScroll();
    el.scrollIntoView(opts);
  }

  private holdScroll(): void {
    this.autoScrolling = true;
    clearTimeout(this.autoTimer);
    this.autoTimer = setTimeout(() => (this.autoScrolling = false), 800);
  }
}

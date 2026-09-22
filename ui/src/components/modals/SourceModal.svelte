<script lang="ts">
  // The source viewer: the cited chunk highlighted between its neighbours,
  // prev/next through the document chunk by chunk, and "Open full document"
  // to read the whole thing as indexed (all chunks) with the cited one in
  // place. The original file itself isn't served by the API.
  import { tick } from "svelte";
  import { get } from "../../lib/api";
  import type { Chunk, ChunksResponse, Source } from "../../lib/types";
  import { locator } from "../../lib/format";
  import { chat } from "../../state/chat.svelte";
  import { ui } from "../../state/ui.svelte";
  import ModalShell from "../ui/ModalShell.svelte";

  interface Props {
    turnKey: string;
    handle: string;
  }
  const { turnKey, handle }: Props = $props();

  const turn = $derived.by(() => {
    for (const v of chat.views.values()) {
      const t = v.turns.find((x) => x.key === turnKey);
      if (t) return t;
    }
    return null;
  });
  const source = $derived<Source | null>(turn?.sources.find((s) => s.handle === handle) ?? null);
  // Cited first (in reader order), then uncited -- there's no meaningful
  // number to sort the latter by, and mixing them by retrieval order would
  // put an uncited passage ahead of a cited [1:1].
  const handles = $derived.by(() => {
    const sources = turn?.sources ?? [];
    const cited = sources.filter((s) => s.cited)
      .sort((a, b) => (a.doc_number ?? 0) - (b.doc_number ?? 0) || (a.chunk_number ?? 0) - (b.chunk_number ?? 0));
    const uncited = sources.filter((s) => !s.cited);
    return [...cited, ...uncited].map((s) => s.handle);
  });

  let focus = $state(0); // chunk index the viewer is centred on
  let chunks = $state<Chunk[]>([]);
  let total = $state<number | null>(null);
  let full = $state(false);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let body: HTMLDivElement | undefined = $state();

  async function load(sha: string, center: number, whole: boolean): Promise<void> {
    loading = true;
    error = null;
    try {
      if (whole) {
        const all: Chunk[] = [];
        let offset = 0;
        for (;;) {
          const res = await get<ChunksResponse>(`/api/documents/${sha}/chunks?offset=${offset}&limit=200`);
          all.push(...res.chunks);
          total = res.total;
          offset += res.chunks.length;
          if (!res.chunks.length || offset >= res.total) break;
        }
        chunks = all;
      } else {
        const offset = Math.max(0, center - 1);
        const res = await get<ChunksResponse>(`/api/documents/${sha}/chunks?offset=${offset}&limit=3`);
        chunks = res.chunks;
        total = res.total;
      }
    } catch (e) {
      // The document may have been deleted or re-indexed since the answer;
      // the answer's own stored copy of the passage still shows.
      chunks = [];
      error = e instanceof Error ? e.message : String(e);
    } finally {
      loading = false;
    }
    await tick();
    body?.querySelector(".hit")?.scrollIntoView({ block: whole ? "center" : "nearest" });
  }

  // (Re)start whenever the viewer is pointed at a different source.
  $effect(() => {
    const s = source;
    if (!s) return;
    focus = s.chunk_index;
    full = false;
    void load(s.doc_sha, s.chunk_index, false);
  });

  function step(delta: number): void {
    if (!source || total === null) return;
    const next = focus + delta;
    if (next < 0 || next >= total) return;
    focus = next;
    if (full) {
      body?.querySelector(`[data-i="${next}"]`)?.scrollIntoView({ block: "center", behavior: "smooth" });
    } else {
      void load(source.doc_sha, next, false);
    }
  }

  function openFull(): void {
    if (!source) return;
    full = true;
    void load(source.doc_sha, focus, true);
  }

  function otherSource(delta: number): void {
    const i = handles.indexOf(handle) + delta;
    if (i < 0 || i >= handles.length) return;
    ui.focusSources([handles[i]], turnKey);
    ui.replace({ kind: "source", turnKey, handle: handles[i] });
  }

  function onKey(e: KeyboardEvent): void {
    if (ui.dialog || (e.target as HTMLElement)?.closest("input, textarea")) return;
    if (e.key === "ArrowDown" || e.key === "j") { e.preventDefault(); step(1); }
    if (e.key === "ArrowUp" || e.key === "k") { e.preventDefault(); step(-1); }
    if (e.key === "ArrowRight") otherSource(1);
    if (e.key === "ArrowLeft") otherSource(-1);
  }

  const origin = $derived.by(() => {
    if (!source) return "";
    const parts = [];
    if (focus !== source.chunk_index) parts.push(`chunk ${focus + 1}${total ? ` of ${total}` : ""} · ${focus > source.chunk_index ? "+" : ""}${focus - source.chunk_index} from the cited one`);
    else parts.push(`chunk ${focus + 1}${total ? ` of ${total}` : ""}`);
    if (source.rerank_score !== null) parts.push(`rerank ${source.rerank_score.toFixed(2)}`);
    parts.push(source.found_by === "tool" ? "found by a model search" : "found by auto-search");
    return parts.join(" · ");
  });
</script>

<svelte:window onkeydown={onKey} />

<ModalShell width={820} labelledby="source-title">
  {#if !source}
    <p class="muted">This source is no longer available.</p>
  {:else}
    <div class="src-head">
      {#if source.doc_number !== null}<span class="num">[{source.doc_number}:{source.chunk_number}]</span>{/if}
      <div class="titles">
        <h4 id="source-title">{source.filename}</h4>
        <div class="loc">
          {locator(source.pages, source.headings) || "—"}
          {#if source.headings.length > 1}<span class="muted"> · {source.headings.slice(0, -1).join(" › ")}</span>{/if}
        </div>
        <div class="loc muted">found for “{source.query}”</div>
      </div>
      {#if handles.length > 1}
        <div class="src-nav">
          <button class="btn btn-ghost btn-sm" onclick={() => otherSource(-1)} disabled={handles.indexOf(handle) === 0} aria-label="Previous source">‹</button>
          <button class="btn btn-ghost btn-sm" onclick={() => otherSource(1)} disabled={handles.indexOf(handle) === handles.length - 1} aria-label="Next source">›</button>
        </div>
      {/if}
      <button class="x" onclick={() => ui.close()} aria-label="Close">×</button>
    </div>

    <div class="doc rag-scroll" bind:this={body}>
      <div class="prose" class:loading>
        {#if error || (!loading && chunks.length === 0)}
          <!-- The stored copy: always there, whatever happened to the index. -->
          <div class="hit"><p>{source.text}</p></div>
          {#if error}<p class="muted small">Showing the passage as the answer saw it — the document's chunks couldn't be loaded ({error}).</p>{/if}
        {:else}
          {#each chunks as c (c.chunk_index)}
            {#if c.chunk_index === focus}
              <div class="hit" data-i={c.chunk_index}>
                {#if c.headings.length}<div class="chunk-heading">{c.headings[c.headings.length - 1]}</div>{/if}
                <p>{c.text}</p>
              </div>
            {:else}
              <div class="ctx" data-i={c.chunk_index}>
                {#if full && c.headings.length && c.headings.join() !== chunks.find((x) => x.chunk_index === c.chunk_index - 1)?.headings.join()}
                  <div class="chunk-heading">{c.headings[c.headings.length - 1]}</div>
                {/if}
                <p>{c.text}</p>
              </div>
            {/if}
          {/each}
        {/if}
      </div>
    </div>
  {/if}
  {#snippet footer()}
    {#if source}
      <button class="btn btn-secondary" onclick={() => step(-1)} disabled={focus <= 0 || loading}>↑ Previous chunk</button>
      <button class="btn btn-secondary" onclick={() => step(1)} disabled={total === null || focus >= total - 1 || loading}>Next chunk ↓</button>
      <span class="origin">{origin}</span>
      {#if !full}
        <button class="btn btn-ghost" onclick={openFull} disabled={loading || !!error}>Open full document</button>
      {/if}
    {/if}
  {/snippet}
</ModalShell>

<style>
  .src-head { display: flex; align-items: flex-start; gap: 14px; padding-bottom: 14px; }
  .num { font-family: var(--font-heading); font-size: 20px; font-weight: 600; color: var(--color-accent-700); }
  .titles { flex: 1; min-width: 0; }
  h4 { margin: 0 0 2px; font-size: 20px; overflow-wrap: anywhere; }
  .loc { font-size: 11.5px; color: var(--color-neutral-700); }
  .src-nav { display: flex; gap: 2px; }
  .x { border: 0; background: transparent; font-size: 20px; line-height: 1; cursor: pointer; color: var(--color-neutral-700); padding: 2px 4px; }
  .doc { max-height: 58vh; overflow-y: auto; margin: 0 -4px; padding: 0 4px; }
  .prose { max-width: 64ch; font-size: 15px; line-height: 1.75; transition: opacity .15s; }
  .prose.loading { opacity: .5; }
  .prose p { white-space: pre-wrap; overflow-wrap: anywhere; }
  .ctx p { color: var(--color-neutral-600); }
  .hit {
    background: var(--color-accent-100); box-shadow: -3px 0 0 var(--color-accent);
    padding: 12px 16px; margin: 0 0 15px 0;
  }
  .hit p { margin: 0; }
  .chunk-heading { font-family: var(--font-heading); font-weight: 600; font-size: 13px; letter-spacing: .02em; margin: 4px 0 6px; color: var(--color-neutral-800); }
  .small { font-size: 12px; }
  .origin { font-size: 11.5px; color: var(--color-neutral-600); margin-left: auto; text-align: right; }
  @media (min-width: 700px) { .hit { margin-left: -16px; margin-right: -16px; } }
</style>

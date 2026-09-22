<script lang="ts">
  // The reading pane both viewers share: a ChunkReader's chunks, the marked
  // ones highlighted with their [doc:chunk], and the sidebar kept on the
  // chunk you're on -- jumped to or scrolled to -- while it's open.
  import { onDestroy } from "svelte";
  import type { ChunkReader } from "../../lib/reader.svelte";
  import type { Turn } from "../../lib/turns";
  import { ui } from "../../state/ui.svelte";

  interface Props {
    reader: ChunkReader;
    /** The conversation, for which of its passages sit on the current chunk. */
    turns: Turn[];
    /** Shown if the document's chunks can't be loaded: the stored passage. */
    fallback: string;
  }
  const { reader, turns, fallback }: Props = $props();

  // Where the document carries on past what's loaded: ⋮ above unless the
  // first chunk shown is the document's first, below unless it's the last.
  const moreAbove = $derived((reader.chunks[0]?.chunk_index ?? 0) > 0);
  const moreBelow = $derived.by(() => {
    const last = reader.chunks[reader.chunks.length - 1]?.chunk_index;
    return last !== undefined && reader.total !== null && last < reader.total - 1;
  });

  // A [doc:chunk] label under the pointer stands in for the chunk you're on.
  let hovered = $state<number | null>(null);

  // Every passage of the conversation that is this chunk, from any turn.
  // Stepping onto a chunk nothing retrieved marks nothing.
  $effect(() => {
    const sha = reader.docSha;
    const at = hovered ?? reader.focus;
    const here = new Set<string>();
    for (const t of turns) {
      for (const s of t.sources) if (s.doc_sha === sha && s.chunk_index === at) here.add(s.handle);
    }
    ui.focusedSources = [...here];
  });

  // Only while it's open -- left behind, the mark would stick until
  // something else took focus.
  $effect(() => {
    ui.focusedDoc = reader.docSha;
  });

  onDestroy(() => {
    ui.focusedSources = [];
    ui.focusedDoc = null;
    reader.destroy();
  });
</script>

<div class="doc rag-scroll" bind:this={reader.body} onscroll={() => reader.onScroll()} onscrollend={() => reader.onScrollEnd()}>
  <div class="prose" class:loading={reader.loading}>
    {#if reader.error || (!reader.loading && reader.chunks.length === 0)}
      <!-- The stored copy: always there, whatever happened to the index. -->
      <div class="hit"><p>{fallback}</p></div>
      {#if reader.error}<p class="muted small">Showing the passage as the answer saw it — the document's chunks couldn't be loaded ({reader.error}).</p>{/if}
    {:else}
      {#if moreAbove}<div class="more" aria-hidden="true">⋮</div>{/if}
      {#each reader.chunks as c, i (c.chunk_index)}
        {@const mark = reader.byChunk.get(c.chunk_index)}
        {#if mark}
          <div class="hit" class:current={c.chunk_index === reader.focus} data-i={c.chunk_index}>
            {#if mark.label || c.headings.length}
              <div class="chunk-heading">
                {#if mark.label}
                  <span class="label" role="note" onmouseenter={() => (hovered = c.chunk_index)}
                        onmouseleave={() => (hovered = null)}>{mark.label}</span>
                {/if}
                {#if c.headings.length}{c.headings[c.headings.length - 1]}{/if}
              </div>
            {/if}
            <p>{c.text}</p>
          </div>
        {:else}
          <div class="ctx" data-i={c.chunk_index}>
            {#if reader.full && c.headings.length && c.headings.join() !== reader.chunks[i - 1]?.headings.join()}
              <div class="chunk-heading">{c.headings[c.headings.length - 1]}</div>
            {/if}
            <p>{c.text}</p>
          </div>
        {/if}
      {/each}
      {#if moreBelow}<div class="more" aria-hidden="true">⋮</div>{/if}
    {/if}
  </div>
</div>

<style>
  .doc { max-height: 58vh; overflow-y: auto; margin: 0 -4px; padding: 0 4px; }
  .prose { max-width: 92%; margin: 0 auto; font-size: 15px; line-height: 1.75; transition: opacity .15s; }
  .prose.loading { opacity: .5; }
  .prose p { white-space: pre-wrap; overflow-wrap: anywhere; }
  .ctx p { color: var(--color-neutral-600); }
  .hit {
    background: var(--color-accent-100); box-shadow: -3px 0 0 var(--color-accent);
    padding: 12px 16px; margin: 0 0 15px 0;
  }
  /* Which of several highlighted passages you're on. */
  .hit.current { box-shadow: -3px 0 0 var(--color-accent-700); }
  .hit p { margin: 0; }
  .chunk-heading { font-family: var(--font-heading); font-weight: 600; font-size: 13px; letter-spacing: .02em; margin: 4px 0 6px; color: var(--color-neutral-800); }
  .label { color: var(--color-accent-700); margin-right: 6px; }
  .small { font-size: 12px; }
  .more { text-align: center; font-size: 20px; line-height: 1; color: var(--color-neutral-400); margin: 2px 0 12px; user-select: none; }
  @media (min-width: 700px) { .hit { margin-left: -16px; margin-right: -16px; } }
</style>

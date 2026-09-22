<script lang="ts">
  // Binds a StreamingMarkdown instance to one <div>. Token events are
  // coalesced onto requestAnimationFrame; the moment status leaves
  // "streaming" (immediately, for a reopened turn) one authoritative
  // finalize() parse replaces the incremental render.
  import { onDestroy } from "svelte";
  import { StreamingMarkdown } from "../../lib/render";
  import type { TurnStatus } from "../../lib/turns";

  interface Props {
    answer: string;
    status: TurnStatus;
    known: Set<number>;
  }
  const { answer, status, known }: Props = $props();

  let container: HTMLDivElement | undefined = $state();
  let renderer: StreamingMarkdown | undefined;
  let rafId: number | null = null;
  let finalized = false;

  function scheduleUpdate(): void {
    if (rafId !== null) return;
    rafId = requestAnimationFrame(() => {
      rafId = null;
      if (!container) return;
      if (status === "streaming") {
        // A retry resets a finalized turn back to streaming: start over.
        if (!renderer || finalized) {
          renderer = new StreamingMarkdown(container);
          finalized = false;
        }
        renderer.update(answer, known);
      } else if (!finalized) {
        renderer ??= new StreamingMarkdown(container);
        finalized = true;
        renderer.finalize(answer, known);
      }
    });
  }

  $effect(() => {
    void answer;
    void status;
    void known;
    scheduleUpdate();
  });

  onDestroy(() => {
    if (rafId !== null) cancelAnimationFrame(rafId);
  });
</script>

<div class="md" class:streaming={status === "streaming"} bind:this={container}></div>

<style>
  .md { font-size: 16px; line-height: 1.72; overflow-wrap: anywhere; }
  .md :global(p) { margin: 0 0 15px; }
  .md :global(ul), .md :global(ol) { margin: 0 0 15px; padding-left: 20px; }
  .md :global(li) { margin-bottom: 6px; }
  .md :global(li > p) { margin-bottom: 6px; }
  .md :global(h1), .md :global(h2), .md :global(h3), .md :global(h4) { margin: 22px 0 10px; }
  .md :global(h1) { font-size: 26px; }
  .md :global(h2) { font-size: 22px; }
  .md :global(h3) { font-size: 19px; }
  .md :global(h4), .md :global(h5), .md :global(h6) { font-size: 16px; }
  .md :global(blockquote) {
    margin: 0 0 15px; padding: 2px 0 2px 14px; border-left: 2px solid var(--color-divider);
    color: var(--color-neutral-800);
  }
  .md :global(code) { background: var(--color-surface); padding: 1px 4px; border-radius: var(--radius-md); }
  .md :global(pre) {
    background: var(--color-surface); padding: 10px 13px; border-radius: var(--radius-md);
    overflow-x: auto; line-height: 1.5; margin: 0 0 15px;
  }
  .md :global(pre code) { background: none; padding: 0; }
  .md :global(table) { border-collapse: collapse; margin: 0 0 15px; font-size: 14px; display: block; overflow-x: auto; }
  .md :global(th), .md :global(td) { padding: 6px 10px; border-bottom: 1px solid var(--color-divider); text-align: left; }
  .md :global(th) { font-size: 11px; letter-spacing: .08em; text-transform: uppercase; font-weight: 400; color: var(--color-neutral-700); }
  .md :global(hr) { border: 0; height: 1px; background: var(--color-divider); margin: 20px 0; }
  .md :global(> div > :last-child), .md :global(> :last-child) { margin-bottom: 0; }

  /* Citations: the design's superscript [n]. */
  .md :global(.cite) {
    font-size: .72em; vertical-align: super; line-height: 0; font-weight: 600;
    color: var(--color-accent-700); cursor: pointer; border-radius: var(--radius-sm);
    padding: 0 1px; white-space: nowrap;
  }
  .md :global(.cite:hover), .md :global(.cite:focus-visible) { background: var(--color-accent-100); }
  .md :global(.cite.invalid) { color: var(--color-accent-2-700); text-decoration: line-through; cursor: help; }

  /* The block cursor at the end of a streaming answer: after the last
     text-bearing element of the tail, or of the stable part if the tail
     is empty. */
  .md.streaming :global(> div:last-child > :is(p, h1, h2, h3, h4, h5, h6, blockquote):last-child::after),
  .md.streaming :global(> div:last-child > :is(ul, ol):last-child > li:last-child::after),
  .md.streaming:has(> div:last-child:empty) :global(> div:first-child > :is(p, h1, h2, h3, h4, h5, h6, blockquote):last-child::after),
  .md.streaming:has(> div:last-child:empty) :global(> div:first-child > :is(ul, ol):last-child > li:last-child::after) {
    content: ""; display: inline-block; width: 8px; height: 17px; margin-left: 2px;
    vertical-align: -3px; background: var(--color-accent);
  }
</style>

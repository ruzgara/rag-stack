<script lang="ts">
  import type { Source } from "../lib/types";
  import { pageRange, score } from "../lib/format";
  import { ui } from "../state/ui.svelte";

  interface Props {
    source: Source;
    cited: boolean;
  }
  const { source, cited }: Props = $props();

  const flash = $derived(ui.focusedSources.includes(source.number));
  const pages = $derived(pageRange(source.pages));
  const scoreText = $derived(score(source.rerank_score));

  let cardEl: HTMLDivElement | undefined = $state();

  $effect(() => {
    if (flash) cardEl?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  });
</script>

<div bind:this={cardEl} class="src" class:cited class:flash id="src-{source.number}">
  <b>[{source.number}]</b> {source.filename}
  {#if pages}&middot; {pages}{/if}
  {#if scoreText}&middot; {scoreText}{/if}
  <div class="meta">
    {#if source.headings.length}{source.headings.join(" › ")} &middot; {/if}
    {source.found_by === "tool" ? "tool search" : "auto search"}
    {#if cited}&middot; cited{/if}
  </div>
  <div class="text">{source.text}</div>
</div>

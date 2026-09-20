<script lang="ts">
  import { session } from "../state/session.svelte";

  const h = $derived(session.health);
</script>

{#if !h}
  <span class="small">checking&hellip;</span>
{:else if h.ok}
  <span class="small" style="color: var(--ok);">
    &check; healthy
    {#if h.qdrant.ok}&middot; {h.qdrant.chunks} chunks{/if}
    {#if h.reranker}
      &middot; {"loading" in h.reranker ? "reranker loading" : `reranker on ${h.reranker.device}`}
    {/if}
  </span>
{:else}
  <span class="small" style="color: var(--err);">
    &cross; unhealthy
    {#if !h.qdrant.ok}&middot; {h.qdrant.error}{/if}
    {#if !h.ollama.ok}&middot; ollama: {h.ollama.error}{/if}
    {#if !h.database.ok}&middot; db: {h.database.error}{/if}
  </span>
{/if}

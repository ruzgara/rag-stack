<script lang="ts">
  // session.quota.load pairs with the live `queued` SSE event to make a
  // real queue/capacity indicator -- net-new surface, not a port.
  import { session } from "../state/session.svelte";
  import { chat } from "../state/chat.svelte";

  const load = $derived(session.quota?.load ?? null);
  const queuePosition = $derived(chat.turns.at(-1)?.queuePosition ?? null);
</script>

{#if load}
  <span class="small" title="{load.running} of {load.max_concurrent} turns running across all sessions">
    {load.running} of {load.max_concurrent} running
    {#if load.queued > 0}&middot; {load.queued} waiting{/if}
    {#if queuePosition !== null && chat.busy}&middot; you're #{queuePosition} in line{/if}
  </span>
{/if}

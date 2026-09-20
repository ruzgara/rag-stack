<script lang="ts">
  interface Props {
    events: string[];
  }
  const { events }: Props = $props();
</script>

{#if events.length}
  <div class="status-line" aria-live="polite">{events.join(" · ")}</div>
{/if}

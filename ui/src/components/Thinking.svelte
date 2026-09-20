<script lang="ts">
  interface Props {
    text: string;
  }
  const { text }: Props = $props();
</script>

{#if text}
  <details class="think">
    <summary>thinking&hellip;</summary>
    <pre>{text}</pre>
  </details>
{/if}

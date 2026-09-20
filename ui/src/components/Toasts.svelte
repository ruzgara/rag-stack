<script lang="ts">
  import { ui } from "../state/ui.svelte";
</script>

<div id="toast">
  {#each ui.toasts as t (t.id)}
    <button type="button" class="item {t.kind}" onclick={() => ui.dismissToast(t.id)}>{t.message}</button>
  {/each}
</div>

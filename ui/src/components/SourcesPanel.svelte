<script lang="ts">
  import { chat } from "../state/chat.svelte";
  import { ui } from "../state/ui.svelte";
  import SourceCard from "./SourceCard.svelte";

  // Default to the most recent turn with sources, matching test-ui.html's
  // behavior of showing the last assistant message's sources on open --
  // but re-derived reactively rather than set once at load time, and
  // overridable by explicitly selecting an earlier turn.
  const turn = $derived(
    (ui.selectedTurnKey && chat.turns.find((t) => t.key === ui.selectedTurnKey)) ||
      [...chat.turns].reverse().find((t) => t.sources.length > 0) ||
      null,
  );
</script>

<section class="sources">
  <h3>Sources</h3>
  {#if !turn || turn.sources.length === 0}
    <p class="small">Ask a question to see sources here.</p>
  {:else}
    {#each turn.sources as s (s.number)}
      <SourceCard source={s} cited={s.cited ?? false} />
    {/each}
  {/if}
</section>

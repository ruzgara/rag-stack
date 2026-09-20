<script lang="ts">
  // Defect 1 fix: test-ui.html scrolled to the bottom unconditionally on
  // every frame, including unhandled events, yanking a scrolled-up reader
  // back down. Here scrolling only follows new content when the reader was
  // already near the bottom; otherwise a "Jump to latest" pill appears.
  import type { Turn } from "../lib/turns";
  import TurnComponent from "./Turn.svelte";

  interface Props {
    turns: Turn[];
  }
  const { turns }: Props = $props();

  let logEl: HTMLDivElement | undefined = $state();
  let stick = $state(true);
  const NEAR_BOTTOM_PX = 120;

  function onScroll(): void {
    if (!logEl) return;
    stick = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < NEAR_BOTTOM_PX;
  }

  function jumpToLatest(): void {
    stick = true;
    if (logEl) logEl.scrollTop = logEl.scrollHeight;
  }

  $effect(() => {
    // Registers a dependency on the last turn's answer length (and turn
    // count), so this re-runs on every token as well as on a new turn --
    // without depending on the whole `turns` array reference, which the
    // token-append mutation path deliberately keeps stable.
    const last = turns.at(-1);
    void last?.answer.length;
    void turns.length;
    if (stick && logEl) logEl.scrollTop = logEl.scrollHeight;
  });
</script>

<div id="log" bind:this={logEl} onscroll={onScroll}>
  {#if turns.length === 0}
    <p class="small" style="color: var(--muted);">Ask a question to get started.</p>
  {/if}
  {#each turns as turn (turn.key)}
    <TurnComponent {turn} />
  {/each}
  {#if !stick}
    <button class="stick-pill primary" onclick={jumpToLatest}>Jump to latest &darr;</button>
  {/if}
</div>

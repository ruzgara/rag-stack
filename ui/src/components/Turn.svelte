<script lang="ts">
  // THE one renderer for a question+answer pair -- live and reopened turns
  // are the exact same Turn shape (lib/turns.ts), so this component cannot
  // tell which path built the one it was given. See plan §7.
  import type { Turn } from "../lib/turns";
  import { deriveInvalid } from "../lib/turns";
  import { ui } from "../state/ui.svelte";
  import Markdown from "./Markdown.svelte";
  import Thinking from "./Thinking.svelte";
  import StatusLine from "./StatusLine.svelte";

  interface Props {
    turn: Turn;
  }
  const { turn }: Props = $props();

  const known = $derived(new Set(turn.sources.map((s) => s.number)));
  const invalid = $derived(turn.status !== "streaming" ? deriveInvalid(turn.answer, turn.sources) : []);

  let bodyEl: HTMLDivElement | undefined = $state();

  // Delegated citation click (defect 2 fix): the click becomes state, not a
  // second DOM render racing the first. No ancestor handler exists to bubble
  // into and clobber the flash, because there isn't one -- selecting a
  // turn's sources is the separate "Sources" button below, not this click.
  // The individual .cite spans carry tabindex="0" and role="button"
  // (citations.ts), so this delegates the same activation on Enter/Space --
  // the wrapper itself is not interactive, only the citations inside it are.
  function activateCitation(el: Element | null): void {
    if (!(el instanceof HTMLElement) || !el.dataset.nums) return;
    const nums = el.dataset.nums.split(",").map(Number);
    ui.focusSources(nums, turn.key);
  }

  function onBodyClick(e: MouseEvent): void {
    activateCitation((e.target as Element).closest(".cite"));
  }

  function onBodyKeydown(e: KeyboardEvent): void {
    if (e.key !== "Enter" && e.key !== " ") return;
    const el = (e.target as Element).closest(".cite");
    if (!el) return;
    e.preventDefault();
    activateCitation(el);
  }
</script>

<div class="msg user">
  <div class="who">You</div>
  <div class="body">{turn.question}</div>
</div>

<div class="msg assistant">
  <div class="who">Assistant</div>
  <StatusLine events={turn.events} />
  <Thinking text={turn.thinking} />
  <div bind:this={bodyEl} onclick={onBodyClick} onkeydown={onBodyKeydown} role="presentation">
    <Markdown answer={turn.answer} status={turn.status} {known} />
  </div>
  {#if turn.status === "error" && turn.error}
    <div class="err-text">{turn.error}</div>
  {/if}
  {#if invalid.length > 0}
    <div class="small err-text">invalid citation{invalid.length > 1 ? "s" : ""}: [{invalid.join(", ")}]</div>
  {/if}
  <div class="footer-actions">
    {#if turn.sources.length > 0}
      <button class="small" onclick={() => ui.showTurnSources(turn.key)}>
        Sources ({turn.sources.length})
      </button>
    {/if}
    {#if turn.stats}
      <span class="small">
        {turn.stats.total_s.toFixed(1)}s
        {#if turn.stats.first_token_s !== null}&middot; first token {turn.stats.first_token_s.toFixed(1)}s{/if}
        {#if turn.stats.tool_rounds}&middot; {turn.stats.tool_rounds} tool round(s){/if}
      </span>
    {/if}
  </div>
</div>

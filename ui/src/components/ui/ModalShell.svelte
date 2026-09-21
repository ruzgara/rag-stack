<script lang="ts">
  // The design's popup frame: a dimmed backdrop over the whole app and a
  // centred card. Backdrop click and Escape close only the TOP popup (the
  // ui.modals stack), so Sharing opened from a Library popup returns to it.
  import type { Snippet } from "svelte";
  import { ui } from "../../state/ui.svelte";

  interface Props {
    /** Card width in px (the design uses 520 / 760 / 820 / 940). */
    width?: number;
    kicker?: string;
    title?: string;
    /** Right of the title: tags, filters. The × is added after it. */
    headerExtra?: Snippet;
    /** Under the title: meta line. */
    subtitle?: Snippet;
    children: Snippet;
    footer?: Snippet;
    /** Tall popups scroll their body; short ones size to content. */
    scroll?: boolean;
    onclose?: () => void;
    /** Leading element before the title block (e.g. the source viewer's [n]). */
    lead?: Snippet;
    labelledby?: string;
  }
  const { width = 520, kicker, title, headerExtra, subtitle, children, footer, scroll = true, onclose, lead,
          labelledby }: Props = $props();

  const titleId = `modal-${Math.random().toString(36).slice(2)}`;
  let card: HTMLDivElement | undefined = $state();

  function close(): void {
    if (onclose) onclose();
    else ui.close();
  }

  function onKey(e: KeyboardEvent): void {
    // Only the topmost shell reacts; a confirm dialog on top handles its own.
    if (e.key !== "Escape" || ui.dialog) return;
    const shells = document.querySelectorAll(".modal-backdrop");
    if (shells[shells.length - 1] !== card?.parentElement) return;
    e.preventDefault();
    close();
  }

  $effect(() => {
    const prev = document.activeElement as HTMLElement | null;
    queueMicrotask(() => {
      // Land on the card itself, not the first field: text inputs and
      // selects always show their focus ring in Chrome regardless of how
      // they were focused, so auto-jumping into one paints a ring the
      // instant the popup opens, before the person has touched anything.
      // A modal that genuinely wants immediate typing opts in with autofocus.
      const el = card?.querySelector<HTMLElement>("[autofocus]") ?? card;
      el?.focus({ preventScroll: true });
    });
    return () => prev?.focus?.({ preventScroll: true });
  });
</script>

<svelte:window onkeydown={onKey} />

<!-- svelte-ignore a11y_click_events_have_key_events -->
<div class="modal-backdrop" role="presentation" onclick={(e) => { if (e.target === e.currentTarget) close(); }}>
  <div
    bind:this={card}
    class="modal-card"
    class:scroll
    style:--w="{width}px"
    role="dialog"
    aria-modal="true"
    aria-labelledby={labelledby ?? (title ? titleId : undefined)}
    tabindex="-1"
  >
    {#if title || kicker || headerExtra || lead}
      <header class="modal-head">
        {#if lead}{@render lead()}{/if}
        <div class="modal-titles">
          {#if kicker}<div class="kicker">{kicker}</div>{/if}
          {#if title}<h3 id={titleId}>{title}</h3>{/if}
          {#if subtitle}<div class="modal-sub">{@render subtitle()}</div>{/if}
        </div>
        {#if headerExtra}<div class="modal-extra">{@render headerExtra()}</div>{/if}
        <button class="modal-x" onclick={close} aria-label="Close">×</button>
      </header>
    {/if}
    <div class="modal-body rag-scroll">
      {@render children()}
    </div>
    {#if footer}
      <footer class="modal-foot">{@render footer()}</footer>
    {/if}
  </div>
</div>

<style>
  .modal-backdrop {
    position: fixed; inset: 0; z-index: 60;
    display: grid; place-items: center; padding: 36px;
    background: color-mix(in srgb, var(--color-neutral-900) 42%, transparent);
    animation: fade .12s ease-out;
  }
  @media (prefers-color-scheme: dark) {
    .modal-backdrop { background: rgb(0 0 0 / 55%); }
  }
  .modal-card {
    width: min(var(--w), 100%);
    max-height: 86vh;
    display: flex; flex-direction: column;
    background: var(--color-bg); border-radius: var(--radius-lg); box-shadow: var(--shadow-lg);
    padding: 24px 26px 18px;
    animation: rise .14s ease-out;
  }
  .modal-card:focus { outline: none; }
  .modal-head { display: flex; align-items: flex-start; gap: 14px; margin-bottom: 12px; }
  .modal-titles { flex: 1; min-width: 0; }
  .modal-titles h3 { margin: 0; font-size: 26px; overflow-wrap: anywhere; }
  .modal-sub { margin-top: 6px; font-size: 12px; color: var(--color-neutral-700); }
  .modal-extra { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; justify-content: flex-end; align-self: center; }
  .modal-x {
    border: 0; background: transparent; font-size: 20px; line-height: 1; cursor: pointer;
    color: var(--color-neutral-700); padding: 2px 4px; border-radius: var(--radius-md);
  }
  .modal-x:hover { background: color-mix(in srgb, var(--color-text) 7%, transparent); }
  .modal-body { min-height: 0; }
  .scroll .modal-body { flex: 1; overflow-y: auto; }
  .modal-foot { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; padding-top: 14px; }
  @keyframes fade { from { opacity: 0 } }
  @keyframes rise { from { opacity: 0; transform: translateY(6px) } }
  @media (max-width: 640px) {
    .modal-backdrop { padding: 12px; }
    .modal-card { padding: 18px 16px 14px; max-height: 92vh; }
    .modal-titles h3 { font-size: 22px; }
  }
  @media (prefers-reduced-motion: reduce) {
    .modal-backdrop, .modal-card { animation: none; }
  }
</style>

<script lang="ts">
  // A vertical drag handle on one edge of its (positioned) parent. Dragging,
  // or arrow keys when focused, changes `width`; double-click resets it.
  let {
    width = $bindable(),
    edge,
    min,
    max,
    reset,
    label,
    oncommit,
  }: {
    width: number;
    /** Which edge of the parent the handle sits on; dragging away from the
     *  parent grows it. */
    edge: "left" | "right";
    min: number;
    /** Upper bound, computed at drag start so it can depend on the viewport. */
    max: () => number;
    reset: number;
    label: string;
    oncommit?: () => void;
  } = $props();

  let el: HTMLDivElement;
  let dragging = $state(false);
  let startX = 0;
  let startW = 0;
  let limit = 0;

  $effect(() => {
    document.body.classList.toggle("resizing", dragging);
    return () => document.body.classList.remove("resizing");
  });

  const clamp =(w: number) => Math.round(Math.max(min, Math.min(limit, w)));

  function down(e: PointerEvent) {
    if (e.button !== 0) return;
    e.preventDefault();
    el.setPointerCapture(e.pointerId);
    dragging = true;
    startX = e.clientX;
    // The rendered width, which CSS may have capped below the stored one.
    startW = el.parentElement?.getBoundingClientRect().width ?? width;
    limit = Math.max(min, max());
  }

  function move(e: PointerEvent) {
    if (!dragging) return;
    const dx = e.clientX - startX;
    width = clamp(edge === "right" ? startW + dx : startW - dx);
  }

  function up() {
    if (!dragging) return;
    dragging = false;
    oncommit?.();
  }

  function key(e: KeyboardEvent) {
    const step = e.shiftKey ? 40 : 10;
    const grow = edge === "right" ? "ArrowRight" : "ArrowLeft";
    const shrink = edge === "right" ? "ArrowLeft" : "ArrowRight";
    if (e.key !== grow && e.key !== shrink) return;
    e.preventDefault();
    limit = Math.max(min, max());
    width = clamp(width + (e.key === grow ? step : -step));
    oncommit?.();
  }

  function dbl() {
    width = reset;
    oncommit?.();
  }
</script>

<!-- A focusable separator is interactive per ARIA; svelte's a11y lint doesn't know. -->
<!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
<div
  bind:this={el}
  class="handle {edge}"
  class:dragging
  role="separator"
  aria-orientation="vertical"
  aria-label={label}
  aria-valuenow={width}
  aria-valuemin={min}
  tabindex="0"
  title="Drag to resize · double-click to reset"
  onpointerdown={down}
  onpointermove={move}
  onpointerup={up}
  onpointercancel={up}
  onkeydown={key}
  ondblclick={dbl}
></div>

<style>
  .handle {
    position: absolute; top: 0; bottom: 0; width: 9px; z-index: 5;
    cursor: col-resize; touch-action: none;
  }
  .handle.right { right: -5px; }
  .handle.left { left: -5px; }
  .handle::after {
    content: ""; position: absolute; top: 0; bottom: 0; left: 4px; width: 1px;
    background: transparent; transition: background .12s ease;
  }
  .handle:hover::after, .handle.dragging::after, .handle:focus-visible::after {
    background: var(--color-accent); width: 2px; left: 3.5px;
  }
  .handle:focus-visible { outline: none; }
  :global(body.resizing) { cursor: col-resize; user-select: none; }
  :global(body.resizing iframe) { pointer-events: none; }
</style>

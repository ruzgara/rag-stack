<script lang="ts">
  import { ui } from "../../state/ui.svelte";
</script>

<div class="toasts" aria-live="polite">
  {#each ui.toasts as t (t.id)}
    <div class="toast" class:err={t.kind === "err"} role={t.kind === "err" ? "alert" : "status"}>
      <span>{t.message}</span>
      <button onclick={() => ui.dismissToast(t.id)} aria-label="Dismiss">×</button>
    </div>
  {/each}
</div>

<style>
  .toasts {
    position: fixed; left: 50%; bottom: 20px; transform: translateX(-50%); z-index: 100;
    display: flex; flex-direction: column; gap: 8px; align-items: center; width: min(520px, calc(100vw - 32px));
    pointer-events: none;
  }
  .toast {
    pointer-events: auto; display: flex; align-items: flex-start; gap: 12px; width: 100%;
    background: var(--color-text); color: var(--color-bg); border-radius: var(--radius-lg);
    box-shadow: var(--shadow-lg); padding: 10px 12px 10px 16px; font-size: 13.5px; line-height: 1.45;
    white-space: pre-wrap; overflow-wrap: anywhere; animation: up .16s ease-out;
    border-left: 3px solid var(--color-accent);
  }
  .toast.err { border-left-color: var(--color-accent-2-500); }
  .toast span { flex: 1; }
  button { border: 0; background: transparent; color: inherit; opacity: .7; cursor: pointer; font-size: 16px; line-height: 1; padding: 0 2px; }
  button:hover { opacity: 1; }
  @keyframes up { from { opacity: 0; transform: translateY(6px) } }
</style>

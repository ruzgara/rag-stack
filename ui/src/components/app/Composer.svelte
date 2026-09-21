<script lang="ts">
  import { chat } from "../../state/chat.svelte";
  import { session } from "../../state/session.svelte";
  import { ui, guard } from "../../state/ui.svelte";
  import { workspace } from "../../state/workspace.svelte";
  import { plural } from "../../lib/format";
  import type { Preset } from "../../lib/types";

  let textarea: HTMLTextAreaElement | undefined = $state();

  const streaming = $derived(chat.activeStreaming);
  const libCount = $derived(workspace.libraries.length);
  const model = $derived(session.presetInfo(chat.preset)?.model ?? null);

  const placeholder = $derived(
    streaming ? "Ask a follow-up…"
    : chat.active?.turns.length ? "Ask a follow-up…"
    : libCount ? `Ask across ${plural(libCount, "library", "libraries")}…`
    : "Ask anything — this workspace has no libraries to search yet…",
  );

  const loadMeta = $derived.by(() => {
    const load = session.quota?.load;
    if (!load) return "";
    const parts: string[] = [];
    if (streaming) parts.push("streaming");
    if (load.queued) parts.push(`${load.queued} queued`);
    if (load.running && !streaming) parts.push(`${load.running} running`);
    return parts.join(" · ") || "idle";
  });

  // Autosize: grows with the text up to a cap, then scrolls.
  $effect(() => {
    void chat.draft;
    if (!textarea) return;
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 240)}px`;
  });

  // Focus on switching chats (not on touch devices, where it pops a keyboard).
  $effect(() => {
    void chat.activeId;
    if (matchMedia("(pointer: fine)").matches) queueMicrotask(() => textarea?.focus({ preventScroll: true }));
  });

  function submit(): void {
    if (streaming) return;
    void chat.send(chat.draft);
  }

  function onKey(e: KeyboardEvent): void {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      submit();
    }
  }

  const setPreset = (p: Preset) => guard(() => chat.setPreset(p))();
</script>

<div class="composer-wrap">
  <form class="composer" onsubmit={(e) => { e.preventDefault(); submit(); }}>
    <label class="visually-hidden" for="composer-input">Message</label>
    <textarea
      id="composer-input"
      bind:this={textarea}
      bind:value={chat.draft}
      onkeydown={onKey}
      {placeholder}
      rows="2"
    ></textarea>
    <div class="bar">
      {#if model}
        <span class="model" title="The model this preset runs (set per preset in .env)">{model}</span>
      {/if}
      <div class="seg" role="group" aria-label="Preset">
        <button type="button" aria-pressed={chat.preset === "hard"} onclick={() => setPreset("hard")}>Hard</button>
        <button type="button" aria-pressed={chat.preset === "everyday"} onclick={() => setPreset("everyday")}>Everyday</button>
      </div>
      <button type="button" class="attach" onclick={() => ui.open({ kind: "upload" })}>＋ Attach documents</button>
      <span class="load" aria-live="polite">{loadMeta}</span>
      {#if streaming}
        <button type="button" class="btn btn-primary send stop" onclick={guard(() => chat.stop())}>Stop</button>
      {:else}
        <button type="submit" class="btn btn-primary send" disabled={!chat.draft.trim()}>Send</button>
      {/if}
    </div>
  </form>
</div>

<style>
  .composer-wrap { padding: 6px clamp(16px, 5vw, 64px) 22px; }
  .composer {
    max-width: 720px; margin: 0 auto; background: var(--color-surface); border-radius: 3px;
    padding: 12px 14px 10px; box-shadow: var(--shadow-sm);
  }
  .composer:focus-within { box-shadow: var(--shadow-md); }
  textarea {
    display: block; width: 100%; min-height: 46px; resize: none; border: 0; background: transparent; padding: 0;
    font: inherit; font-size: 15.5px; line-height: 1.5; color: var(--color-text); caret-color: var(--color-accent);
  }
  textarea::placeholder { color: var(--color-neutral-600); }
  textarea:focus-visible { outline: none; }
  .bar { display: flex; align-items: center; gap: 8px; margin-top: 6px; flex-wrap: wrap; }
  .model {
    display: inline-flex; align-items: center; border: 1px solid var(--color-divider); border-radius: var(--radius-md);
    padding: 4px 9px; font-family: var(--font-heading); font-size: 12.5px; color: var(--color-text);
  }
  .attach {
    display: flex; align-items: center; gap: 6px; border: 0; background: transparent; cursor: pointer;
    font-family: var(--font-heading); font-size: 12.5px; color: var(--color-accent-700); padding: 4px 6px;
    border-radius: var(--radius-md);
  }
  .attach:hover { background: color-mix(in srgb, var(--color-accent) 10%, transparent); }
  .load { margin-left: auto; font-size: 11px; color: var(--color-neutral-600); }
  .send { padding: 6px 16px; }
  .stop { background: var(--color-accent-2-600); }
  .stop:hover:not(:disabled) { background: var(--color-accent-2-700); }
  @media (max-width: 560px) {
    .model { display: none; }
    .attach { font-size: 0; padding: 4px 8px; }
    .attach::before { content: "＋"; font-size: 14px; }
  }
</style>

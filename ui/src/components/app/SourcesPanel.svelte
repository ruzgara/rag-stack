<script lang="ts">
  import { chat } from "../../state/chat.svelte";
  import { session } from "../../state/session.svelte";
  import { ui, guard } from "../../state/ui.svelte";
  import { initials, locator } from "../../lib/format";
  import type { Source } from "../../lib/types";

  const turn = $derived.by(() => {
    const turns = chat.active?.turns ?? [];
    return turns.find((t) => t.key === ui.selectedTurnKey) ?? turns[turns.length - 1] ?? null;
  });
  const isLatest = $derived(turn !== null && turn === chat.active?.turns[chat.active.turns.length - 1]);

  // Carried-forward sources ("earlier") this answer did not itself cite
  // weren't retrieved for this turn -- they're excluded from the panel
  // entirely, not just uncited-styled, and from the "N retrieved" count.
  const retrieved = $derived((turn?.sources ?? []).filter((s) => s.found_by !== "earlier" || s.cited));
  // Mid-stream nothing is cited yet (`cited` arrives with `done`), so every
  // source shows as retrieved until then.
  const cited = $derived(retrieved.filter((s) => s.cited)
    .sort((a, b) => (a.doc_number ?? 0) - (b.doc_number ?? 0) || (a.chunk_number ?? 0) - (b.chunk_number ?? 0)));
  const uncited = $derived(retrieved.filter((s) => !s.cited));

  function scoreColor(s: Source): string {
    return (s.rerank_score ?? 0) >= 0.8 ? "var(--color-accent-700)" : "var(--color-neutral-600)";
  }

  function openSource(s: Source): void {
    if (!turn) return;
    ui.open({ kind: "source", turnKey: turn.key, handle: s.handle });
  }

  // Scroll the first focused card into view (citation hover/click).
  let list: HTMLDivElement | undefined = $state();
  $effect(() => {
    const handle = ui.focusedSources[0];
    if (handle === undefined || !list) return;
    list.querySelector(`[data-handle="${handle}"]`)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  });

  let menuEl: HTMLDivElement | undefined = $state();
  function onDocClick(e: MouseEvent): void {
    if (ui.userMenuOpen && menuEl && !menuEl.contains(e.target as Node)) ui.userMenuOpen = false;
  }
  function onKey(e: KeyboardEvent): void {
    if (e.key === "Escape" && ui.userMenuOpen) ui.userMenuOpen = false;
  }

  const signOut = guard(async () => {
    ui.closeAll();
    await session.logout();
  });
</script>

<svelte:document onclick={onDocClick} onkeydown={onKey} />

{#snippet card(s: Source)}
  <button class="src" class:focused={ui.focusedSources.includes(s.handle)} class:uncited={!s.cited && turn?.status !== "streaming"}
          data-handle={s.handle} onclick={() => openSource(s)}>
    <div class="src-head">
      {#if s.doc_number !== null}<span class="n">[{s.doc_number}:{s.chunk_number}]</span>{/if}
      <span class="title">{s.filename}</span>
    </div>
    <div class="src-meta">
      <span class="loc">{locator(s.pages, s.headings)}</span>
      {#if s.rerank_score !== null}
        <span class="score" style:color={scoreColor(s)}>{s.rerank_score.toFixed(2)}</span>
      {/if}
    </div>
    <p class="text">{s.text}</p>
  </button>
{/snippet}

<aside class="panel">
  <div class="user" bind:this={menuEl}>
    <button class="user-btn" onclick={() => (ui.userMenuOpen = !ui.userMenuOpen)} aria-expanded={ui.userMenuOpen} aria-haspopup="menu">
      <span class="avatar">{session.me ? initials(session.me.user.display_name, session.me.user.email) : ""}</span>
      <span class="uname ellipsis">{session.displayName}</span>
      <span class="caret">▾</span>
    </button>
    {#if ui.userMenuOpen}
      <div class="menu" role="menu">
        <button role="menuitem" onclick={() => ui.open({ kind: "libraries" })}>Libraries</button>
        <button role="menuitem" onclick={() => ui.open({ kind: "groups" })}>Groups</button>
        <button role="menuitem" onclick={() => ui.open({ kind: "settings" })}>Settings</button>
        {#if session.isAdmin}
          <a role="menuitem" href="/admin" rel="external">Admin console</a>
        {/if}
        <div class="sep"></div>
        <button role="menuitem" class="signout" onclick={signOut}>Sign out</button>
      </div>
    {/if}
  </div>

  <div class="head">
    <h6>Sources</h6>
    {#if turn && retrieved.length}
      <span class="counts">{turn.status !== "streaming" ? `${cited.length} cited · ` : ""}{retrieved.length} retrieved</span>
    {/if}
  </div>
  {#if turn && !isLatest && retrieved.length}
    <button class="back" onclick={() => (ui.selectedTurnKey = null)}>Showing an earlier answer · back to latest</button>
  {/if}

  <div class="list rag-scroll" bind:this={list}>
    {#if !turn || retrieved.length === 0}
      <p class="empty">
        {#if turn?.status === "streaming"}Passages appear here as the answer searches.
        {:else if turn}This answer didn't draw on any documents.
        {:else}The passages and answer cites appear here, marked [doc:chunk] as it cites them.{/if}
      </p>
    {:else}
      {#each cited as s (s.handle)}{@render card(s)}{/each}
      {#if uncited.length}
        {#if cited.length}<div class="sub">Retrieved, not cited</div>{/if}
        {#each uncited as s (s.handle)}{@render card(s)}{/each}
      {/if}
    {/if}
  </div>
</aside>

<style>
  .panel { display: flex; flex-direction: column; min-height: 0; height: 100%; background: var(--color-surface); padding: 14px 0; }
  .user { padding: 0 14px 14px; position: relative; }
  .user-btn {
    display: flex; align-items: center; gap: 9px; width: 100%; background: var(--color-bg); border: 0; cursor: pointer;
    padding: 9px 12px; border-radius: var(--radius-md); box-shadow: var(--shadow-sm); color: var(--color-text);
  }
  .user-btn:hover { box-shadow: var(--shadow-md); }
  .avatar {
    width: 26px; height: 26px; flex: none; border-radius: 50%; background: var(--color-accent-2-500); color: var(--color-bg);
    display: grid; place-items: center; font-family: var(--font-heading); font-size: 12px;
  }
  .uname { font-family: var(--font-heading); font-size: 14.5px; }
  .caret { margin-left: auto; font-size: 10px; color: var(--color-neutral-600); }
  .menu {
    position: absolute; top: 52px; right: 14px; left: 14px; z-index: 40; background: var(--color-bg);
    border-radius: 3px; box-shadow: var(--shadow-lg); padding: 5px; display: flex; flex-direction: column;
  }
  .menu > button, .menu > a {
    text-align: left; border: 0; background: transparent; cursor: pointer; padding: 7px 10px;
    font-family: var(--font-heading); font-size: 13.5px; border-radius: var(--radius-md); color: var(--color-text);
    text-decoration: none;
  }
  .menu > button:hover, .menu > a:hover { background: color-mix(in srgb, var(--color-accent) 12%, transparent); }
  .menu .signout { color: var(--color-accent-700); }
  .sep { height: 1px; background: var(--color-divider); margin: 4px 8px; }

  .head { display: flex; align-items: baseline; gap: 8px; padding: 0 16px 8px; }
  h6 { margin: 0; font-size: 10px; letter-spacing: .14em; color: var(--color-neutral-700); }
  .counts { font-size: 10.5px; color: var(--color-neutral-600); margin-left: auto; }
  .back {
    margin: 0 12px 8px; border: 0; background: var(--color-accent-100); color: var(--color-accent-800);
    font: inherit; font-size: 11.5px; padding: 5px 9px; border-radius: var(--radius-md); cursor: pointer; text-align: left;
  }
  .list { flex: 1; min-height: 0; overflow-y: auto; padding: 0 12px 8px; display: flex; flex-direction: column; gap: 8px; }
  .empty { font-size: 12.5px; line-height: 1.55; color: var(--color-neutral-600); padding: 4px 4px; font-style: italic; }
  .sub { font-size: 10px; letter-spacing: .14em; text-transform: uppercase; color: var(--color-neutral-600); padding: 8px 4px 0; }

  .src {
    display: block; width: 100%; text-align: left; border: 0; font: inherit; color: inherit;
    cursor: pointer; background: var(--color-bg); border-radius: var(--radius-md); padding: 9px 11px 10px;
    box-shadow: var(--shadow-sm); transition: box-shadow .15s, opacity .15s;
  }
  .src:hover { box-shadow: var(--shadow-md); }
  .src.focused { box-shadow: 0 0 0 1.5px var(--color-accent), var(--shadow-md); }
  .src.uncited { opacity: .72; }
  .src.uncited:hover { opacity: 1; }
  .src-head { display: flex; align-items: baseline; gap: 7px; margin-bottom: 4px; }
  .n { font-family: var(--font-heading); font-size: 11.5px; font-weight: 600; color: var(--color-accent-700); flex: none; }
  .title { font-size: 12.5px; font-family: var(--font-heading); line-height: 1.25; flex: 1; min-width: 0; overflow-wrap: anywhere; }
  .src-meta { display: flex; align-items: baseline; gap: 8px; font-size: 10.5px; color: var(--color-neutral-600); margin-bottom: 5px; }
  .loc { flex: 1; min-width: 0; }
  .score { flex: none; font-variant-numeric: tabular-nums; }
  .text {
    margin: 0; font-size: 12px; line-height: 1.5; color: var(--color-neutral-800);
    display: -webkit-box; -webkit-line-clamp: 4; line-clamp: 4; -webkit-box-orient: vertical; overflow: hidden;
  }
</style>

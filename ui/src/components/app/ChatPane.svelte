<script lang="ts">
  import { tick } from "svelte";
  import { chat } from "../../state/chat.svelte";
  import { library } from "../../state/library.svelte";
  import { session } from "../../state/session.svelte";
  import { ui } from "../../state/ui.svelte";
  import { workspace } from "../../state/workspace.svelte";
  import { clock, plural } from "../../lib/format";
  import TurnView from "./TurnView.svelte";
  import Composer from "./Composer.svelte";

  const view = $derived(chat.active);
  const hasLibraries = $derived(workspace.libraries.length > 0);
  const empty = $derived(!chat.activeId && workspace.current !== null && !hasLibraries && library.loaded);

  const title = $derived(view?.title ?? (view ? "Untitled chat" : ""));
  const listRow = $derived(chat.activeId ? workspace.conversations.find((c) => c.id === chat.activeId) : undefined);
  const chatMeta = $derived.by(() => {
    if (!view) return "";
    const parts: string[] = [];
    const messages = Math.max(listRow?.message_count ?? 0, view.turns.length * 2 - (chat.activeStreaming ? 1 : 0));
    if (messages) parts.push(plural(messages, "message"));
    const lastStats = [...view.turns].reverse().find((t) => t.stats?.context_used_pct != null)?.stats;
    if (lastStats?.context_used_pct != null) parts.push(`context ${Math.round(lastStats.context_used_pct)}% full`);
    return parts.join(" · ");
  });

  // Empty-state figures (design: Available to you / Via groups / Index).
  const viaGroups = $derived(library.libraries.filter((l) => l.source === "group"));
  const index = $derived.by(() => {
    const h = session.health;
    const ingest = library.documents?.ingest;
    if (ingest?.running) return { label: "Indexing", note: "ingest is running now" };
    if (h && !h.qdrant.ok) return { label: "Offline", note: "the vector index isn't reachable" };
    const last = ingest?.last_run;
    const waiting = library.documents?.inbox.length ?? 0;
    const note = [
      h?.qdrant.ok && h.qdrant.chunks != null ? `${h.qdrant.chunks.toLocaleString()} chunks` : "",
      last ? `last run ${clock(last.finished_at)}` : "",
      waiting ? `${waiting} waiting` : "",
    ].filter(Boolean).join(" · ");
    return { label: h ? "Ready" : "—", note };
  });

  // Stick to the bottom while an answer streams, unless the reader scrolled up.
  let scroller: HTMLDivElement | undefined = $state();
  let pinned = true;
  function onScroll(): void {
    if (!scroller) return;
    pinned = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
  }
  $effect(() => {
    // Re-run on any growth of the active transcript.
    const last = view?.turns[view.turns.length - 1];
    void last?.answer.length;
    void last?.thinking.length;
    void last?.trace.length;
    if (pinned && scroller) scroller.scrollTop = scroller.scrollHeight;
  });
  // Opening a chat or sending: jump to the end.
  $effect(() => {
    void chat.activeId;
    void view?.turns.length;
    void view?.loaded;
    pinned = true;
    void tick().then(() => { if (scroller) scroller.scrollTop = scroller.scrollHeight; });
  });

  const cited = $derived.by(() => {
    const t = view?.turns.find((x) => x.key === ui.selectedTurnKey) ?? view?.turns[view.turns.length - 1];
    return t?.sources.length ?? 0;
  });
</script>

<main class="pane">
  <div class="topbar">
    <button class="icon-btn" onclick={() => (ui.railOpen = true)} aria-label="Open workspace and chats">
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M2 4h12M2 8h12M2 12h12"/></svg>
    </button>
    <span class="topbar-title ellipsis">{workspace.current?.name ?? ""}</span>
    <button class="icon-btn sources-btn" onclick={() => (ui.sourcesOpen = true)} aria-label="Sources">
      Sources{cited ? ` (${cited})` : ""}
    </button>
  </div>

  {#if empty}
    <div class="empty">
      <div class="kicker">{workspace.current?.name ?? "Workspace"}</div>
      <h1>Nothing filed yet.</h1>
      <p class="lede">Add libraries to this workspace and every chat here can search them. Nothing leaves the machine.</p>
      <div class="row">
        <button class="btn btn-primary" onclick={() => ui.open({ kind: "libraries" })}>Add libraries</button>
        <button class="btn btn-secondary" onclick={() => ui.open({ kind: "upload" })}>Upload documents</button>
      </div>
      <div class="figures">
        <div>
          <div class="fig-label">Available to you</div>
          <div class="fig">{library.libraries.length}</div>
          <div class="fig-note">{plural(library.libraries.length, "library", "libraries")} · {plural(library.totalDocuments, "document")}</div>
        </div>
        <div>
          <div class="fig-label">Via groups</div>
          <div class="fig">{viaGroups.length}</div>
          <div class="fig-note">{session.me?.user.groups.join(" · ") || "not in any group"}</div>
        </div>
        <div>
          <div class="fig-label">Index</div>
          <div class="fig">{index.label}</div>
          <div class="fig-note">{index.note}</div>
        </div>
      </div>
      <p class="aside muted">You can still chat here without libraries — answers just won't search anything.</p>
    </div>
    <Composer />
  {:else}
    <div class="scroll rag-scroll" bind:this={scroller} onscroll={onScroll}>
      <div class="column">
        {#if view}
          <div class="head">
            <h2>{title}</h2>
            <span class="meta">{chatMeta}</span>
          </div>
          {#if !view.loaded && chat.loadingId === view.id}
            <p class="muted">Loading…</p>
          {/if}
          {#each view.turns as t, i (t.key)}
            <TurnView turn={t} isLast={i === view.turns.length - 1} preset={view.preset} busy={chat.activeStreaming} />
          {/each}
        {:else if workspace.current}
          <div class="intro">
            <div class="kicker">New chat · {workspace.current.name}</div>
            <h2>What are you looking for?</h2>
            {#if hasLibraries}
              <p class="lede">Answers search {plural(workspace.libraries.length, "library", "libraries")} in this workspace and cite the passages they rely on.</p>
              <div class="lib-tags">
                {#each workspace.libraries as lib (lib.id)}
                  <button class="tag tag-neutral" onclick={() => ui.open({ kind: "library", id: lib.id })}>{lib.name} · {lib.document_count}</button>
                {/each}
              </div>
            {:else}
              <p class="lede">This workspace has no libraries yet, so nothing will be searched. <button class="linkish" onclick={() => ui.open({ kind: "libraries" })}>Add some</button>.</p>
            {/if}
          </div>
        {:else if workspace.workspaces.length === 0 && library.loaded}
          <div class="intro">
            <h2>No workspace</h2>
            <p class="lede">Create one to start chatting. <button class="linkish" onclick={() => ui.open({ kind: "workspaces" })}>Workspaces…</button></p>
          </div>
        {/if}
      </div>
    </div>
    <Composer />
  {/if}
</main>

<style>
  .pane { display: flex; flex-direction: column; min-height: 0; min-width: 0; position: relative; }
  .scroll { flex: 1; min-height: 0; overflow-y: auto; padding: 26px clamp(16px, 5vw, 64px) 8px; }
  .column { max-width: 720px; margin: 0 auto; }
  .head { display: flex; align-items: baseline; gap: 12px; margin-bottom: 26px; }
  .head h2 { font-size: 26px; margin: 0; min-width: 0; overflow-wrap: anywhere; }
  .meta { font-size: 11px; color: var(--color-neutral-600); margin-left: auto; text-align: right; flex: none; }

  .intro { padding-top: clamp(20px, 12vh, 120px); }
  .intro h2 { font-size: 34px; margin-bottom: 12px; }
  .lede { font-size: 16px; line-height: 1.6; max-width: 48ch; color: var(--color-neutral-800); margin-bottom: 18px; }
  .lib-tags { display: flex; flex-wrap: wrap; gap: 6px; }
  .linkish { border: 0; background: none; padding: 0; font: inherit; color: var(--color-accent-700); cursor: pointer; text-decoration: underline; text-underline-offset: 3px; }

  .empty {
    flex: 1; display: flex; flex-direction: column; justify-content: center;
    padding: 24px clamp(20px, 7vw, 96px); max-width: 820px; overflow-y: auto;
  }
  .empty h1 { font-size: 46px; line-height: 1.05; margin: 0 0 14px; }
  .empty .lede { max-width: 44ch; margin-bottom: 26px; }
  .row { display: flex; gap: 10px; flex-wrap: wrap; }
  .figures { margin-top: 44px; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 26px; max-width: 640px; }
  .fig-label { font-size: 11px; letter-spacing: .1em; text-transform: uppercase; color: var(--color-neutral-600); margin-bottom: 5px; }
  .fig { font-size: 26px; font-weight: 600; font-family: var(--font-heading); }
  .fig-note { font-size: 12px; color: var(--color-neutral-700); }
  .aside { font-size: 12.5px; margin-top: 30px; }

  .topbar { display: none; align-items: center; gap: 10px; padding: 10px 14px; border-bottom: 1px solid var(--color-divider); }
  .topbar-title { flex: 1; font-family: var(--font-heading); font-weight: 600; font-size: 15px; }
  .icon-btn {
    border: 0; background: transparent; cursor: pointer; padding: 5px 8px; border-radius: var(--radius-md);
    font-family: var(--font-heading); font-size: 13px; color: var(--color-text); display: flex; align-items: center;
  }
  .icon-btn:hover { background: color-mix(in srgb, var(--color-text) 7%, transparent); }
  .sources-btn { color: var(--color-accent-700); display: none; }

  @media (max-width: 1180px) {
    .topbar { display: flex; }
    .sources-btn { display: flex; }
  }
  @media (min-width: 861px) and (max-width: 1180px) {
    .topbar > .icon-btn:first-child { display: none; }
  }
  @media (max-width: 640px) {
    .figures { grid-template-columns: 1fr 1fr; }
    .empty h1 { font-size: 36px; }
    .head { flex-direction: column; gap: 4px; }
    .meta { margin-left: 0; text-align: left; }
  }
</style>

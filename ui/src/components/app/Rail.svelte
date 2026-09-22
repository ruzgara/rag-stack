<script lang="ts">
  import { chat } from "../../state/chat.svelte";
  import { library, libraryKind, kindColor } from "../../state/library.svelte";
  import { navigate } from "../../state/route.svelte";
  import { session } from "../../state/session.svelte";
  import { ui, guard } from "../../state/ui.svelte";
  import { workspace } from "../../state/workspace.svelte";
  import { when, plural, initials } from "../../lib/format";
  import type { ConversationListItem } from "../../lib/types";

  const ws = $derived(workspace.current);
  const wsMeta = $derived.by(() => {
    if (!ws) return "";
    const libs = ws.library_ids.length ? plural(ws.library_ids.length, "library", "libraries") : "no libraries";
    const chats = workspace.conversationsLoaded ? workspace.conversations.length : (ws.conversation_count ?? 0);
    return `${libs} · ${plural(chats, "chat")}`;
  });

  const unread = (c: ConversationListItem) => c.id !== chat.activeId && c.last_message_at > c.last_read_at + 0.5;

  function newChat(): void {
    ui.railOpen = false;
    navigate("/");
  }

  const removeLib = (id: string, name: string) => guard(async () => {
    await workspace.removeLibrary(id);
    ui.toast(`Removed ${name} from this workspace`);
  })();

  const archive = (id: string) => guard(async () => {
    await workspace.archiveConversation(id);
    chat.drop(id);
    ui.toast("Archived — find it under Recently deleted › Archived");
  })();

  const remove = (c: ConversationListItem) => guard(async () => {
    const ok = await ui.confirm({
      title: `Delete “${c.title ?? "Untitled chat"}”?`,
      body: "It moves to Recently deleted, where you can restore it.",
      okLabel: "Delete", danger: true,
    });
    if (!ok) return;
    await workspace.deleteConversation(c.id);
    chat.drop(c.id);
  })();

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

<aside class="rail">
  <button class="ws-card" onclick={() => ui.open({ kind: "workspaces" })}>
    <div class="ws-kicker">Workspace</div>
    <div class="ws-line">
      <span class="ws-name">{ws?.name ?? "No workspace"}</span>
      <span class="ws-switch">Switch</span>
    </div>
    <div class="ws-meta">{wsMeta}</div>
  </button>

  <div class="sections">
    <section class="libs">
      <h6>Libraries</h6>
      <div class="list rag-scroll">
        {#each workspace.libraries as lib (lib.id)}
          <div class="row lib-row">
            <button class="row-main" onclick={() => ui.open({ kind: "library", id: lib.id })}>
              <span class="dot" style:background={kindColor(libraryKind(lib.source))}></span>
              <span class="name ellipsis">{lib.name}</span>
              <span class="count">{lib.document_count.toLocaleString()}</span>
            </button>
            <button class="row-act x" title="Remove from workspace" aria-label="Remove {lib.name} from workspace"
                    onclick={() => removeLib(lib.id, lib.name)}>×</button>
          </div>
        {/each}
        {#if ws && workspace.libraries.length === 0 && library.loaded}
          <div class="hint">No libraries in this workspace.</div>
        {/if}
        <button class="add" onclick={() => ui.open({ kind: "libraries" })}>+ Add more</button>
      </div>
    </section>

    <section class="chats">
      <h6>Chats</h6>
      <div class="list rag-scroll">
        <button class="add" onclick={newChat}>+ New chat</button>
        {#each workspace.conversations as c (c.id)}
          {@const active = chat.streaming.includes(c.id)}
          {@const current = c.id === chat.activeId}
          <div class="row chat-row" class:current>
            <a class="row-main chat-link" href="/c/{c.id}" onclick={() => (ui.railOpen = false)} aria-current={current ? "page" : undefined}>
              {#if active}<span class="pulse" title="Answering"></span>
              {:else if unread(c)}<span class="unread" title="New answer"></span>{/if}
              <span class="chat-text">
                <span class="chat-title ellipsis">{c.title ?? "Untitled chat"}</span>
                <span class="when">{when(c.last_message_at ?? c.updated_at)}</span>
              </span>
            </a>
            <div class="row-acts">
              <button class="row-act" title="Archive" aria-label="Archive" onclick={() => archive(c.id)}>
                <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="1.5" y="2" width="13" height="3" rx="0.5"></rect><path d="M2.5 5.5V13a1 1 0 0 0 1 1h9a1 1 0 0 0 1-1V5.5"></path><path d="M6.5 8.5h3"></path></svg>
              </button>
              <button class="row-act del" title="Delete" aria-label="Delete" onclick={() => remove(c)}>
                <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M3 4.5h10"></path><path d="M6 4.5V3a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1v1.5"></path><path d="M4.5 4.5 5 13a1 1 0 0 0 1 1h4a1 1 0 0 0 1-1l0.5-8.5"></path></svg>
              </button>
            </div>
          </div>
        {/each}
        {#if workspace.conversationsLoaded && workspace.conversations.length === 0}
          <div class="hint">No chats here yet.</div>
        {/if}
      </div>
    </section>
  </div>

  <button class="trash" onclick={() => ui.open({ kind: "trash" })}>
    <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M3 4.5h10"></path><path d="M6 4.5V3a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1v1.5"></path><path d="M4.5 4.5 5 13a1 1 0 0 0 1 1h4a1 1 0 0 0 1-1l0.5-8.5"></path></svg>
    Recently deleted
    <span class="trash-n">{workspace.deletedConversations.length + workspace.deletedWorkspaces.length || ""}</span>
  </button>

  <div class="user" bind:this={menuEl}>
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
    <button class="user-btn" onclick={() => (ui.userMenuOpen = !ui.userMenuOpen)} aria-expanded={ui.userMenuOpen} aria-haspopup="menu">
      <span class="avatar">{session.me ? initials(session.me.user.display_name, session.me.user.email) : ""}</span>
      <span class="uname ellipsis">{session.displayName}</span>
      <span class="caret up">▾</span>
    </button>
  </div>
</aside>

<style>
  .rail { display: flex; flex-direction: column; min-height: 0; height: 100%; background: var(--color-surface); padding: 16px 0 12px; }
  .ws-card {
    display: block; width: calc(100% - 28px); text-align: left; margin: 0 14px 14px; padding: 10px 12px;
    background: var(--color-bg); border: 0; border-radius: var(--radius-md); box-shadow: var(--shadow-sm);
    cursor: pointer; font-family: var(--font-heading); color: var(--color-text);
  }
  .ws-card:hover { box-shadow: var(--shadow-md); }
  .ws-kicker { font-size: 9.5px; letter-spacing: .14em; text-transform: uppercase; color: var(--color-neutral-600); margin-bottom: 2px; }
  .ws-line { display: flex; align-items: baseline; gap: 8px; }
  .ws-name { font-size: 17px; font-weight: 600; letter-spacing: -.015em; line-height: 1.15; min-width: 0; overflow-wrap: anywhere; }
  .ws-switch { font-size: 11px; color: var(--color-accent-700); margin-left: auto; flex: none; }
  .ws-meta { font-size: 11px; font-family: var(--font-body); color: var(--color-neutral-700); margin-top: 2px; }

  .sections { display: flex; flex-direction: column; min-height: 0; flex: 1; gap: 18px; padding-top: 6px; }
  section { display: flex; flex-direction: column; min-height: 0; }
  .libs { flex: 0 1 auto; max-height: 40%; }
  .chats { flex: 1 1 auto; }
  h6 { margin: 0 0 6px; padding: 0 18px; font-size: 10px; letter-spacing: .14em; color: var(--color-neutral-700); }
  .list { overflow-y: auto; min-height: 0; padding: 0 10px 0 8px; }

  .row { display: flex; align-items: center; border-radius: var(--radius-md); position: relative; }
  .row:hover { background: color-mix(in srgb, var(--color-text) 6%, transparent); }
  .row-main {
    flex: 1; min-width: 0; display: flex; align-items: center; gap: 7px; padding: 5px 6px 5px 10px;
    border: 0; background: transparent; cursor: pointer; font: inherit; color: inherit; text-align: left; text-decoration: none;
  }
  .lib-row .row-main { font-size: 13.5px; line-height: 1.3; }
  .name { flex: 1; }
  .count { font-size: 10.5px; color: var(--color-neutral-600); font-variant-numeric: tabular-nums; }
  .row-act {
    width: 18px; height: 18px; flex: none; display: grid; place-items: center; padding: 0; border: 0;
    background: transparent; cursor: pointer; color: var(--color-neutral-700); border-radius: var(--radius-md);
    opacity: 0;
  }
  .x { font-size: 13px; line-height: 1; margin-right: 6px; }
  .row:hover .row-act, .row:focus-within .row-act { opacity: 1; }
  .row-act:hover { background: var(--color-accent-200); color: var(--color-accent-800); }
  .x:hover, .row-act.del:hover { background: var(--color-accent-2-200); color: var(--color-accent-2-800); }
  @media (hover: none) { .row-act { opacity: .7; } }

  .chat-row.current { background: color-mix(in srgb, var(--color-accent) 13%, transparent); }
  .chat-link { gap: 6px; }
  .chat-text { flex: 1; min-width: 0; display: flex; flex-direction: column; }
  .chat-title { font-size: 13.5px; line-height: 1.3; }
  .current .chat-title { font-weight: 600; }
  .when { font-size: 10.5px; color: var(--color-neutral-600); line-height: 1.4; }
  .unread { width: 6px; height: 6px; border-radius: 50%; background: var(--color-accent-2-500); flex: none; }
  .row-acts { display: flex; gap: 2px; padding-right: 6px; }

  .add {
    display: block; width: 100%; text-align: center; margin-top: 2px; padding: 5px 6px; border: 0;
    background: transparent; cursor: pointer; font-family: var(--font-heading); font-size: 13px;
    color: var(--color-accent-700); border-radius: var(--radius-md);
  }
  .add:hover { background: color-mix(in srgb, var(--color-accent) 10%, transparent); }
  .hint { font-size: 12px; color: var(--color-neutral-600); padding: 3px 10px; font-style: italic; }

  .trash {
    margin: 8px 14px 0; padding: 7px 10px; text-align: left; border: 0; border-radius: var(--radius-md);
    background: transparent; cursor: pointer; font-family: var(--font-heading); font-size: 12.5px;
    color: var(--color-neutral-700); display: flex; align-items: center; gap: 8px;
  }
  .trash:hover { background: color-mix(in srgb, var(--color-text) 7%, transparent); color: var(--color-text); }
  .trash-n { margin-left: auto; font-size: 11px; color: var(--color-neutral-600); }

  .user { position: relative; margin: 10px 14px 10px; }
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
  .caret.up { display: inline-block; transform: rotate(180deg); }
  .menu {
    position: absolute; bottom: calc(100% + 6px); right: 0; left: 0; z-index: 40; background: var(--color-bg);
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
</style>

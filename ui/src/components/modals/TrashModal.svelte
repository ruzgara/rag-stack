<script lang="ts">
  // No mockup for this one (design/RAG Client.dc.html has a rail button
  // "Recently deleted" at ~83-86 but no popup) -- drawn in the same
  // language as the Workspace/Library popups: kicker + serif heading,
  // table-like rows, tag-styled segmented tabs.
  import ModalShell from "../ui/ModalShell.svelte";
  import { ui, guard } from "../../state/ui.svelte";
  import { workspace } from "../../state/workspace.svelte";
  import { navigate } from "../../state/route.svelte";
  import { when } from "../../lib/format";
  import type { ConversationListItem } from "../../lib/types";
  import { untrack } from "svelte";

  interface Props {
    tab?: "chats" | "workspaces" | "archived";
  }
  const props: Props = $props();

  // Seeds from the prop once; switching tabs afterwards is purely local
  // state, so this deliberately does not track further prop changes.
  let tab = $state<"chats" | "workspaces" | "archived">(untrack(() => props.tab ?? "chats"));

  $effect(() => {
    void workspace.loadTrays();
  });

  const kicker = $derived(tab === "archived" ? "Archive" : "Recently deleted");
  const title = $derived(
    tab === "chats" ? "Deleted chats" : tab === "workspaces" ? "Deleted workspaces" : "Archived chats",
  );

  function chatMeta(c: ConversationListItem): string {
    const ws = workspace.workspaceName(c.workspace_id);
    const parts = [ws ?? "no workspace", when(c.updated_at), `${c.message_count} message${c.message_count === 1 ? "" : "s"}`];
    return parts.join(" · ");
  }

  const restoreConversation = guard(async (id: string) => {
    await workspace.restoreConversation(id);
    ui.toast("Chat restored");
  });

  const restoreWorkspace = guard(async (id: string) => {
    await workspace.restore(id);
    ui.toast("Workspace restored");
  });

  const unarchive = guard(async (id: string) => {
    await workspace.unarchiveConversation(id);
    ui.toast("Chat unarchived");
  });

  function open(id: string): void {
    navigate(`/c/${id}`);
    ui.closeAll();
  }
</script>

<ModalShell width={700} {kicker} {title}>
  {#snippet headerExtra()}
    <div class="seg">
      <button type="button" aria-pressed={tab === "chats"} onclick={() => (tab = "chats")}>
        Deleted chats {workspace.deletedConversations.length}
      </button>
      <button type="button" aria-pressed={tab === "workspaces"} onclick={() => (tab = "workspaces")}>
        Deleted workspaces {workspace.deletedWorkspaces.length}
      </button>
      <button type="button" aria-pressed={tab === "archived"} onclick={() => (tab = "archived")}>
        Archived chats {workspace.archivedConversations.length}
      </button>
    </div>
  {/snippet}

  {#snippet children()}
    {#if tab === "chats"}
      {#if workspace.deletedConversations.length === 0}
        <div class="empty-note">Nothing in Recently deleted.</div>
      {:else}
        <div class="rows">
          {#each workspace.deletedConversations as c (c.id)}
            <div class="row">
              <div class="row-main">
                <span class="row-title">{c.title ?? "Untitled chat"}</span>
                <span class="row-meta muted">{chatMeta(c)}</span>
              </div>
              <button type="button" class="btn btn-secondary btn-sm" onclick={() => restoreConversation(c.id)}>
                Restore
              </button>
            </div>
          {/each}
        </div>
      {/if}
    {:else if tab === "workspaces"}
      {#if workspace.deletedWorkspaces.length === 0}
        <div class="empty-note">No deleted workspaces.</div>
      {:else}
        <div class="rows">
          {#each workspace.deletedWorkspaces as w (w.id)}
            <div class="row">
              <div class="row-main">
                <span class="row-title">{w.name}</span>
                <span class="row-meta muted">Deleted {when(w.last_used_at)}</span>
              </div>
              <button type="button" class="btn btn-secondary btn-sm" onclick={() => restoreWorkspace(w.id)}>
                Restore
              </button>
            </div>
          {/each}
        </div>
      {/if}
    {:else}
      {#if workspace.archivedConversations.length === 0}
        <div class="empty-note">No archived chats.</div>
      {:else}
        <div class="rows">
          {#each workspace.archivedConversations as c (c.id)}
            <div class="row">
              <div class="row-main">
                <span class="row-title">{c.title ?? "Untitled chat"}</span>
                <span class="row-meta muted">{chatMeta(c)}</span>
              </div>
              <div class="row-actions">
                <button type="button" class="btn btn-ghost btn-sm" onclick={() => open(c.id)}>Open</button>
                <button type="button" class="btn btn-secondary btn-sm" onclick={() => unarchive(c.id)}>Unarchive</button>
              </div>
            </div>
          {/each}
        </div>
      {/if}
    {/if}
  {/snippet}

  {#snippet footer()}
    <span class="empty-note" style="padding:0">Nothing here is permanently removed.</span>
    <button type="button" class="btn btn-ghost" style="margin-left:auto" onclick={() => ui.closeAll()}>Close</button>
  {/snippet}
</ModalShell>

<style>
  .rows { display: flex; flex-direction: column; gap: 2px; margin: 0 -8px; }
  .row {
    display: flex; align-items: center; gap: 12px; padding: 9px 10px; border-radius: var(--radius-md);
  }
  .row:hover { background: color-mix(in srgb, var(--color-text) 4%, transparent); }
  .row-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
  .row-title { font-family: var(--font-heading); font-size: 14.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .row-meta { font-size: 11.5px; }
  .row-actions { display: flex; gap: 6px; flex: none; }
</style>

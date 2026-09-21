<script lang="ts">
  // The design's "POPUP · WORKSPACE" (design/RAG Client.dc.html ~349-375):
  // switch, create, rename or delete a workspace.
  import ModalShell from "../ui/ModalShell.svelte";
  import { ui, guard } from "../../state/ui.svelte";
  import { workspace, ADMIN_WORKSPACE_ID } from "../../state/workspace.svelte";
  import { navigate } from "../../state/route.svelte";
  import { plural } from "../../lib/format";

  function meta(w: { library_ids: string[]; conversation_count?: number }): string {
    const libs = w.library_ids.length === 0 ? "no libraries" : plural(w.library_ids.length, "library", "libraries");
    return `${libs} · ${plural(w.conversation_count ?? 0, "chat")}`;
  }

  const pick = guard(async (id: string) => {
    await workspace.switchTo(id);
    navigate("/");
    ui.closeAll();
  });

  const createWorkspace = guard(async () => {
    const r = await ui.ask({
      kicker: "Workspaces",
      title: "New workspace",
      okLabel: "Create",
      fields: [{ name: "name", label: "Name", required: true, placeholder: "e.g. Halcyon diligence" }],
    });
    if (!r) return;
    await workspace.create(String(r.name));
    navigate("/");
    ui.closeAll();
  });

  const renameCurrent = guard(async () => {
    const cur = workspace.current;
    if (!cur) return;
    const r = await ui.ask({
      kicker: "Workspaces",
      title: "Rename workspace",
      okLabel: "Save",
      fields: [{ name: "name", label: "Name", required: true, value: cur.name }],
    });
    if (!r) return;
    await workspace.rename(cur.id, String(r.name));
  });

  const deleteCurrent = guard(async () => {
    const cur = workspace.current;
    if (!cur) return;
    const ok = await ui.confirm({
      kicker: "Workspaces",
      title: `Delete "${cur.name}"?`,
      body: "Its chats are hidden with it, not removed -- restore the workspace from Recently deleted to get them back.",
      okLabel: "Delete",
      danger: true,
    });
    if (!ok) return;
    await workspace.remove(cur.id);
    navigate("/");
  });
</script>

<ModalShell width={520} kicker="Workspaces" title="Switch or manage">
  {#snippet children()}
    <div class="rows">
      {#each workspace.workspaces as w (w.id)}
        <button type="button" class="row" class:current={w.id === workspace.currentId} onclick={() => pick(w.id)}>
          <span
            class="dot"
            style:background={w.id === ADMIN_WORKSPACE_ID ? "var(--kind-group)" : "var(--kind-mine)"}
          ></span>
          <span class="row-main">
            <span class="row-name">{w.name}</span>
            <span class="row-meta muted">{meta(w)}</span>
          </span>
          {#if w.id === workspace.currentId}
            <span class="row-state">Current</span>
          {/if}
        </button>
      {/each}
      {#if workspace.workspaces.length === 0}
        <div class="empty-note">No workspaces yet.</div>
      {/if}
    </div>
  {/snippet}
  {#snippet footer()}
    <button type="button" class="btn btn-primary" onclick={createWorkspace}>＋ New workspace</button>
    <button
      type="button"
      class="btn btn-secondary"
      disabled={!workspace.current || workspace.currentId === ADMIN_WORKSPACE_ID}
      onclick={renameCurrent}
    >
      Rename
    </button>
    <button
      type="button"
      class="btn btn-secondary btn-delete"
      disabled={!workspace.current || workspace.currentId === ADMIN_WORKSPACE_ID}
      onclick={deleteCurrent}
    >
      Delete
    </button>
    <button type="button" class="btn btn-ghost" style="margin-left:auto" onclick={() => ui.closeAll()}>Close</button>
  {/snippet}
</ModalShell>

<style>
  .rows { display: flex; flex-direction: column; margin: 0 -8px; }
  .row {
    display: flex; align-items: center; gap: 10px; padding: 9px 10px; border-radius: var(--radius-md);
    cursor: pointer; background: transparent; border: 0; text-align: left; font: inherit; color: inherit;
    width: 100%;
  }
  .row:hover { background: color-mix(in srgb, var(--color-text) 6%, transparent); }
  .row.current { background: color-mix(in srgb, var(--color-accent) 13%, transparent); }
  .dot { width: 6px; height: 6px; flex: none; border-radius: 50%; }
  .row-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 1px; }
  .row-name { font-family: var(--font-heading); font-size: 15px; }
  .row-meta { font-size: 11.5px; }
  .row-state { font-size: 11px; color: var(--color-accent-700); font-family: var(--font-heading); flex: none; }
  .btn-delete { color: var(--color-accent-2-700); border-color: var(--color-accent-2-300); }
  .btn-delete:hover:not(:disabled) { background: var(--color-accent-2-100); }
</style>

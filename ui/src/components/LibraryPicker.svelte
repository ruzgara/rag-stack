<script lang="ts">
  import { chat } from "../state/chat.svelte";
  import { library } from "../state/library.svelte";
  import { session } from "../state/session.svelte";
  import { guard, ui } from "../state/ui.svelte";

  const createLibrary = guard(async () => {
    const result = await ui.ask("New library", [
      { name: "name", label: "Name", type: "text", required: true },
      { name: "description", label: "Description (optional)", type: "text" },
    ], "Create");
    if (!result) return;
    await library.createLibrary(String(result.name), String(result.description || "") || null);
  });

  const deleteLibrary = guard(async (id: string) => {
    if (!window.confirm("Delete this library? Documents already indexed are not removed.")) return;
    if (chat.libraryIds.includes(id)) await chat.toggleLibrary(id);
    await library.deleteLibrary(id);
  });

  const toggle = guard(async (id: string) => {
    await chat.toggleLibrary(id);
  });
</script>

<h3>Libraries</h3>
{#if library.libraries.length === 0}
  <p class="small">No libraries yet.</p>
{/if}
{#each library.libraries as lib (lib.id)}
  <label class="row" style="justify-content: space-between; gap: 6px;" title={lib.description ?? ""}>
    <span class="row" style="gap: 6px;">
      <input type="checkbox" checked={chat.libraryIds.includes(lib.id)} onchange={() => toggle(lib.id)} />
      <span>{lib.name} <span class="small">({lib.document_count})</span></span>
    </span>
    {#if session.canManageLibraries}
      <button class="small danger" onclick={() => deleteLibrary(lib.id)} title="Delete library">&cross;</button>
    {/if}
  </label>
{/each}
{#if session.canManageLibraries}
  <div class="row"><button class="small" onclick={createLibrary}>+ New library</button></div>
{/if}

<script lang="ts">
  // "Manage · Libraries" -- every library the caller can read, filterable by
  // how they can see it, with workspace add/remove and the ingest status
  // line the design puts in the footer.
  import ModalShell from "../ui/ModalShell.svelte";
  import { ui, guard } from "../../state/ui.svelte";
  import { library, libraryKind, kindColor, accessLabel } from "../../state/library.svelte";
  import { workspace } from "../../state/workspace.svelte";
  import { clock } from "../../lib/format";

  type Tab = "all" | "mine" | "shared" | "group";
  let tab = $state<Tab>("all");
  let filterText = $state("");

  $effect(() => {
    if (!library.loaded) void library.load().catch(() => {});
    if (!library.documents) void library.loadDocuments().catch(() => {});
  });

  const counts = $derived({
    all: library.libraries.length,
    mine: library.libraries.filter((l) => l.source === "owner").length,
    shared: library.libraries.filter((l) => l.source === "user" || l.source === "admin").length,
    group: library.libraries.filter((l) => l.source === "group").length,
  });

  function matchesTab(source: (typeof library.libraries)[number]["source"]): boolean {
    if (tab === "all") return true;
    if (tab === "mine") return source === "owner";
    if (tab === "shared") return source === "user" || source === "admin";
    return source === "group";
  }

  const rows = $derived(
    library.libraries
      .filter((l) => matchesTab(l.source))
      .filter((l) => !filterText.trim() || l.name.toLowerCase().includes(filterText.trim().toLowerCase()))
      .sort((a, b) => a.name.localeCompare(b.name)),
  );

  const wsIds = $derived(workspace.current?.library_ids ?? null);

  const statusLine = $derived.by(() => {
    const ingest = library.documents?.ingest;
    if (!ingest) return "";
    const waiting = library.documents?.inbox.length ?? 0;
    let base: string;
    if (ingest.running) base = "Ingest running…";
    else if (ingest.last_run) {
      base = `Ingest idle · last run ${clock(ingest.last_run.finished_at)} · exit ${ingest.last_run.exit_code}`;
    } else base = "Ingest idle";
    if (waiting > 0) base += ` · ${waiting} waiting`;
    if (ingest.autorun) base += " · inbox watcher on";
    return base;
  });

  async function addToWorkspace(id: string): Promise<void> {
    await workspace.addLibrary(id);
  }
  async function removeFromWorkspace(id: string): Promise<void> {
    await workspace.removeLibrary(id);
  }

  const newLibrary = guard(async () => {
    const r = await ui.ask({
      title: "New library",
      okLabel: "Create",
      fields: [
        { name: "name", label: "Name", required: true },
        { name: "description", label: "Description", type: "textarea" },
        { name: "addToWorkspace", label: "Add to this workspace", type: "checkbox", value: true },
      ],
    });
    if (!r) return;
    const lib = await library.create(String(r.name), r.description ? String(r.description) : null);
    if (r.addToWorkspace) await workspace.addLibrary(lib.id);
    ui.toast(`Created "${lib.name}"`);
  });
</script>

<ModalShell width={940} kicker="Manage" title="Libraries" scroll={true}>
  {#snippet headerExtra()}
    <button class="tag" class:tag-accent={tab === "all"} class:tag-neutral={tab !== "all"} aria-pressed={tab === "all"} onclick={() => (tab = "all")}>All {counts.all}</button>
    <button class="tag" class:tag-accent={tab === "mine"} class:tag-neutral={tab !== "mine"} aria-pressed={tab === "mine"} onclick={() => (tab = "mine")}>Mine {counts.mine}</button>
    <button class="tag" class:tag-accent={tab === "shared"} class:tag-neutral={tab !== "shared"} aria-pressed={tab === "shared"} onclick={() => (tab = "shared")}>Shared {counts.shared}</button>
    <button class="tag" class:tag-accent={tab === "group"} class:tag-neutral={tab !== "group"} aria-pressed={tab === "group"} onclick={() => (tab = "group")}>Via groups {counts.group}</button>
  {/snippet}

  <div class="filter-row">
    <input class="input" placeholder="Filter libraries…" bind:value={filterText} aria-label="Filter libraries" />
  </div>

  {#if rows.length === 0}
    <p class="empty-note">
      {#if library.libraries.length === 0}You don't have any libraries yet.{:else}No libraries match this filter.{/if}
    </p>
  {:else}
    <div class="table-wrap rag-scroll">
      <table class="table">
        <thead>
          <tr>
            <th style="width:34%">Library</th>
            <th>Documents</th>
            <th class="hide-narrow">Owner</th>
            <th class="hide-narrow">Access</th>
            {#if wsIds}<th style="text-align:right">In workspace</th>{/if}
          </tr>
        </thead>
        <tbody>
          {#each rows as l (l.id)}
            <tr>
              <td>
                <div class="lib-name">
                  <span class="dot" style:background={kindColor(libraryKind(l.source))}></span>
                  <button class="link-btn heading-font" onclick={() => ui.open({ kind: "library", id: l.id })}>{l.name}</button>
                </div>
                {#if l.description}<div class="lib-desc">{l.description}</div>{/if}
              </td>
              <td class="tabular-nums">{l.document_count}</td>
              <td class="hide-narrow">{l.source === "owner" ? "You" : (l.owner_name ?? "—")}</td>
              <td class="hide-narrow muted-cell">{accessLabel(l)}</td>
              {#if wsIds}
                <td style="text-align:right">
                  {#if wsIds.includes(l.id)}
                    <button class="ws-toggle added" onclick={guard(() => removeFromWorkspace(l.id))}>
                      <span class="show-default">Added ✓</span>
                      <span class="show-hover">Remove</span>
                    </button>
                  {:else}
                    <button class="ws-toggle" onclick={guard(() => addToWorkspace(l.id))}>+ Add</button>
                  {/if}
                </td>
              {/if}
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}

  {#snippet footer()}
    <button class="btn btn-primary" onclick={newLibrary}>＋ New library</button>
    <button class="btn btn-secondary" onclick={() => ui.open({ kind: "upload", folder: true })}>Import folder</button>
    {#if statusLine}<span class="ingest-status">{statusLine}</span>{/if}
    <button class="btn btn-ghost" class:auto-left={!statusLine} onclick={() => ui.closeAll()}>Done</button>
  {/snippet}
</ModalShell>

<style>
  .filter-row { display: flex; align-items: center; gap: 8px; margin: 0 0 10px; }
  .filter-row .input { max-width: 260px; }
  .table-wrap { overflow-x: auto; margin: 0 -6px; }
  .table { min-width: 560px; }
  .lib-name { display: flex; align-items: center; gap: 8px; }
  .lib-desc { font-size: 11.5px; color: var(--color-neutral-600); padding-left: 13px; }
  .heading-font { font-family: var(--font-heading); font-size: 14px; }
  .link-btn {
    border: 0; background: transparent; padding: 0; cursor: pointer; color: var(--color-text);
    text-align: left; font: inherit;
  }
  .link-btn:hover { color: var(--color-accent-700); text-decoration: underline; text-underline-offset: 3px; }
  .tabular-nums { font-variant-numeric: tabular-nums; font-size: 13.5px; }
  .muted-cell { font-size: 12.5px; color: var(--color-neutral-700); }
  .ws-toggle {
    border: 0; background: transparent; cursor: pointer; padding: 2px 4px;
    font-size: 12.5px; font-family: var(--font-heading); color: var(--color-accent-700);
  }
  .ws-toggle.added { color: var(--color-neutral-600); }
  .ws-toggle .show-hover { display: none; }
  .ws-toggle.added:hover { color: var(--color-accent-2-700); }
  .ws-toggle.added:hover .show-default { display: none; }
  .ws-toggle.added:hover .show-hover { display: inline; }
  .ingest-status { margin-left: auto; font-size: 11.5px; color: var(--color-neutral-600); white-space: nowrap; }
  .auto-left { margin-left: auto; }
  @media (max-width: 640px) {
    .hide-narrow { display: none; }
  }
</style>

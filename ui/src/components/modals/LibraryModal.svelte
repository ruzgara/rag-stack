<script lang="ts">
  // One library's detail: its documents, ingest status, and the actions its
  // role allows (upload, share, rename, workspace membership, delete).
  import ModalShell from "../ui/ModalShell.svelte";
  import { ui, guard } from "../../state/ui.svelte";
  import { library, canWrite, canManage, accessLabel } from "../../state/library.svelte";
  import { workspace } from "../../state/workspace.svelte";
  import { clock } from "../../lib/format";
  import type { LibraryDetail } from "../../lib/types";

  interface Props { id: string }
  const { id }: Props = $props();

  let detail = $state<LibraryDetail | null>(null);
  let loading = $state(true);
  let error = $state<string | null>(null);
  let filterText = $state("");

  /** The list-row copy of this library, for `source` (LibraryDetail omits
   *  it) -- canManage needs it for Sharing/Rename/Delete visibility. */
  const summary = $derived(library.byId.get(id));

  async function load(): Promise<void> {
    loading = true;
    error = null;
    try {
      detail = await library.detail(id);
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    void id;
    void library.libraries; // reload after an upload/rename changes the list
    void load();
  });

  $effect(() => {
    if (!library.documents) void library.loadDocuments().catch(() => {});
  });

  const docs = $derived(
    (detail?.documents ?? [])
      .filter((d) => !filterText.trim() || d.filename.toLowerCase().includes(filterText.trim().toLowerCase()))
      .sort((a, b) => a.filename.localeCompare(b.filename)),
  );

  const indexedAt = $derived.by(() => {
    const times = (detail?.documents ?? []).map((d) => d.processed_at).filter((t): t is number => t !== null);
    return times.length ? Math.max(...times) : null;
  });
  const pendingCount = $derived(library.documents?.inbox.length ?? 0);

  function statusLabel(status: string | null): string {
    if (status === "processed") return "Indexed";
    if (status === "failed") return "Failed";
    if (!status) return "—";
    return status.charAt(0).toUpperCase() + status.slice(1);
  }
  function statusClass(status: string | null): string {
    if (status === "processed") return "st-neutral";
    if (status === "failed") return "st-danger";
    return "st-accent";
  }

  const inWorkspace = $derived(workspace.current?.library_ids.includes(id) ?? false);

  const removeDoc = guard(async (sha: string, filename: string) => {
    const ok = await ui.confirm({
      title: "Remove document",
      body: `Remove "${filename}" from this library? It stays indexed and searchable from any other library it's in \u2014 but if this is the last one, the document is deleted from the machine for good.`,
      okLabel: "Remove",
      danger: true,
    });
    if (!ok) return;
    const purged = await library.removeDocument(id, sha);
    await load();
    if (purged) ui.toast(`Deleted "${filename}" \u2014 it was in no other library`);
  });

  const rename = guard(async () => {
    if (!detail) return;
    const r = await ui.ask({
      title: "Rename library",
      okLabel: "Save",
      fields: [
        { name: "name", label: "Name", required: true, value: detail.name },
        { name: "description", label: "Description", type: "textarea", value: detail.description ?? "" },
      ],
    });
    if (!r) return;
    await library.update(id, { name: String(r.name), description: String(r.description ?? "") });
  });

  const toggleWorkspace = guard(async () => {
    if (inWorkspace) await workspace.removeLibrary(id);
    else await workspace.addLibrary(id);
  });

  const removeLibrary = guard(async () => {
    const ok = await ui.confirm({
      title: "Delete library",
      body: "Its documents stay indexed and searchable from any other library they're in -- only this library goes away.",
      okLabel: "Delete",
      danger: true,
    });
    if (!ok) return;
    await library.remove(id);
    ui.closeAll();
  });
</script>

<ModalShell width={760} kicker="Library" title={detail?.name ?? summary?.name ?? "Library"}>
  {#snippet subtitle()}
    {#if detail}
      <div class="sub-row">
        <span class="tag tag-accent">{accessLabel({ role: detail.role, source: summary?.source ?? null })}</span>
        <span>{detail.role === "owner" ? "You" : (detail.owner_name ?? "—")}</span>
        <span>·</span>
        <span>{detail.document_count} document{detail.document_count === 1 ? "" : "s"}</span>
      </div>
      {#if detail.description}<div class="sub-desc">{detail.description}</div>{/if}
    {/if}
  {/snippet}

  {#if loading && !detail}
    <p class="empty-note">Loading…</p>
  {:else if error}
    <p class="err-text">{error}</p>
  {:else if detail}
    <div class="filter-row">
      <input class="input" placeholder="Filter documents…" bind:value={filterText} aria-label="Filter documents" />
      <span class="status-line">
        {#if indexedAt !== null}Indexed {clock(indexedAt)}{/if}
        {#if pendingCount > 0}
          {indexedAt !== null ? " · " : ""}{pendingCount} of your uploads waiting
        {/if}
      </span>
    </div>

    {#if docs.length === 0}
      <p class="empty-note">
        {#if (detail.documents ?? []).length === 0}No documents yet.{:else}No documents match this filter.{/if}
      </p>
    {:else}
      <div class="table-wrap rag-scroll">
        <table class="table">
          <thead>
            <tr>
              <th style="width:52%">Document</th>
              <th class="hide-narrow">Pages</th>
              <th class="hide-narrow">Route</th>
              <th>Status</th>
              {#if canWrite(detail)}<th></th>{/if}
            </tr>
          </thead>
          <tbody>
            {#each docs as doc (doc.sha256)}
              <tr>
                <td class="doc-name">{doc.filename}</td>
                <td class="hide-narrow tabular-nums">{doc.pages ?? "—"}</td>
                <td class="hide-narrow route-cell">{doc.route ?? "—"}</td>
                <td class={statusClass(doc.status)} title={doc.status === "failed" ? (doc.error ?? undefined) : undefined}>
                  {statusLabel(doc.status)}
                </td>
                {#if canWrite(detail)}
                  <td>
                    <button class="row-x" aria-label={`Remove ${doc.filename} from library`} onclick={() => removeDoc(doc.sha256, doc.filename)}>×</button>
                  </td>
                {/if}
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  {/if}

  {#snippet footer()}
    {#if detail && canWrite(detail)}
      <button class="btn btn-primary" onclick={() => ui.open({ kind: "upload", libraryId: id })}>Upload documents</button>
    {/if}
    {#if summary && canManage(summary)}
      <button class="btn btn-secondary" onclick={() => ui.open({ kind: "sharing", id })}>Sharing…</button>
      <button class="btn btn-secondary" onclick={rename}>Rename</button>
    {/if}
    {#if workspace.current || (summary && canManage(summary))}
      <span class="trailing-group">
        {#if workspace.current}
          <button class="btn btn-ghost ws-btn" onclick={toggleWorkspace}>
            {inWorkspace ? "Remove from workspace" : "Add to workspace"}
          </button>
        {/if}
        {#if summary && canManage(summary)}
          <button class="btn btn-ghost btn-danger" onclick={removeLibrary}>Delete library</button>
        {/if}
      </span>
    {/if}
  {/snippet}
</ModalShell>

<style>
  .sub-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .sub-desc { margin-top: 4px; font-size: 12px; color: var(--color-neutral-600); }
  .filter-row { display: flex; align-items: center; gap: 8px; margin: 14px 0 8px; }
  .filter-row .input { max-width: 260px; }
  .status-line { font-size: 11.5px; color: var(--color-neutral-600); margin-left: auto; white-space: nowrap; }
  .table-wrap { overflow-x: auto; margin: 0 -6px; }
  .table { min-width: 480px; }
  .doc-name { font-size: 13.5px; }
  .tabular-nums { font-variant-numeric: tabular-nums; font-size: 13px; color: var(--color-neutral-700); }
  .route-cell { font-size: 12.5px; color: var(--color-neutral-700); }
  .st-neutral { font-size: 12px; color: var(--color-neutral-700); }
  .st-danger { font-size: 12px; color: var(--color-accent-2-700); cursor: help; }
  .st-accent { font-size: 12px; color: var(--color-accent-700); }
  .row-x {
    border: 0; background: transparent; cursor: pointer; font-size: 15px; line-height: 1;
    color: var(--color-neutral-600); padding: 2px 6px;
  }
  .row-x:hover { color: var(--color-accent-2-700); }
  .trailing-group { display: flex; align-items: center; gap: 8px; margin-left: auto; }
  .ws-btn { color: var(--color-accent-2-700); }
  @media (max-width: 640px) {
    .hide-narrow { display: none; }
  }
</style>

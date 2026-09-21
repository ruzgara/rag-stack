<script lang="ts">
  // Upload files (or a dropped/picked folder) into a library.
  import ModalShell from "../ui/ModalShell.svelte";
  import { ui, guard } from "../../state/ui.svelte";
  import { library, canWrite, SUPPORTED } from "../../state/library.svelte";
  import { workspace } from "../../state/workspace.svelte";
  import { session } from "../../state/session.svelte";
  import { byteSize } from "../../lib/format";
  import type { UploadResponse } from "../../lib/types";

  interface Props { libraryId?: string; folder?: boolean }
  const { libraryId, folder = false }: Props = $props();

  const writable = $derived(library.libraries.filter((l) => canWrite(l)));

  /** Two libraries can share a name (every user's own is literally "Default
   *  Library"), so once a name collides in this list, disambiguate every
   *  copy of it with its owner -- otherwise the <select> shows indistinguishable
   *  duplicate rows. */
  const libraryLabel = $derived.by(() => {
    const counts = new Map<string, number>();
    for (const l of writable) counts.set(l.name, (counts.get(l.name) ?? 0) + 1);
    return (l: (typeof writable)[number]): string =>
      (counts.get(l.name) ?? 0) > 1 ? `${l.name} — ${l.source === "owner" ? "You" : (l.owner_name ?? "Shared")}` : l.name;
  });

  function defaultLibraryId(): string {
    if (libraryId && writable.some((l) => l.id === libraryId)) return libraryId;
    const inWorkspace = writable.find((l) => workspace.current?.library_ids.includes(l.id));
    if (inWorkspace) return inWorkspace.id;
    const own = writable.find((l) => l.source === "owner" && l.name === "Default Library") ??
      writable.find((l) => l.source === "owner");
    return (own ?? writable[0])?.id ?? "";
  }

  let selectedLibrary = $state(defaultLibraryId());

  let files = $state<File[]>([]);
  let dragOver = $state(false);
  let result = $state<UploadResponse | null>(null);
  let fileInput: HTMLInputElement | undefined = $state();

  function isDotfile(name: string): boolean {
    return name.startsWith(".");
  }
  function isSupported(name: string): boolean {
    if (isDotfile(name)) return false;
    const dot = name.lastIndexOf(".");
    if (dot < 0) return false;
    return SUPPORTED.includes(name.slice(dot).toLowerCase());
  }

  const sendable = $derived(files.filter((f) => isSupported(f.name)));
  const skipped = $derived(files.filter((f) => !isSupported(f.name)));

  function addFiles(list: File[]): void {
    const seen = new Set(files.map((f) => `${f.name}:${f.size}:${f.lastModified}`));
    const fresh = list.filter((f) => !seen.has(`${f.name}:${f.size}:${f.lastModified}`));
    files = [...files, ...fresh];
  }

  function onPick(e: Event): void {
    const input = e.currentTarget as HTMLInputElement;
    if (input.files) addFiles(Array.from(input.files));
    input.value = "";
  }

  /** Best-effort recursive walk of a dropped folder via the (non-standard
   *  but widely supported) webkitGetAsEntry API. */
  async function walkEntry(entry: FileSystemEntry): Promise<File[]> {
    if (entry.isFile) {
      return new Promise((resolve) => {
        (entry as FileSystemFileEntry).file((f) => resolve([f]), () => resolve([]));
      });
    }
    if (entry.isDirectory) {
      const reader = (entry as FileSystemDirectoryEntry).createReader();
      const entries: FileSystemEntry[] = await new Promise((resolve) => {
        const all: FileSystemEntry[] = [];
        const readBatch = () => {
          reader.readEntries((batch) => {
            if (batch.length === 0) resolve(all);
            else {
              all.push(...batch);
              readBatch();
            }
          }, () => resolve(all));
        };
        readBatch();
      });
      const nested = await Promise.all(entries.map(walkEntry));
      return nested.flat();
    }
    return [];
  }

  async function onDrop(e: DragEvent): Promise<void> {
    e.preventDefault();
    dragOver = false;
    const items = e.dataTransfer?.items;
    if (items && items.length && "webkitGetAsEntry" in items[0]) {
      const entries = Array.from(items)
        .map((it) => it.webkitGetAsEntry?.())
        .filter((x): x is FileSystemEntry => x !== null && x !== undefined);
      if (entries.length) {
        const collected = await Promise.all(entries.map(walkEntry));
        addFiles(collected.flat());
        return;
      }
    }
    if (e.dataTransfer?.files.length) addFiles(Array.from(e.dataTransfer.files));
  }

  function removeFile(f: File): void {
    files = files.filter((x) => x !== f);
  }

  const quotaLine = $derived.by(() => {
    const q = session.quota?.uploads;
    if (!q) return "";
    if (q.unlimited) return "No upload limit";
    return `${q.files_left} files · ${q.mb_left.toFixed(0)} MB left in this ${q.window_hours}h window, max ${q.max_file_mb} MB per file`;
  });

  /** Per-file status shown in the list: "ready" until Upload is pressed, then
   *  "uploading" for the whole batch (one request, no per-file progress from
   *  the server), then "saved"/"rejected"/"skipped" once the result lands. */
  type RowStatus = "ready" | "uploading" | "saved" | "rejected" | "skipped";

  function rowStatus(f: File): { label: string; status: RowStatus } {
    if (!isSupported(f.name)) return { label: "Unsupported type", status: "skipped" };
    if (!result) return library.uploading ? { label: "Uploading…", status: "uploading" } : { label: "Ready", status: "ready" };
    if (result.saved.includes(f.name)) return { label: "Saved", status: "saved" };
    const rejection = result.rejected.find((r) => r.filename === f.name);
    if (rejection) return { label: rejection.reason, status: "rejected" };
    return { label: "Ready", status: "ready" };
  }

  const metaLine = $derived.by(() => {
    if (result) {
      const n = result.saved.length;
      const r = result.rejected.length;
      return r === 0 ? `Saved ${n} file${n === 1 ? "" : "s"}` : `Saved ${n}, ${r} rejected`;
    }
    if (library.uploading) return `Uploading ${sendable.length} file${sendable.length === 1 ? "" : "s"}…`;
    const parts: string[] = [];
    if (sendable.length) parts.push(`${sendable.length} ready`);
    if (skipped.length) parts.push(`${skipped.length} skipped`);
    return parts.join(" · ");
  });

  const doUpload = guard(async () => {
    if (!selectedLibrary || sendable.length === 0) return;
    result = await library.upload(sendable, selectedLibrary);
    const n = result.saved.length;
    const r = result.rejected.length;
    ui.toast(r === 0 ? `Uploaded ${n} file${n === 1 ? "" : "s"}` : `Uploaded ${n}, ${r} rejected`, r && !n ? "err" : "ok");
  });

  function done(): void {
    ui.close();
  }
</script>

<ModalShell width={620} kicker="Upload" title={folder ? "Import a folder" : "Add documents"}>
  <div class="lib-row">
    <label for="upload-lib">Destination library</label>
    <select id="upload-lib" class="input" bind:value={selectedLibrary} disabled={writable.length === 0 || library.uploading}>
      {#each writable as l (l.id)}
        <option value={l.id}>{libraryLabel(l)}</option>
      {/each}
    </select>
  </div>
  {#if writable.length === 0}<p class="err-text">You don't have write access to any library.</p>{/if}

  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <div
    class="dropzone"
    class:over={dragOver}
    role="button"
    tabindex="0"
    onclick={() => fileInput?.click()}
    onkeydown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput?.click(); } }}
    ondragover={(e) => { e.preventDefault(); dragOver = true; }}
    ondragleave={() => (dragOver = false)}
    ondrop={(e) => void onDrop(e)}
  >
    <svg width="26" height="26" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" class="drop-icon">
      <path d="M8 10.5V2.5" /><path d="M5 5.2 8 2.2 11 5.2" />
      <path d="M2.5 10v2.5a1 1 0 0 0 1 1h9a1 1 0 0 0 1-1V10" />
    </svg>
    <div class="drop-hint">Drag {folder ? "a folder" : "files"} here, or click to browse</div>
    <div class="drop-sub">{SUPPORTED.map((s) => s.slice(1).toUpperCase()).join(", ")} — supported formats</div>
    {#if folder}
      <input bind:this={fileInput} type="file" multiple webkitdirectory class="visually-hidden" onchange={onPick} />
    {:else}
      <input bind:this={fileInput} type="file" multiple class="visually-hidden" onchange={onPick} />
    {/if}
  </div>

  {#if files.length > 0}
    <ul class="file-list rag-scroll">
      {#each files as f (f.name + f.size + f.lastModified)}
        {@const row = rowStatus(f)}
        <li>
          <div class="fname-wrap">
            <span class="fname ellipsis">{f.name}</span>
            {#if row.status === "uploading"}<div class="progress"><div class="progress-bar indeterminate"></div></div>{/if}
          </div>
          <span class="fsize">{byteSize(f.size)}</span>
          <span class="fstatus status-{row.status}">{row.label}</span>
          <button class="row-x" aria-label={`Remove ${f.name}`} disabled={library.uploading} onclick={() => removeFile(f)}>
            <svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3.5 3.5l9 9" /><path d="M12.5 3.5l-9 9" /></svg>
          </button>
        </li>
      {/each}
    </ul>
  {/if}

  {#if quotaLine}<p class="quota-line">{quotaLine}</p>{/if}
  <p class="ocr-note">Files are OCR'd and indexed locally; each becomes searchable once ingest finishes.</p>

  {#snippet footer()}
    <span class="meta">{metaLine}</span>
    {#if !result}
      <button class="btn btn-ghost" style="margin-left:auto" onclick={done}>Cancel</button>
      <button class="btn btn-primary" disabled={sendable.length === 0 || library.uploading || !selectedLibrary} onclick={doUpload}>
        {library.uploading ? "Uploading…" : `Upload ${sendable.length || ""}`}
      </button>
    {:else}
      <button class="btn btn-ghost" style="margin-left:auto" onclick={done}>Done</button>
    {/if}
  {/snippet}
</ModalShell>

<style>
  .lib-row { display: flex; align-items: center; gap: 8px; margin-bottom: 14px; }
  .lib-row label { font-size: 12.5px; color: var(--color-neutral-700); white-space: nowrap; }
  .lib-row select { flex: 1; font-size: 13.5px; }
  .dropzone {
    display: flex; flex-direction: column; align-items: center; gap: 8px;
    border: 1.5px dashed var(--color-neutral-400); border-radius: 3px;
    background: var(--color-surface); padding: 26px 20px; text-align: center; cursor: pointer;
  }
  .dropzone.over, .dropzone:hover { border-color: var(--color-accent); background: var(--color-accent-100); }
  .dropzone:focus-visible { outline: 2px solid var(--color-accent); outline-offset: -2px; }
  .drop-icon { color: var(--color-accent-700); }
  .drop-hint { font-family: var(--font-heading); font-size: 14.5px; }
  .drop-sub { font-size: 11.5px; color: var(--color-neutral-600); }
  .file-list { list-style: none; margin: 14px -6px 0; padding: 0; max-height: 220px; overflow-y: auto; display: flex; flex-direction: column; gap: 2px; }
  .file-list li { display: flex; align-items: center; gap: 10px; font-size: 13px; padding: 7px 6px; border-radius: 2px; }
  .fname-wrap { flex: 1; min-width: 0; }
  .fname { display: block; }
  .progress { height: 3px; border-radius: 2px; background: var(--color-neutral-200); margin-top: 4px; overflow: hidden; }
  .progress-bar.indeterminate { height: 100%; width: 40%; background: var(--color-accent); animation: indeterminate 1.1s ease-in-out infinite; }
  @keyframes indeterminate { 0% { margin-left: -40%; } 100% { margin-left: 100%; } }
  .fsize { color: var(--color-neutral-600); font-size: 11px; white-space: nowrap; flex: none; }
  .fstatus { font-size: 11.5px; white-space: nowrap; flex: none; width: 96px; text-align: right; }
  .fstatus.status-ready { color: var(--color-neutral-600); }
  .fstatus.status-uploading { color: var(--color-accent-700); }
  .fstatus.status-saved { color: var(--color-neutral-700); }
  .fstatus.status-rejected, .fstatus.status-skipped { color: var(--color-accent-2-700); }
  .row-x {
    width: 18px; height: 18px; flex: none; display: flex; align-items: center; justify-content: center;
    border: 0; background: transparent; cursor: pointer; color: var(--color-neutral-700); border-radius: 2px; padding: 0;
  }
  .row-x:hover { background: var(--color-accent-2-200); color: var(--color-accent-2-800); }
  .row-x:disabled { opacity: 0.4; cursor: not-allowed; }
  .quota-line { margin: 12px 0 0; font-size: 11.5px; color: var(--color-neutral-600); }
  .ocr-note { margin: 6px 0 0; font-size: 11.5px; color: var(--color-neutral-600); }
  .meta { font-size: 11.5px; color: var(--color-neutral-600); }
</style>

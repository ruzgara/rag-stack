<script lang="ts">
  import { library } from "../state/library.svelte";
  import { guard, ui } from "../state/ui.svelte";

  let fileInput: HTMLInputElement | undefined = $state();
  let selectedLibrary = $state("");

  const onUpload = guard(async (e: Event) => {
    const files = (e.currentTarget as HTMLInputElement).files;
    if (!files || files.length === 0) return;
    const res = await library.upload(files, selectedLibrary || null);
    if (fileInput) fileInput.value = "";

    const parts: string[] = [];
    if (res.saved.length) parts.push(`saved: ${res.saved.join(", ")}`);
    if (res.rejected.length) {
      parts.push(`rejected: ${res.rejected.map((r) => `${r.filename ?? "(unnamed)"} -- ${r.reason}`).join("; ")}`);
    }
    parts.push(res.ingest);
    ui.toast(parts.join("\n"), res.rejected.length ? "err" : "ok");
  });
</script>

<h3>Upload</h3>
<div class="row">
  <input bind:this={fileInput} type="file" multiple onchange={onUpload} disabled={library.uploading} style="max-width: 100%;" />
</div>
<div class="row">
  <select bind:value={selectedLibrary} disabled={library.uploading}>
    <option value="">(no library)</option>
    {#each library.libraries as lib (lib.id)}
      <option value={lib.id}>{lib.name}</option>
    {/each}
  </select>
</div>

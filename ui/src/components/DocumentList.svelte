<script lang="ts">
  import { library } from "../state/library.svelte";
  import { onMount, onDestroy } from "svelte";

  onMount(() => void library.loadDocuments());
  onDestroy(() => library.stopDocPoll());

  const docs = $derived(library.documents);
</script>

<h3>Documents</h3>
{#if !docs}
  <p class="small">Loading&hellip;</p>
{:else}
  {#if docs.ingest.running || docs.ingest.pending}
    <p class="small"><b>Ingesting&hellip;</b></p>
  {/if}
  {#if docs.ingest.last_run && docs.ingest.last_run.exit_code !== 0}
    <p class="small err-text">Last ingest run failed (exit {docs.ingest.last_run.exit_code}) &mdash; see data/logs/ingest-api.log</p>
  {/if}
  {#if docs.inbox.length > 0}
    <p class="small">In inbox: {docs.inbox.join(", ")}</p>
  {/if}
  <div class="small">
    {#each docs.documents as d (d.sha256)}
      <div style="margin-bottom: 4px;">
        &bull; {d.filename} <span class="small">({d.status ?? "unknown"}{d.pages ? `, ${d.pages} p.` : ""})</span>
        {#if d.error}<div class="err-text">{d.error}</div>{/if}
      </div>
    {:else}
      <p>No documents yet.</p>
    {/each}
  </div>
{/if}

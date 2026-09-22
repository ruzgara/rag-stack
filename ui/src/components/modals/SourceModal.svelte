<script lang="ts">
  // The source viewer: one passage highlighted between its neighbours, ‹ ›
  // through the rest of that answer's passages, prev/next through the chat's
  // citations of this document in citation order, and "Open full document"
  // to hand over to DocumentModal for the whole thing. The
  // original file isn't served by the API. DocumentModal is the Sources
  // tab's document-at-a-time counterpart; ChunkView is the pane they share.
  import { untrack } from "svelte";
  import type { Source } from "../../lib/types";
  import { citedDocuments, type Mark } from "../../lib/turns";
  import { locator } from "../../lib/format";
  import { ChunkReader } from "../../lib/reader.svelte";
  import { chat } from "../../state/chat.svelte";
  import { ui } from "../../state/ui.svelte";
  import ModalShell from "../ui/ModalShell.svelte";
  import ChunkView from "./ChunkView.svelte";

  interface Props {
    turnKey: string;
    handle: string;
    /** Chunks to mark, in citation order -- `handle`'s own and every other
     *  passage of this document the conversation referenced (chat.openSource). */
    marks?: Mark[];
  }
  const { turnKey, handle, marks }: Props = $props();

  const turns = $derived([...chat.views.values()].find((v) => v.turns.some((t) => t.key === turnKey))?.turns ?? []);
  const turn = $derived(turns.find((t) => t.key === turnKey) ?? null);
  const source = $derived<Source | null>(turn?.sources.find((s) => s.handle === handle) ?? null);
  // Cited first (in reader order), then uncited -- there's no meaningful
  // number to sort the latter by, and mixing them by retrieval order would
  // put an uncited passage ahead of a cited [1:1].
  const handles = $derived.by(() => {
    const sources = turn?.sources ?? [];
    const cited = sources.filter((s) => s.cited)
      .sort((a, b) => (a.doc_number ?? 0) - (b.doc_number ?? 0) || (a.chunk_number ?? 0) - (b.chunk_number ?? 0));
    const uncited = sources.filter((s) => !s.cited);
    return [...cited, ...uncited].map((s) => s.handle);
  });

  const reader = new ChunkReader();
  $effect(() => {
    reader.marks = marks?.length ? marks : source ? [{ chunkIndex: source.chunk_index, label: null }] : [];
  });
  // (Re)start whenever the viewer is pointed at a different source. Stepping
  // to another source through the arrows drops back to the single passage.
  $effect(() => {
    const s = source;
    if (!s) return;
    untrack(() => reader.open(s.doc_sha, s.chunk_index, false));
  });

  // Reading the whole document is DocumentModal's job: hand over to it on
  // the chunk you're on, with the sidebar on the Sources tab beside it. A
  // document the chat never cited isn't one of its pages, so that one
  // opens out in place instead.
  function openFull(): void {
    if (!source) return;
    if (citedDocuments(turns).some((d) => d.docSha === source.doc_sha)) {
      ui.panelTab = "sources";
      ui.replace({ kind: "document", docSha: source.doc_sha, chunk: reader.focus });
    } else {
      reader.openFull();
    }
  }

  function otherSource(delta: number): void {
    const i = handles.indexOf(handle) + delta;
    if (i < 0 || i >= handles.length) return;
    ui.selectedTurnKey = turnKey;
    chat.openSource(turnKey, handles[i], { replace: true });
  }

  function onKey(e: KeyboardEvent): void {
    if (ui.dialog || (e.target as HTMLElement)?.closest("input, textarea")) return;
    if (e.key === "ArrowDown" || e.key === "j") { e.preventDefault(); reader.jump(reader.nextMark); }
    if (e.key === "ArrowUp" || e.key === "k") { e.preventDefault(); reader.jump(reader.prevMark); }
    if (e.key === "ArrowRight") otherSource(1);
    if (e.key === "ArrowLeft") otherSource(-1);
  }

  const origin = $derived.by(() => {
    if (!source) return "";
    const { focus, total } = reader;
    const parts = [];
    if (focus !== source.chunk_index) parts.push(`chunk ${focus + 1}${total ? ` of ${total}` : ""} · ${focus > source.chunk_index ? "+" : ""}${focus - source.chunk_index} from the cited one`);
    else parts.push(`chunk ${focus + 1}${total ? ` of ${total}` : ""}`);
    if (reader.marks.length > 1) parts.push(`${reader.marks.length} passages from this chat`);
    if (source.rerank_score !== null) parts.push(`rerank ${source.rerank_score.toFixed(2)}`);
    parts.push(source.found_by === "tool" ? "found by a model search" : "found by auto-search");
    return parts.join(" · ");
  });
</script>

<svelte:window onkeydown={onKey} />

<ModalShell width={820} labelledby="source-title">
  {#if !source}
    <p class="muted">This source is no longer available.</p>
  {:else}
    <div class="src-head">
      {#if source.doc_number !== null}<span class="num">[{source.doc_number}:{source.chunk_number}]</span>{/if}
      <div class="titles">
        <h4 id="source-title">{source.filename}</h4>
        <div class="loc">
          {locator(source.pages, source.headings) || "—"}
          {#if source.headings.length > 1}<span class="muted"> · {source.headings.slice(0, -1).join(" › ")}</span>{/if}
        </div>
        <div class="loc muted">found for “{source.query}”</div>
      </div>
      {#if handles.length > 1}
        <div class="src-nav">
          <button class="btn btn-ghost btn-sm" onclick={() => otherSource(-1)} disabled={handles.indexOf(handle) === 0} aria-label="Previous source">‹</button>
          <button class="btn btn-ghost btn-sm" onclick={() => otherSource(1)} disabled={handles.indexOf(handle) === handles.length - 1} aria-label="Next source">›</button>
        </div>
      {/if}
      <button class="x" onclick={() => ui.close()} aria-label="Close">×</button>
    </div>

    <ChunkView {reader} {turns} fallback={source.text} />
  {/if}
  {#snippet footer()}
    {#if source}
      <div class="nav">
        <button class="btn btn-secondary" onclick={() => reader.jump(reader.prevMark)} disabled={reader.prevMark === null || reader.loading}>← Previous citation</button>
        <button class="btn btn-secondary" onclick={() => reader.jump(reader.nextMark)} disabled={reader.nextMark === null || reader.loading}>Next citation →</button>
      </div>
      <div class="meta">
        <span class="origin">{origin}</span>
        {#if !reader.full}
          <button class="btn btn-ghost" onclick={openFull} disabled={reader.loading || !!reader.error}>Open full document</button>
        {/if}
      </div>
    {/if}
  {/snippet}
</ModalShell>

<style>
  .src-head { display: flex; align-items: flex-start; gap: 14px; padding-bottom: 14px; }
  .num { font-family: var(--font-heading); font-size: 20px; font-weight: 600; color: var(--color-accent-700); }
  .titles { flex: 1; min-width: 0; }
  h4 { margin: 0 0 2px; font-size: 20px; overflow-wrap: anywhere; }
  .loc { font-size: 11.5px; color: var(--color-neutral-700); }
  .src-nav { display: flex; gap: 2px; }
  .x { border: 0; background: transparent; font-size: 20px; line-height: 1; cursor: pointer; color: var(--color-neutral-700); padding: 2px 4px; }
  /* Two fixed rows, so nothing moves as the status text changes length:
     the citation buttons, then the status and any action right-aligned. */
  .nav { display: flex; gap: 8px; flex-basis: 100%; }
  .meta { display: flex; align-items: center; justify-content: flex-end; gap: 12px; flex-basis: 100%; min-width: 0; }
  .origin { font-size: 11.5px; color: var(--color-neutral-600); text-align: right; flex: 1; min-width: 0; }
  .meta .btn { flex: none; }
</style>

<script lang="ts">
  // The document viewer, the Sources tab's: one cited document read whole,
  // every passage the chat cited from it highlighted, prev/next through
  // those citations in citation order, and ‹ › on to the chat's other cited
  // documents in the Sources tab's order. SourceModal is the one-passage
  // counterpart a citation opens; ChunkView is the pane they share.
  import { untrack } from "svelte";
  import { plural } from "../../lib/format";
  import { ChunkReader } from "../../lib/reader.svelte";
  import { citedDocuments, passageMarks } from "../../lib/turns";
  import { chat } from "../../state/chat.svelte";
  import { ui } from "../../state/ui.svelte";
  import ModalShell from "../ui/ModalShell.svelte";
  import ChunkView from "./ChunkView.svelte";

  interface Props {
    docSha: string;
    /** The chunk to open on; the document's first citation otherwise. */
    chunk?: number;
  }
  const { docSha, chunk }: Props = $props();

  const turns = $derived(chat.active?.turns ?? []);
  const docs = $derived(citedDocuments(turns));
  const index = $derived(docs.findIndex((d) => d.docSha === docSha));
  const doc = $derived(index === -1 ? null : docs[index]);

  const reader = new ChunkReader();
  $effect(() => {
    reader.marks = doc ? passageMarks(doc.passages) : [];
  });
  // Only on a new document or starting chunk: `doc` itself is rebuilt
  // whenever any turn changes, and that mustn't reload what you're reading.
  $effect(() => {
    const sha = docSha;
    const start = chunk;
    untrack(() => {
      const first = doc?.passages[0]?.chunkIndex;
      if (start === undefined && first === undefined) return;
      reader.open(sha, start ?? first!, true);
    });
  });

  function otherDocument(delta: number): void {
    const next = docs[index + delta];
    if (next) ui.replace({ kind: "document", docSha: next.docSha });
  }

  function onKey(e: KeyboardEvent): void {
    if (ui.dialog || (e.target as HTMLElement)?.closest("input, textarea")) return;
    if (e.key === "ArrowDown" || e.key === "j") { e.preventDefault(); reader.jump(reader.nextMark); }
    if (e.key === "ArrowUp" || e.key === "k") { e.preventDefault(); reader.jump(reader.prevMark); }
    if (e.key === "ArrowRight") otherDocument(1);
    if (e.key === "ArrowLeft") otherDocument(-1);
  }

  /** "chunk 5 of 48 · [4:1], citation 1 of 3" */
  const origin = $derived.by(() => {
    const { focus, total, current, marks } = reader;
    const parts = [`chunk ${focus + 1}${total ? ` of ${total}` : ""}`];
    if (current?.label) parts.push(`${current.label}, citation ${marks.findIndex((m) => m.chunkIndex === focus) + 1} of ${marks.length}`);
    return parts.join(" · ");
  });
</script>

<svelte:window onkeydown={onKey} />

<ModalShell width={820} labelledby="document-title">
  {#if !doc}
    <p class="muted">This document is no longer cited in this chat.</p>
  {:else}
    <div class="src-head">
      {#if doc.docNumber !== null}<span class="num">[{doc.docNumber}]</span>{/if}
      <div class="titles">
        <h4 id="document-title">{doc.filename}</h4>
        <div class="loc">{plural(doc.citedCount, "passage")} cited · in {plural(doc.turnKeys.length, "answer")}</div>
      </div>
      {#if docs.length > 1}
        <div class="src-nav">
          <button class="btn btn-ghost btn-sm" onclick={() => otherDocument(-1)} disabled={index === 0} aria-label="Previous document">‹</button>
          <button class="btn btn-ghost btn-sm" onclick={() => otherDocument(1)} disabled={index === docs.length - 1} aria-label="Next document">›</button>
        </div>
      {/if}
      <button class="x" onclick={() => ui.close()} aria-label="Close">×</button>
    </div>

    <ChunkView {reader} {turns} fallback={doc.passages[0]?.text ?? ""} />
  {/if}
  {#snippet footer()}
    {#if doc}
      <div class="nav">
        <button class="btn btn-secondary" onclick={() => reader.jump(reader.prevMark)} disabled={reader.prevMark === null || reader.loading}>← Previous citation</button>
        <button class="btn btn-secondary" onclick={() => reader.jump(reader.nextMark)} disabled={reader.nextMark === null || reader.loading}>Next citation →</button>
      </div>
      <div class="meta"><span class="origin">{origin}</span></div>
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
</style>

<script lang="ts">
  import { chat } from "../../state/chat.svelte";
  import { ui } from "../../state/ui.svelte";
  import { locator, plural } from "../../lib/format";
  import { citedDocuments, type DocumentRef } from "../../lib/turns";
  import type { Source } from "../../lib/types";

  const turn = $derived.by(() => {
    const turns = chat.active?.turns ?? [];
    return turns.find((t) => t.key === ui.selectedTurnKey) ?? turns[turns.length - 1] ?? null;
  });
  // Carried-forward sources ("earlier") this answer did not itself cite
  // weren't retrieved for this turn -- they're excluded from the panel
  // entirely, not just uncited-styled, and from the "N retrieved" count.
  const retrieved = $derived((turn?.sources ?? []).filter((s) => s.found_by !== "earlier" || s.cited));
  // Mid-stream nothing is cited yet (`cited` arrives with `done`), so every
  // source shows as retrieved until then.
  const cited = $derived(retrieved.filter((s) => s.cited)
    .sort((a, b) => (a.doc_number ?? 0) - (b.doc_number ?? 0) || (a.chunk_number ?? 0) - (b.chunk_number ?? 0)));
  const uncited = $derived(retrieved.filter((s) => !s.cited));

  function scoreColor(s: Source): string {
    return (s.rerank_score ?? 0) >= 0.8 ? "var(--color-accent-700)" : "var(--color-neutral-600)";
  }

  function openSource(s: Source): void {
    if (!turn) return;
    chat.openSource(turn.key, s.handle);
  }

  // Scroll the first focused card into view (citation hover/click, or the
  // document a viewer has open) -- on whichever tab is showing.
  let list: HTMLDivElement | undefined = $state();
  $effect(() => {
    const handle = ui.focusedSources[0];
    const doc = ui.focusedDoc;
    if (!list) return;
    const el = (handle !== undefined ? list.querySelector(`[data-handle="${handle}"]`) : null)
      ?? (doc !== null ? list.querySelector(`[data-doc="${doc}"]`) : null);
    el?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  });

  // The Sources tab is the whole conversation, not the selected turn: every
  // document any answer cited, folded together, and only the passages it
  // cited -- what was merely retrieved stays on the Citations tab. Both the
  // title and a passage open the document viewer, the latter on that passage.
  // A card lights up while a viewer has its document open (or it's hovered);
  // a citation hovered in an answer lights just its passage row.
  const documents = $derived(citedDocuments(chat.active?.turns ?? []));

  /** "3 passages cited · in 2 answers" */
  function docMeta(d: DocumentRef): string {
    return `${plural(d.citedCount, "passage")} cited · in ${plural(d.turnKeys.length, "answer")}`;
  }
</script>

{#snippet card(s: Source)}
  <button class="src" class:focused={ui.focusedSources.includes(s.handle)} class:uncited={!s.cited && turn?.status !== "streaming"}
          data-handle={s.handle} onclick={() => openSource(s)}>
    <div class="src-head">
      {#if s.doc_number !== null}<span class="n">[{s.doc_number}:{s.chunk_number}]</span>{/if}
      <span class="title">{s.filename}</span>
    </div>
    <div class="src-meta">
      <span class="loc">{locator(s.pages, s.headings)}</span>
      {#if s.rerank_score !== null}
        <span class="score" style:color={scoreColor(s)}>{s.rerank_score.toFixed(2)}</span>
      {/if}
    </div>
    <p class="text">{s.text}</p>
  </button>
{/snippet}

{#snippet doc(d: DocumentRef)}
  <div class="doc" class:focused={ui.focusedDoc === d.docSha} data-doc={d.docSha}>
    <button class="src-head title-row" onclick={() => ui.open({ kind: "document", docSha: d.docSha })}
            title="Read the whole document with this chat's passages marked">
      {#if d.docNumber !== null}<span class="n">[{d.docNumber}]</span>{/if}
      <span class="title">{d.filename}</span>
    </button>
    <div class="src-meta"><span class="loc">{docMeta(d)}</span></div>
    <div class="passages">
      {#each d.passages as p (p.handle)}
        <button class="passage" class:focused={ui.focusedSources.includes(p.handle)} data-handle={p.handle} onclick={() => ui.open({ kind: "document", docSha: d.docSha, chunk: p.chunkIndex })}>
          <span class="n">[{p.docNumber}:{p.chunkNumber}]</span>
          <span class="where">{locator(p.pages, p.headings) || p.text.slice(0, 60)}</span>
        </button>
      {/each}
    </div>
  </div>
{/snippet}

<aside class="panel">
  <div class="head">
    <div class="seg wide" role="group" aria-label="Panel">
      <button type="button" aria-pressed={ui.panelTab === "citations"} onclick={() => (ui.panelTab = "citations")}>Citations</button>
      <button type="button" aria-pressed={ui.panelTab === "sources"} onclick={() => (ui.panelTab = "sources")}>Sources</button>
    </div>
    {#if ui.panelTab === "citations" && turn && retrieved.length}
      <span class="counts">{turn.status !== "streaming" ? `${cited.length} cited · ` : ""}{retrieved.length} retrieved</span>
    {:else if ui.panelTab === "sources" && documents.length}
      <span class="counts">{plural(documents.length, "document")} cited</span>
    {/if}
  </div>

  {#if ui.panelTab === "citations"}
    <div class="list rag-scroll" bind:this={list}>
      {#if !turn || retrieved.length === 0}
        <p class="empty">
          {#if turn?.status === "streaming"}Passages appear here as the answer searches.
          {:else if turn}This answer didn't draw on any documents.
          {:else}The passages and answer cites appear here, marked [doc:chunk] as it cites them.{/if}
        </p>
      {:else}
        {#each cited as s (s.handle)}{@render card(s)}{/each}
        {#if uncited.length}
          {#if cited.length}<div class="sub">Retrieved, not cited</div>{/if}
          {#each uncited as s (s.handle)}{@render card(s)}{/each}
        {/if}
      {/if}
    </div>
  {:else}
    <div class="list rag-scroll" bind:this={list}>
      {#if documents.length === 0}
        <p class="empty">Every document this chat cites collects here, however far back it was cited.</p>
      {:else}
        {#each documents as d (d.docSha)}{@render doc(d)}{/each}
      {/if}
    </div>
  {/if}
</aside>

<style>
  .panel { display: flex; flex-direction: column; min-height: 0; height: 100%; background: var(--color-surface); padding: 14px 0; }
  .head { display: flex; flex-direction: column; align-items: center; gap: 6px; padding: 0 16px 8px; }
  .seg.wide { align-self: stretch; }
  .seg.wide > button { flex: 1; text-align: center; }
  .counts { font-size: 10.5px; color: var(--color-neutral-600); }
  .list { flex: 1; min-height: 0; overflow-y: auto; padding: 0 12px 8px; display: flex; flex-direction: column; gap: 8px; }
  .empty { font-size: 12.5px; line-height: 1.55; color: var(--color-neutral-600); padding: 4px 4px; font-style: italic; }
  .sub { font-size: 10px; letter-spacing: .14em; text-transform: uppercase; color: var(--color-neutral-600); padding: 8px 4px 0; }

  .src {
    display: block; width: 100%; text-align: left; border: 0; font: inherit; color: inherit;
    cursor: pointer; background: var(--color-bg); border-radius: var(--radius-md); padding: 9px 11px 10px;
    box-shadow: var(--shadow-sm); transition: box-shadow .15s, opacity .15s;
  }
  .src:hover { box-shadow: var(--shadow-md); }
  .src.focused { box-shadow: 0 0 0 1.5px var(--color-accent), var(--shadow-md); }
  .src.uncited { opacity: .72; }
  .src.uncited:hover { opacity: 1; }
  .src-head { display: flex; align-items: baseline; gap: 7px; margin-bottom: 4px; }
  .n { font-family: var(--font-heading); font-size: 11.5px; font-weight: 600; color: var(--color-accent-700); flex: none; }
  .title { font-size: 12.5px; font-family: var(--font-heading); line-height: 1.25; flex: 1; min-width: 0; overflow-wrap: anywhere; }
  .src-meta { display: flex; align-items: baseline; gap: 8px; font-size: 10.5px; color: var(--color-neutral-600); margin-bottom: 5px; }
  .loc { flex: 1; min-width: 0; }
  .score { flex: none; font-variant-numeric: tabular-nums; }
  .title-row {
    width: 100%; border: 0; background: transparent; font: inherit; color: inherit; text-align: left;
    cursor: pointer; padding: 0 0 4px; margin-bottom: 0;
  }
  .title-row:hover .title { text-decoration: underline; text-underline-offset: 2px; }
  .doc {
    background: var(--color-bg); border-radius: var(--radius-md); padding: 9px 11px 8px; box-shadow: var(--shadow-sm);
    transition: box-shadow .15s;
  }
  .doc:hover, .doc.focused { box-shadow: 0 0 0 1.5px var(--color-accent), var(--shadow-md); }
  .passages { display: flex; flex-direction: column; gap: 1px; }
  .passage {
    display: flex; align-items: baseline; gap: 7px; width: 100%; text-align: left; border: 0; font: inherit;
    color: inherit; cursor: pointer; background: transparent; border-radius: var(--radius-sm); padding: 3px 5px;
    transition: background .15s;
  }
  .passage:hover { background: var(--color-accent-100); }
  .passage.focused { box-shadow: 0 0 0 1.5px var(--color-accent); }
  .passage .n { font-size: 10.5px; flex: none; }
  .where { font-size: 11.5px; color: var(--color-neutral-700); flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  .text {
    margin: 0; font-size: 12px; line-height: 1.5; color: var(--color-neutral-800);
    display: -webkit-box; -webkit-line-clamp: 4; line-clamp: 4; -webkit-box-orient: vertical; overflow: hidden;
  }
</style>

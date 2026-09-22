<script lang="ts">
  // One question + answer, as the design lays it out: the question with a
  // magenta rule, the answer with a cyan one, and between the answer's
  // header and its text a collapsible "process" -- the searches, listings
  // and reasoning that produced it. Live and reopened turns are the same
  // Turn shape, so this can't tell which it was given; the only difference
  // is that reasoning text is never stored, so a reopened turn has none.
  import { deriveInvalid, processSummary, type Turn } from "../../lib/turns";
  import { buildCiteMap } from "../../lib/citations";
  import { clock, locator, plural } from "../../lib/format";
  import { session } from "../../state/session.svelte";
  import { ui, guard } from "../../state/ui.svelte";
  import { chat } from "../../state/chat.svelte";
  import Markdown from "./Markdown.svelte";

  interface Props {
    turn: Turn;
    isLast: boolean;
    /** The conversation's preset, for the header before `done` says which ran. */
    preset: string;
    busy: boolean;
  }
  const { turn, isLast, preset, busy }: Props = $props();

  const streaming = $derived(turn.status === "streaming");
  const map = $derived(buildCiteMap(turn.sources));
  const invalid = $derived(streaming ? [] : deriveInvalid(turn.answer, turn.sources));
  const thinkPhase = $derived(streaming && !turn.answer && turn.thinking.length > 0);
  const searches = $derived(turn.trace.filter((s) => s.verb === "search").length);
  const bySource = $derived(new Map(turn.sources.map((s) => [s.handle, s])));

  const model = $derived(turn.model ?? session.presetInfo(turn.preset ?? preset)?.model ?? null);
  const presetName = $derived(turn.preset ?? preset);

  // null / absent = follow the phase (open while reasoning, as the design does).
  let processOpenSet = $state<boolean | null>(null);
  let thinkOpenSet = $state<Record<number, boolean>>({});
  let openStep = $state<number>(-1);
  const processOpen = $derived(processOpenSet ?? thinkPhase);
  // The run being written right now, if reasoning is what the turn is doing.
  const liveThink = $derived(
    thinkPhase && turn.trace[turn.trace.length - 1]?.verb === "thinking" ? turn.trace.length - 1 : -1,
  );
  const thinkOpen = (i: number): boolean => thinkOpenSet[i] ?? i === liveThink;

  // Ticks the "first token" clock while waiting for it.
  let now = $state(Date.now());
  $effect(() => {
    if (!streaming) return;
    const id = setInterval(() => (now = Date.now()), 250);
    return () => clearInterval(id);
  });

  const liveSummary = $derived.by(() => {
    if (turn.queuePosition !== null) return `waiting in line · position ${turn.queuePosition}`;
    const s = searches ? `${plural(searches, "search", "searches")}` : "";
    if (thinkPhase) return `reasoning${s ? ` · ${s} done` : ""}`;
    if (turn.answer) return `generating${s ? ` · ${s} so far` : ""}`;
    const last = turn.trace[turn.trace.length - 1];
    if (last?.failed) return "planning failed · searching the message as written";
    if (last?.verb === "search") return `searching · “${last.text}”`;
    if (last?.verb === "list") return `listing ${last.text}`;
    return "starting";
  });

  const firstToken = $derived.by(() => {
    if (turn.firstTokenAt && turn.startedAt) return (turn.firstTokenAt - turn.startedAt) / 1000;
    return null;
  });

  function stepNote(i: number): string {
    const step = turn.trace[i];
    if (step.verb !== "search") return "";
    const parts: string[] = [];
    if (step.foundBy === "tool") parts.push("asked for by the model");
    const hits = step.sources.map((n) => bySource.get(n)).filter((s) => s !== undefined);
    parts.push(hits.length ? plural(hits.length, "passage") : streaming && i === turn.trace.length - 1 ? "…" : "nothing new");
    const scores = hits.map((s) => s.rerank_score).filter((x): x is number => x !== null);
    if (scores.length) {
      const lo = Math.min(...scores).toFixed(2);
      const hi = Math.max(...scores).toFixed(2);
      parts.push(lo === hi ? `rerank ${hi}` : `rerank ${lo}–${hi}`);
    }
    if (step.seconds !== undefined) parts.push(`${step.seconds.toFixed(1)}s`);
    return " · " + parts.join(" · ");
  }

  const VERB: Record<string, string> = { planned: "planned", search: "search", list: "list", note: "decided", thinking: "thinking" };

  function openSource(handle: string): void {
    ui.focusSources([handle], turn.key);
    ui.open({ kind: "source", turnKey: turn.key, handle });
  }

  /** null for an invalid span too, since those carry no data-handle by
   *  construction (citations.ts) -- the click/hover handlers below ignore
   *  them without needing a class check of their own. */
  function citationHandle(el: Element | null): string | null {
    if (!(el instanceof HTMLElement) || !el.dataset.handle) return null;
    return el.dataset.handle;
  }

  function onAnswerClick(e: MouseEvent): void {
    const handle = citationHandle((e.target as Element).closest(".cite"));
    if (handle) openSource(handle);
  }

  function onAnswerKey(e: KeyboardEvent): void {
    if (e.key !== "Enter" && e.key !== " ") return;
    const handle = citationHandle((e.target as Element).closest(".cite"));
    if (!handle) return;
    e.preventDefault();
    openSource(handle);
  }

  function onAnswerOver(e: Event): void {
    const handle = citationHandle((e.target as Element).closest(".cite"));
    if (handle) ui.focusSources([handle], turn.key);
  }

  let copied = $state(false);
  const copy = guard(async () => {
    await navigator.clipboard.writeText(turn.answer);
    copied = true;
    setTimeout(() => (copied = false), 1500);
  });

  const showingSources = $derived(ui.selectedTurnKey === turn.key || (ui.selectedTurnKey === null && isLast));
</script>

<article class="turn">
  <div class="q">
    <div class="who">You · {clock(turn.askedAt)}</div>
    <p class="q-text">{turn.question}</p>
  </div>

  <div class="a" class:dim={!showingSources && turn.sources.length > 0}>
    <div class="who">
      <span>Assistant{model ? ` · ${model}` : ""}{presetName ? ` · ${presetName} preset` : ""}</span>
    </div>

    <div class="process">
      <button class="process-toggle" onclick={() => (processOpenSet = !processOpen)} aria-expanded={processOpen}>
        {#if streaming}<span class="pulse" aria-hidden="true"></span>{/if}
        <span>{streaming ? liveSummary : processSummary(turn)}</span>
        {#if turn.trace.length}
          <span class="toggle-label">{processOpen ? "Hide ▲" : "Show ▼"}</span>
        {/if}
      </button>

      {#if processOpen}
        <div class="trace">
          {#each turn.trace as step, i (i)}
            <div class="step">
              <span class="verb" class:err-text={step.failed}>{step.failed ? "no plan" : VERB[step.verb]}</span>
              <div class="step-body">
                {#if step.verb === "thinking"}
                  {@const open = thinkOpen(i)}
                  <button class="step-btn" onclick={() => (thinkOpenSet[i] = !open)} aria-expanded={open}>
                    <span>Reasoning · {plural(step.tokens ?? 0, "token")}{i === liveThink ? " so far" : ""}</span>
                    <span class="toggle-label">{open ? "Hide ▲" : "Show ▼"}</span>
                  </button>
                  {#if open}
                    <div class="thinking rag-scroll">
                      {step.text}{#if i === liveThink}<span class="cursor-block small" aria-hidden="true"></span>{/if}
                    </div>
                  {/if}
                {:else if step.failed}
                  <span>{step.text}<span class="muted"> · the planning step failed, so the message was searched exactly as written</span></span>
                {:else if step.sources.length}
                  <button class="step-btn" onclick={() => (openStep = openStep === i ? -1 : i)} aria-expanded={openStep === i}>
                    <span>{step.text}<span class="muted">{stepNote(i)}</span></span>
                    <span class="toggle-label">{openStep === i ? "Hide ▲" : "Show ▼"}</span>
                  </button>
                {:else}
                  <span>{step.text}<span class="muted">{stepNote(i)}</span></span>
                {/if}
                {#if openStep === i && step.sources.length}
                  <div class="results">
                    {#each step.sources as handle (handle)}
                      {@const s = bySource.get(handle)}
                      {#if s}
                        <button class="result" onclick={() => openSource(handle)}>
                          {#if s.doc_number !== null}<span class="n">[{s.doc_number}:{s.chunk_number}]</span>{/if}
                          <span class="r-title">{s.filename}<span class="muted">{locator(s.pages, s.headings) ? ` · ${locator(s.pages, s.headings)}` : ""}</span></span>
                        </button>
                      {/if}
                    {/each}
                  </div>
                {/if}
              </div>
            </div>
          {/each}
          {#if !streaming && turn.trace.length === 0}
            <div class="step"><span class="verb"></span><span class="muted">No steps recorded for this answer.</span></div>
          {/if}
        </div>
      {/if}
    </div>

    {#if turn.answer || !streaming}
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <div class="answer" onclick={onAnswerClick} onkeydown={onAnswerKey} onmouseover={onAnswerOver} onfocusin={onAnswerOver}>
        {#if turn.answer}
          <Markdown answer={turn.answer} status={turn.status} {map} />
        {:else if turn.status === "complete"}
          <p class="muted">(empty answer)</p>
        {/if}
      </div>
    {/if}

    {#if turn.status === "error"}
      <p class="note err-text">The answer failed{turn.error ? `: ${turn.error}` : "."}</p>
    {:else if turn.status === "stopped"}
      <p class="note muted">Stopped{turn.answer ? " — this is as far as it got." : " before it wrote anything."}</p>
    {/if}
    {#if invalid.length > 0}
      <!-- Never prints an entry of `invalid` itself: it may be a raw handle,
           and a raw handle must never reach the reader, garbled or not. -->
      <p class="note err-text">Cites {plural(invalid.length, "source")} that {invalid.length > 1 ? "don't" : "doesn't"} exist in this answer.</p>
    {/if}
  </div>

  {#if streaming}
    {#if turn.answer}
      <div class="foot">
        <span class="stat">{plural(turn.answerTokens, "token")}{firstToken !== null ? ` · first token ${firstToken.toFixed(1)}s` : ""}</span>
      </div>
    {:else}
      <div class="foot"><span class="stat">{turn.startedAt ? `${((now - turn.startedAt) / 1000).toFixed(0)}s` : ""}</span></div>
    {/if}
  {:else}
    <div class="foot actions">
      {#if turn.answer}
        <button class="btn btn-ghost act" onclick={copy}>{copied ? "Copied" : "Copy"}</button>
      {/if}
      {#if isLast}
        <button class="btn btn-ghost act" onclick={guard(() => chat.retry())} disabled={busy}>Retry</button>
      {/if}
      {#if turn.trace.length}
        <button class="btn btn-ghost act" onclick={() => (processOpenSet = !processOpen)}>
          {processOpen ? "Hide searches" : "Show searches"}
        </button>
      {/if}
      {#if turn.sources.length && !showingSources}
        <button class="btn btn-ghost act" onclick={() => { ui.focusSources([], turn.key); ui.sourcesOpen = true; }}>Show sources</button>
      {/if}
      {#if turn.stats?.completion_tokens}
        <span class="stat">{plural(turn.stats.completion_tokens, "token")}{turn.stats.first_token_s != null ? ` · first token ${turn.stats.first_token_s.toFixed(1)}s` : ""}</span>
      {/if}
    </div>
  {/if}
</article>

<style>
  .turn { margin-bottom: 8px; }
  .who {
    display: flex; align-items: center; gap: 10px;
    font-size: 10px; letter-spacing: .14em; text-transform: uppercase;
    color: var(--color-neutral-600); margin-bottom: 6px;
  }
  .q { margin-bottom: 30px; padding-left: 14px; border-left: 2px solid var(--color-accent-2-500); }
  .q-text { font-size: 16.5px; line-height: 1.6; margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
  .a { padding-left: 14px; border-left: 2px solid var(--color-accent); transition: border-color .2s; }
  .a.dim { border-left-color: color-mix(in srgb, var(--color-accent) 40%, transparent); }
  .a .who { margin-bottom: 4px; }

  .process { margin-bottom: 16px; }
  .process-toggle {
    border: 0; background: transparent; padding: 0; cursor: pointer; font: inherit;
    font-size: 12px; color: var(--color-accent-700); display: flex; align-items: center; gap: 7px; text-align: left;
  }
  .process-toggle:hover { color: var(--color-accent-800); }
  .toggle-label { font-size: 10px; color: var(--color-accent-700); flex: none; }

  .trace { display: flex; flex-direction: column; gap: 5px; margin-top: 10px; font-size: 12.5px; color: var(--color-neutral-700); }
  .step { display: flex; gap: 9px; }
  .verb { color: var(--color-accent-700); width: 62px; flex: none; padding-top: 1px; }
  .verb.err-text { color: var(--color-accent-2-700); }
  .step-body { min-width: 0; flex: 1; overflow-wrap: anywhere; }
  .step-body > span { color: var(--color-text); }
  .step-btn {
    border: 0; background: transparent; padding: 0; cursor: pointer; font: inherit; color: var(--color-text);
    display: flex; align-items: baseline; gap: 7px; text-align: left;
  }
  .step-btn:hover { color: var(--color-accent-700); }
  .results { margin-top: 6px; display: flex; flex-direction: column; gap: 3px; }
  .result {
    border: 0; background: var(--color-surface); border-radius: var(--radius-md); padding: 6px 10px; cursor: pointer;
    font: inherit; text-align: left; display: flex; gap: 8px; align-items: baseline;
  }
  .result:hover { background: var(--color-accent-100); }
  .n { font-family: var(--font-heading); font-size: 11px; font-weight: 600; color: var(--color-accent-700); flex: none; }
  .r-title { font-size: 12px; color: var(--color-text); }
  .thinking {
    margin-top: 7px; padding: 10px 13px; background: var(--color-surface); border-radius: var(--radius-md);
    font-size: 12.5px; line-height: 1.6; font-style: italic; color: var(--color-neutral-800);
    max-height: 180px; overflow-y: auto; white-space: pre-wrap;
  }
  .cursor-block.small {
    display: inline-block; width: 7px; height: 14px; margin-left: 2px; vertical-align: -2px;
    background: var(--color-accent); animation: rag-pulse 1s ease-in-out infinite;
  }

  .note { font-size: 13px; margin: 10px 0 0; }
  .foot {
    display: flex; gap: 14px; align-items: center; flex-wrap: wrap;
    margin: 6px 0 26px; padding-left: 14px; font-size: 12px; color: var(--color-neutral-700); min-height: 20px;
  }
  .actions { gap: 6px 10px; margin-top: 4px; }
  .act { font-size: 12px; padding: 2px 6px; }
  .stat { font-size: 11.5px; color: var(--color-neutral-600); }
  .actions .stat { margin-left: auto; }
</style>

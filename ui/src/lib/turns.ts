// One shape for a question+answer pair, built identically whether it came
// from a live SSE stream or from GET /api/conversations/{id}: <Turn> receives
// one of these and can't tell which path built it. Pure -- no Svelte, no
// DOM -- so the event handling and the stored-trace reconstruction are
// unit-tested (tests/turns.test.ts).

import type { AnswerStatus, Message, Source, TurnEvent, TurnStats } from "./types";
import { buildCiteMap, parseCitations } from "./citations";

export type TurnStatus = "streaming" | AnswerStatus;

/** One line of the "process" trace under an answer's header: what the turn
 *  did before (and between) writing. */
export interface TraceStep {
  verb: "planned" | "search" | "list" | "note" | "thinking";
  text: string;
  /** For searches: the handles of the sources this query brought in. */
  sources: string[];
  foundBy?: "auto" | "tool";
  /** Rewrite only: the model never answered, so `text` is the raw message. */
  failed?: boolean;
  /** Seconds, when the server measured it (only the auto search is). */
  seconds?: number;
  /** For reasoning: tokens in this run of it. */
  tokens?: number;
}

export interface Turn {
  key: string; // stable {#each} key: server msg id, or "pending:<n>"
  question: string;
  askedAt: number; // epoch seconds
  questionId: string | null;
  answer: string;
  thinking: string;
  /** Approximate: Ollama sends about one token per event. */
  thinkingTokens: number;
  answerTokens: number;
  status: TurnStatus;
  error: string | null;
  model: string | null;
  preset: string | null;
  searchQueries: string[];
  sources: Source[];
  stats: TurnStats | null;
  trace: TraceStep[];
  queuePosition: number | null;
  /** Client clock, for "first token 1.4s" while it streams. */
  startedAt: number | null;
  firstTokenAt: number | null;
  /** Whether the thinking text is this session's (it isn't stored). */
  hasLiveThinking: boolean;
}

export function newTurn(key: string, question: string, askedAt = Date.now() / 1000): Turn {
  return {
    key,
    question,
    askedAt,
    questionId: null,
    answer: "",
    thinking: "",
    thinkingTokens: 0,
    answerTokens: 0,
    status: "streaming",
    error: null,
    model: null,
    preset: null,
    searchQueries: [],
    sources: [],
    stats: null,
    trace: [],
    queuePosition: null,
    startedAt: Date.now(),
    firstTokenAt: null,
    hasLiveThinking: false,
  };
}

/** Back to a blank streaming answer for the same question -- a retry. */
export function resetForRetry(t: Turn): void {
  Object.assign(t, {
    answer: "", thinking: "", thinkingTokens: 0, answerTokens: 0, status: "streaming", error: null,
    searchQueries: [], sources: [], stats: null, trace: [], queuePosition: null,
    startedAt: Date.now(), firstTokenAt: null, hasLiveThinking: false,
  } satisfies Partial<Turn>);
}

/** Applies one SSE event to a turn in place. Mutate the reactive proxy
 *  (the element read back out of the $state array), not a plain copy. */
export function applyEvent(t: Turn, ev: TurnEvent): void {
  switch (ev.type) {
    case "queued":
      t.queuePosition = ev.position;
      break;
    case "user":
      t.questionId = ev.id;
      t.queuePosition = null;
      break;
    case "plan":
      t.queuePosition = null;
      t.trace.push(ev.failed
        ? { verb: "planned", text: planText(ev.queries), sources: [], failed: true }
        : ev.search
          ? { verb: "planned", text: planText(ev.queries), sources: [] }
          : { verb: "note", text: "no search needed for this message", sources: [] });
      break;
    case "searching":
      t.queuePosition = null;
      t.searchQueries.push(ev.query);
      t.trace.push({ verb: "search", text: ev.query, sources: [], foundBy: ev.found_by });
      break;
    case "sources": {
      // A `sources` event follows its own `searching` event. Deduped and
      // REPLACED on handle collision, not appended: a carried-forward
      // source that a fresh search re-finds is re-emitted with the same
      // handle (chat.py._search's upgrade-in-place), so the second copy
      // here must overwrite the first rather than sit beside it.
      const step = [...t.trace].reverse().find((s) => s.verb === "search");
      for (const s of ev.sources) {
        const i = t.sources.findIndex((x) => x.handle === s.handle);
        if (i === -1) t.sources.push(s);
        else t.sources[i] = s;
        if (step && !step.sources.includes(s.handle)) step.sources.push(s.handle);
      }
      break;
    }
    case "listing":
      t.trace.push({ verb: "list", text: ev.document ? `documents matching “${ev.document}”` : "all documents", sources: [] });
      break;
    case "listing_libraries":
      t.trace.push({ verb: "list", text: "libraries", sources: [] });
      break;
    case "thinking": {
      t.queuePosition = null;
      t.thinking += ev.text;
      t.thinkingTokens += 1;
      t.hasLiveThinking = true;
      // Reasoning that resumes after a search (or a listing) is a run of
      // its own, so the trace reads think, search, think in the order it
      // happened rather than collapsing into one block at the end.
      const last = t.trace[t.trace.length - 1];
      if (last?.verb === "thinking") {
        last.text += ev.text;
        last.tokens = (last.tokens ?? 0) + 1;
      } else {
        t.trace.push({ verb: "thinking", text: ev.text, sources: [], tokens: 1 });
      }
      break;
    }
    case "token":
      t.queuePosition = null;
      if (t.firstTokenAt === null) t.firstTokenAt = Date.now();
      t.answer += ev.text;
      t.answerTokens += 1;
      break;
    case "done": {
      t.answer = ev.answer;
      t.model = ev.model;
      t.preset = ev.preset;
      t.searchQueries = ev.search_queries;
      t.sources = ev.sources; // cumulative and authoritative -- replace, don't merge
      t.stats = ev.stats;
      t.status = "complete";
      const auto = t.trace.find((s) => s.verb === "search" && s.foundBy === "auto");
      if (auto && ev.stats.search_s !== undefined) auto.seconds = ev.stats.search_s;
      break;
    }
    case "title":
      break; // the conversation's, not the turn's -- the caller handles it
    case "citation": {
      // Onto the source itself rather than a map of its own: the citation
      // map is derived from t.sources, so numbering the source is what makes
      // the answer render [doc:chunk] instead of a placeholder. The source
      // always arrived first -- passages are streamed before the tokens
      // citing them -- so a miss here would mean a server-side bug, not a race.
      const src = t.sources.find((s) => s.handle === ev.handle);
      if (src) {
        src.doc_number = ev.doc;
        src.chunk_number = ev.chunk;
      }
      break;
    }
    case "error":
      t.status = "error";
      t.error = ev.error;
      break;
    case "stopped":
      if (ev.answer !== undefined) t.answer = ev.answer;
      if (ev.cited) {
        const cited = new Set(ev.cited);
        const refs = ev.refs ?? {};
        t.sources = t.sources.map((s) => {
          const ref = refs[s.handle];
          return { ...s, cited: cited.has(s.handle), doc_number: ref ? ref[0] : s.doc_number,
                   chunk_number: ref ? ref[1] : s.chunk_number };
        });
      }
      t.status = "stopped";
      break;
  }
}

/** One trace line for the planned queries, however many there were. Each one
 *  also gets its own `search` step, so this reads as the plan, not a repeat. */
function planText(queries: string[]): string {
  return queries.join(" · ");
}

/** The trace as far as the server kept it. What's stored is the search
 *  queries (the planner's first, then each tool query), each source's query,
 *  and a few stats -- not the order tool calls interleaved in, not what a
 *  document listing returned, and not the reasoning text. */
export function traceFromStored(searchQueries: string[], sources: Source[], stats: TurnStats | null): TraceStep[] {
  const steps: TraceStep[] = [];
  const auto = stats?.auto_search === true;
  const planned = auto ? Math.max(1, stats?.auto_queries ?? 1) : 0;
  if (stats?.plan_s !== undefined) {
    steps.push(stats.plan_failed
      ? { verb: "planned", text: planText(searchQueries.slice(0, Math.max(1, planned))), sources: [], failed: true }
      : auto
        ? { verb: "planned", text: planText(searchQueries.slice(0, planned)), sources: [] }
        : { verb: "note", text: "no search needed for this message", sources: [] });
  }
  const claimed = new Set<string>();
  searchQueries.forEach((q, i) => {
    const foundBy = i < planned ? "auto" : "tool";
    // found_by here is only ever "auto" or "tool" -- a carried-forward
    // ("earlier") source never matches, so it's excluded from the trace
    // without this needing to say so. Correct, but only because nothing
    // upstream can produce query === q with found_by "earlier": it's
    // carried in at run_turn's start, not from a search step at all.
    const handles = sources
      .filter((s) => s.query === q && s.found_by === foundBy && !claimed.has(s.handle))
      .map((s) => s.handle);
    handles.forEach((h) => claimed.add(h));
    steps.push({ verb: "search", text: q, sources: handles, foundBy,
                 seconds: foundBy === "auto" ? stats?.search_s : undefined });
  });
  const listed = stats?.listed_documents ?? 0;
  if (listed) {
    steps.push({ verb: "list", text: `the documents${listed > 1 ? `, ${listed} times` : ""}`, sources: [] });
  }
  return steps;
}

/** GET /api/conversations/{id} returns a flat alternating list of user and
 *  assistant messages; pair them into the same Turn objects the live path
 *  builds. An orphan user message with no reply yet (still running, or the
 *  server died before storing even a partial) stays as a turn with no
 *  answer rather than being dropped. */
export function toTurns(messages: Message[]): Turn[] {
  const turns: Turn[] = [];
  let i = 0;

  while (i < messages.length) {
    const msg = messages[i];
    if (msg.role !== "user") {
      i++;
      continue;
    }

    const turn = newTurn(msg.id, msg.content, msg.created_at);
    turn.questionId = msg.id;
    turn.startedAt = null;

    const next = messages[i + 1];
    if (next && next.role === "assistant") {
      turn.key = next.id;
      turn.answer = next.content;
      turn.model = next.model;
      turn.preset = next.preset;
      turn.searchQueries = next.search_queries;
      turn.sources = next.sources;
      turn.stats = next.stats;
      turn.status = next.status;
      turn.error = next.error;
      turn.trace = traceFromStored(next.search_queries, next.sources, next.stats);
      i += 2;
    } else {
      i += 1;
    }

    turns.push(turn);
  }

  return turns;
}

/** The server's invalid_citations is sent on `done` but never stored, so a
 *  reopened turn derives it here, from the message's own sources -- mirrors
 *  citations.py's resolve() (the invalid half of it; cited isn't needed
 *  here, the stored `sources[].cited` already has it). Entries may be a raw
 *  handle -- never rendered, see citations.ts's renderItem -- a "d:c" ref
 *  string, or a malformed token; callers must not print one verbatim. */
export function deriveInvalid(answer: string, sources: Source[]): string[] {
  const map = buildCiteMap(sources);
  const invalid: string[] = [];
  for (const citation of parseCitations(answer)) {
    for (const item of citation.items) {
      if (item.handle !== undefined) {
        if (!map.handles.has(item.handle) && !invalid.includes(item.handle)) invalid.push(item.handle);
      } else if (item.ref !== undefined) {
        const key = `${item.ref[0]}:${item.ref[1]}`;
        if (!map.refs.has(key) && !invalid.includes(key)) invalid.push(key);
      } else if (item.raw !== undefined && !invalid.includes(item.raw)) {
        invalid.push(item.raw);
      }
    }
  }
  return invalid;
}

/** "3 searches · 7 sources cited · 4.2s" */
export function processSummary(t: Turn): string {
  const searches = t.trace.filter((s) => s.verb === "search").length;
  const cited = t.sources.filter((s) => s.cited).length;
  const parts: string[] = [];
  if (t.trace.some((s) => s.failed === true)) parts.push("plan failed");
  parts.push(searches === 0 ? "no search" : `${searches} search${searches === 1 ? "" : "es"}`);
  if (t.sources.length) parts.push(`${cited} source${cited === 1 ? "" : "s"} cited`);
  if (t.stats?.total_s !== undefined) parts.push(`${t.stats.total_s.toFixed(1)}s`);
  return parts.join(" · ");
}

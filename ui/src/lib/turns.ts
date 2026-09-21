// One shape for a question+answer pair, built identically whether it came
// from a live SSE stream or from GET /api/conversations/{id}: <Turn> receives
// one of these and can't tell which path built it. Pure -- no Svelte, no
// DOM -- so the event handling and the stored-trace reconstruction are
// unit-tested (tests/turns.test.ts).

import type { AnswerStatus, Message, Source, TurnEvent, TurnStats } from "./types";
import { citationRe, parseCitation } from "./citations";

export type TurnStatus = "streaming" | AnswerStatus;

/** One line of the "process" trace under an answer's header: what the turn
 *  did before (and between) writing. */
export interface TraceStep {
  verb: "rewrote" | "search" | "list" | "note";
  text: string;
  /** For searches: the source numbers this query brought in. */
  sources: number[];
  foundBy?: "auto" | "tool";
  /** Seconds, when the server measured it (only the auto search is). */
  seconds?: number;
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
    case "rewrite":
      t.queuePosition = null;
      t.trace.push(ev.search
        ? { verb: "rewrote", text: ev.query, sources: [] }
        : { verb: "note", text: "no search needed for this message", sources: [] });
      break;
    case "searching":
      t.queuePosition = null;
      t.searchQueries.push(ev.query);
      t.trace.push({ verb: "search", text: ev.query, sources: [], foundBy: ev.found_by });
      break;
    case "sources": {
      // A `sources` event follows its own `searching` event.
      const step = [...t.trace].reverse().find((s) => s.verb === "search");
      for (const s of ev.sources) {
        if (!t.sources.some((x) => x.number === s.number)) t.sources.push(s);
        if (step && !step.sources.includes(s.number)) step.sources.push(s.number);
      }
      break;
    }
    case "listing":
      t.trace.push({ verb: "list", text: ev.document ? `documents matching “${ev.document}”` : "all documents", sources: [] });
      break;
    case "listing_libraries":
      t.trace.push({ verb: "list", text: "libraries", sources: [] });
      break;
    case "thinking":
      t.queuePosition = null;
      t.thinking += ev.text;
      t.thinkingTokens += 1;
      t.hasLiveThinking = true;
      break;
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
    case "error":
      t.status = "error";
      t.error = ev.error;
      break;
    case "stopped":
      if (ev.answer !== undefined) t.answer = ev.answer;
      if (ev.cited) {
        const cited = new Set(ev.cited);
        t.sources = t.sources.map((s) => ({ ...s, cited: cited.has(s.number) }));
      }
      t.status = "stopped";
      break;
  }
}

/** The trace as far as the server kept it. What's stored is the search
 *  queries (auto first, then each tool query), each source's query, and a
 *  few stats -- not the order tool calls interleaved in, not what a
 *  document listing returned, and not the reasoning text. */
export function traceFromStored(searchQueries: string[], sources: Source[], stats: TurnStats | null): TraceStep[] {
  const steps: TraceStep[] = [];
  const auto = stats?.auto_search === true;
  if (stats?.rewrite_s !== undefined) {
    steps.push(auto
      ? { verb: "rewrote", text: searchQueries[0] ?? "", sources: [] }
      : { verb: "note", text: "no search needed for this message", sources: [] });
  }
  const claimed = new Set<number>();
  searchQueries.forEach((q, i) => {
    const foundBy = auto && i === 0 ? "auto" : "tool";
    const nums = sources
      .filter((s) => s.query === q && s.found_by === foundBy && !claimed.has(s.number))
      .map((s) => s.number);
    nums.forEach((n) => claimed.add(n));
    steps.push({ verb: "search", text: q, sources: nums, foundBy,
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
 *  reopened turn derives it here. Mirrors chat.py:resolve_citations. */
export function deriveInvalid(answer: string, sources: Source[]): number[] {
  const known = new Set(sources.map((s) => s.number));
  const seen: number[] = [];
  const re = citationRe();
  for (let m = re.exec(answer); m; m = re.exec(answer)) {
    for (const n of parseCitation(m[1])) {
      if (!seen.includes(n)) seen.push(n);
    }
  }
  return seen.filter((n) => !known.has(n));
}

/** "3 searches · 7 sources cited · 4.2s" */
export function processSummary(t: Turn): string {
  const searches = t.trace.filter((s) => s.verb === "search").length;
  const cited = t.sources.filter((s) => s.cited).length;
  const parts: string[] = [];
  parts.push(searches === 0 ? "no search" : `${searches} search${searches === 1 ? "" : "es"}`);
  if (t.sources.length) parts.push(`${cited} source${cited === 1 ? "" : "s"} cited`);
  if (t.stats?.total_s !== undefined) parts.push(`${t.stats.total_s.toFixed(1)}s`);
  return parts.join(" · ");
}

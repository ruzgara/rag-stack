// One shape for a question+answer pair, built identically whether it came
// from a live SSE stream or from GET /api/conversations/{id}. See plan §7:
// <Turn.svelte> receives one of these and cannot tell which path built it.

import type { Message, Source, TurnStats } from "./types";
import { citationRe, parseCitation } from "./citations";

export type TurnStatus = "streaming" | "complete" | "error";

export interface Turn {
  key: string; // stable {#each} key: server msg id, or "pending:<n>"
  question: string;
  questionId: string | null; // filled by the `user` SSE event; reconciles the optimistic bubble
  answer: string; // tokens so far, or the stored content
  thinking: string;
  status: TurnStatus;
  error: string | null;
  model: string | null;
  preset: string | null;
  searchQueries: string[];
  sources: Source[];
  stats: TurnStats | null;
  events: string[]; // the status line, built identically on both paths
  queuePosition: number | null;
}

export function newTurn(key: string, question: string): Turn {
  return {
    key,
    question,
    questionId: null,
    answer: "",
    thinking: "",
    status: "streaming",
    error: null,
    model: null,
    preset: null,
    searchQueries: [],
    sources: [],
    stats: null,
    events: [],
    queuePosition: null,
  };
}

/** GET /api/conversations/{id} returns a flat alternating list of user and
 *  assistant messages; pair them into the exact same Turn objects the live
 *  path builds. An orphan user message with no following assistant reply
 *  (the server crashed before persisting even a partial) is still shown, as
 *  a streaming-looking turn with no answer yet -- it will look right once
 *  the page is reloaded after the server recovers. */
export function toTurns(messages: Message[]): Turn[] {
  const turns: Turn[] = [];
  let i = 0;

  while (i < messages.length) {
    const msg = messages[i];
    if (msg.role !== "user") {
      i++; // shouldn't happen given the server's alternation, but don't crash on it
      continue;
    }

    const turn = newTurn(msg.id, msg.content);
    turn.questionId = msg.id;

    const next = messages[i + 1];
    if (next && next.role === "assistant") {
      turn.answer = next.content;
      turn.model = next.model;
      turn.preset = next.preset;
      turn.searchQueries = next.search_queries;
      turn.sources = next.sources;
      turn.stats = next.stats;
      turn.status = next.status;
      turn.error = next.error;
      turn.events = buildEvents(next.search_queries, next.status, next.error);
      i += 2;
    } else {
      // No assistant reply persisted at all -- leave it looking "in flight"
      // rather than silently dropping the question.
      i += 1;
    }

    turns.push(turn);
  }

  return turns;
}

function buildEvents(searchQueries: string[], status: "complete" | "error", error: string | null): string[] {
  const events = searchQueries.map((q) => `search: “${q}”`);
  if (status === "error" && error) events.push(`error: ${error}`);
  return events;
}

/** The server computes invalid_citations at chat.py:483 and sends it in the
 *  `done` payload, but NEVER PERSISTS it -- db.py stores only `cited` per
 *  source. So a reopened turn has no way to show the chip a live turn shows
 *  unless this is derived client-side. Mirrors chat.py:resolve_citations. */
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

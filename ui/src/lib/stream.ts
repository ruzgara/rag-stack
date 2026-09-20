// The rag-stack-specific half of the SSE client: POSTs a turn, drives
// SseParser, and resolves to one of five terminal outcomes. See plan §3 for
// how each outcome maps to server state and to what chat.svelte.ts should do.

import { SseParser, type SseFrame } from "./sse";
import { toApiError } from "./api";
import type { TurnEvent } from "./types";

export type TurnOutcome =
  | { kind: "complete" } // `done` seen (a `title` may follow)
  | { kind: "rejected"; error: string } // `error` with NO preceding `user` -- gate busy, nothing stored
  | { kind: "failed"; error: string } // `error` AFTER `user` -- a partial was persisted
  | { kind: "truncated" } // neither -- network died mid-stream; the turn may still finish server-side
  | { kind: "aborted" }; // we called abort(); the server turn keeps going

export async function runTurn(
  convId: string,
  body: { content: string; preset?: string },
  onEvent: (ev: TurnEvent) => void,
  signal: AbortSignal,
): Promise<TurnOutcome> {
  const res = await fetch(`/api/conversations/${convId}/messages`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  // 401/403/404/422 arrive here, before any stream exists. Once the stream is
  // open the status is always 200 and every failure is an `error` event.
  if (!res.ok || !res.body) throw await toApiError(res);

  const parser = new SseParser();
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let sawUser = false;
  let sawDone = false;
  let failure: string | null = null;

  const dispatch = (frames: SseFrame[]) => {
    for (const f of frames) {
      let payload: Record<string, unknown>;
      try {
        payload = f.data ? JSON.parse(f.data) : {};
      } catch {
        continue; // one bad frame is not a dead stream
      }
      const ev = { type: f.event, ...payload } as TurnEvent;
      if (ev.type === "user") sawUser = true;
      if (ev.type === "done") sawDone = true; // do NOT stop reading: `title` comes after
      if (ev.type === "error") failure = ev.error;
      onEvent(ev);
    }
  };

  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      dispatch(parser.push(decoder.decode(value, { stream: true })));
    }
    dispatch(parser.push(decoder.decode())); // flush a split multi-byte UTF-8 sequence
    dispatch(parser.flush()); // flush a final frame with no blank line
  } catch {
    return signal.aborted ? { kind: "aborted" } : { kind: "truncated" };
  } finally {
    reader.cancel().catch(() => {});
  }

  if (failure !== null) {
    return sawUser ? { kind: "failed", error: failure } : { kind: "rejected", error: failure };
  }
  return sawDone ? { kind: "complete" } : { kind: "truncated" };
}

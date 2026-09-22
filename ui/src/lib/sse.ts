// A spec-shaped SSE frame parser. Feed it decoded string chunks in order; it
// returns whatever frames completed. Chunk boundaries fall anywhere -- no
// assumption is made about where a chunk starts or ends relative to a frame.
//
// This module knows nothing about rag-stack: no DOM, no fetch. See stream.ts
// for the part that turns frames into TurnEvents and owns the terminal state
// machine.

export interface SseFrame {
  event: string;
  data: string;
  id?: string;
  retry?: number;
}

export class SseParser {
  private buf = "";

  push(chunk: string): SseFrame[] {
    this.buf += chunk;
    let text = this.buf;

    // A CRLF split across a chunk boundary would normalize to "\n" now and
    // leave a stray "\n" next time -- indistinguishable from the blank line
    // that ends a frame. Hold a trailing lone \r back until the next chunk.
    let held = "";
    if (text.endsWith("\r")) {
      held = "\r";
      text = text.slice(0, -1);
    }
    text = text.replace(/\r\n|\r/g, "\n");

    const frames: SseFrame[] = [];
    for (let i = text.indexOf("\n\n"); i >= 0; i = text.indexOf("\n\n")) {
      const frame = parseBlock(text.slice(0, i));
      if (frame) frames.push(frame);
      text = text.slice(i + 2);
    }
    this.buf = text + held;
    return frames;
  }

  /** End of stream. A server that closes without a trailing blank line still
   *  sent a frame; EventSource drops it, and we must not. */
  flush(): SseFrame[] {
    const rest = this.buf.replace(/\r\n|\r/g, "\n").replace(/\n+$/, "");
    this.buf = "";
    const frame = rest ? parseBlock(rest) : null;
    return frame ? [frame] : [];
  }
}

function parseBlock(block: string): SseFrame | null {
  let event = "";
  let id: string | undefined;
  let retry: number | undefined;
  const data: string[] = [];

  for (const line of block.split("\n")) {
    if (line === "" || line.startsWith(":")) continue; // blank / comment (keep-alive)
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1); // exactly one optional leading space
    if (field === "event") event = value;
    else if (field === "data") data.push(value); // multi-line data joins with \n
    else if (field === "id") id = value;
    else if (field === "retry") retry = Number(value) || undefined;
  }

  if (!event && !data.length) return null;
  return { event: event || "message", data: data.join("\n"), id, retry };
}

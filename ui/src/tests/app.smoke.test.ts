// Mounts the whole app in jsdom against a fake API: signs in, opens a
// conversation, streams a turn (with a citation and a stop), and opens every
// popup. Catches what svelte-check can't -- runtime errors, reactivity
// loops, a popup that throws on mount.

import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { flushSync, mount, tick, unmount } from "svelte";

const now = Date.now() / 1000;
const me = {
  user: { id: "u1", email: "ellis@example.com", display_name: "Ellis Marchetti", is_admin: false, disabled: false,
          groups: ["Corporate"], created_at: now - 1e6, last_login_at: now },
  auth_mode: "password",
};
const libraries = [
  { id: "L1", name: "Halcyon — Executed MSAs", description: "vendor agreements", owner_id: "u1", owner_name: "Ellis Marchetti",
    created_at: now, document_count: 2, role: "owner", source: "owner" },
  { id: "L2", name: "Precedent Bank", description: null, owner_id: "u9", owner_name: "R. Okonjo",
    created_at: now, document_count: 1180, role: "read", source: "group" },
];
const workspaces = [
  { id: "W1", name: "Halcyon diligence", description: null, created_at: now, updated_at: now, last_used_at: now,
    library_ids: ["L1", "L2"], conversation_count: 1 },
  { id: "W2", name: "Scratch", description: null, created_at: now, updated_at: now, last_used_at: now - 10,
    library_ids: [], conversation_count: 0 },
];
// HANDLE_RE-shaped test handles -- see services/query/citations.py: S + 4
// alphabet chars with a digit forced third.
const CITED_HANDLE = "Sk29fp";
const UNCITED_HANDLE = "Sm4p2q";
const EARLIER_HANDLE = "Sz29zz"; // carried forward, not cited by m2 -- excluded from its panel/count
const RETRY_HANDLE = "Sa2bcd";

const source = (handle: string, cited: boolean, foundBy: "auto" | "tool" | "earlier" = "auto") => ({
  found_by: foundBy, query: "indemnity cap", point_id: `p-${handle}`, doc_sha: "sha1", filename: "Arbor MSA.pdf",
  chunk_index: 4, pages: [24], headings: ["11 Liability", "11.3 Limitation"], text: `Passage ${handle} text.`,
  rerank_score: 0.91, handle, doc_number: cited ? 1 : null, chunk_number: cited ? 1 : null, cited,
});
const convList = [{ id: "C1", title: "Indemnity caps", preset: "hard", workspace_id: "W1", created_at: now - 100,
  updated_at: now - 50, last_read_at: now - 60, last_message_at: now - 50, archived: 0, message_count: 2, library_ids: ["L1"] }];
const convDetail = {
  ...convList[0],
  messages: [
    { id: "m1", role: "user", content: "What caps did we agree?", created_at: now - 60 },
    { id: "m2", role: "assistant", content: `Arbor is capped at 1× fees [${CITED_HANDLE}].`, created_at: now - 50, model: "qwen", preset: "hard",
      search_queries: ["indemnity cap"], status: "complete", error: null,
      stats: { auto_search: true, plan_s: 0.2, search_s: 0.8, total_s: 4.2, completion_tokens: 12, first_token_s: 1.1, context_used_pct: 62 },
      // The third source is carried forward from an earlier turn and this
      // answer doesn't cite it -- SourcesPanel must exclude it from both the
      // list and the "N retrieved" count (it wasn't retrieved this turn).
      sources: [source(CITED_HANDLE, true), source(UNCITED_HANDLE, false), source(EARLIER_HANDLE, false, "earlier")] },
  ],
};

let signedIn = false;
let stopCalled = false;

function sse(events: [string, unknown][]): Response {
  const text = events.map(([e, d]) => `event: ${e}\ndata: ${JSON.stringify(d)}\n\n`).join("");
  const bytes = new TextEncoder().encode(text);
  return new Response(new ReadableStream({
    start(c) { c.enqueue(bytes.slice(0, 40)); c.enqueue(bytes.slice(40)); c.close(); },
  }), { status: 200, headers: { "content-type": "text/event-stream" } });
}

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function route(url: string, init?: RequestInit): Response {
  const u = new URL(url, "http://localhost");
  const p = u.pathname;
  const m = init?.method ?? "GET";
  if (p === "/api/health") return json({ ok: true, ollama: { ok: true }, qdrant: { ok: true, chunks: 187 }, database: { ok: true },
    load: { ok: true, running: 0, queued: 0, max_concurrent: 2, max_per_user: 2 },
    presets: { ok: true, everyday: { model: "qwen", tools: true, think: false }, hard: { model: "qwen", tools: true, think: true } },
    auth: { ok: true, mode: "password" } });
  if (p === "/api/auth/me") return signedIn ? json(me) : json({ detail: "not signed in" }, 401);
  if (p === "/api/auth/login") { signedIn = true; return json(me); }
  if (p === "/api/quota") return json({ uploads: { unlimited: true, files_used: 0, mb_used: 0 }, load: { running: 0, queued: 1, max_concurrent: 2, max_per_user: 2 } });
  if (p === "/api/libraries") return json({ libraries });
  if (p === "/api/libraries/L1") return json({ ...libraries[0], documents: [
    { sha256: "sha1", filename: "Arbor MSA.pdf", status: "processed", route: "digital", pages: 48, chars: 1, error: null, processed_at: now },
    { sha256: "sha2", filename: "Broken.pdf", status: "failed", route: null, pages: null, chars: null, error: "docling failed", processed_at: now }] });
  if (p === "/api/libraries/L1/access") return json({ owner: { id: "u1", name: "Ellis" }, access: [{ subject_type: "group", subject_id: "g1", name: "Corporate", role: "read" }] });
  if (p === "/api/documents") return json({ documents: [], inbox: ["new.pdf"], ingest: { autorun: false, running: false, pending: false, last_run: { started_at: now - 100, finished_at: now - 90, exit_code: 0 } } });
  if (p === "/api/documents/sha1/chunks") return json({ sha256: "sha1", filename: "Arbor MSA.pdf", total: 10, chunks: [
    { chunk_index: 3, headings: ["11 Liability"], pages: [24], text: "11.2 before" },
    { chunk_index: 4, headings: ["11 Liability"], pages: [24], text: "11.3 the cap" },
    { chunk_index: 5, headings: ["11 Liability"], pages: [24], text: "11.4 after" }] });
  if (p === "/api/workspaces") return json({ workspaces: u.searchParams.get("deleted") ? [] : workspaces });
  if (p === "/api/workspaces/W1/conversations") return json(convList);
  if (p === "/api/workspaces/W2/conversations") return json([]);
  if (p === "/api/conversations" && m === "GET") return json([]);
  if (p === "/api/conversations/C1" && m === "GET") return json(convDetail);
  if (p === "/api/conversations/C1/messages") return sse([
    ["user", { id: "m3" }], ["plan", { queries: ["northgate cap"], search: true }],
    ["searching", { query: "northgate cap", found_by: "auto" }], ["sources", { sources: [source(RETRY_HANDLE, false)] }],
    ["thinking", { text: "Considering" }], ["token", { text: "Northgate is uncapped " }], ["token", { text: `[${RETRY_HANDLE}].` }],
    ["stopped", { message_id: "m4", answer: `Northgate is uncapped [${RETRY_HANDLE}].`, cited: [RETRY_HANDLE],
                  refs: { [RETRY_HANDLE]: [1, 1] } }]]);
  if (p === "/api/conversations/C1/stop") { stopCalled = true; return json({ stopped: 1 }); }
  if (p === "/api/groups") return json({ groups: [{ id: "g1", name: "Corporate", description: null, type: "open", member_count: 3 }] });
  if (p === "/api/tokens") return json({ tokens: [] });
  if (p === "/api/uploads") return json({ uploads: [] });
  console.warn(`UNMOCKED ${m} ${u.pathname}${u.search}`);
  return json({ detail: `unmocked ${m} ${p}` }, 404);
}

const errors: unknown[] = [];

beforeAll(() => {
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => route(url, init)));
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  Element.prototype.scrollIntoView = () => {};
  window.addEventListener("error", (e) => errors.push(e.error));
  window.addEventListener("unhandledrejection", (e) => errors.push(e.reason));
});

let app: Record<string, unknown> | null = null;
afterEach(() => { if (app) unmount(app); app = null; });

async function settle(rounds = 8): Promise<void> {
  for (let i = 0; i < rounds; i++) {
    await new Promise((r) => setTimeout(r, 0));
    await tick();
  }
  // requestAnimationFrame-driven markdown
  await new Promise((r) => setTimeout(r, 40));
  flushSync();
}

const text = () => document.body.textContent ?? "";
const button = (label: string | RegExp) => {
  const b = [...document.querySelectorAll("button")].reverse().find((x) => {
    const name = x.getAttribute("aria-label") ?? x.textContent?.trim() ?? "";
    return typeof label === "string" ? name === label || x.textContent?.trim() === label : label.test(name);
  });
  if (!b) throw new Error(`no button ${label}`);
  return b as HTMLButtonElement;
};

describe("app smoke", () => {
  it("signs in, opens a chat, streams and stops a turn, opens every popup", async () => {
    history.replaceState({}, "", "/c/C1");
    document.body.innerHTML = '<div id="app"></div>';
    const { default: App } = await import("../App.svelte");
    app = mount(App, { target: document.getElementById("app")! });
    await settle();

    // Gate
    expect(text()).toContain("Sign in");
    (document.getElementById("email") as HTMLInputElement).value = "ellis@example.com";
    document.getElementById("email")!.dispatchEvent(new Event("input"));
    (document.getElementById("password") as HTMLInputElement).value = "pw";
    document.getElementById("password")!.dispatchEvent(new Event("input"));
    document.querySelector("form")!.dispatchEvent(new Event("submit", { cancelable: true }));
    await settle();

    // Shell
    expect(text()).toContain("Halcyon diligence");
    expect(text()).toContain("Precedent Bank");
    expect(text()).toContain("Indemnity caps");
    expect(text()).toContain("What caps did we agree?");
    expect(text()).toContain("1 search · 1 source cited · 4.2s");
    expect(text()).toContain("context 62% full");
    expect(document.querySelector(".md .cite")?.textContent).toBe("[1:1]");
    // 2 retrieved, not 3: the carried-forward EARLIER_HANDLE source this
    // answer didn't cite is excluded from the panel and this count.
    expect(text()).toContain("1 cited · 2 retrieved");
    expect(text()).toContain("Ellis Marchetti");

    // Expand the process trace
    button(/1 search · 1 source cited/).click();
    await settle();
    expect(text()).toContain("planned");
    expect(text()).toContain("indemnity cap");

    // Citation -> source viewer with its neighbouring chunks
    (document.querySelector(".md .cite") as HTMLElement).click();
    await settle();
    expect(text()).toContain("11.3 the cap");
    expect(text()).toContain("chunk 5 of 10");
    button("Close").click();
    await settle();

    // Send a turn that streams and ends in `stopped`
    const ta = document.getElementById("composer-input") as HTMLTextAreaElement;
    ta.value = "And Northgate?";
    ta.dispatchEvent(new Event("input"));
    await settle();
    button("Send").click();
    await settle(20);
    expect(text()).toContain("And Northgate?");
    expect(text()).toContain("Northgate is uncapped");
    expect(text()).toContain("Stopped — this is as far as it got.");

    // Every popup mounts
    const popups: [() => void, string][] = [
      [() => button(/Switch/).click(), "Switch or manage"],
      [() => button("+ Add more").click(), "Libraries"],
      [() => (document.querySelector(".lib-row .row-main") as HTMLElement).click(), "Arbor MSA.pdf"],
      [() => button(/Recently deleted/).click(), "Recently deleted"],
      [() => button(/Attach documents/).click(), "Add documents"],
    ];
    for (const [open, expected] of popups) {
      open();
      await settle();
      expect(text()).toContain(expected);
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
      await settle();
      expect(document.querySelector(".modal-backdrop")).toBeNull();
    }

    // Library -> Sharing stacks, and closing returns to the library
    (document.querySelector(".lib-row .row-main") as HTMLElement).click();
    await settle();
    button("Sharing…").click();
    await settle();
    expect(document.querySelectorAll(".modal-backdrop")).toHaveLength(2);
    expect(text()).toContain("Corporate");

    for (const item of ["Groups", "Settings"]) {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
      await settle();
      button(/Ellis Marchetti/).click();
      await settle();
      button(item).click();
      await settle();
      expect(document.querySelector(".modal-backdrop")).not.toBeNull();
    }

    expect(stopCalled).toBe(false);
    expect(errors).toEqual([]);
  });
});

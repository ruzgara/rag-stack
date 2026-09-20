// Conversations, the active turn list, and the send/abort lifecycle. See
// plan §3 for how each TurnOutcome maps to state here, and §7 for why
// <Turn.svelte> is the one renderer for both a live and a reopened turn.

import { get, post, patch, del } from "../lib/api";
import { runTurn } from "../lib/stream";
import { toTurns, newTurn, type Turn } from "../lib/turns";
import type { ConversationBase, ConversationDetail, ConversationListItem, Preset, TurnEvent } from "../lib/types";
import { navigate } from "../router/routes.svelte";
import { session } from "./session.svelte";
import { ui } from "./ui.svelte";

/** Mutates the live reactive Turn (read from the $state array, never a
 *  plain object held separately -- see the comment on send() for why that
 *  distinction matters under Svelte 5's proxy-based deep reactivity). */
function applyEvent(t: Turn, ev: TurnEvent): void {
  switch (ev.type) {
    case "queued":
      t.queuePosition = ev.position;
      t.events.push(`waiting for a free slot (position ${ev.position})`);
      break;
    case "user":
      t.questionId = ev.id;
      break;
    case "rewrite":
      t.events.push(ev.search ? `rewritten: “${ev.query}”` : "no search needed");
      break;
    case "searching":
      t.events.push(ev.found_by === "tool" ? `tool search: “${ev.query}”` : `search: “${ev.query}”`);
      break;
    case "sources":
      // Incremental: append only what's new (dedupe by number, since a tool
      // round can re-surface a source the auto-search already found).
      for (const s of ev.sources) {
        if (!t.sources.some((existing) => existing.number === s.number)) t.sources.push(s);
      }
      break;
    case "listing":
      t.events.push(ev.document ? `looking up document: “${ev.document}”` : "listing the knowledge base");
      break;
    case "listing_libraries":
      t.events.push("listing libraries");
      break;
    case "thinking":
      t.thinking += ev.text;
      break;
    case "token":
      t.answer += ev.text;
      break;
    case "done":
      t.answer = ev.answer;
      t.model = ev.model;
      t.preset = ev.preset;
      t.searchQueries = ev.search_queries;
      // done.sources is cumulative and authoritative -- replace, don't merge.
      t.sources = ev.sources;
      t.stats = ev.stats;
      t.status = "complete";
      if (ev.stats.total_s !== undefined) {
        const parts = [`${ev.stats.total_s.toFixed(1)}s total`];
        if (ev.stats.first_token_s !== null) parts.push(`first token ${ev.stats.first_token_s.toFixed(1)}s`);
        if (ev.stats.tool_rounds) parts.push(`${ev.stats.tool_rounds} tool round(s)`);
        t.events.push(parts.join(", "));
      }
      break;
    case "title":
      // Handled by the caller (send()) via loadConversations(); nothing to
      // do on the turn itself.
      break;
    case "error":
      t.status = "error";
      t.error = ev.error;
      t.events.push(`error: ${ev.error}`);
      break;
  }
}

class ChatState {
  conversations = $state<ConversationListItem[]>([]);
  activeId = $state<string | null>(null);
  turns = $state<Turn[]>([]);
  preset = $state<Preset>("everyday");
  libraryIds = $state<string[]>([]);
  draft = $state("");
  inFlight = $state<AbortController | null>(null);
  busy = $derived(this.inFlight !== null);

  private turnSeq = 0;
  private openToken = 0; // guards against a stale in-flight open() clobbering a newer one

  async loadConversations(): Promise<void> {
    this.conversations = await get<ConversationListItem[]>("/api/conversations");
  }

  /** Called by Chat.svelte's $effect whenever the route's :id changes,
   *  including to null for "/" (new chat). */
  async open(id: string | null): Promise<void> {
    this.abortActive();
    const token = ++this.openToken;

    if (id === null) {
      this.activeId = null;
      this.turns = [];
      this.preset = "everyday";
      this.libraryIds = [];
      return;
    }

    this.activeId = id;
    let detail: ConversationDetail;
    try {
      detail = await get<ConversationDetail>(`/api/conversations/${id}`);
    } catch (e) {
      if (token === this.openToken) {
        ui.toast(e instanceof Error ? e.message : "couldn't open that conversation", "err");
        navigate("/", { replace: true });
      }
      return;
    }
    if (token !== this.openToken) return; // navigated away while this was in flight

    this.preset = detail.preset;
    this.libraryIds = detail.library_ids;
    this.turns = toTurns(detail.messages);
  }

  async setPreset(preset: Preset): Promise<void> {
    this.preset = preset;
    if (this.activeId) {
      await patch(`/api/conversations/${this.activeId}`, { preset });
      await this.loadConversations();
    }
  }

  async toggleLibrary(id: string): Promise<void> {
    this.libraryIds = this.libraryIds.includes(id)
      ? this.libraryIds.filter((x) => x !== id)
      : [...this.libraryIds, id];
    if (this.activeId) {
      await patch(`/api/conversations/${this.activeId}`, { library_ids: this.libraryIds });
    }
  }

  async deleteConversation(id: string): Promise<void> {
    await del(`/api/conversations/${id}`);
    if (this.activeId === id) navigate("/", { replace: true });
    await this.loadConversations();
  }

  abortActive(): void {
    this.inFlight?.abort();
    this.inFlight = null;
  }

  async send(content: string): Promise<void> {
    const trimmed = content.trim();
    if (!trimmed || this.busy) return;
    this.draft = "";

    let convId = this.activeId;
    if (!convId) {
      let created: ConversationBase;
      try {
        created = await post<ConversationBase>("/api/conversations", {
          preset: this.preset,
          library_ids: this.libraryIds.length ? this.libraryIds : null,
        });
      } catch (e) {
        this.draft = trimmed;
        ui.toast(e instanceof Error ? e.message : "couldn't start a new chat", "err");
        return;
      }
      convId = created.id;
      this.activeId = convId;
      this.openToken++; // this open() is already "current"; no further fetch needed
      // replaceState, not push: Back from a fresh new-chat shouldn't land on
      // an empty "/" that immediately re-creates another conversation.
      navigate(`/c/${convId}`, { replace: true });
      void this.loadConversations();
    }

    this.turns.push(newTurn(`pending:${++this.turnSeq}`, trimmed));
    // Read back through the reactive array: this is the live proxy, not the
    // plain object literal newTurn() returned. Svelte 5's $state deep-proxies
    // on write, so mutating the original reference wouldn't be tracked --
    // every further mutation in this function goes through `t`, never
    // through a separately-held plain object.
    const t = this.turns[this.turns.length - 1];

    const controller = new AbortController();
    this.inFlight = controller;

    let outcome;
    try {
      outcome = await runTurn(convId, { content: trimmed, preset: this.preset }, (ev) => applyEvent(t, ev), controller.signal);
    } catch (e) {
      // Pre-stream failure (401/403/404/422): the turn never started.
      this.turns = this.turns.filter((x) => x.key !== t.key);
      this.draft = trimmed;
      this.inFlight = null;
      ui.toast(e instanceof Error ? e.message : "the request failed", "err");
      return;
    }

    this.inFlight = null;
    void session.refreshQuota();

    switch (outcome.kind) {
      case "complete":
        void this.loadConversations(); // picks up the `title` SSE event's effect and updated_at
        break;
      case "rejected":
        this.turns = this.turns.filter((x) => x.key !== t.key);
        this.draft = trimmed;
        ui.toast(outcome.error, "err");
        break;
      case "failed":
        // Already marked status:"error" by applyEvent; keep the bubble.
        break;
      case "truncated":
      case "aborted":
        // The turn finished (or is finishing) server-side either way --
        // refetch rather than guess at what we missed.
        await this.open(convId);
        break;
    }
  }
}

export const chat = new ChatState();

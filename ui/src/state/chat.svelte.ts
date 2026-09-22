// Conversations as the centre column shows them, and the send / stop /
// retry lifecycle. Each opened conversation gets a cached ConvView, and a
// turn streams into its own view's Turn -- so switching to another chat
// mid-answer leaves the stream running (the list shows it pulsing), and
// switching back shows it still going rather than a refetched snapshot.

import { SvelteMap } from "svelte/reactivity";
import { get, patch, post } from "../lib/api";
import { runTurn, type TurnOutcome } from "../lib/stream";
import { applyEvent, documentMarks, newTurn, resetForRetry, toTurns, type Mark, type Turn } from "../lib/turns";
import type { ConversationBase, ConversationDetail, ConversationListItem, Preset } from "../lib/types";
import { navigate } from "./route.svelte";
import { session } from "./session.svelte";
import { ui } from "./ui.svelte";
import { workspace } from "./workspace.svelte";

export class ConvView {
  id: string;
  title = $state<string | null>(null);
  preset = $state<Preset>("everyday");
  workspaceId = $state<string | null>(null);
  turns = $state<Turn[]>([]);
  loaded = $state(false);

  constructor(id: string) {
    this.id = id;
  }

  fill(d: ConversationBase & { messages?: ConversationDetail["messages"] }): void {
    this.title = d.title;
    this.preset = d.preset;
    this.workspaceId = d.workspace_id;
    if (d.messages) {
      this.turns = toTurns(d.messages);
      this.loaded = true;
    }
  }
}

const NEW = "new";

class ChatState {
  views = new SvelteMap<string, ConvView>();
  activeId = $state<string | null>(null);
  /** Conversations with a turn streaming in this tab. */
  streaming = $state<string[]>([]);
  /** Composer text per conversation ("new" for the not-yet-created chat). */
  drafts = $state<Record<string, string>>({});
  newChatPreset = $state<Preset>("everyday");
  loadingId = $state<string | null>(null);

  active = $derived(this.activeId ? (this.views.get(this.activeId) ?? null) : null);
  activeStreaming = $derived(this.activeId !== null && this.streaming.includes(this.activeId));
  preset = $derived(this.active ? this.active.preset : this.newChatPreset);

  private controllers = new Map<string, AbortController>();
  private turnSeq = 0;
  private openSeq = 0;

  constructor() {
    session.onSignOut(() => {
      for (const c of this.controllers.values()) c.abort();
      this.controllers.clear();
      this.views.clear();
      this.activeId = null;
      this.streaming = [];
      this.drafts = {};
      this.newChatPreset = "everyday";
      this.loadingId = null;
      this.openSeq++; // an open() still in flight must not act on its result
      session.busy = false;
    });
  }

  /** The one way into the source viewer: on `handle`'s passage, with every
   *  passage of the same document this conversation referenced marked beside
   *  it -- so it reads the same from a citation, either panel tab, or the
   *  viewer's own prev/next. `replace` swaps the top popup instead of
   *  stacking another. */
  openSource(turnKey: string, handle: string, opts: { replace?: boolean } = {}): void {
    let marks: Mark[] | undefined;
    for (const view of this.views.values()) {
      const src = view.turns.find((t) => t.key === turnKey)?.sources.find((s) => s.handle === handle);
      if (!src) continue;
      marks = documentMarks(view.turns, src.doc_sha, src.chunk_index);
      break;
    }
    const m = { kind: "source", turnKey, handle, marks } as const;
    if (opts.replace) ui.replace(m);
    else ui.open(m);
  }

  get draft(): string {
    return this.drafts[this.activeId ?? NEW] ?? "";
  }

  set draft(v: string) {
    this.drafts[this.activeId ?? NEW] = v;
  }

  /** Driven by the route: "/" -> null, "/c/<id>" -> id. */
  async open(id: string | null): Promise<void> {
    const seq = ++this.openSeq;
    this.activeId = id;
    ui.selectedTurnKey = null;
    ui.focusedSources = [];
    if (id === null) return;

    let view = this.views.get(id);
    if (!view) {
      view = new ConvView(id);
      this.views.set(id, view);
    }
    // A view with a live stream is more current than the server's copy.
    if (this.streaming.includes(id)) return;

    this.loadingId = id;
    try {
      const detail = await get<ConversationDetail>(`/api/conversations/${id}`);
      if (this.streaming.includes(id)) return;
      view.fill(detail);
      if (seq === this.openSeq && detail.workspace_id && detail.workspace_id !== workspace.currentId) {
        await workspace.switchTo(detail.workspace_id);
      }
      workspace.patchConversation(id, { last_read_at: Date.now() / 1000 });
    } catch (e) {
      this.views.delete(id);
      if (seq === this.openSeq) {
        ui.toast(e instanceof Error ? e.message : "couldn't open that conversation", "err");
        navigate("/", { replace: true });
      }
    } finally {
      if (this.loadingId === id) this.loadingId = null;
    }
  }

  async setPreset(p: Preset): Promise<void> {
    const view = this.active;
    if (!view) {
      this.newChatPreset = p;
      return;
    }
    const before = view.preset;
    view.preset = p;
    try {
      await patch(`/api/conversations/${view.id}`, { preset: p });
    } catch (e) {
      view.preset = before;
      throw e;
    }
  }

  async send(content: string): Promise<void> {
    const text = content.trim();
    if (!text) return;
    let view = this.active;
    if (view && this.streaming.includes(view.id)) return;
    this.draft = "";

    if (!view) {
      let created: ConversationBase;
      try {
        created = await post<ConversationBase>("/api/conversations", {
          preset: this.newChatPreset,
          workspace_id: workspace.currentId,
        });
      } catch (e) {
        this.drafts[NEW] = text;
        ui.toast(e instanceof Error ? e.message : "couldn't start a new chat", "err");
        return;
      }
      view = new ConvView(created.id);
      view.fill(created);
      view.loaded = true;
      this.views.set(created.id, view);
      this.activeId = created.id;
      this.openSeq++; // this conversation is already current; no fetch needed
      navigate(`/c/${created.id}`, { replace: true });
      workspace.conversations.unshift({
        ...created, message_count: 0, last_message_at: created.created_at,
      } as ConversationListItem);
    }

    view.turns.push(newTurn(`pending:${++this.turnSeq}`, text));
    // Read back through the reactive array: that's the live proxy.
    const t = view.turns[view.turns.length - 1];
    ui.selectedTurnKey = null;
    await this.run(view, t, { content: text, preset: view.preset });
  }

  /** Regenerates the last answer in place. */
  async retry(): Promise<void> {
    const view = this.active;
    if (!view || this.streaming.includes(view.id)) return;
    const t = view.turns[view.turns.length - 1];
    if (!t) return;
    resetForRetry(t);
    await this.run(view, t, null);
  }

  async stop(): Promise<void> {
    const view = this.active;
    if (!view) return;
    try {
      await post(`/api/conversations/${view.id}/stop`);
    } catch (e) {
      ui.toast(e instanceof Error ? e.message : "couldn't stop it", "err");
    }
  }

  private async run(view: ConvView, t: Turn, body: { content: string; preset: string } | null): Promise<void> {
    const controller = new AbortController();
    this.controllers.set(view.id, controller);
    this.streaming.push(view.id);
    session.busy = true;

    let outcome: TurnOutcome;
    try {
      outcome = await runTurn(view.id, body, (ev) => {
        applyEvent(t, ev);
        if (ev.type === "title") {
          view.title = ev.title;
          workspace.patchConversation(view.id, { title: ev.title });
        }
      }, controller.signal);
    } catch (e) {
      // Before any stream existed (401/403/404/422): nothing was stored.
      this.finish(view.id);
      if (this.views.get(view.id) !== view) return; // signed out meanwhile
      if (body) {
        view.turns = view.turns.filter((x) => x.key !== t.key);
        this.drafts[view.id] = body.content;
      } else {
        await this.reload(view);
      }
      ui.toast(e instanceof Error ? e.message : "the request failed", "err");
      return;
    }
    this.finish(view.id);
    if (this.views.get(view.id) !== view) return; // signed out meanwhile

    switch (outcome.kind) {
      case "complete":
      case "failed":
        break;
      case "stopped":
        if (t.questionId === null && body) {
          // Stopped while queued: not even the question was stored.
          view.turns = view.turns.filter((x) => x.key !== t.key);
          this.drafts[view.id] ||= body.content;
        }
        break;
      case "rejected":
        if (body) {
          view.turns = view.turns.filter((x) => x.key !== t.key);
          this.drafts[view.id] = body.content;
        } else {
          await this.reload(view); // the old answer was deleted before the rejection
        }
        ui.toast(outcome.error, "err");
        break;
      case "truncated":
      case "aborted":
        // It finished (or is finishing) server-side either way.
        await this.reload(view);
        break;
    }
    void workspace.loadConversations();
    void session.refreshQuota();
  }

  private finish(id: string): void {
    this.controllers.delete(id);
    this.streaming = this.streaming.filter((x) => x !== id);
    session.busy = this.streaming.length > 0;
  }

  private async reload(view: ConvView): Promise<void> {
    try {
      view.fill(await get<ConversationDetail>(`/api/conversations/${view.id}`));
    } catch {
      // gone (deleted elsewhere); leave what we had
    }
  }

  /** Forget a view whose conversation was deleted/archived from here. */
  drop(id: string): void {
    this.controllers.get(id)?.abort();
    this.views.delete(id);
    if (this.activeId === id) navigate("/", { replace: true });
  }
}

export const chat = new ChatState();

// Workspaces, the current one, and its chats -- plus the Recently deleted
// and Archived trays. A chat in a workspace searches the workspace's
// libraries at each turn, so adding/removing a library here changes what
// every chat in it searches.

import { get, post, patch, del } from "../lib/api";
import type { ConversationListItem, Workspace } from "../lib/types";
import { library } from "./library.svelte";
import { session } from "./session.svelte";

const STORE_KEY = "rag.workspace";

function remembered(): string | null {
  try {
    return localStorage.getItem(STORE_KEY);
  } catch {
    return null;
  }
}

function remember(id: string): void {
  try {
    localStorage.setItem(STORE_KEY, id);
  } catch {
    // private window / blocked storage: just don't remember it
  }
}

export const ADMIN_WORKSPACE_ID = "admin-workspace";

class WorkspaceState {
  workspaces = $state<Workspace[]>([]);
  currentId = $state<string | null>(null);
  conversations = $state<ConversationListItem[]>([]);
  conversationsLoaded = $state(false);
  deletedConversations = $state<ConversationListItem[]>([]);
  archivedConversations = $state<ConversationListItem[]>([]);
  deletedWorkspaces = $state<Workspace[]>([]);

  current = $derived(this.workspaces.find((w) => w.id === this.currentId) ?? null);
  /** The current workspace's libraries that the caller can still read. */
  libraries = $derived(
    (this.current?.library_ids ?? []).map((id) => library.byId.get(id)).filter((l) => l !== undefined),
  );

  private listSeq = 0;

  constructor() {
    session.onSignOut(() => {
      this.workspaces = [];
      this.currentId = null;
      this.conversations = [];
      this.conversationsLoaded = false;
      this.deletedConversations = [];
      this.archivedConversations = [];
      this.deletedWorkspaces = [];
      this.listSeq++; // a list still loading belongs to the last user
    });
  }

  async load(): Promise<void> {
    const res = await get<{ workspaces: Workspace[] }>("/api/workspaces");
    this.workspaces = res.workspaces;
    if (!this.current) {
      const want = remembered();
      const pick = this.workspaces.find((w) => w.id === want) ?? this.workspaces[0] ?? null;
      this.currentId = pick?.id ?? null;
    }
    await this.loadConversations();
  }

  async switchTo(id: string): Promise<void> {
    if (id === this.currentId) return;
    this.currentId = id;
    remember(id);
    this.conversations = [];
    this.conversationsLoaded = false;
    await this.loadConversations();
  }

  async loadConversations(): Promise<void> {
    const id = this.currentId;
    const seq = ++this.listSeq;
    if (!id) {
      this.conversations = [];
      this.conversationsLoaded = true;
      return;
    }
    const list = await get<ConversationListItem[]>(`/api/workspaces/${id}/conversations`);
    if (seq !== this.listSeq) return; // switched workspace meanwhile
    this.conversations = list;
    this.conversationsLoaded = true;
  }

  async loadTrays(): Promise<void> {
    const [deleted, archived, ws] = await Promise.all([
      get<ConversationListItem[]>("/api/conversations?deleted=true"),
      get<ConversationListItem[]>("/api/conversations?archived=true"),
      get<{ workspaces: Workspace[] }>("/api/workspaces?deleted=true"),
    ]);
    this.deletedConversations = deleted;
    this.archivedConversations = archived;
    this.deletedWorkspaces = ws.workspaces;
  }

  /** Patches one list row in place (title from the `title` event, etc.). */
  patchConversation(id: string, fields: Partial<ConversationListItem>): void {
    const c = this.conversations.find((x) => x.id === id);
    if (c) Object.assign(c, fields);
  }

  async create(name: string, description: string | null = null): Promise<Workspace> {
    const ws = await post<Workspace>("/api/workspaces", { name, description });
    await this.load();
    await this.switchTo(ws.id);
    return ws;
  }

  async rename(id: string, name: string): Promise<void> {
    await patch(`/api/workspaces/${id}`, { name });
    await this.load();
  }

  async remove(id: string): Promise<void> {
    await del(`/api/workspaces/${id}`);
    const res = await get<{ workspaces: Workspace[] }>("/api/workspaces");
    this.workspaces = res.workspaces;
    if (this.currentId === id) {
      this.currentId = null;
      const next = this.workspaces[0];
      if (next) await this.switchTo(next.id);
      else this.conversations = [];
    }
    void this.loadTrays();
  }

  async restore(id: string): Promise<void> {
    await post(`/api/workspaces/${id}/restore`);
    await Promise.all([this.load(), this.loadTrays()]);
  }

  async setLibraries(ids: string[]): Promise<void> {
    const ws = this.current;
    if (!ws) return;
    const updated = await patch<Workspace>(`/api/workspaces/${ws.id}`, { library_ids: ids });
    const row = this.workspaces.find((w) => w.id === ws.id);
    if (row) row.library_ids = updated.library_ids;
  }

  addLibrary(id: string): Promise<void> {
    const ids = this.current?.library_ids ?? [];
    return ids.includes(id) ? Promise.resolve() : this.setLibraries([...ids, id]);
  }

  removeLibrary(id: string): Promise<void> {
    return this.setLibraries((this.current?.library_ids ?? []).filter((x) => x !== id));
  }

  // ------------------------------------------------------------ chats --

  async deleteConversation(id: string): Promise<void> {
    await del(`/api/conversations/${id}`);
    this.conversations = this.conversations.filter((c) => c.id !== id);
    void this.loadTrays();
  }

  async archiveConversation(id: string): Promise<void> {
    await post(`/api/conversations/${id}/archive`);
    this.conversations = this.conversations.filter((c) => c.id !== id);
    void this.loadTrays();
  }

  async unarchiveConversation(id: string): Promise<void> {
    await post(`/api/conversations/${id}/unarchive`);
    await Promise.all([this.loadConversations(), this.loadTrays()]);
  }

  async restoreConversation(id: string): Promise<void> {
    await post(`/api/conversations/${id}/restore`);
    await Promise.all([this.loadConversations(), this.loadTrays(), this.refreshCounts()]);
  }

  /** conversation_count on the switcher. */
  async refreshCounts(): Promise<void> {
    const res = await get<{ workspaces: Workspace[] }>("/api/workspaces");
    this.workspaces = res.workspaces;
  }

  workspaceName(id: string | null): string | null {
    if (!id) return null;
    return this.workspaces.find((w) => w.id === id)?.name ?? null;
  }
}

export const workspace = new WorkspaceState();

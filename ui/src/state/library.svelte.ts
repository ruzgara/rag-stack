// Libraries the caller can read, the ingest status, and uploads. The
// documents/ingest poll reschedules itself only while ingest is running or
// has a run pending.

import { get, post, patch, del, put, api } from "../lib/api";
import type {
  DocumentsResponse, Library, LibraryAccess, LibraryDetail, LibrarySource, UploadResponse,
} from "../lib/types";
import { session } from "./session.svelte";

/** The design's three dot colours: mine / shared with me / via a group. */
export type LibraryKind = "mine" | "shared" | "group";

export function libraryKind(source: LibrarySource | null): LibraryKind {
  if (source === "owner") return "mine";
  if (source === "group") return "group";
  return "shared"; // "user" (shared directly), and "admin" (visible as an admin)
}

export function kindColor(kind: LibraryKind): string {
  return `var(--kind-${kind})`;
}

export function accessLabel(lib: Pick<Library, "role" | "source">): string {
  switch (lib.source) {
    case "owner": return "Owner";
    case "admin": return "Admin access";
    case "group": return lib.role === "write" ? "Via group · can edit" : "Via group";
    default: return lib.role === "write" ? "Shared with you · can edit" : "Shared with you";
  }
}

export const canWrite = (lib: Pick<Library, "role"> | null | undefined): boolean =>
  lib?.role === "owner" || lib?.role === "write";
/** Owner or admin: rename, share, delete (Accounts.can_manage). */
export const canManage = (lib: Pick<Library, "source"> | null | undefined): boolean =>
  lib?.source === "owner" || lib?.source === "admin";

export const SUPPORTED = [".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp",
  ".docx", ".pptx", ".xlsx", ".html", ".htm", ".md", ".txt", ".asciidoc"];

class LibraryState {
  libraries = $state<Library[]>([]);
  loaded = $state(false);
  documents = $state<DocumentsResponse | null>(null);
  uploading = $state(false);

  byId = $derived(new Map(this.libraries.map((l) => [l.id, l])));
  totalDocuments = $derived(this.libraries.reduce((n, l) => n + l.document_count, 0));

  private pollTimer: ReturnType<typeof setTimeout> | null = null;

  constructor() {
    session.onSignOut(() => {
      if (this.pollTimer) clearTimeout(this.pollTimer);
      this.pollTimer = null;
      this.libraries = [];
      this.loaded = false;
      this.documents = null;
      this.uploading = false;
    });
  }

  async load(): Promise<void> {
    const res = await get<{ libraries: Library[] }>("/api/libraries");
    this.libraries = res.libraries;
    this.loaded = true;
  }

  async loadDocuments(): Promise<void> {
    this.documents = await get<DocumentsResponse>("/api/documents");
    this.schedulePoll();
  }

  private schedulePoll(): void {
    if (this.pollTimer) clearTimeout(this.pollTimer);
    this.pollTimer = null;
    const ingest = this.documents?.ingest;
    const waiting = (this.documents?.inbox.length ?? 0) > 0;
    if (!ingest || (!ingest.running && !ingest.pending && !waiting)) return;
    this.pollTimer = setTimeout(() => {
      void this.loadDocuments().then(() => this.load()).catch(() => {});
    }, ingest.running ? 3000 : 10000);
  }

  detail(id: string): Promise<LibraryDetail> {
    return get<LibraryDetail>(`/api/libraries/${id}`);
  }

  async create(name: string, description: string | null): Promise<Library> {
    const lib = await post<Library>("/api/libraries", { name, description });
    await this.load();
    return this.byId.get(lib.id) ?? lib;
  }

  async update(id: string, fields: { name?: string; description?: string }): Promise<void> {
    await patch(`/api/libraries/${id}`, fields);
    await this.load();
  }

  async remove(id: string): Promise<void> {
    await del(`/api/libraries/${id}`);
    await this.load();
  }

  async removeDocument(libraryId: string, sha: string): Promise<boolean> {
    const { purged } = await del<{ purged: boolean }>(`/api/libraries/${libraryId}/documents/${sha}`);
    await this.load();
    return purged;  // true when that was its last library, so the document itself went
  }

  access(id: string): Promise<LibraryAccess> {
    return get<LibraryAccess>(`/api/libraries/${id}/access`);
  }

  async share(id: string, subjectType: "user" | "group", subjectId: string, role: "read" | "write"): Promise<void> {
    await put(`/api/libraries/${id}/access`, { subject_type: subjectType, subject_id: subjectId, role });
  }

  async unshare(id: string, subjectType: "user" | "group", subjectId: string): Promise<void> {
    await del(`/api/libraries/${id}/access/${subjectType}/${subjectId}`);
  }

  /** Files the server would reject by type anyway are filtered out first, so
   *  a folder import doesn't spend the upload quota on .DS_Store. */
  async upload(files: File[], libraryId: string): Promise<UploadResponse> {
    const form = new FormData();
    for (const f of files) form.append("files", f);
    form.append("library_id", libraryId);
    this.uploading = true;
    try {
      const res = await api<UploadResponse>("/api/upload", { method: "POST", body: form });
      await Promise.all([this.loadDocuments(), this.load(), session.refreshQuota()]);
      return res;
    } finally {
      this.uploading = false;
    }
  }
}

export const library = new LibraryState();

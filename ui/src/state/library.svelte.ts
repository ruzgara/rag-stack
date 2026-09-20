// Libraries, documents, and uploads. The 3s ingest poll is self-rescheduling
// only while ingest.running || ingest.pending, matching test-ui.html:230.

import { get, post, del, api } from "../lib/api";
import type { DocumentsResponse, Library, UploadResponse } from "../lib/types";
import { session } from "./session.svelte";

class LibraryState {
  libraries = $state<Library[]>([]);
  documents = $state<DocumentsResponse | null>(null);
  uploading = $state(false);

  private docPollTimer: ReturnType<typeof setTimeout> | null = null;

  async loadLibraries(): Promise<void> {
    const res = await get<{ libraries: Library[] }>("/api/libraries");
    this.libraries = res.libraries;
  }

  async loadDocuments(): Promise<void> {
    this.documents = await get<DocumentsResponse>("/api/documents");
    this.scheduleDocPoll();
  }

  private scheduleDocPoll(): void {
    if (this.docPollTimer) clearTimeout(this.docPollTimer);
    this.docPollTimer = null;
    const ingest = this.documents?.ingest;
    if (!ingest || (!ingest.running && !ingest.pending)) return;
    this.docPollTimer = setTimeout(() => {
      this.loadDocuments().catch(() => {});
    }, 3000);
  }

  stopDocPoll(): void {
    if (this.docPollTimer) clearTimeout(this.docPollTimer);
    this.docPollTimer = null;
  }

  async createLibrary(name: string, description: string | null): Promise<void> {
    await post("/api/admin/libraries", { name, description });
    await this.loadLibraries();
  }

  async deleteLibrary(id: string): Promise<void> {
    await del(`/api/admin/libraries/${id}`);
    await this.loadLibraries();
  }

  async upload(files: FileList, libraryId: string | null): Promise<UploadResponse> {
    const form = new FormData();
    for (const f of Array.from(files)) form.append("files", f);
    if (libraryId) form.append("library_id", libraryId);

    this.uploading = true;
    try {
      const res = await api<UploadResponse>("/api/upload", { method: "POST", body: form });
      await this.loadDocuments();
      await session.refreshQuota();
      return res;
    } finally {
      this.uploading = false;
    }
  }
}

export const library = new LibraryState();

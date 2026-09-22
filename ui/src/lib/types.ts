// The API contract for services/query, as api.py / db.py / accounts.py
// return it. No logic here.

export type Preset = "everyday" | "hard";
export type AuthMode = "password" | "proxy";

// ------------------------------------------------------------------ auth --

export interface PublicUser {
  id: string;
  email: string;
  display_name: string | null;
  is_admin: boolean;
  disabled: boolean;
  /** Group NAMES (accounts.py _row_to_user), not ids. */
  groups: string[];
  created_at: number;
  last_login_at: number | null;
}

/** api.py me(): what a caller may do is per library now (Library.role),
 *  so there is no permissions map -- is_admin is the only global fact. */
export interface Me {
  user: PublicUser;
  auth_mode: AuthMode;
}

export interface ApiToken {
  id: string;
  name: string;
  hint: string;
  created_at: number;
  last_used_at: number | null;
  expires_at: number | null;
}

export interface UserHit {
  id: string;
  name: string | null;
  email: string;
}

export interface Group {
  id: string;
  name: string;
  description: string | null;
  type: "open" | "locked";
  member_count: number;
}

// ------------------------------------------------------------- libraries --

export type LibraryRole = "owner" | "write" | "read";
/** Why the caller can see it: they own it, they're an admin, it was shared
 *  with them directly, or via one of their groups. */
export type LibrarySource = "owner" | "admin" | "user" | "group";

export interface Library {
  id: string;
  name: string;
  description: string | null;
  owner_id: string | null;
  owner_name: string | null;
  created_at: number;
  document_count: number;
  role: LibraryRole | null;
  source: LibrarySource | null;
}

export interface DocumentInfo {
  sha256: string;
  filename: string;
  status: string | null; // "processed" | "failed"
  route: string | null;
  pages: number | null;
  chars: number | null;
  error: string | null;
  processed_at: number | null;
}

export interface LibraryDetail extends Omit<Library, "source"> {
  documents: DocumentInfo[];
}

export interface AccessEntry {
  subject_type: "user" | "group";
  subject_id: string;
  name: string;
  role: "read" | "write";
}

export interface LibraryAccess {
  owner: { id: string; name: string | null } | null;
  access: AccessEntry[];
}

export interface IngestStatus {
  autorun: boolean;
  running: boolean;
  pending: boolean;
  last_run: { started_at: number; finished_at: number; exit_code: number } | null;
}

export interface DocumentsResponse {
  documents: DocumentInfo[];
  inbox: string[];
  ingest: IngestStatus;
}

export interface Chunk {
  chunk_index: number;
  headings: string[];
  pages: number[];
  text: string;
}

export interface ChunksResponse {
  sha256: string;
  filename: string;
  total: number;
  chunks: Chunk[];
}

export interface UploadRejection {
  filename: string | null;
  reason: string;
}

export type UploadQuota =
  | { unlimited: true; files_used: number; mb_used: number }
  | {
      unlimited: false;
      files_left: number;
      mb_left: number;
      files_per_window: number;
      mb_per_window: number;
      max_file_mb: number;
      window_hours: number;
    };

export interface UploadResponse {
  saved: string[];
  rejected: UploadRejection[];
  ingest: string;
  quota: UploadQuota;
}

export interface UploadRecord {
  id: string;
  filename: string;
  bytes: number;
  doc_sha: string;
  created_at: number;
  status: string; // "waiting" | "processed" | "failed" | "unknown"
}

export interface LoadStats {
  running: number;
  queued: number;
  max_concurrent: number;
  max_per_user: number;
}

export interface QuotaResponse {
  uploads: UploadQuota;
  load: LoadStats;
}

// ------------------------------------------------------------ workspaces --

export interface Workspace {
  id: string;
  name: string;
  description: string | null;
  created_at: number;
  updated_at: number;
  last_used_at: number;
  library_ids: string[];
  /** Only on the list endpoint. */
  conversation_count?: number;
}

// ------------------------------------------------------------- conversations --

export interface ConversationBase {
  id: string;
  title: string | null;
  preset: Preset;
  workspace_id: string | null;
  created_at: number;
  updated_at: number;
  last_read_at: number;
  last_message_at?: number;
  archived: number | boolean;
  library_ids: string[];
}

export interface ConversationListItem extends ConversationBase {
  message_count: number;
  last_message_at: number;
}

export interface ConversationDetail extends ConversationBase {
  messages: Message[];
}

export interface TurnStats {
  auto_search?: boolean;
  tool_rounds?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  first_token_s?: number | null;
  total_s?: number;
  source_tokens?: number;
  plan_s?: number;
  /** Only present, and only true, when the planner didn't answer. */
  plan_failed?: boolean;
  /** How many of search_queries the planner wrote; the rest are the tool's. */
  auto_queries?: number;
  search_s?: number;
  listed_documents?: number;
  context_used?: number;
  context_limit?: number | null;
  context_used_pct?: number | null;
}

/** chat.py Source. `cited` is absent on the incremental `sources` SSE event
 *  and present once `done` (or the stored copy) arrives. `handle` is the
 *  citation identifier the model actually cites by -- opaque, stable
 *  forever, never shown to the reader (see lib/citations.ts). `doc_number`/
 *  `chunk_number` are the reader-facing [doc:chunk], set only once this
 *  passage has actually been cited in the conversation. "earlier": carried
 *  forward from a previous turn (db.citation_state), not retrieved this one
 *  -- see turns.ts's traceFromStored comment on why it's excluded there. */
export interface Source {
  number: number;
  found_by: "auto" | "tool" | "earlier";
  query: string;
  point_id: string;
  doc_sha: string;
  filename: string;
  chunk_index: number;
  pages: number[];
  headings: string[];
  text: string;
  rerank_score: number | null;
  handle: string;
  doc_number: number | null;
  chunk_number: number | null;
  cited?: boolean;
}

export type DoneSource = Source & { cited: boolean };

export type AnswerStatus = "complete" | "error" | "stopped";

export type Message =
  | { id: string; role: "user"; content: string; created_at: number }
  | {
      id: string;
      role: "assistant";
      content: string;
      created_at: number;
      model: string | null;
      preset: string | null;
      search_queries: string[];
      status: AnswerStatus;
      error: string | null;
      stats: TurnStats;
      sources: DoneSource[];
    };

// -------------------------------------------------------------------- sse --

export type TurnEvent =
  | { type: "queued"; position: number }
  | { type: "user"; id: string }
  | { type: "plan"; queries: string[]; search: boolean; failed?: boolean }
  | { type: "searching"; query: string; found_by: "auto" | "tool" }
  | { type: "sources"; sources: Source[] }
  | { type: "listing"; document: string }
  | { type: "listing_libraries" }
  | { type: "thinking"; text: string }
  /** A handle got its [doc:chunk] as its bracket closed, so the answer can
   *  show a real citation while it's still streaming. */
  | { type: "citation"; handle: string; doc: number; chunk: number }
  | { type: "token"; text: string }
  | {
      type: "done";
      message_id: string;
      answer: string;
      model: string;
      preset: string;
      search_queries: string[];
      cited: string[];
      refs: Record<string, [number, number]>;
      invalid_citations: string[];
      stats: TurnStats;
      /** US dollars for the turn's model calls; null when the provider has no
       *  price (Ollama). Live only -- not part of the stored message. */
      cost?: number | null;
      sources: DoneSource[];
    }
  | { type: "title"; title: string }
  | { type: "error"; error: string }
  /** Empty ({}) when stopped while still queued -- nothing was stored. */
  | { type: "stopped"; message_id?: string; answer?: string; cited?: string[];
      refs?: Record<string, [number, number]>; invalid_citations?: string[] };

// ----------------------------------------------------------------- health --

export interface HealthCheckOk { ok: true; [k: string]: unknown }
export interface HealthCheckErr { ok: false; error: string }
export type HealthCheck = HealthCheckOk | HealthCheckErr;

export interface PresetInfo { model: string; tools: boolean; think: boolean }

export interface HealthResponse {
  ok: boolean;
  ollama: HealthCheck;
  /** Present only with CHAT_PROVIDER=openrouter: chat models are served from there. */
  openrouter?: HealthCheck;
  qdrant: HealthCheck & { collection?: string; chunks?: number };
  database: HealthCheck;
  reranker?: { ok: true; model: string; device: string } | { ok: true; loading: true };
  load: { ok: true } & LoadStats;
  presets: { ok: true } & Record<string, PresetInfo | boolean>;
  auth?: { ok: true; mode: AuthMode };
}

// ------------------------------------------------------------------ errors --

export interface ValidationErrorItem {
  type: string;
  loc: (string | number)[];
  msg: string;
  input: unknown;
}

export type ApiErrorBody = { detail: string } | { detail: ValidationErrorItem[] };

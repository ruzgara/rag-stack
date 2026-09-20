// The verified API contract for services/query. No logic here -- see
// docs/plan §2 for the file:line citations each shape was checked against.

export type Preset = "everyday" | "hard";
export type AuthMode = "password" | "proxy";

// ------------------------------------------------------------------ auth --

export interface PublicUser {
  id: string;
  email: string;
  display_name: string | null;
  is_admin: boolean;
  disabled: boolean;
  groups: string[];
  created_at: number;
  last_login_at: number | null;
}

/** api.py:291. Keyed by permission name. Values are ["*"] for an admin, the
 *  LITERAL STRING "None" for a non-admin holding a global permission, or an
 *  array of library ids for library.read / library.write. See permissions.ts. */
export type Permissions = Record<string, string[]>;

export interface Me {
  user: PublicUser;
  permissions: Permissions;
  auth_mode: AuthMode;
}

// ------------------------------------------------------------- conversations --

/** Three distinct shapes. Do not collapse into one interface -- see plan §2. */
export interface ConversationBase {
  id: string;
  title: string | null;
  preset: Preset;
  created_at: number;
  updated_at: number;
  library_ids: string[];
}

export interface ConversationListItem extends ConversationBase {
  message_count: number;
}

export interface ConversationDetail extends ConversationBase {
  messages: Message[];
}

export interface TurnStats {
  auto_search: boolean;
  tool_rounds: number;
  prompt_tokens: number;
  completion_tokens: number;
  first_token_s: number | null;
  total_s: number;
  source_tokens: number;
  rewrite_s?: number;
  search_s?: number;
  listed_documents?: number;
}

/** chat.py:163-176, asdict(Source) -- 11 fields, no `cited` on the wire from
 *  the incremental `sources` SSE event. `cited` is declared optional here
 *  (rather than only on DoneSource) so a Turn's `sources` array can hold
 *  either shape without a cast: it starts as bare Source[] mid-stream and is
 *  wholesale-replaced by DoneSource[] once `done` arrives. */
export interface Source {
  number: number;
  found_by: "auto" | "tool";
  query: string;
  point_id: string;
  doc_sha: string;
  filename: string;
  chunk_index: number;
  pages: number[];
  headings: string[];
  text: string;
  rerank_score: number | null;
  cited?: boolean;
}

export type DoneSource = Source & { cited: boolean };
export type StoredSource = DoneSource;

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
      status: "complete" | "error";
      error: string | null;
      stats: TurnStats;
      sources: StoredSource[];
    };

// -------------------------------------------------------------------- sse --

export type TurnEvent =
  | { type: "queued"; position: number }
  | { type: "user"; id: string }
  | { type: "rewrite"; query: string; search: boolean }
  | { type: "searching"; query: string; found_by: "auto" | "tool" }
  | { type: "sources"; sources: Source[] }
  | { type: "listing"; document: string }
  | { type: "listing_libraries" }
  | { type: "thinking"; text: string }
  | { type: "token"; text: string }
  | {
      type: "done";
      message_id: string;
      answer: string;
      model: string;
      preset: string;
      search_queries: string[];
      cited: number[];
      invalid_citations: number[];
      stats: TurnStats;
      sources: DoneSource[];
    }
  | { type: "title"; title: string }
  | { type: "error"; error: string };

// ------------------------------------------------------------- libraries --

export interface Library {
  id: string;
  name: string;
  description: string | null;
  created_at: number;
  document_count: number;
}

export interface DocumentInfo {
  sha256: string;
  filename: string;
  status: string | null;
  route: string | null;
  pages: number | null;
  chars: number | null;
  error: string | null;
  processed_at: number | null;
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

// ----------------------------------------------------------------- health --

export interface HealthCheckOk { ok: true; [k: string]: unknown }
export interface HealthCheckErr { ok: false; error: string }
export type HealthCheck = HealthCheckOk | HealthCheckErr;

export interface HealthResponse {
  ok: boolean;
  ollama: HealthCheck;
  qdrant: HealthCheck & { collection?: string; chunks?: number };
  database: HealthCheck;
  reranker?: { ok: true; model: string; device: string } | { ok: true; loading: true };
  load: { ok: true } & LoadStats;
  presets: { ok: true; [preset: string]: unknown };
  /** Added alongside this UI -- see plan §8(d). Absent on an older server. */
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

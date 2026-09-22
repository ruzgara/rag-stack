// Same-origin fetch wrapper. No CORS middleware exists on the server (only
// SessionMiddleware, api.py:175), so this only ever talks same-origin (or to
// the dev proxy, which makes it look the same).

import type { ApiErrorBody } from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public retryAfter: number | null = null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

let onUnauthorized: (() => void) | null = null;

/** Registered by session.svelte.ts: a 401 from any call means the session
 *  ended (signed out elsewhere, password changed, account disabled). */
export function setUnauthorizedHandler(fn: () => void): void {
  onUnauthorized = fn;
}

/** 401 = not signed in. The caller (session.svelte.ts) reacts by showing the
 *  gate; this module has no dependency on Svelte or app state, so it just
 *  throws a typed error and lets state modules decide what a 401 means. */
export async function api<T = unknown>(path: string, init: RequestInit = {}): Promise<T> {
  const isFormData = init.body instanceof FormData;
  const headers: Record<string, string> = { ...(init.headers as Record<string, string>) };
  let body = init.body;

  if (body !== undefined && body !== null && !isFormData && typeof body !== "string") {
    headers["content-type"] = "application/json";
    body = JSON.stringify(body);
  } else if (!isFormData && init.method && init.method !== "GET" && body !== undefined) {
    headers["content-type"] = "application/json";
  }

  const res = await fetch(path, { ...init, headers, body: body as BodyInit | null | undefined });

  if (!res.ok) {
    if (res.status === 401 && !path.startsWith("/api/auth/")) onUnauthorized?.();
    throw await toApiError(res);
  }
  if (res.status === 204) return null as T;
  const text = await res.text();
  return (text ? JSON.parse(text) : null) as T;
}

export async function toApiError(res: Response): Promise<ApiError> {
  const retryAfter = res.headers.get("retry-after");
  let message = `${res.status} ${res.statusText}`;
  try {
    const raw = await res.text();
    const parsed = raw ? (JSON.parse(raw) as ApiErrorBody) : null;
    if (parsed && "detail" in parsed) {
      message = typeof parsed.detail === "string"
        ? parsed.detail
        : parsed.detail.map((d) => `${d.loc.join(".")}: ${d.msg}`).join("; ");
    }
  } catch {
    // body wasn't JSON -- keep the status-line message
  }
  return new ApiError(message, res.status, retryAfter ? Number(retryAfter) : null);
}

export const get = <T>(path: string) => api<T>(path);
export const post = <T>(path: string, body?: unknown) => api<T>(path, { method: "POST", body: body as never });
export const patch = <T>(path: string, body?: unknown) => api<T>(path, { method: "PATCH", body: body as never });
export const del = <T>(path: string) => api<T>(path, { method: "DELETE" });
export const put = <T>(path: string, body?: unknown) => api<T>(path, { method: "PUT", body: body as never });

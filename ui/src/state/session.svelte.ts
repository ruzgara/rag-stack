// Who's signed in, what they can do, and the two poll-driven pieces of
// server state (health, quota/load) that the whole shell reads from.

import { get, post, ApiError } from "../lib/api";
import type { AuthMode, HealthResponse, Me, QuotaResponse } from "../lib/types";
import { can, type GlobalPermission } from "../lib/permissions";

class Session {
  me = $state<Me | null>(null);
  booted = $state(false); // true once the first /api/auth/me attempt has resolved (either way)
  authMode = $state<AuthMode>("password"); // best guess before boot(); real value is on `me` once signed in
  health = $state<HealthResponse | null>(null);
  quota = $state<QuotaResponse | null>(null);
  loginError = $state<string | null>(null);
  loginRetryAfter = $state<number | null>(null);

  signedIn = $derived(this.me !== null);
  canChat = $derived(can(this.me, "chat.use"));
  canManageUsers = $derived(can(this.me, "users.manage"));
  canManageLibraries = $derived(can(this.me, "libraries.manage"));

  private healthTimer: ReturnType<typeof setInterval> | null = null;

  async boot(): Promise<void> {
    try {
      this.me = await get<Me>("/api/auth/me");
      this.authMode = this.me.auth_mode;
    } catch {
      this.me = null;
    } finally {
      this.booted = true;
    }
    // Learn auth_mode even when signed out, so the gate can hide the sign-in
    // form in proxy mode instead of offering a form that can never succeed.
    // /api/auth/me 401s exactly when we'd need this, so it comes from
    // /api/health instead (api.py health(): checks["auth"]).
    if (!this.me) {
      try {
        const health = await get<HealthResponse>("/api/health");
        if (health.auth) this.authMode = health.auth.mode;
      } catch {
        // Unreachable API -- HealthLine will report this; the gate just
        // keeps its default guess.
      }
    }
    this.startHealthPoll();
    if (this.signedIn) await this.refreshQuota();
  }

  async login(email: string, password: string): Promise<void> {
    this.loginError = null;
    this.loginRetryAfter = null;
    try {
      this.me = await post<Me>("/api/auth/login", { email, password });
      this.authMode = this.me.auth_mode;
      await this.refreshQuota();
    } catch (e) {
      if (e instanceof ApiError) {
        this.loginError = e.message;
        this.loginRetryAfter = e.retryAfter;
      } else {
        this.loginError = "sign-in failed";
      }
      throw e;
    }
  }

  async logout(): Promise<void> {
    try {
      await post("/api/auth/logout");
    } finally {
      this.me = null;
      this.quota = null;
    }
  }

  async changePassword(currentPassword: string, newPassword: string): Promise<void> {
    await post("/api/auth/password", { current_password: currentPassword, new_password: newPassword });
  }

  async refreshHealth(): Promise<void> {
    try {
      this.health = await get<HealthResponse>("/api/health");
    } catch {
      this.health = null;
    }
  }

  async refreshQuota(): Promise<void> {
    if (!this.signedIn) return;
    try {
      this.quota = await get<QuotaResponse>("/api/quota");
    } catch {
      // transient; LoadMeter just shows nothing this tick
    }
  }

  private startHealthPoll(): void {
    if (this.healthTimer) return;
    void this.refreshHealth();
    this.healthTimer = setInterval(() => void this.refreshHealth(), 15_000);
  }
}

export const session = new Session();

export function canPerm(perm: GlobalPermission): boolean {
  return can(session.me, perm);
}

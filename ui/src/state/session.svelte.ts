// Who's signed in, and the poll-driven server state the whole shell reads:
// health (models, index) and quota/load (the queue meter).

import { get, post, ApiError, setUnauthorizedHandler } from "../lib/api";
import type { AuthMode, HealthResponse, Me, PresetInfo, QuotaResponse } from "../lib/types";

class Session {
  me = $state<Me | null>(null);
  booted = $state(false);
  authMode = $state<AuthMode>("password");
  health = $state<HealthResponse | null>(null);
  quota = $state<QuotaResponse | null>(null);
  loginError = $state<string | null>(null);
  loginRetryAfter = $state<number | null>(null);
  /** Set by chat state while any turn streams, so the meter polls faster. */
  busy = $state(false);

  signedIn = $derived(this.me !== null);
  isAdmin = $derived(this.me?.user.is_admin ?? false);
  displayName = $derived(this.me ? this.me.user.display_name || this.me.user.email : "");

  private healthTimer: ReturnType<typeof setInterval> | null = null;
  private quotaTimer: ReturnType<typeof setTimeout> | null = null;

  constructor() {
    setUnauthorizedHandler(() => {
      this.me = null;
      this.quota = null;
    });
  }

  /** The model a preset runs, from /api/health. */
  presetInfo(name: string): PresetInfo | null {
    const v = this.health?.presets?.[name];
    return v && typeof v === "object" ? (v as PresetInfo) : null;
  }

  async boot(): Promise<void> {
    try {
      this.me = await get<Me>("/api/auth/me");
      this.authMode = this.me.auth_mode;
    } catch {
      this.me = null;
    } finally {
      this.booted = true;
    }
    this.startPolls();
  }

  async refreshMe(): Promise<void> {
    this.me = await get<Me>("/api/auth/me");
  }

  async login(email: string, password: string): Promise<void> {
    this.loginError = null;
    this.loginRetryAfter = null;
    try {
      this.me = await post<Me>("/api/auth/login", { email, password });
      this.authMode = this.me.auth_mode;
      void this.refreshQuota();
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
    } catch (e) {
      // 503 still carries the checks; anything else means unreachable.
      this.health = null;
      void e;
      try {
        const res = await fetch("/api/health");
        this.health = (await res.json()) as HealthResponse;
      } catch {
        this.health = null;
      }
    }
    if (this.health?.auth) this.authMode = this.health.auth.mode;
  }

  async refreshQuota(): Promise<void> {
    if (!this.signedIn) return;
    try {
      this.quota = await get<QuotaResponse>("/api/quota");
    } catch {
      // transient; the meter shows nothing this tick
    }
  }

  private startPolls(): void {
    if (this.healthTimer) return;
    void this.refreshHealth();
    this.healthTimer = setInterval(() => void this.refreshHealth(), 30_000);
    const tick = async () => {
      if (document.visibilityState === "visible") await this.refreshQuota();
      this.quotaTimer = setTimeout(tick, this.busy ? 3_000 : 15_000);
    };
    void tick();
  }
}

export const session = new Session();

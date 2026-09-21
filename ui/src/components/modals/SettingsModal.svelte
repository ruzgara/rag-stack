<script lang="ts">
  // No mockup for this one -- account/tokens/uploads/system drawn as
  // table-y sections in the same language as the Library popups, switched
  // by a .seg in the header (design/RAG Client.dc.html's popups all use
  // kicker + serif heading + .table).
  import ModalShell from "../ui/ModalShell.svelte";
  import { ui, guard } from "../../state/ui.svelte";
  import { session } from "../../state/session.svelte";
  import { library } from "../../state/library.svelte";
  import { get, post, del } from "../../lib/api";
  import { when, relTime, byteSize } from "../../lib/format";
  import type { ApiToken, UploadRecord, PresetInfo } from "../../lib/types";

  type Section = "account" | "tokens" | "uploads" | "system";
  let section = $state<Section>("account");

  const me = $derived(session.me?.user ?? null);
  const modalTitle = $derived(me?.display_name || me?.email || "Settings");

  // ------------------------------------------------------------- account --
  let curPassword = $state("");
  let newPassword = $state("");
  let confirmPassword = $state("");
  let changingPassword = $state(false);

  const changePassword = guard(async () => {
    if (newPassword.length < 8) {
      ui.toast("New password must be at least 8 characters", "err");
      return;
    }
    if (newPassword !== confirmPassword) {
      ui.toast("New password and confirmation don't match", "err");
      return;
    }
    changingPassword = true;
    try {
      await session.changePassword(curPassword, newPassword);
      curPassword = "";
      newPassword = "";
      confirmPassword = "";
      ui.toast("Password changed -- other sessions were signed out");
    } finally {
      changingPassword = false;
    }
  });

  // ------------------------------------------------------------- tokens --
  let tokens = $state<ApiToken[]>([]);
  let tokensLoaded = $state(false);
  let tokenName = $state("");
  let tokenExpiry = $state("never");
  let newToken = $state<string | null>(null);
  let creatingToken = $state(false);

  async function loadTokens(): Promise<void> {
    const res = await get<{ tokens: ApiToken[] }>("/api/tokens");
    tokens = res.tokens;
    tokensLoaded = true;
  }

  const createToken = guard(async () => {
    if (!tokenName.trim()) {
      ui.toast("Give the token a name", "err");
      return;
    }
    creatingToken = true;
    try {
      const expires_in_days = tokenExpiry === "never" ? null : Number(tokenExpiry);
      const res = await post<ApiToken & { token: string }>("/api/tokens", { name: tokenName.trim(), expires_in_days });
      newToken = res.token;
      tokenName = "";
      tokenExpiry = "never";
      await loadTokens();
    } finally {
      creatingToken = false;
    }
  });

  const revokeToken = guard(async (t: ApiToken) => {
    const ok = await ui.confirm({
      kicker: "API tokens",
      title: `Revoke "${t.name}"?`,
      body: "Anything using this token stops working immediately.",
      okLabel: "Revoke",
      danger: true,
    });
    if (!ok) return;
    await del(`/api/tokens/${t.id}`);
    await loadTokens();
    ui.toast("Token revoked");
  });

  async function copyToken(): Promise<void> {
    if (!newToken) return;
    try {
      await navigator.clipboard.writeText(newToken);
      ui.toast("Copied");
    } catch {
      ui.toast("Couldn't copy -- select and copy manually", "err");
    }
  }

  // ------------------------------------------------------------ uploads --
  let uploads = $state<UploadRecord[]>([]);
  let uploadsLoaded = $state(false);

  async function loadUploads(): Promise<void> {
    const res = await get<{ uploads: UploadRecord[] }>("/api/uploads");
    uploads = res.uploads;
    uploadsLoaded = true;
  }

  function statusLabel(status: string): { text: string; cls: string } {
    if (status === "waiting") return { text: "Waiting for ingest", cls: "st-waiting" };
    if (status === "processed") return { text: "Indexed", cls: "st-ok" };
    if (status === "failed") return { text: "Failed", cls: "st-err" };
    return { text: status, cls: "" };
  }

  const quotaLine = $derived.by(() => {
    const u = session.quota?.uploads;
    if (!u) return null;
    if (u.unlimited) return `Unlimited uploads -- ${u.files_used} files · ${u.mb_used.toFixed(1)} MB used`;
    return `${u.files_left} files · ${u.mb_left.toFixed(1)} MB left of ${u.files_per_window} files / ${u.mb_per_window} MB per ${u.window_hours}h window, max ${u.max_file_mb} MB per file`;
  });

  const deleteUpload = guard(async (u: UploadRecord) => {
    const ok = await ui.confirm({
      kicker: "Uploads",
      title: `Delete "${u.filename}"?`,
      body: "Removes the document from every library, the index and disk -- unless someone else uploaded the same file, in which case only your upload and its place in your own libraries go.",
      okLabel: "Delete",
      danger: true,
    });
    if (!ok) return;
    const res = await del<{ purged: boolean }>(`/api/uploads/${u.id}`);
    ui.toast(res.purged ? "Deleted everywhere" : "Removed your upload");
    await Promise.all([loadUploads(), library.load()]);
  });

  // ------------------------------------------------------------- system --
  const presetEntries = $derived.by(() => {
    const presets = session.health?.presets;
    if (!presets) return [];
    return Object.entries(presets).filter(([k]) => k !== "ok") as [string, PresetInfo][];
  });

  const refreshHealth = guard(async () => {
    await session.refreshHealth();
  });

  $effect(() => {
    if (section === "tokens" && !tokensLoaded) void loadTokens();
    if (section === "uploads" && !uploadsLoaded) void loadUploads();
  });
</script>

<ModalShell width={760} kicker="Settings" title={modalTitle}>
  {#snippet headerExtra()}
    <div class="seg">
      <button type="button" aria-pressed={section === "account"} onclick={() => (section = "account")}>Account</button>
      <button type="button" aria-pressed={section === "tokens"} onclick={() => (section = "tokens")}>API tokens</button>
      <button type="button" aria-pressed={section === "uploads"} onclick={() => (section = "uploads")}>Uploads</button>
      <button type="button" aria-pressed={section === "system"} onclick={() => (section = "system")}>System</button>
    </div>
  {/snippet}

  {#snippet children()}
    {#if section === "account" && me}
      <section>
        <h6>Account</h6>
        <div class="kv">
          <div class="kv-row"><span class="kv-k muted">Email</span><span>{me.email}</span></div>
          <div class="kv-row"><span class="kv-k muted">Display name</span><span>{me.display_name || "--"}</span></div>
          <div class="kv-row">
            <span class="kv-k muted">Role</span>
            <span class="tag" class:tag-accent={me.is_admin} class:tag-neutral={!me.is_admin}>
              {me.is_admin ? "Admin" : "Member"}
            </span>
          </div>
          <div class="kv-row">
            <span class="kv-k muted">Groups</span>
            <span class="tag-row">
              {#if me.groups.length === 0}
                <span class="muted">none</span>
              {:else}
                {#each me.groups as g (g)}<span class="tag tag-neutral">{g}</span>{/each}
              {/if}
            </span>
          </div>
          <div class="kv-row"><span class="kv-k muted">Joined</span><span>{when(me.created_at)}</span></div>
          <div class="kv-row">
            <span class="kv-k muted">Last sign-in</span>
            <span>{me.last_login_at ? when(me.last_login_at) : "never"}</span>
          </div>
        </div>
      </section>

      {#if session.authMode === "password"}
        <section>
          <h6>Change password</h6>
          <form class="pw-form" onsubmit={(e) => { e.preventDefault(); changePassword(); }}>
            <div class="field">
              <label for="cur-pw">Current password</label>
              <input id="cur-pw" class="input" type="password" autocomplete="current-password" bind:value={curPassword} required />
            </div>
            <div class="field">
              <label for="new-pw">New password</label>
              <input id="new-pw" class="input" type="password" autocomplete="new-password" bind:value={newPassword} required minlength="8" />
            </div>
            <div class="field">
              <label for="confirm-pw">Confirm new password</label>
              <input id="confirm-pw" class="input" type="password" autocomplete="new-password" bind:value={confirmPassword} required minlength="8" />
            </div>
            <button type="submit" class="btn btn-primary" disabled={changingPassword}>Change password</button>
          </form>
        </section>
      {:else}
        <section>
          <h6>Sign-in</h6>
          <p class="muted" style="margin:0">Sign-in is handled by the proxy in front of this app.</p>
        </section>
      {/if}

      {#if me.is_admin}
        <section>
          <h6>Admin</h6>
          <p class="muted" style="margin:0 0 6px">Admin accounts are managed at the terminal (<code>ragctl user</code>).</p>
          <a href="/admin">Open the admin console</a>
        </section>
      {/if}
    {:else if section === "tokens"}
      <section>
        <h6>API tokens</h6>
        {#if newToken}
          <div class="token-box">
            <div class="token-value">{newToken}</div>
            <button type="button" class="btn btn-secondary btn-sm" onclick={copyToken}>Copy</button>
            <button type="button" class="btn btn-ghost btn-sm" onclick={() => (newToken = null)}>Dismiss</button>
            <p class="muted token-warn">This won't be shown again -- copy it now.</p>
          </div>
        {/if}
        {#if !tokensLoaded}
          <div class="empty-note">Loading…</div>
        {:else if tokens.length === 0}
          <div class="empty-note">No API tokens yet.</div>
        {:else}
          <div class="table-wrap rag-scroll">
            <table class="table">
              <thead>
                <tr><th>Name</th><th>Token</th><th>Created</th><th>Last used</th><th>Expires</th><th></th></tr>
              </thead>
              <tbody>
                {#each tokens as t (t.id)}
                  <tr>
                    <td>{t.name}</td>
                    <td class="mono">{t.hint}…</td>
                    <td>{when(t.created_at)}</td>
                    <td>{t.last_used_at ? relTime(t.last_used_at) : "never"}</td>
                    <td>{t.expires_at ? when(t.expires_at) : "never"}</td>
                    <td><button type="button" class="btn btn-ghost btn-danger btn-sm" onclick={() => revokeToken(t)}>Revoke</button></td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
      </section>
      <section>
        <h6>New token</h6>
        <form class="token-form" onsubmit={(e) => { e.preventDefault(); createToken(); }}>
          <div class="field token-name">
            <label for="tok-name">Name</label>
            <input id="tok-name" class="input" bind:value={tokenName} placeholder="e.g. laptop script" required />
          </div>
          <div class="field">
            <label for="tok-expiry">Expires</label>
            <select id="tok-expiry" class="input" bind:value={tokenExpiry}>
              <option value="never">Never</option>
              <option value="30">30 days</option>
              <option value="90">90 days</option>
              <option value="365">365 days</option>
            </select>
          </div>
          <button type="submit" class="btn btn-primary" disabled={creatingToken}>Create token</button>
        </form>
        <p class="muted" style="margin-top:10px">
          <code>Authorization: Bearer rsk_…</code> works for <code>/api</code> and the OpenAI-compatible <code>/v1</code>.
        </p>
      </section>
    {:else if section === "uploads"}
      <section>
        <h6>Uploads</h6>
        {#if quotaLine}<p class="muted" style="margin:0 0 10px">{quotaLine}</p>{/if}
        {#if !uploadsLoaded}
          <div class="empty-note">Loading…</div>
        {:else if uploads.length === 0}
          <div class="empty-note">No uploads yet.</div>
        {:else}
          <div class="table-wrap rag-scroll">
            <table class="table">
              <thead><tr><th>File</th><th>Size</th><th>Uploaded</th><th>Status</th><th></th></tr></thead>
              <tbody>
                {#each uploads as u (u.id)}
                  {@const st = statusLabel(u.status)}
                  <tr>
                    <td class="ellipsis" style="max-width:260px">{u.filename}</td>
                    <td class="nowrap">{byteSize(u.bytes)}</td>
                    <td class="nowrap">{when(u.created_at)}</td>
                    <td class="nowrap {st.cls}">{st.text}</td>
                    <td><button type="button" class="btn btn-sm btn-ghost btn-danger" onclick={() => deleteUpload(u)}>Delete</button></td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
      </section>
    {:else if section === "system"}
      <section>
        <h6>System</h6>
        {#if !session.health}
          <div class="empty-note">Health unavailable.</div>
        {:else}
          <div class="sys-rows">
            <div class="sys-row">
              <span class="dot" class:ok={session.health.ollama.ok} class:err={!session.health.ollama.ok}></span>
              <span class="sys-label">Ollama</span>
              {#if !session.health.ollama.ok}<span class="err-text sys-detail">{session.health.ollama.error}</span>{/if}
            </div>
            <div class="sys-row">
              <span class="dot" class:ok={session.health.qdrant.ok} class:err={!session.health.qdrant.ok}></span>
              <span class="sys-label">Qdrant</span>
              {#if session.health.qdrant.ok}
                <span class="sys-detail muted">{session.health.qdrant.chunks ?? 0} chunks</span>
              {:else}
                <span class="err-text sys-detail">{session.health.qdrant.error}</span>
              {/if}
            </div>
            <div class="sys-row">
              <span class="dot" class:ok={session.health.database.ok} class:err={!session.health.database.ok}></span>
              <span class="sys-label">Database</span>
              {#if !session.health.database.ok}<span class="err-text sys-detail">{session.health.database.error}</span>{/if}
            </div>
            <div class="sys-row">
              <span class="dot" class:ok={!!session.health.reranker?.ok} class:err={!session.health.reranker}></span>
              <span class="sys-label">Reranker</span>
              {#if session.health.reranker && "loading" in session.health.reranker}
                <span class="sys-detail muted">loading…</span>
              {:else if session.health.reranker}
                <span class="sys-detail muted">{session.health.reranker.model} · {session.health.reranker.device}</span>
              {:else}
                <span class="sys-detail muted">off</span>
              {/if}
            </div>
            <div class="sys-row">
              <span class="dot ok"></span>
              <span class="sys-label">Queue</span>
              <span class="sys-detail muted">
                {session.health.load.running} running · {session.health.load.queued} queued, max {session.health.load.max_concurrent} at once, {session.health.load.max_per_user} per person
              </span>
            </div>
          </div>
        {/if}
        <button type="button" class="btn btn-secondary btn-sm" style="margin-top:10px" onclick={refreshHealth}>Refresh</button>
      </section>
      {#if presetEntries.length}
        <section>
          <h6>Presets</h6>
          <table class="table">
            <thead><tr><th>Preset</th><th>Model</th><th>Tools</th><th>Thinking</th></tr></thead>
            <tbody>
              {#each presetEntries as [name, p] (name)}
                <tr>
                  <td>{name}</td>
                  <td class="mono">{p.model}</td>
                  <td>{p.tools ? "yes" : "no"}</td>
                  <td>{p.think ? "yes" : "no"}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </section>
      {/if}
    {/if}
  {/snippet}

  {#snippet footer()}
    <button type="button" class="btn btn-ghost" style="margin-left:auto" onclick={() => ui.closeAll()}>Close</button>
  {/snippet}
</ModalShell>

<style>
  section + section { margin-top: 22px; }
  section h6 {
    margin: 0 0 10px; font-size: 10px; letter-spacing: .14em; text-transform: uppercase;
    color: var(--color-neutral-700); font-weight: 400;
  }
  .kv { display: flex; flex-direction: column; gap: 8px; }
  .kv-row { display: flex; align-items: center; gap: 10px; font-size: 13.5px; }
  .kv-k { width: 120px; flex: none; font-size: 12px; }
  .tag-row { display: inline-flex; gap: 6px; flex-wrap: wrap; }

  .pw-form { display: flex; flex-direction: column; gap: 10px; max-width: 340px; }

  .token-box {
    background: var(--color-accent-100); border-radius: var(--radius-md); padding: 12px 14px; margin-bottom: 14px;
    display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  }
  .token-value {
    font-family: var(--font-mono); font-size: 13px; flex: 1 1 260px; word-break: break-all;
  }
  .token-warn { margin: 6px 0 0; width: 100%; font-size: 12px; }

  .table-wrap { overflow-x: auto; }
  .table { min-width: 480px; }
  .mono { font-family: var(--font-mono); font-size: 12.5px; }
  .st-waiting { color: var(--color-accent-700); }
  .st-ok { color: var(--color-text); }
  .st-err { color: var(--color-accent-2-700); }

  .token-form { display: flex; align-items: flex-end; gap: 10px; flex-wrap: wrap; }
  .token-name { flex: 1 1 200px; }

  .sys-rows { display: flex; flex-direction: column; gap: 8px; }
  .sys-row { display: flex; align-items: center; gap: 10px; font-size: 13.5px; }
  .sys-label { font-family: var(--font-heading); width: 90px; flex: none; }
  .sys-detail { font-size: 12.5px; }
  .dot.ok { background: var(--color-accent); }
  .dot.err { background: var(--color-accent-2); }

  @media (max-width: 560px) {
    .kv-row { flex-wrap: wrap; }
    .kv-k { width: 100%; }
  }
</style>

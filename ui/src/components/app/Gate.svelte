<script lang="ts">
  // Sign-in. The mockup has no design for it; this keeps to the same
  // editorial register -- a kicker, a serif headline, one card.
  import { session } from "../../state/session.svelte";

  let email = $state("");
  let password = $state("");
  let submitting = $state(false);
  let countdown = $state<number | null>(null);

  $effect(() => {
    if (countdown === null || countdown <= 0) return;
    const id = setTimeout(() => (countdown = countdown! > 1 ? countdown! - 1 : null), 1000);
    return () => clearTimeout(id);
  });

  async function submit(e: SubmitEvent): Promise<void> {
    e.preventDefault();
    submitting = true;
    try {
      await session.login(email.trim(), password);
      password = "";
    } catch {
      if (session.loginRetryAfter) countdown = session.loginRetryAfter;
    } finally {
      submitting = false;
    }
  }

  const health = $derived(session.health);
</script>

<div class="gate">
  <div class="intro">
    <div class="kicker">rag-stack</div>
    <h1>Your documents, answered.</h1>
    <p>A private research desk over your own libraries. Everything runs on this machine.</p>
  </div>

  <div class="card">
    {#if session.authMode === "proxy"}
      <h4>Sign-in happens upstream</h4>
      <p class="muted">This service trusts an authenticating proxy, and the request didn't arrive with a signed-in identity. Open it through the proxy's address instead.</p>
    {:else}
      <form onsubmit={submit}>
        <h4>Sign in</h4>
        <div class="field">
          <label for="email">Email</label>
          <input id="email" class="input" type="email" autocomplete="username" required bind:value={email} />
        </div>
        <div class="field">
          <label for="password">Password</label>
          <input id="password" class="input" type="password" autocomplete="current-password" required bind:value={password} />
        </div>
        {#if session.loginError}
          <p class="err-text" role="alert">{session.loginError}{countdown ? ` — try again in ${countdown}s` : ""}</p>
        {/if}
        <button class="btn btn-primary" type="submit" disabled={submitting || countdown !== null}>
          {submitting ? "Signing in…" : "Sign in"}
        </button>
        <p class="muted small">Accounts are made by an admin (<code>ragctl user add</code>).</p>
      </form>
    {/if}
  </div>

  <div class="status muted">
    {#if health === null}
      The query service isn't answering.
    {:else}
      <span class="dot" style:background={health.ok ? "var(--color-accent)" : "var(--color-accent-2-500)"}></span>
      {health.ok ? "All services up" : "Some services are down"}
    {/if}
  </div>
</div>

<style>
  .gate {
    min-height: 100vh; min-height: 100dvh; box-sizing: border-box;
    display: grid; grid-template-columns: minmax(0, 520px) minmax(0, 380px);
    justify-content: center; align-content: center; align-items: center;
    gap: clamp(24px, 6vw, 96px); padding: 40px clamp(20px, 8vw, 120px) 64px;
    position: relative;
  }
  h1 { font-size: clamp(34px, 5vw, 52px); line-height: 1.04; margin: 0 0 16px; max-width: 12ch; }
  .intro p { font-size: 17px; line-height: 1.6; color: var(--color-neutral-800); max-width: 38ch; }
  .card { background: var(--color-surface); border-radius: var(--radius-lg); box-shadow: var(--shadow-sm); padding: 24px 24px 20px; }
  h4 { font-size: 20px; margin: 0 0 16px; }
  form { display: flex; flex-direction: column; gap: 12px; }
  form .btn { margin-top: 4px; }
  .small { font-size: 12px; margin: 4px 0 0; }
  .status { position: absolute; left: 0; right: 0; bottom: 20px; font-size: 12px; display: flex; gap: 7px; align-items: center; justify-content: center; }
  @media (max-width: 760px) {
    .gate { grid-template-columns: minmax(0, 380px); }
  }
  @media (max-width: 760px), (max-height: 640px) {
    .status { position: static; grid-column: 1 / -1; }
  }
</style>

<script lang="ts">
  import { session } from "../state/session.svelte";
  import { guard } from "../state/ui.svelte";

  let email = $state("");
  let password = $state("");
  let submitting = $state(false);
  let countdown = $state<number | null>(null);
  let countdownTimer: ReturnType<typeof setInterval> | null = null;

  function startCountdown(seconds: number): void {
    countdown = seconds;
    if (countdownTimer) clearInterval(countdownTimer);
    countdownTimer = setInterval(() => {
      if (countdown === null || countdown <= 1) {
        countdown = null;
        if (countdownTimer) clearInterval(countdownTimer);
        countdownTimer = null;
      } else {
        countdown -= 1;
      }
    }, 1000);
  }

  const submit = guard(async (e: SubmitEvent) => {
    e.preventDefault();
    submitting = true;
    try {
      await session.login(email, password);
      password = "";
    } catch {
      if (session.loginRetryAfter) startCountdown(session.loginRetryAfter);
    } finally {
      submitting = false;
    }
  });
</script>

<div id="gate">
  <h2>rag-stack</h2>

  {#if session.authMode === "proxy"}
    <p class="hint" style="margin-top: 12px;">
      This service authenticates through a proxy. Sign in there, then reload this page.
    </p>
  {:else}
    <form onsubmit={submit} class="row" style="flex-direction: column; align-items: stretch; margin-top: 16px; gap: 10px;">
      <label class="small" for="email">Email</label>
      <input id="email" type="email" autocomplete="username" bind:value={email} required />
      <label class="small" for="password">Password</label>
      <input id="password" type="password" autocomplete="current-password" bind:value={password} required />
      <button type="submit" class="primary" disabled={submitting || countdown !== null}>
        {countdown !== null ? `Try again in ${countdown}s` : submitting ? "Signing in…" : "Sign in"}
      </button>
      {#if session.loginError}
        <p class="note err">{session.loginError}</p>
      {/if}
    </form>
  {/if}
</div>

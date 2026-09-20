<script lang="ts">
  import { session } from "../state/session.svelte";
  import { guard, ui } from "../state/ui.svelte";

  let currentPassword = $state("");
  let newPassword = $state("");
  let confirmPassword = $state("");

  const submit = guard(async (e: SubmitEvent) => {
    e.preventDefault();
    if (newPassword !== confirmPassword) {
      ui.toast("New passwords don't match", "err");
      return;
    }
    await session.changePassword(currentPassword, newPassword);
    currentPassword = "";
    newPassword = "";
    confirmPassword = "";
    ui.toast("Password changed", "ok");
  });
</script>

<main class="chat" style="padding: 24px 20px; max-width: 480px;">
  <h2>Settings</h2>

  <h3>Account</h3>
  <p class="small">
    Signed in as {session.me?.user.email}
    {#if session.me?.user.groups.length}&middot; groups: {session.me.user.groups.join(", ")}{/if}
  </p>

  {#if session.authMode === "password"}
    <h3>Change password</h3>
    <form onsubmit={submit} class="row" style="flex-direction: column; align-items: stretch; gap: 10px;">
      <label class="small" for="current">Current password</label>
      <input id="current" type="password" autocomplete="current-password" bind:value={currentPassword} required />
      <label class="small" for="new">New password</label>
      <input id="new" type="password" autocomplete="new-password" bind:value={newPassword} required />
      <label class="small" for="confirm">Confirm new password</label>
      <input id="confirm" type="password" autocomplete="new-password" bind:value={confirmPassword} required />
      <div class="row"><button type="submit" class="primary">Change password</button></div>
    </form>
  {:else}
    <p class="hint">Password changes are handled by the identity proxy in this deployment.</p>
  {/if}

  <p style="margin-top: 24px;"><a href="/">&larr; Back to chat</a></p>
</main>

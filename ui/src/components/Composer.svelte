<script lang="ts">
  // Enter submits, Shift+Enter inserts a newline; disabled while a turn is
  // in flight (defect 6 fix -- test-ui.html silently swallowed a second
  // Enter with no feedback). Send becomes Stop while busy.
  import { chat } from "../state/chat.svelte";
  import { session } from "../state/session.svelte";
  import { guard } from "../state/ui.svelte";

  let textarea: HTMLTextAreaElement | undefined = $state();
  const disabled = $derived(chat.busy || !session.canChat);

  const submit = guard(async (e: SubmitEvent) => {
    e.preventDefault();
    if (chat.busy) return;
    const content = chat.draft;
    if (!content.trim()) return;
    await chat.send(content);
    textarea?.focus();
  });

  function onKeydown(e: KeyboardEvent): void {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      (e.currentTarget as HTMLTextAreaElement).form?.requestSubmit();
    }
  }

  function onStop(): void {
    chat.abortActive();
  }
</script>

<form id="ask" onsubmit={submit}>
  <textarea
    bind:this={textarea}
    bind:value={chat.draft}
    onkeydown={onKeydown}
    placeholder={session.canChat ? "Ask a question…" : "You don't have permission to chat"}
    disabled={disabled}
  ></textarea>
  {#if chat.busy}
    <button type="button" class="danger" onclick={onStop} title="The answer keeps being written server-side; reopen this chat to see it if you stop watching.">
      Stop
    </button>
  {:else}
    <button type="submit" class="primary" disabled={disabled || !chat.draft.trim()}>Send</button>
  {/if}
</form>

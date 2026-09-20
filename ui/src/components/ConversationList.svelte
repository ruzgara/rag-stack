<script lang="ts">
  import { chat } from "../state/chat.svelte";
  import { route } from "../router/routes.svelte";
  import { guard } from "../state/ui.svelte";

  const del = guard(async (e: MouseEvent, id: string) => {
    e.preventDefault();
    e.stopPropagation();
    if (!window.confirm("Delete this conversation? This can't be undone.")) return;
    await chat.deleteConversation(id);
  });

  function isActive(id: string): boolean {
    return route.name === "chat" && route.params.id === id;
  }
</script>

<div id="convs">
  {#if chat.conversations.length === 0}
    <p class="small">No conversations yet.</p>
  {/if}
  {#each chat.conversations as c (c.id)}
    <div class="conv-row" class:active={isActive(c.id)}>
      <a href="/c/{c.id}" class="conv">{c.title || "Untitled"} <span class="small">&middot; {c.preset}</span></a>
      <button type="button" class="del" title="Delete" onclick={(e) => del(e, c.id)}>&cross;</button>
    </div>
  {/each}
</div>

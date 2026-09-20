<script lang="ts">
  import { route } from "./routes.svelte";
  import Chat from "../routes/Chat.svelte";
  import Settings from "../routes/Settings.svelte";
</script>

{#if route.name === "chat"}
  <Chat id={route.params.id ?? null} />
{:else if route.name === "settings"}
  <Settings />
{:else}
  <div style="padding: 40px 20px; max-width: 480px; margin: 0 auto;">
    <h2>Not found</h2>
    <p class="hint">There's nothing at this address. <a href="/">Go back to your chats</a>.</p>
  </div>
{/if}

<script lang="ts">
  import { session } from "../state/session.svelte";
  import { chat } from "../state/chat.svelte";
  import { library } from "../state/library.svelte";
  import { guard } from "../state/ui.svelte";
  import { onMount } from "svelte";
  import ConversationList from "./ConversationList.svelte";
  import LibraryPicker from "./LibraryPicker.svelte";
  import Uploader from "./Uploader.svelte";
  import DocumentList from "./DocumentList.svelte";
  import HealthLine from "./HealthLine.svelte";

  onMount(() => {
    void chat.loadConversations();
    void library.loadLibraries();
  });

  const signOut = guard(async () => {
    await session.logout();
  });
</script>

<aside>
  <div class="small"><b>rag-stack</b></div>

  <div class="row">
    <a href="/" class="primary" style="display: inline-block; padding: 6px 8px; border-radius: var(--radius); text-decoration: none; background: var(--accent); color: #fff;">+ New chat</a>
  </div>

  <ConversationList />

  <LibraryPicker />

  <Uploader />

  <DocumentList />

  <h3>Account</h3>
  <div class="small">
    {session.me?.user.email}
    {#if session.me?.user.is_admin}<span class="tag admin">admin</span>{/if}
    <div class="small">{session.authMode} auth</div>
  </div>
  <div class="row">
    <a href="/admin" class="small">admin console</a>
    <a href="/settings" class="small">settings</a>
  </div>
  <div class="row">
    <button class="small" onclick={signOut}>Sign out</button>
  </div>

  <div class="row" style="margin-top: auto; padding-top: 8px; border-top: 1px solid var(--line);">
    <HealthLine />
  </div>
</aside>

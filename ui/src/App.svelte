<script lang="ts">
  import { onMount } from "svelte";
  import { session } from "./state/session.svelte";
  import { library } from "./state/library.svelte";
  import { workspace } from "./state/workspace.svelte";
  import { chat } from "./state/chat.svelte";
  import { ui } from "./state/ui.svelte";
  import { initRouter, route } from "./state/route.svelte";
  import Gate from "./components/app/Gate.svelte";
  import Rail from "./components/app/Rail.svelte";
  import ChatPane from "./components/app/ChatPane.svelte";
  import SourcesPanel from "./components/app/SourcesPanel.svelte";
  import ModalHost from "./components/ui/ModalHost.svelte";
  import DialogHost from "./components/ui/DialogHost.svelte";
  import Toasts from "./components/ui/Toasts.svelte";
  import ResizeHandle from "./components/ui/ResizeHandle.svelte";
  import { RAIL_WIDTH, SOURCES_WIDTH } from "./state/ui.svelte";

  onMount(() => {
    initRouter();
    void session.boot();
  });

  // Everything a signed-in shell needs, loaded once per sign-in.
  let loadedFor: string | null = null;
  $effect(() => {
    const id = session.me?.user.id ?? null;
    if (id === loadedFor) return;
    loadedFor = id;
    if (!id) {
      ui.closeAll();
      return;
    }
    const fail = (e: unknown) => ui.toast(e instanceof Error ? e.message : String(e), "err");
    void library.load().catch(fail);
    void library.loadDocuments().catch(() => {});
    void workspace.load().catch(fail);
    void workspace.loadTrays().catch(() => {});
  });

  // The route drives the open conversation.
  $effect(() => {
    if (!session.signedIn) return;
    const id = route.convId;
    if (id !== chat.activeId || (id && !chat.views.has(id))) void chat.open(id);
  });

  // Chat list freshness: other tabs/sessions finish turns too (unread dots).
  $effect(() => {
    if (!session.signedIn) return;
    const refresh = () => {
      if (document.visibilityState === "visible") void workspace.loadConversations().catch(() => {});
    };
    const id = setInterval(refresh, 20_000);
    addEventListener("focus", refresh);
    return () => {
      clearInterval(id);
      removeEventListener("focus", refresh);
    };
  });

  // Drag limits keep the chat column usable; they mirror the CSS caps below.
  const CHAT_MIN = 420;
  const sourcesShown = () => innerWidth > 1180;
  const railMax = () =>
    Math.min(560, innerWidth * 0.4, innerWidth - (sourcesShown() ? ui.sourcesWidth : 0) - CHAT_MIN);
  const sourcesMax = () => Math.min(720, innerWidth * 0.45, innerWidth - ui.railWidth - CHAT_MIN);

  const title = $derived(chat.active?.title ? `${chat.active.title} · rag-stack` : "rag-stack");
</script>

<svelte:head><title>{title}</title></svelte:head>

{#if !session.booted}
  <div class="boot">Loading…</div>
{:else if !session.signedIn}
  <Gate />
{:else}
  <div
    class="shell"
    class:rail-open={ui.railOpen}
    class:sources-open={ui.sourcesOpen}
    style:--rail-w="{ui.railWidth}px"
    style:--sources-w="{ui.sourcesWidth}px"
  >
    <div class="rail-slot">
      <Rail />
      <ResizeHandle
        bind:width={ui.railWidth} edge="right" min={200} max={railMax} reset={RAIL_WIDTH}
        label="Resize chat list" oncommit={() => ui.saveWidths()}
      />
    </div>
    {#if route.notFound}
      <main class="notfound">
        <div class="kicker">404</div>
        <h2>Nothing at this address.</h2>
        <p><a href="/">Back to your chats</a></p>
      </main>
    {:else}
      <ChatPane />
    {/if}
    <div class="sources-slot">
      <SourcesPanel />
      <ResizeHandle
        bind:width={ui.sourcesWidth} edge="left" min={260} max={sourcesMax} reset={SOURCES_WIDTH}
        label="Resize sources panel" oncommit={() => ui.saveWidths()}
      />
    </div>
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <div class="scrim" role="presentation" onclick={() => { ui.railOpen = false; ui.sourcesOpen = false; }}></div>
  </div>
  <ModalHost />
{/if}
<DialogHost />
<Toasts />

<style>
  .boot { padding: 40px; text-align: center; color: var(--color-neutral-600); }
  .shell {
    display: grid;
    grid-template-columns: min(var(--rail-w), 40vw) minmax(0, 1fr) min(var(--sources-w), 45vw);
    height: 100vh; height: 100dvh; overflow: hidden; position: relative;
  }
  .rail-slot, .sources-slot { min-height: 0; position: relative; }
  .notfound { padding: 60px clamp(20px, 6vw, 80px); }
  .scrim { display: none; }

  /* Below 1180px the sources panel becomes a drawer from the right. */
  @media (max-width: 1180px) {
    .shell { grid-template-columns: min(var(--rail-w), 40vw) minmax(0, 1fr); }
    .sources-slot :global(.handle) { display: none; }
    .sources-slot {
      position: fixed; top: 0; right: 0; bottom: 0; width: min(360px, 92vw); z-index: 50;
      transform: translateX(100%); transition: transform .2s ease; box-shadow: var(--shadow-lg);
    }
    .sources-open .sources-slot { transform: none; }
    .sources-open .scrim { display: block; }
  }
  /* Below 860px the rail does too, from the left. */
  @media (max-width: 860px) {
    .shell { grid-template-columns: minmax(0, 1fr); }
    .rail-slot :global(.handle) { display: none; }
    .rail-slot {
      position: fixed; top: 0; left: 0; bottom: 0; width: min(300px, 88vw); z-index: 50;
      transform: translateX(-100%); transition: transform .2s ease; box-shadow: var(--shadow-lg);
    }
    .rail-open .rail-slot { transform: none; }
    .rail-open .scrim { display: block; }
  }
  .scrim {
    position: fixed; inset: 0; z-index: 45;
    background: color-mix(in srgb, var(--color-neutral-900) 30%, transparent);
  }
  @media (prefers-reduced-motion: reduce) {
    .rail-slot, .sources-slot { transition: none; }
  }
</style>

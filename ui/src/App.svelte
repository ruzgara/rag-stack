<script lang="ts">
  import { onMount } from "svelte";
  import { session } from "./state/session.svelte";
  import { initRouter } from "./router/routes.svelte";
  import Router from "./router/Router.svelte";
  import Gate from "./components/Gate.svelte";
  import Sidebar from "./components/Sidebar.svelte";
  import SourcesPanel from "./components/SourcesPanel.svelte";
  import Toasts from "./components/Toasts.svelte";
  import Modal from "./components/Modal.svelte";

  onMount(() => {
    initRouter();
    void session.boot();
  });
</script>

<Toasts />
<Modal />

{#if !session.booted}
  <div style="padding: 40px 20px; text-align: center; color: var(--muted);">Loading&hellip;</div>
{:else if !session.signedIn}
  <Gate />
{:else}
  <div class="shell">
    <Sidebar />
    <Router />
    <SourcesPanel />
  </div>
{/if}

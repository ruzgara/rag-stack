<script lang="ts">
  import { untrack } from "svelte";
  import { chat } from "../state/chat.svelte";
  import { session } from "../state/session.svelte";
  import { guard } from "../state/ui.svelte";
  import Transcript from "../components/Transcript.svelte";
  import Composer from "../components/Composer.svelte";
  import LoadMeter from "../components/LoadMeter.svelte";
  import type { Preset } from "../lib/types";

  interface Props {
    id: string | null;
  }
  const { id }: Props = $props();

  // The URL is the source of truth for which conversation is open (plan
  // §4): this effect is the only place chat.open() is called from the route
  // side, driven purely by the :id param. `id` must be the ONLY tracked
  // read. chat.open()'s synchronous prefix (before its first await) calls
  // abortActive(), which reads chat.inFlight -- if that read happened inside
  // this effect's tracking window, then chat.send() setting inFlight to the
  // controller it just created would re-trigger this effect, which would
  // call open() -> abortActive() -> abort the turn that started a moment
  // ago. untrack() keeps the effect's dependency set to `id` alone.
  $effect(() => {
    const next = id;
    untrack(() => void chat.open(next));
  });

  const setPreset = guard(async (e: Event) => {
    await chat.setPreset((e.currentTarget as HTMLSelectElement).value as Preset);
  });
</script>

<main class="chat">
  <div class="row" style="padding: 10px 20px; border-bottom: 1px solid var(--line);">
    <select value={chat.preset} onchange={setPreset} disabled={chat.busy}>
      <option value="everyday">everyday</option>
      <option value="hard">hard</option>
    </select>
    <span class="sp" style="flex: 1;"></span>
    {#if session.canChat}
      <LoadMeter />
    {:else}
      <span class="small err-text">You don't have permission to chat. Ask an admin for chat.use.</span>
    {/if}
  </div>

  <Transcript turns={chat.turns} />
  <Composer />
</main>

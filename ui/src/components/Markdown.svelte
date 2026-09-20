<script lang="ts">
  // Binds a StreamingMarkdown instance to one <div>. Coalesces token events
  // onto requestAnimationFrame rather than rendering per token (plan §5
  // step 1); switches to the one-shot authoritative finalize() the moment
  // status leaves "streaming" -- which is immediately, for a reopened turn
  // that was never streamed in this session at all.
  import { onDestroy } from "svelte";
  import { StreamingMarkdown } from "../lib/render";

  interface Props {
    answer: string;
    status: "streaming" | "complete" | "error";
    known: Set<number>;
  }
  const { answer, status, known }: Props = $props();

  let container: HTMLDivElement | undefined = $state();
  let renderer: StreamingMarkdown | undefined;
  let rafId: number | null = null;
  let finalized = false;

  function scheduleUpdate(): void {
    if (rafId !== null) return;
    rafId = requestAnimationFrame(() => {
      rafId = null;
      if (!container) return;
      if (!renderer) renderer = new StreamingMarkdown(container);
      if (status === "streaming") {
        renderer.update(answer, known);
      } else if (!finalized) {
        finalized = true;
        renderer.finalize(answer, known);
      }
    });
  }

  $effect(() => {
    // Track every dependency this render pass needs -- referencing them
    // here (not just inside the rAF closure) is what registers them with
    // the effect.
    void answer;
    void status;
    void known;
    scheduleUpdate();
  });

  onDestroy(() => {
    if (rafId !== null) cancelAnimationFrame(rafId);
  });
</script>

<div class="body" bind:this={container}></div>

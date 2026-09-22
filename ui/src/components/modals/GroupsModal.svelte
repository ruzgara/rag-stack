<script lang="ts">
  // No mockup for this one -- drawn like the Library management popup's
  // rows (design/RAG Client.dc.html ~454-498): tag chips, hover tints.
  import ModalShell from "../ui/ModalShell.svelte";
  import { ui, guard } from "../../state/ui.svelte";
  import { session } from "../../state/session.svelte";
  import { library } from "../../state/library.svelte";
  import { get, post } from "../../lib/api";
  import type { Group } from "../../lib/types";

  let groups = $state<Group[]>([]);
  let loaded = $state(false);
  let busy = $state<string | null>(null);

  async function loadGroups(): Promise<void> {
    const res = await get<{ groups: Group[] }>("/api/groups");
    groups = res.groups;
    loaded = true;
  }

  $effect(() => {
    void loadGroups();
  });

  const memberNames = $derived(new Set(session.me?.user.groups ?? []));
  const memberOf = $derived(groups.filter((g) => memberNames.has(g.name)));
  const otherGroups = $derived(groups.filter((g) => !memberNames.has(g.name)));

  async function afterChange(msg: string): Promise<void> {
    await Promise.all([session.refreshMe(), library.load(), loadGroups()]);
    ui.toast(msg);
  }

  const join = guard(async (g: Group) => {
    busy = g.id;
    try {
      await post(`/api/groups/${g.id}/join`);
      await afterChange(`Joined ${g.name} -- library access may have changed.`);
    } finally {
      busy = null;
    }
  });

  const leave = guard(async (g: Group) => {
    const ok = await ui.confirm({
      kicker: "Groups",
      title: `Leave ${g.name}?`,
      body: "You'll lose access to any libraries shared only through this group.",
      okLabel: "Leave",
      danger: true,
    });
    if (!ok) return;
    busy = g.id;
    try {
      await post(`/api/groups/${g.id}/leave`);
      await afterChange(`Left ${g.name} -- library access may have changed.`);
    } finally {
      busy = null;
    }
  });
</script>

<ModalShell width={620} kicker="Groups" title="Your groups">
  {#snippet subtitle()}
    Libraries shared with a group are available to everyone in it.
  {/snippet}
  {#snippet children()}
    {#if !loaded}
      <div class="empty-note">Loading…</div>
    {:else}
      <section class="section">
        <h6>Member of</h6>
        {#if memberOf.length === 0}
          <div class="empty-note">You're not a member of any group.</div>
        {:else}
          <div class="rows">
            {#each memberOf as g (g.id)}
              <div class="row">
                <div class="row-main">
                  <div class="row-top">
                    <span class="row-name">{g.name}</span>
                    <span class="tag" class:tag-accent={g.type === "open"} class:tag-neutral={g.type === "locked"}>
                      {g.type === "open" ? "Open" : "Locked"}
                    </span>
                  </div>
                  {#if g.description}<div class="row-desc muted">{g.description}</div>{/if}
                  <div class="row-meta muted">{g.member_count} member{g.member_count === 1 ? "" : "s"}</div>
                </div>
                <button type="button" class="btn btn-secondary btn-sm" disabled={busy === g.id} onclick={() => leave(g)}>
                  Leave
                </button>
              </div>
            {/each}
          </div>
        {/if}
      </section>

      <section class="section">
        <h6>Other groups</h6>
        {#if otherGroups.length === 0}
          <div class="empty-note">No other groups.</div>
        {:else}
          <div class="rows">
            {#each otherGroups as g (g.id)}
              <div class="row">
                <div class="row-main">
                  <div class="row-top">
                    <span class="row-name">{g.name}</span>
                    <span class="tag" class:tag-accent={g.type === "open"} class:tag-neutral={g.type === "locked"}>
                      {g.type === "open" ? "Open" : "Locked"}
                    </span>
                  </div>
                  {#if g.description}<div class="row-desc muted">{g.description}</div>{/if}
                  <div class="row-meta muted">{g.member_count} member{g.member_count === 1 ? "" : "s"}</div>
                </div>
                {#if g.type === "open"}
                  <button type="button" class="btn btn-secondary btn-sm" disabled={busy === g.id} onclick={() => join(g)}>
                    Join
                  </button>
                {:else}
                  <span class="muted ask-admin">Ask an admin</span>
                {/if}
              </div>
            {/each}
          </div>
        {/if}
      </section>
    {/if}
  {/snippet}
  {#snippet footer()}
    <button type="button" class="btn btn-ghost" style="margin-left:auto" onclick={() => ui.closeAll()}>Close</button>
  {/snippet}
</ModalShell>

<style>
  .section + .section { margin-top: 20px; }
  .section h6 {
    margin: 0 0 6px; font-size: 10px; letter-spacing: .14em; text-transform: uppercase;
    color: var(--color-neutral-700); font-weight: 400;
  }
  .rows { display: flex; flex-direction: column; gap: 2px; margin: 0 -8px; }
  .row { display: flex; align-items: center; gap: 12px; padding: 9px 10px; border-radius: var(--radius-md); }
  .row:hover { background: color-mix(in srgb, var(--color-text) 4%, transparent); }
  .row-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px; }
  .row-top { display: flex; align-items: center; gap: 8px; }
  .row-name { font-family: var(--font-heading); font-size: 14.5px; }
  .row-desc { font-size: 12.5px; }
  .row-meta { font-size: 11.5px; }
  .ask-admin { font-size: 12.5px; flex: none; }
</style>

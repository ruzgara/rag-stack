<script lang="ts">
  // Who a library is shared with: the owner line, existing grants (each with
  // a role toggle and an unshare ×), and a combined user/group picker to add
  // more.
  import ModalShell from "../ui/ModalShell.svelte";
  import { ui, guard } from "../../state/ui.svelte";
  import { library } from "../../state/library.svelte";
  import { get } from "../../lib/api";
  import type { AccessEntry, Group, LibraryAccess, UserHit } from "../../lib/types";

  interface Props { id: string }
  const { id }: Props = $props();

  let access = $state<LibraryAccess | null>(null);
  let loading = $state(true);
  let error = $state<string | null>(null);
  let groupsAll = $state<Group[]>([]);

  let query = $state("");
  let userResults = $state<UserHit[]>([]);
  let newRole = $state<"read" | "write">("read");

  const libName = $derived(library.byId.get(id)?.name ?? "Library");

  async function loadAccess(): Promise<void> {
    loading = true;
    error = null;
    try {
      access = await library.access(id);
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    void id;
    void loadAccess();
  });

  $effect(() => {
    void get<{ groups: Group[] }>("/api/groups").then((r) => (groupsAll = r.groups)).catch(() => {});
  });

  let searchSeq = 0;
  $effect(() => {
    const q = query.trim();
    if (q.length < 1) {
      userResults = [];
      return;
    }
    const seq = ++searchSeq;
    const t = setTimeout(() => {
      void get<{ users: UserHit[] }>(`/api/users/search?q=${encodeURIComponent(q)}`)
        .then((r) => {
          if (seq === searchSeq) userResults = r.users;
        })
        .catch(() => {});
    }, 200);
    return () => clearTimeout(t);
  });

  const existingIds = $derived(new Set((access?.access ?? []).map((a) => `${a.subject_type}:${a.subject_id}`)));

  const groupSuggestions = $derived(
    query.trim()
      ? groupsAll.filter(
          (g) => g.name.toLowerCase().includes(query.trim().toLowerCase()) && !existingIds.has(`group:${g.id}`),
        )
      : [],
  );
  const userSuggestions = $derived(
    userResults.filter((u) => u.id !== access?.owner?.id && !existingIds.has(`user:${u.id}`)),
  );

  const grant = guard(async (subjectType: "user" | "group", subjectId: string) => {
    await library.share(id, subjectType, subjectId, newRole);
    query = "";
    userResults = [];
    await loadAccess();
  });

  const changeRole = guard(async (entry: AccessEntry, role: "read" | "write") => {
    if (entry.role === role) return;
    await library.share(id, entry.subject_type, entry.subject_id, role);
    await loadAccess();
  });

  const unshare = guard(async (entry: AccessEntry) => {
    const ok = await ui.confirm({
      title: "Remove access",
      body: `Remove ${entry.name}'s access to "${libName}"?`,
      okLabel: "Remove",
      danger: true,
    });
    if (!ok) return;
    await library.unshare(id, entry.subject_type, entry.subject_id);
    await loadAccess();
  });
</script>

<ModalShell width={560} kicker="Sharing" title={libName}>
  {#if loading && !access}
    <p class="empty-note">Loading…</p>
  {:else if error}
    <p class="err-text">{error}</p>
  {:else if access}
    <div class="owner-line">
      <span class="label-word">Owner</span>
      <span>{access.owner ? (access.owner.name ?? "—") : "no owner"}</span>
    </div>

    {#if access.access.length === 0}
      <p class="empty-note">Not shared with anyone else yet.</p>
    {:else}
      <ul class="entries">
        {#each access.access as entry (entry.subject_type + ":" + entry.subject_id)}
          <li class="entry">
            <div class="entry-name">
              <span>{entry.name}</span>
              <span class="entry-type">{entry.subject_type}</span>
            </div>
            <div class="seg" role="group" aria-label={`Access for ${entry.name}`}>
              <button aria-pressed={entry.role === "read"} onclick={() => changeRole(entry, "read")}>Read</button>
              <button aria-pressed={entry.role === "write"} onclick={() => changeRole(entry, "write")}>Can edit</button>
            </div>
            <button class="row-x" aria-label={`Remove ${entry.name}`} onclick={() => unshare(entry)}>×</button>
          </li>
        {/each}
      </ul>
    {/if}

    <div class="add-block">
      <div class="label">Add people or groups</div>
      <div class="add-row">
        <input class="input" placeholder="Search by name or email…" bind:value={query} aria-label="Search users or groups" />
        <div class="seg" role="group" aria-label="Role for new grant">
          <button aria-pressed={newRole === "read"} onclick={() => (newRole = "read")}>Read</button>
          <button aria-pressed={newRole === "write"} onclick={() => (newRole = "write")}>Can edit</button>
        </div>
      </div>
      {#if groupSuggestions.length || userSuggestions.length}
        <ul class="suggestions rag-scroll">
          {#each groupSuggestions as g (g.id)}
            <li>
              <button class="sug-btn" onclick={() => grant("group", g.id)}>
                <span>{g.name}</span>
                <span class="sug-type">Group</span>
              </button>
            </li>
          {/each}
          {#each userSuggestions as u (u.id)}
            <li>
              <button class="sug-btn" onclick={() => grant("user", u.id)}>
                <span>{u.name ?? u.email}</span>
                <span class="sug-type">{u.email}</span>
              </button>
            </li>
          {/each}
        </ul>
      {:else if query.trim().length > 0}
        <p class="empty-note">No matches.</p>
      {/if}
    </div>
  {/if}

  {#snippet footer()}
    <button class="btn btn-ghost" onclick={() => ui.close()}>Done</button>
  {/snippet}
</ModalShell>

<style>
  .owner-line { display: flex; align-items: center; gap: 8px; font-size: 13.5px; margin-bottom: 10px; }
  .label-word { font-family: var(--font-heading); font-size: 12px; color: var(--color-neutral-600); }
  .entries { list-style: none; margin: 0 0 16px; padding: 0; display: flex; flex-direction: column; gap: 2px; }
  .entry {
    display: flex; align-items: center; gap: 10px; padding: 7px 4px;
    border-bottom: 1px solid color-mix(in srgb, var(--color-text) 8%, transparent);
  }
  .entry-name { flex: 1; min-width: 0; display: flex; align-items: baseline; gap: 8px; font-size: 14px; }
  .entry-type { font-size: 11px; color: var(--color-neutral-600); text-transform: capitalize; }
  .row-x {
    border: 0; background: transparent; cursor: pointer; font-size: 15px; line-height: 1;
    color: var(--color-neutral-600); padding: 2px 6px;
  }
  .row-x:hover { color: var(--color-accent-2-700); }
  .add-block { border-top: 1px solid var(--color-divider); padding-top: 14px; }
  .label { display: block; font-size: 12px; margin-bottom: 6px; color: color-mix(in srgb, var(--color-text) 70%, transparent); }
  .add-row { display: flex; align-items: center; gap: 8px; }
  .add-row .input { flex: 1; min-width: 0; }
  .suggestions { list-style: none; margin: 8px 0 0; padding: 0; max-height: 180px; overflow-y: auto; border: 1px solid var(--color-divider); border-radius: var(--radius-md); }
  .sug-btn {
    width: 100%; display: flex; align-items: center; justify-content: space-between; gap: 8px;
    border: 0; background: transparent; cursor: pointer; text-align: left; font: inherit;
    padding: 7px 10px; font-size: 13.5px; color: var(--color-text);
  }
  .sug-btn:hover { background: color-mix(in srgb, var(--color-text) 6%, transparent); }
  .sug-type { font-size: 11px; color: var(--color-neutral-600); }
</style>

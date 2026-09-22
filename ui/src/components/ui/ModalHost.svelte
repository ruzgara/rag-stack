<script lang="ts">
  // Renders the popup stack, bottom to top, so a popup opened from another
  // (Sharing from a Library) sits over it and closing returns to it.
  import { ui } from "../../state/ui.svelte";
  import WorkspacesModal from "../modals/WorkspacesModal.svelte";
  import LibrariesModal from "../modals/LibrariesModal.svelte";
  import LibraryModal from "../modals/LibraryModal.svelte";
  import SharingModal from "../modals/SharingModal.svelte";
  import SourceModal from "../modals/SourceModal.svelte";
  import UploadModal from "../modals/UploadModal.svelte";
  import GroupsModal from "../modals/GroupsModal.svelte";
  import SettingsModal from "../modals/SettingsModal.svelte";
  import TrashModal from "../modals/TrashModal.svelte";
</script>

{#each ui.modals as m, i (i + m.kind)}
  {#if m.kind === "workspaces"}<WorkspacesModal />
  {:else if m.kind === "libraries"}<LibrariesModal />
  {:else if m.kind === "library"}<LibraryModal id={m.id} />
  {:else if m.kind === "sharing"}<SharingModal id={m.id} />
  {:else if m.kind === "source"}<SourceModal turnKey={m.turnKey} handle={m.handle} />
  {:else if m.kind === "upload"}<UploadModal libraryId={m.libraryId} folder={m.folder} />
  {:else if m.kind === "groups"}<GroupsModal />
  {:else if m.kind === "settings"}<SettingsModal />
  {:else if m.kind === "trash"}<TrashModal tab={m.tab} />
  {/if}
{/each}

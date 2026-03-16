<script lang="ts">
  import ExplorerShell from "$lib/components/explorer/ExplorerShell.svelte";
  import ChatSidebar from "@/components/explorer/ChatSidebar.svelte";
  import * as Resizable from "$lib/components/ui/resizable/index.js";
  import { explorerStore } from "@/stores";
</script>

{#if explorerStore.chatSidebarOpen}
  <Resizable.PaneGroup direction="horizontal" autoSaveId="explorer-chat" class="h-full w-full">
    <Resizable.Pane defaultSize={65} minSize={40}>
      <ExplorerShell />
    </Resizable.Pane>
    <Resizable.Handle />
    <Resizable.Pane defaultSize={35} minSize={20} maxSize={50}>
      <ChatSidebar />
    </Resizable.Pane>
  </Resizable.PaneGroup>
{:else}
  <div class="flex w-full h-full overflow-hidden">
    <ExplorerShell />
  </div>
{/if}

<script lang="ts">
  import ExplorerShell from "$lib/components/explorer/ExplorerShell.svelte";
  import ChatSidebar from "@/components/explorer/ChatSidebar.svelte";
  import { explorerStore } from "@/stores";
</script>

<div class="flex w-full h-full overflow-hidden">
  <ExplorerShell />
  {#if explorerStore.chatSidebarOpen}
    <ChatSidebar />
  {/if}
</div>

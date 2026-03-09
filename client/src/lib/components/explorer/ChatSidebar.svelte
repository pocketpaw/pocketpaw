<script lang="ts">
  import ChatPanel from "$lib/components/chat/ChatPanel.svelte";
  import ChatSessionDropdown from "$lib/components/chat/ChatSessionDropdown.svelte";
  import { slide } from "svelte/transition";
</script>

<div transition:slide={{ axis: "x"}} class="flex w-[360px] ml-2 shrink-0 flex-col bg-border/20 rounded-lg">
  <!-- Session selector -->
  <div class="shrink-0 border-b border-border/40">
    <ChatSessionDropdown />
  </div>
  <!-- Chat body -->
  <div class="flex-1 overflow-hidden">
    <ChatPanel />
  </div>
</div>

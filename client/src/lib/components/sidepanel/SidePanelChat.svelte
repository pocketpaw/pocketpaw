<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { chatStore } from "$lib/stores";
  import { isTauri } from "$lib/auth";
  import { emitChatSync } from "$lib/tauri/bridge";
  import MessageList from "$lib/components/chat/MessageList.svelte";
  import ChatInput from "$lib/components/chat/ChatInput.svelte";

  let isEmpty = $derived(chatStore.isEmpty && !chatStore.isStreaming);
  let quickAskUnsub: (() => void) | null = null;

  // Sync chat back to main window when streaming ends
  let prevStreaming = $state(false);
  $effect(() => {
    const streaming = chatStore.isStreaming;
    if (prevStreaming && !streaming && chatStore.messages.length > 0) {
      emitChatSync({
        messages: chatStore.messages,
        streaming: false,
        streamingContent: "",
      });
    }
    prevStreaming = streaming;
  });

  onMount(async () => {
    if (!isTauri()) return;

    // Check for a pending QuickAsk message (side panel was just created)
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      const pending = await invoke<string | null>("get_pending_quickask");
      if (pending) {
        chatStore.sendMessage(pending);
      }
    } catch {
      // ignore
    }

    // Listen for future QuickAsk messages (side panel already exists)
    try {
      const { listen } = await import("@tauri-apps/api/event");
      const unsub = await listen<string>("quickask-message", (event) => {
        chatStore.sendMessage(event.payload);
      });
      quickAskUnsub = unsub;
    } catch {
      // ignore
    }
  });

  onDestroy(() => {
    quickAskUnsub?.();
  });
</script>

<div class="flex flex-1 flex-col overflow-hidden">
  {#if isEmpty}
    <div class="flex flex-1 items-center justify-center px-3">
      <div class="flex flex-col items-center gap-2 text-center">
        <span class="text-2xl">🐾</span>
        <p class="text-sm font-medium text-foreground">Quick Chat</p>
        <p class="text-xs text-muted-foreground">
          Ask anything about what you're working on.
        </p>
        <p class="mt-1 text-[10px] text-muted-foreground/60">
          Synced with the main window session.
        </p>
      </div>
    </div>
  {:else}
    <MessageList />
  {/if}

  <ChatInput />
</div>

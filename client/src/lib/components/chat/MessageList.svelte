<script lang="ts">
  import { chatStore, platformStore } from "$lib/stores";
  import { ArrowDown } from "@lucide/svelte";
  import ChatMessage from "./ChatMessage.svelte";
  import StreamingMessage from "./StreamingMessage.svelte";

  let messages = $derived(chatStore.messages);
  let isStreaming = $derived(chatStore.isStreaming);

  let lastAssistantIdx = $derived.by(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === "assistant") return i;
    }
    return -1;
  });

  function handleRegenerate() {
    chatStore.regenerateLastResponse();
  }

  let scrollEl: HTMLDivElement | undefined = $state();
  let isAtBottom = $state(true);
  let autoScroll = $state(true);

  function handleScroll() {
    if (!scrollEl) return;
    const threshold = 60;
    const distFromBottom = scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight;
    isAtBottom = distFromBottom < threshold;
    if (isAtBottom) {
      autoScroll = true;
    } else {
      autoScroll = false;
    }
  }

  function scrollToBottom() {
    if (!scrollEl) return;
    scrollEl.scrollTo({ top: scrollEl.scrollHeight, behavior: "smooth" });
    autoScroll = true;
  }

  // Auto-scroll when messages change or streaming content/status updates
  $effect(() => {
    // Touch reactive dependencies
    messages.length;
    chatStore.streamingContent;
    chatStore.streamingStatus;

    if (autoScroll && scrollEl) {
      // Use microtask to scroll after DOM update
      queueMicrotask(() => {
        scrollEl?.scrollTo({ top: scrollEl.scrollHeight });
      });
    }
  });
</script>

<div class="relative flex-1 overflow-hidden">
  <div
    bind:this={scrollEl}
    onscroll={handleScroll}
    class="h-full overflow-y-auto px-3 py-4 md:px-4 md:py-6"
  >
    <div class="mx-auto flex max-w-3xl flex-col gap-6">
      {#each messages as message, i (i)}
        <ChatMessage
          {message}
          isLastAssistant={i === lastAssistantIdx && !isStreaming}
          onRegenerate={handleRegenerate}
        />
      {/each}

      {#if isStreaming}
        <StreamingMessage />
      {/if}
    </div>
  </div>

  <!-- Scroll to bottom button -->
  {#if !isAtBottom}
    <button
      onclick={scrollToBottom}
      class={platformStore.isTouch
        ? "absolute bottom-4 left-1/2 flex -translate-x-1/2 items-center gap-1.5 rounded-full border border-border bg-card/50 px-4 py-2.5 text-sm text-muted-foreground"
        : "absolute bottom-4 left-1/2 flex -translate-x-1/2 items-center gap-1.5 rounded-full border border-border bg-card/50 backdrop-blur-sm px-3 py-1.5 text-xs text-foreground"}
    >
      <ArrowDown class="h-3 w-3" />
      New messages
    </button>
  {/if}
</div>

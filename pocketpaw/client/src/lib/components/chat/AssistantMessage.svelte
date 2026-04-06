<script lang="ts">
  import type { ChatMessage } from "$lib/api";
  import { Check, Copy, RefreshCw } from "@lucide/svelte";
  import { platformStore } from "$lib/stores";
  import { chatStore } from "$lib/stores/chat.svelte";
  import MarkdownRenderer from "./MarkdownRenderer.svelte";
  import ActionButtons from "./ActionButtons.svelte";

  let {
    message,
    isLastAssistant = false,
    onRegenerate,
  }: {
    message: ChatMessage;
    isLastAssistant?: boolean;
    onRegenerate?: () => void;
  } = $props();

  let copied = $state(false);

  let timeStr = $derived.by(() => {
    if (!message.timestamp) return "";
    const d = new Date(message.timestamp);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  });

  let actions = $derived.by((): string[] => {
    if (!message.metadata?.actions) return [];
    if (Array.isArray(message.metadata.actions)) return message.metadata.actions;
    return [];
  });

  async function copyContent() {
    try {
      await navigator.clipboard.writeText(message.content);
      copied = true;
      setTimeout(() => { copied = false; }, 2000);
    } catch {
      // noop
    }
  }
</script>

<div class="group flex flex-col gap-1">
  <div class="flex items-center gap-2">
    <span class="text-sm">🐾</span>
    <span class="text-xs font-medium text-muted-foreground">PocketPaw</span>
    <span class={platformStore.isTouch
      ? "text-[10px] text-muted-foreground opacity-50"
      : "text-[10px] text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100"}>
      {timeStr}
    </span>
  </div>

  <div class="relative max-w-full pl-6">
    <div class="text-sm leading-relaxed text-foreground">
      <MarkdownRenderer content={message.content} />
    </div>

    {#if message.metadata?.askUser && Array.isArray(message.metadata.options)}
      <div class="mt-2 flex flex-wrap gap-2">
        {#each message.metadata.options as opt}
          <button
            onclick={() => chatStore.answerAskUser(String(opt))}
            class="rounded-lg border border-border bg-card px-3 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
          >
            {opt}
          </button>
        {/each}
      </div>
    {/if}

    <ActionButtons {actions} />

    {#if platformStore.isTouch}
      <!-- Touch: inline action row always visible -->
      <div class="mt-2 flex items-center gap-2">
        <button
          onclick={copyContent}
          class="flex h-8 items-center gap-1.5 rounded-md px-2.5 text-xs text-muted-foreground transition-colors active:bg-accent active:text-foreground"
        >
          {#if copied}
            <Check class="h-3.5 w-3.5" />
            <span>Copied</span>
          {:else}
            <Copy class="h-3.5 w-3.5" />
            <span>Copy</span>
          {/if}
        </button>
        {#if isLastAssistant && onRegenerate}
          <button
            onclick={onRegenerate}
            class="flex h-8 items-center gap-1.5 rounded-md px-2.5 text-xs text-muted-foreground transition-colors active:bg-accent active:text-foreground"
          >
            <RefreshCw class="h-3.5 w-3.5" />
            <span>Retry</span>
          </button>
        {/if}
      </div>
    {:else}
      <!-- Desktop: hover overlay -->
      <div class=" flex  gap-1 ">
        <button
          onclick={copyContent}
          class="flex h-6 w-6 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
        >
          {#if copied}
            <Check class="h-3 w-3" />
          {:else}
            <Copy class="h-3 w-3" />
          {/if}
        </button>
        {#if isLastAssistant && onRegenerate}
          <button
            onclick={onRegenerate}
            class="flex h-6 w-6 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
          >
            <RefreshCw class="h-3 w-3" />
          </button>
        {/if}
      </div>
    {/if}
  </div>
</div>

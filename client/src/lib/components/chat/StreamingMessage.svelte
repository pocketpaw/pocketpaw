<script lang="ts">
  import { chatStore } from "$lib/stores";
  import { LoaderCircle } from "@lucide/svelte";
  import MarkdownRenderer from "./MarkdownRenderer.svelte";

  let streamingContent = $derived(chatStore.streamingContent);
  let hasContent = $derived(streamingContent.length > 0);
  let statusText = $derived(chatStore.streamingStatus);
</script>

<div class="flex flex-col gap-1">
  <div class="flex items-center gap-2">
    <span class="text-sm">🐾</span>
    <span class="text-xs font-medium text-muted-foreground">PocketPaw</span>
  </div>

  <div class="max-w-full pl-6">
    {#if hasContent}
      <div class="text-sm leading-relaxed text-foreground">
        <MarkdownRenderer content={streamingContent} />
      </div>
    {/if}

    {#if statusText}
      <div class="flex items-center gap-2 {hasContent ? 'mt-2' : ''}">
        <LoaderCircle class="h-3.5 w-3.5 animate-spin text-muted-foreground" />
        <span class="text-xs text-muted-foreground">{statusText}</span>
      </div>
    {/if}
  </div>
</div>

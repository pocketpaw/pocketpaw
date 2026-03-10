<script lang="ts">
  import type { ChatMessage } from "$lib/api";
  import { platformStore } from "$lib/stores";
  import FilePreview from "./FilePreview.svelte";

  let { message }: { message: ChatMessage } = $props();

  let hasMedia = $derived(message.media && message.media.length > 0);

  let timeStr = $derived.by(() => {
    if (!message.timestamp) return "";
    const d = new Date(message.timestamp);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  });
</script>

<div class="group flex flex-col items-end gap-1">
  <div class="max-w-[85%] rounded-2xl rounded-br-md bg-paw-accent-subtle px-4 py-2.5">
    {#if hasMedia}
      <div class="mb-2 flex flex-wrap gap-1.5">
        {#each message.media! as file}
          <FilePreview file={{ name: file.url ?? "file", type: file.type }} />
        {/each}
      </div>
    {/if}
    <p class="whitespace-pre-wrap text-sm text-foreground">{message.content}</p>
  </div>
  <span class={platformStore.isTouch
    ? "px-1 text-[10px] text-muted-foreground opacity-50"
    : "px-1 text-[10px] text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100"}>
    {timeStr}
  </span>
</div>

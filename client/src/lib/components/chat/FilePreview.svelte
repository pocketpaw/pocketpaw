<script lang="ts">
  import { X } from "@lucide/svelte";
  import type { MediaAttachment } from "$lib/api";
  import StyledFileIcon from "$lib/components/explorer/StyledFileIcon.svelte";
  import { getCategoryColor, isTextPreviewable } from "$lib/components/explorer/file-icon-colors";

  let {
    file,
    removable = false,
    onRemove,
  }: {
    file: MediaAttachment | { name: string; type: string; size?: number; data?: string };
    removable?: boolean;
    onRemove?: () => void;
  } = $props();

  let fileName = $derived("name" in file ? file.name : "file");
  let fileType = $derived("type" in file ? file.type : "");
  let extension = $derived(fileName.includes(".") ? fileName.split(".").pop()?.toLowerCase() ?? "" : "");

  let isImage = $derived(fileType.startsWith("image/"));

  // For image previews: create object URL from data if available
  let imageUrl = $state<string | null>(null);
  // For text previews: read snippet from file data
  let textSnippet = $state<string | null>(null);

  $effect(() => {
    // Image preview from data URL or blob
    if (isImage && "data" in file && file.data) {
      if (typeof file.data === "string" && file.data.startsWith("data:")) {
        imageUrl = file.data;
      } else if (typeof file.data === "string") {
        imageUrl = `data:${fileType};base64,${file.data}`;
      }
    } else if (isImage && "url" in file && file.url) {
      imageUrl = file.url;
    }

    // Text snippet preview
    if (!isImage && isTextPreviewable(extension) && "data" in file && file.data) {
      if (typeof file.data === "string") {
        // data might be base64 or plain text
        try {
          if (file.data.startsWith("data:")) {
            const base64Part = file.data.split(",")[1];
            textSnippet = atob(base64Part).slice(0, 300);
          } else {
            textSnippet = atob(file.data).slice(0, 300);
          }
        } catch {
          textSnippet = file.data.slice(0, 300);
        }
      }
    }
  });

  let color = $derived(getCategoryColor(extension));

  function formatSize(bytes?: number): string {
    if (!bytes) return "";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  let sizeStr = $derived("size" in file ? formatSize(file.size as number) : "");
</script>

<div class="group relative inline-flex max-w-[200px] overflow-hidden rounded-lg border border-border bg-card shadow-sm">
  <!-- Preview area -->
  <div class="flex h-14 w-14 shrink-0 items-center justify-center overflow-hidden bg-muted/30">
    {#if imageUrl}
      <!-- Image thumbnail -->
      <img
        src={imageUrl}
        alt={fileName}
        class="h-full w-full object-cover"
      />
    {:else if textSnippet}
      <!-- Text snippet preview -->
      <div class="relative h-full w-full overflow-hidden p-1">
        <pre class="h-full w-full overflow-hidden whitespace-pre-wrap break-all font-mono text-[5px] leading-tight text-muted-foreground/70">{textSnippet}</pre>
        <div
          class="pointer-events-none absolute inset-x-0 bottom-0 h-3"
          style="background: linear-gradient(to bottom, transparent, var(--color-card, hsl(var(--card))))"
        ></div>
        <!-- Extension badge -->
        <span
          class="absolute right-0.5 top-0.5 rounded px-0.5 text-[5px] font-bold leading-none text-white"
          style:background-color={color.fill}
        >
          {color.text || extension.toUpperCase().slice(0, 3)}
        </span>
      </div>
    {:else}
      <!-- Styled file icon fallback -->
      <StyledFileIcon {extension} size={28} />
    {/if}
  </div>

  <!-- File info -->
  <div class="flex min-w-0 flex-1 flex-col justify-center gap-0.5 px-2 py-1.5">
    <span class="truncate text-xs font-medium text-foreground" title={fileName}>
      {fileName}
    </span>
    {#if sizeStr}
      <span class="text-[10px] text-muted-foreground">{sizeStr}</span>
    {/if}
  </div>

  <!-- Remove button -->
  {#if removable && onRemove}
    <button
      onclick={onRemove}
      class="absolute right-1 top-1 rounded-full bg-background/80 p-0.5 text-muted-foreground opacity-0 shadow-sm transition-all hover:bg-accent hover:text-foreground group-hover:opacity-100"
    >
      <X class="h-3 w-3" />
    </button>
  {/if}
</div>

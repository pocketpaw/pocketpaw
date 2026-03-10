<script lang="ts">
  import type { FileEntry } from "$lib/filesystem";
  import { isImageFile } from "$lib/filesystem";
  import FileTypeIcon from "./FileTypeIcon.svelte";
  import TextPreview from "./TextPreview.svelte";
  import InlineRename from "./InlineRename.svelte";
  import { thumbnailAction } from "./use-thumbnail";
  import { contentPreviewAction, type ContentPreviewResult } from "./use-content-preview";
  import { getCategoryColor } from "./file-icon-colors";
  import { cn } from "$lib/utils";

  let {
    file,
    isSelected = false,
    isRenaming = false,
    isCut = false,
    isFocused = false,
    onclick,
    ondblclick,
    oncontextmenu,
    ondragstart,
    ondrop,
  }: {
    file: FileEntry;
    isSelected?: boolean;
    isRenaming?: boolean;
    isCut?: boolean;
    isFocused?: boolean;
    onclick?: (e: MouseEvent) => void;
    ondblclick?: (e: MouseEvent) => void;
    oncontextmenu?: (e: MouseEvent) => void;
    ondragstart?: (e: DragEvent) => void;
    ondrop?: (e: DragEvent) => void;
  } = $props();

  let thumbnailUrl = $state<string | null>(null);
  let thumbnailLoaded = $state(false);
  let contentPreview = $state<ContentPreviewResult | null>(null);
  let contentPreviewLoaded = $state(false);
  let isDragOver = $state(false);
  let buttonRef = $state<HTMLButtonElement | null>(null);

  let isImage = $derived(!file.isDir && isImageFile(file.extension));

  $effect(() => {
    if (isFocused && buttonRef) {
      buttonRef.scrollIntoView({ block: "nearest" });
    }
  });

  function handleThumbnailLoad(url: string) {
    thumbnailUrl = url;
  }

  function handleContentPreviewLoad(result: ContentPreviewResult) {
    contentPreview = result;
    contentPreviewLoaded = true;
  }

  // Determine which preview layer is active
  let hasImageThumb = $derived(thumbnailUrl !== null);
  let hasContentPreview = $derived(contentPreview !== null);
  let showIcon = $derived(!thumbnailLoaded && !contentPreviewLoaded);

  function formatRelativeTime(timestamp: number): string {
    if (!timestamp) return "";
    const now = Date.now() / 1000;
    const diff = now - timestamp;
    if (diff < 60) return "Just now";
    const mins = Math.floor(diff / 60);
    if (diff < 3600) return `${mins} ${mins === 1 ? "minute" : "minutes"} ago`;
    const hrs = Math.floor(diff / 3600);
    if (diff < 86400) return `${hrs} ${hrs === 1 ? "hour" : "hours"} ago`;
    const days = Math.floor(diff / 86400);
    if (diff < 604800) return `${days} ${days === 1 ? "day" : "days"} ago`;
    const weeks = Math.floor(diff / 604800);
    if (diff < 2592000) return `${weeks} ${weeks === 1 ? "week" : "weeks"} ago`;
    return new Date(timestamp * 1000).toLocaleDateString();
  }

  function formatSize(bytes: number): string {
    if (bytes === 0) return "";
    const units = ["B", "KB", "MB", "GB"];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    return `${(bytes / Math.pow(1024, i)).toFixed(i > 0 ? 1 : 0)} ${units[i]}`;
  }
</script>

<button
  bind:this={buttonRef}
  type="button"
  draggable="true"
  class={cn(
    "group flex w-full flex-col items-center gap-1 rounded-lg border border-transparent p-3 text-left transition-all",
    "hover:bg-muted/50",
    isSelected && "border-primary/50 bg-primary/10 ring-1 ring-primary/30",
    isCut && "opacity-50",
    isFocused && "ring-1 ring-ring/50",
    isDragOver && file.isDir && "border-primary bg-primary/10",
  )}
  {onclick}
  {ondblclick}
  {oncontextmenu}
  ondragstart={(e) => ondragstart?.(e)}
  ondragover={(e) => {
    if (file.isDir) {
      e.preventDefault();
      isDragOver = true;
    }
  }}
  ondragleave={() => { isDragOver = false; }}
  ondrop={(e) => {
    isDragOver = false;
    if (file.isDir) {
      e.preventDefault();
      ondrop?.(e);
    }
  }}
>
  <!-- Thumbnail area -->
  <div
    class={cn(
      "relative flex h-28 w-full items-center justify-center overflow-hidden rounded-md",
      file.isDir ? "bg-yellow-500/10" : "bg-muted/30",
    )}
    use:thumbnailAction={{
      path: file.path,
      extension: file.extension,
      modified: file.modified,
      onLoad: handleThumbnailLoad,
    }}
    use:contentPreviewAction={{
      path: file.path,
      extension: file.extension,
      size: file.size,
      isDir: file.isDir,
      onLoad: handleContentPreviewLoad,
    }}
  >
    <!-- Icon (fades out when any preview loads) -->
    <div
      class="transition-opacity duration-300"
      style:opacity={showIcon ? 1 : 0}
    >
      <FileTypeIcon extension={file.extension} isDir={file.isDir} size={36} />
    </div>

    <!-- Image thumbnail (fades in) -->
    {#if hasImageThumb}
      <img
        src={thumbnailUrl}
        alt={file.name}
        class="absolute inset-0 h-full w-full object-cover transition-opacity duration-300"
        style:opacity={thumbnailLoaded ? 1 : 0}
        onload={() => { thumbnailLoaded = true; }}
      />
    {/if}

    <!-- Text content preview (fades in) -->
    {#if hasContentPreview && contentPreview?.type === "text"}
      <div
        class="absolute inset-0 transition-opacity duration-300"
        style:opacity={contentPreviewLoaded ? 1 : 0}
      >
        <TextPreview text={contentPreview.content} extension={file.extension} />
      </div>
    {/if}

    <!-- PDF thumbnail (fades in) -->
    {#if hasContentPreview && contentPreview?.type === "pdf"}
      <img
        src={contentPreview.content}
        alt={file.name}
        class="absolute inset-0 h-full w-full object-cover object-top transition-opacity duration-300"
        style:opacity={contentPreviewLoaded ? 1 : 0}
      />
      <!-- PDF badge overlay -->
      <span
        class="absolute right-1 top-1 rounded px-1 py-0.5 text-[10px] font-bold leading-none text-white transition-opacity duration-300"
        style:opacity={contentPreviewLoaded ? 1 : 0}
        style:background-color={getCategoryColor("pdf").fill}
      >
        PDF
      </span>
    {/if}
  </div>

  <!-- Info area -->
  <div class="w-full min-w-0 space-y-0.5 px-0.5">
    {#if isRenaming}
      <InlineRename fileName={file.name} filePath={file.path} />
    {:else}
      <p class="truncate text-sm font-medium text-foreground" title={file.name}>
        {file.name}
      </p>
    {/if}
    <p class="truncate text-xs text-muted-foreground">
      {#if file.isDir}
        Folder
      {:else if file.modified}
        {formatRelativeTime(file.modified)}
      {/if}
    </p>
  </div>
</button>

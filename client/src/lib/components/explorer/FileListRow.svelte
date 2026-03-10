<script lang="ts">
  import type { FileEntry } from "$lib/filesystem";
  import FileTypeIcon from "./FileTypeIcon.svelte";
  import InlineRename from "./InlineRename.svelte";
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

  let isDragOver = $state(false);
  let buttonRef = $state<HTMLButtonElement | null>(null);

  $effect(() => {
    if (isFocused && buttonRef) {
      buttonRef.scrollIntoView({ block: "nearest" });
    }
  });

  function formatDate(timestamp: number): string {
    if (!timestamp) return "\u2014";
    return new Date(timestamp * 1000).toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
      year: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  }

  function formatSize(bytes: number): string {
    if (bytes === 0) return "\u2014";
    const units = ["B", "KB", "MB", "GB"];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    return `${(bytes / Math.pow(1024, i)).toFixed(i > 0 ? 1 : 0)} ${units[i]}`;
  }

  function getTypeLabel(entry: FileEntry): string {
    if (entry.isDir) return "Folder";
    if (!entry.extension) return "File";
    return entry.extension.toUpperCase();
  }
</script>

<button
  bind:this={buttonRef}
  type="button"
  draggable="true"
  class={cn(
    "flex w-full items-center gap-3 rounded-md px-3 py-1.5 text-left transition-colors",
    "hover:bg-muted/50",
    isSelected && "bg-primary/10 ring-1 ring-primary/30",
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
  <!-- Name column -->
  <div class="flex min-w-0 flex-1 items-center gap-2">
    <div class="shrink-0">
      <FileTypeIcon extension={file.extension} isDir={file.isDir} size={18} />
    </div>
    {#if isRenaming}
      <InlineRename fileName={file.name} filePath={file.path} />
    {:else}
      <span class="truncate text-sm text-foreground">{file.name}</span>
    {/if}
  </div>

  <!-- Modified column -->
  <span class="w-36 shrink-0 text-xs text-muted-foreground">
    {file.isDir ? "\u2014" : formatDate(file.modified)}
  </span>

  <!-- Size column -->
  <span class="w-24 shrink-0 text-right text-xs text-muted-foreground">
    {file.isDir ? "\u2014" : formatSize(file.size)}
  </span>

  <!-- Type column -->
  <span class="w-20 shrink-0 text-xs text-muted-foreground">
    {getTypeLabel(file)}
  </span>
</button>

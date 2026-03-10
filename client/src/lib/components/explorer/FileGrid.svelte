<script lang="ts">
  import { explorerStore } from "$lib/stores";
  import { localFs, joinPath, getFileName } from "$lib/filesystem";
  import FileCard from "./FileCard.svelte";
  import ContextMenu from "./ContextMenu.svelte";
  import type { FileEntry } from "$lib/filesystem";
  import FolderOpen from "@lucide/svelte/icons/folder-open";

  const DRAG_MIME = "application/x-pocketpaw-files";

  let contextMenu = $state<{ x: number; y: number; file: FileEntry | null } | null>(null);

  function handleClick(file: FileEntry, e: MouseEvent) {
    explorerStore.selectFile(file.path, e.ctrlKey || e.metaKey);
  }

  function handleDblClick(file: FileEntry) {
    if (file.isDir) {
      explorerStore.navigateTo(file.path);
    } else {
      explorerStore.openFileDetail(file);
    }
  }

  function handleContextMenu(file: FileEntry, e: MouseEvent) {
    e.preventDefault();
    contextMenu = { x: e.clientX, y: e.clientY, file };
  }

  function handleBackgroundClick(e: MouseEvent) {
    if (e.target === e.currentTarget) {
      explorerStore.clearSelection();
    }
  }

  function handleBackgroundContextMenu(e: MouseEvent) {
    if (e.target === e.currentTarget) {
      e.preventDefault();
      contextMenu = { x: e.clientX, y: e.clientY, file: null };
    }
  }

  function handleDragStart(file: FileEntry, e: DragEvent) {
    if (!e.dataTransfer) return;
    const paths = explorerStore.selectedFiles.has(file.path)
      ? [...explorerStore.selectedFiles]
      : [file.path];
    const mode = e.shiftKey ? "copy" : "move";
    e.dataTransfer.setData(DRAG_MIME, JSON.stringify({ paths, mode }));
    e.dataTransfer.setData("text/plain", paths.join("\n"));
    e.dataTransfer.effectAllowed = "copyMove";
  }

  async function handleFileDrop(targetDir: string, e: DragEvent) {
    if (!e.dataTransfer) return;
    const raw = e.dataTransfer.getData(DRAG_MIME);
    if (!raw) return;
    try {
      const { paths, mode } = JSON.parse(raw) as { paths: string[]; mode: "copy" | "move" };
      for (const srcPath of paths) {
        const name = getFileName(srcPath);
        const destPath = joinPath(targetDir, name);
        const stat = await localFs.stat(srcPath);
        if (mode === "copy") {
          if (stat.isDir) await localFs.copyDir(srcPath, destPath);
          else await localFs.copyFile(srcPath, destPath);
        } else {
          await localFs.moveFile(srcPath, destPath);
        }
      }
      await explorerStore.refresh();
    } catch (err) {
      console.error("Drop failed:", err);
    }
  }
</script>

<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="h-full" onclick={handleBackgroundClick} oncontextmenu={handleBackgroundContextMenu}>
  {#if explorerStore.isLoading}
    <div class="grid grid-cols-[repeat(auto-fill,minmax(140px,1fr))] gap-2 p-2">
      {#each Array(12) as _}
        <div class="flex w-full animate-pulse flex-col items-center gap-1 rounded-lg p-3">
          <div class="h-20 w-full rounded-md bg-muted/30"></div>
          <div class="h-4 w-3/4 rounded bg-muted/30"></div>
          <div class="h-3 w-1/2 rounded bg-muted/20"></div>
        </div>
      {/each}
    </div>
  {:else if explorerStore.error}
    <div class="flex h-full flex-col items-center justify-center gap-2 text-muted-foreground">
      <p class="text-sm">Failed to load directory</p>
      <p class="text-xs text-red-400">{explorerStore.error}</p>
      <button
        class="mt-2 rounded-md bg-primary/10 px-3 py-1.5 text-xs text-primary hover:bg-primary/20"
        onclick={() => explorerStore.refresh()}
      >
        Retry
      </button>
    </div>
  {:else if explorerStore.sortedFiles.length === 0}
    <div class="flex h-full flex-col items-center justify-center gap-2 text-muted-foreground">
      <FolderOpen class="h-12 w-12 opacity-30" />
      <p class="text-sm">
        {explorerStore.searchQuery ? "No files match your filter" : "This folder is empty"}
      </p>
    </div>
  {:else}
    <div class="grid grid-cols-[repeat(auto-fill,minmax(140px,1fr))] gap-2 p-2">
      {#each explorerStore.sortedFiles as file, i (file.path)}
        <FileCard
          {file}
          isSelected={explorerStore.selectedFiles.has(file.path)}
          isRenaming={explorerStore.renamingFile === file.path}
          isCut={explorerStore.clipboardMode === "cut" && explorerStore.clipboardFiles.has(file.path)}
          isFocused={explorerStore.focusedIndex === i}
          onclick={(e) => handleClick(file, e)}
          ondblclick={() => handleDblClick(file)}
          oncontextmenu={(e) => handleContextMenu(file, e)}
          ondragstart={(e) => handleDragStart(file, e)}
          ondrop={(e) => handleFileDrop(file.path, e)}
        />
      {/each}
    </div>
  {/if}
</div>

{#if contextMenu}
  <ContextMenu
    x={contextMenu.x}
    y={contextMenu.y}
    file={contextMenu.file}
    onClose={() => (contextMenu = null)}
  />
{/if}

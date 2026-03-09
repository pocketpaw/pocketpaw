<script lang="ts">
  import { explorerStore } from "$lib/stores";
  import { parentDir } from "$lib/filesystem";
  import StatusBar from "./StatusBar.svelte";
  import HomeView from "./HomeView.svelte";
  import FileGrid from "./FileGrid.svelte";
  import FileList from "./FileList.svelte";
  import FileViewer from "./FileViewer.svelte";
  import { CommandPalette } from "$lib/components/command-palette";
  import { onMount } from "svelte";

  onMount(() => {
    function handleKeydown(e: KeyboardEvent) {
      const tag = (e.target as HTMLElement)?.tagName;
      const isInput = tag === "INPUT" || tag === "TEXTAREA";
      const isEditor = (e.target as HTMLElement)?.closest(".cm-editor") !== null;

      // Skip all custom shortcuts when in input or code editor
      if (isInput || isEditor) return;

      // Delete to delete selected files
      if (e.key === "Delete" && explorerStore.selectedFiles.size > 0) {
        e.preventDefault();
        const count = explorerStore.selectedFiles.size;
        if (confirm(`Delete ${count} item${count > 1 ? "s" : ""}?`)) {
          import("$lib/filesystem").then(({ localFs }) => {
            const promises = [...explorerStore.selectedFiles].map((path) => {
              const file = explorerStore.files.find((f) => f.path === path);
              return localFs.deleteFile(path, file?.isDir ?? false);
            });
            Promise.all(promises).then(() => {
              explorerStore.clearSelection();
              explorerStore.refresh();
            });
          });
        }
        return;
      }

      // Arrow keys to navigate files
      if (e.key === "ArrowDown" && !explorerStore.isHome && !explorerStore.isDetailView) {
        e.preventDefault();
        explorerStore.moveFocus(1);
        return;
      }

      if (e.key === "ArrowUp" && !explorerStore.isHome && !explorerStore.isDetailView) {
        e.preventDefault();
        explorerStore.moveFocus(-1);
        return;
      }

      // Enter to open focused file/folder
      if (e.key === "Enter" && explorerStore.focusedIndex >= 0 && !explorerStore.isDetailView) {
        e.preventDefault();
        const focused = explorerStore.sortedFiles[explorerStore.focusedIndex];
        if (focused) {
          if (focused.isDir) {
            explorerStore.navigateTo(focused.path);
          } else {
            explorerStore.openFileDetail(focused);
          }
        }
        return;
      }

      // Backspace to go to parent directory
      if (e.key === "Backspace" && !explorerStore.isHome && !explorerStore.isDetailView) {
        e.preventDefault();
        const parent = parentDir(explorerStore.currentPath);
        if (parent) explorerStore.navigateTo(parent);
        return;
      }

      // Space to quick preview focused file
      if (e.key === " " && explorerStore.focusedIndex >= 0 && !explorerStore.isDetailView) {
        e.preventDefault();
        const focused = explorerStore.sortedFiles[explorerStore.focusedIndex];
        if (focused && !focused.isDir) {
          explorerStore.openFileDetail(focused);
        }
        return;
      }
    }

    document.addEventListener("keydown", handleKeydown);
    return () => document.removeEventListener("keydown", handleKeydown);
  });
</script>

<div class="flex h-full min-w-0 flex-1 flex-col">
  <div class="shrink-0  px-2 py-1.5">
    <CommandPalette />
  </div>
  <div class="flex flex-1 overflow-hidden">
    {#if explorerStore.webPreviewUrl}
      <div class="flex-1 overflow-hidden m-2 rounded bg-border/20">
        {#await import("./WebPreview.svelte") then mod}
          <mod.default url={explorerStore.webPreviewUrl} />
        {/await}
      </div>
    {:else if explorerStore.isDetailView && explorerStore.openFile}
      <div class="flex-1 overflow-hidden m-2 rounded bg-border/20">
        <FileViewer file={explorerStore.openFile} />
      </div>
    {:else}
      <div class="flex-1 overflow-y-auto rounded bg-border/20 m-2">
        {#if explorerStore.isHome}
          <HomeView />
        {:else if explorerStore.viewMode === "list"}
          <FileList />
        {:else}
          <FileGrid />
        {/if}
      </div>
    {/if}
  </div>
  {#if !explorerStore.isHome}
    <StatusBar />
  {/if}
</div>

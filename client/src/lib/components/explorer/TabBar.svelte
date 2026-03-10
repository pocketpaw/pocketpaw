<script lang="ts">
  import { explorerStore } from "$lib/stores";
  import FolderOpen from "@lucide/svelte/icons/folder-open";
  import Home from "@lucide/svelte/icons/home";
  import Plus from "@lucide/svelte/icons/plus";
  import X from "@lucide/svelte/icons/x";
  import { cn } from "$lib/utils";
  import TabContextMenu from "./TabContextMenu.svelte";

  let dragFromIndex = $state<number | null>(null);
  let dragOverIndex = $state<number | null>(null);
  let contextMenuTabId = $state<string | null>(null);
  let contextMenuPos = $state<{ x: number; y: number } | null>(null);

  function getTabLabel(path: string): string {
    if (!path) return "Home";
    const normalized = path.replace(/\\/g, "/");
    const parts = normalized.split("/").filter(Boolean);
    return parts[parts.length - 1] || path;
  }

  function handleDragStart(e: DragEvent, index: number) {
    dragFromIndex = index;
    if (e.dataTransfer) {
      e.dataTransfer.effectAllowed = "move";
    }
  }

  function handleDragOver(e: DragEvent, index: number) {
    // Only handle tab-reorder drags, not file drags from the grid
    if (dragFromIndex === null) return;
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = "move";
    dragOverIndex = index;
  }

  function handleDrop(e: DragEvent, toIndex: number) {
    // Only handle tab-reorder drags, not file drags
    if (dragFromIndex === null) return;
    e.preventDefault();
    if (dragFromIndex !== toIndex) {
      explorerStore.reorderTabs(dragFromIndex, toIndex);
    }
    dragFromIndex = null;
    dragOverIndex = null;
  }

  function handleDragEnd() {
    dragFromIndex = null;
    dragOverIndex = null;
  }

  function handleMiddleClick(e: MouseEvent, tabId: string) {
    if (e.button === 1) {
      e.preventDefault();
      explorerStore.closeTab(tabId);
    }
  }

  function handleContextMenu(e: MouseEvent, tabId: string) {
    e.preventDefault();
    contextMenuTabId = tabId;
    contextMenuPos = { x: e.clientX, y: e.clientY };
  }

  function closeContextMenu() {
    contextMenuTabId = null;
    contextMenuPos = null;
  }
</script>

<div class="flex h-9 shrink-0 items-center border-b border-border bg-muted/30">
  <!-- Scrollable tab strip -->
  <div class="flex min-w-0 flex-1 items-end gap-0 overflow-x-auto" role="tablist">
    {#each explorerStore.tabs as tab, i (tab.id)}
      <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
      <div
        role="tab"
        tabindex={tab.id === explorerStore.activeTabId ? 0 : -1}
        draggable="true"
        aria-selected={tab.id === explorerStore.activeTabId}
        class={cn(
          "group relative flex h-8 max-w-48 min-w-0 shrink-0 cursor-pointer items-center gap-1.5 border-r border-border px-3 text-xs transition-colors select-none",
          tab.id === explorerStore.activeTabId
            ? "bg-background text-foreground border-b-2 border-b-primary"
            : "bg-muted/40 text-muted-foreground hover:bg-muted/80 hover:text-foreground border-b border-b-transparent",
          dragOverIndex === i && dragFromIndex !== i && "border-l-2 border-l-primary",
        )}
        onclick={() => explorerStore.activateTab(tab.id)}
        onkeydown={(e) => { if (e.key === "Enter" || e.key === " ") explorerStore.activateTab(tab.id); }}
        onauxclick={(e) => handleMiddleClick(e, tab.id)}
        oncontextmenu={(e) => handleContextMenu(e, tab.id)}
        ondragstart={(e) => handleDragStart(e, i)}
        ondragover={(e) => handleDragOver(e, i)}
        ondrop={(e) => handleDrop(e, i)}
        ondragend={handleDragEnd}
      >
        {#if tab.path}
          <FolderOpen class="h-3.5 w-3.5 shrink-0 text-amber-500" />
        {:else}
          <Home class="h-3.5 w-3.5 shrink-0" />
        {/if}
        <span class="truncate">{getTabLabel(tab.path)}</span>
        {#if explorerStore.tabs.length > 1}
          <button
            type="button"
            class="ml-auto shrink-0 rounded-sm p-0.5 opacity-0 transition-opacity hover:bg-muted group-hover:opacity-100"
            onclick={(e) => { e.stopPropagation(); explorerStore.closeTab(tab.id); }}
            title="Close tab"
          >
            <X class="h-3 w-3" />
          </button>
        {/if}
      </div>
    {/each}
  </div>

  <!-- New tab button -->
  <button
    type="button"
    class="mx-1 shrink-0 rounded-md p-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
    onclick={() => explorerStore.createNewTab()}
    title="New tab (Ctrl+T)"
  >
    <Plus class="h-4 w-4" />
  </button>
</div>

{#if contextMenuTabId && contextMenuPos}
  <TabContextMenu
    tabId={contextMenuTabId}
    x={contextMenuPos.x}
    y={contextMenuPos.y}
    onclose={closeContextMenu}
  />
{/if}

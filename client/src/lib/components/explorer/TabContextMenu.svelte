<script lang="ts">
  import { explorerStore } from "$lib/stores";
  import { onMount } from "svelte";

  let {
    tabId,
    x,
    y,
    onclose,
  }: {
    tabId: string;
    x: number;
    y: number;
    onclose: () => void;
  } = $props();

  let menuRef = $state<HTMLDivElement | null>(null);

  onMount(() => {
    function handleClick(e: MouseEvent) {
      if (menuRef && !menuRef.contains(e.target as Node)) {
        onclose();
      }
    }
    function handleKeydown(e: KeyboardEvent) {
      if (e.key === "Escape") onclose();
    }
    document.addEventListener("mousedown", handleClick);
    document.addEventListener("keydown", handleKeydown);
    return () => {
      document.removeEventListener("mousedown", handleClick);
      document.removeEventListener("keydown", handleKeydown);
    };
  });

  const canClose = $derived(explorerStore.tabs.length > 1);
  const tabIndex = $derived(explorerStore.tabs.findIndex((t) => t.id === tabId));
  const hasTabsToRight = $derived(tabIndex < explorerStore.tabs.length - 1);
  const hasOtherTabs = $derived(explorerStore.tabs.length > 1);
</script>

<div
  bind:this={menuRef}
  class="fixed z-[100] min-w-44 rounded-md border border-border bg-popover p-1 shadow-lg"
  style="left: {x}px; top: {y}px;"
>
  {#if canClose}
    <button
      type="button"
      class="flex w-full items-center rounded-sm px-2 py-1.5 text-xs text-foreground transition-colors hover:bg-muted"
      onclick={() => {
        explorerStore.closeTab(tabId);
        onclose();
      }}
    >
      Close Tab
    </button>
  {/if}
  {#if hasOtherTabs}
    <button
      type="button"
      class="flex w-full items-center rounded-sm px-2 py-1.5 text-xs text-foreground transition-colors hover:bg-muted"
      onclick={() => {
        explorerStore.closeOtherTabs(tabId);
        onclose();
      }}
    >
      Close Other Tabs
    </button>
  {/if}
  {#if hasTabsToRight}
    <button
      type="button"
      class="flex w-full items-center rounded-sm px-2 py-1.5 text-xs text-foreground transition-colors hover:bg-muted"
      onclick={() => {
        explorerStore.closeTabsToRight(tabId);
        onclose();
      }}
    >
      Close Tabs to the Right
    </button>
  {/if}
  <div class="my-1 h-px bg-border"></div>
  <button
    type="button"
    class="flex w-full items-center rounded-sm px-2 py-1.5 text-xs text-foreground transition-colors hover:bg-muted"
    onclick={() => {
      explorerStore.duplicateTab(tabId);
      onclose();
    }}
  >
    Duplicate Tab
  </button>
</div>

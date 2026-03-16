<script lang="ts">
  import { X } from "@lucide/svelte";
  import { page } from "$app/state";
  import SidebarHeader from "./SidebarHeader.svelte";
  import SidebarSessions from "./SidebarSessions.svelte";
  import SidebarExplorer from "./SidebarExplorer.svelte";
  import SidebarFooter from "./SidebarFooter.svelte";

  let {
    isDrawer = false,
    onClose,
  }: {
    isDrawer?: boolean;
    onClose?: () => void;
  } = $props();

  let isFilesMode = $derived(page.url.pathname === "/");

  let sidebarClass = $derived(
    isDrawer
      ? "flex h-full w-full flex-col overflow-hidden bg-background border-r border-border"
      : "flex h-full w-full flex-col overflow-hidden"
  );
</script>

<aside class={sidebarClass} data-no-select>
  {#if isDrawer && onClose}
    <div class="flex items-center justify-between px-3 pt-2">
      <div class="flex items-center gap-2">
        <span class="text-lg">🐾</span>
        <span class="text-[13px] font-semibold text-foreground">PocketPaw</span>
      </div>
      <button
        onclick={onClose}
        class="flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground transition-colors active:bg-foreground/10"
      >
        <X class="h-4 w-4" />
      </button>
    </div>
  {/if}
  {#if !isDrawer}
    <SidebarHeader />
  {:else}
    <!-- In drawer mode, show New Chat button below the close row -->
    <SidebarHeader hideLogoRow />
  {/if}
  {#if isFilesMode}
    <SidebarExplorer />
  {:else}
    <SidebarSessions />
  {/if}
  <SidebarFooter />
</aside>

<script lang="ts">
  import type { Snippet } from "svelte";
  import { onMount } from "svelte";
  import { goto } from "$app/navigation";
  import TitleBar from "./titlebar/TitleBar.svelte";
  import MobileHeader from "./MobileHeader.svelte";
  import AppSidebar from "./AppSidebar.svelte";
  import * as Resizable from "$lib/components/ui/resizable/index.js";
  import { uiStore, platformStore, sessionStore } from "$lib/stores";

  let { children }: { children: Snippet } = $props();

  function handleBackdropClick() {
    uiStore.closeDrawer();
  }

  function handleToggleSidebar() {
    if (platformStore.isDesktop) {
      uiStore.toggleSidebar();
    } else {
      uiStore.toggleDrawer();
    }
  }

  // Global keyboard shortcuts
  onMount(() => {
    function handleKeydown(e: KeyboardEvent) {
      const mod = e.metaKey || e.ctrlKey;
      // Cmd/Ctrl+N — new chat
      if (mod && e.key === "n" && !e.shiftKey) {
        e.preventDefault();
        sessionStore.createNewSession();
        // Only navigate if on the files tab - otherwise stay on current page
        if (window.location.pathname === "/") {
          goto("/chat");
        }
      }
    }
    document.addEventListener("keydown", handleKeydown);
    return () => document.removeEventListener("keydown", handleKeydown);
  });


</script>

<!-- Body: sidebar + main content -->
<div class="relative flex flex-1 overflow-hidden">
  {#if platformStore.isDesktop && uiStore.sidebarOpen}
    <!-- Desktop: resizable sidebar + main -->
    <Resizable.PaneGroup direction="horizontal" autoSaveId="app-sidebar" class="h-full">
      <Resizable.Pane defaultSize={20} minSize={15} maxSize={35}>
        <AppSidebar />
      </Resizable.Pane>
      <Resizable.Handle />
      <Resizable.Pane defaultSize={80}>
        <main class="flex h-full flex-col overflow-hidden">
          {@render children()}
        </main>
      </Resizable.Pane>
    </Resizable.PaneGroup>
  {:else}
    <!-- Mobile/tablet drawer overlay -->
    {#if !platformStore.isDesktop && uiStore.sidebarDrawerOpen}
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <div
        class="absolute inset-0 z-40 bg-black/50"
        onclick={handleBackdropClick}
        onkeydown={(e) => { if (e.key === "Escape") uiStore.closeDrawer(); }}
      ></div>
      <div class="absolute inset-y-0 left-0 z-50 w-[280px] max-w-[80vw]">
        <AppSidebar isDrawer onClose={() => uiStore.closeDrawer()} />
      </div>
    {/if}

    <!-- Main content area (no sidebar or sidebar hidden) -->
    <main class="flex flex-1 flex-col overflow-hidden">
      {@render children()}
    </main>
  {/if}
</div>

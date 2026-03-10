<script lang="ts">
  import type { Snippet } from "svelte";
  import TitleBar from "./titlebar/TitleBar.svelte";
  import MobileHeader from "./MobileHeader.svelte";
  import AppSidebar from "./AppSidebar.svelte";
  import { uiStore, platformStore } from "$lib/stores";

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

  let outerClass = $derived.by(() => {
    let bg = platformStore.hasNativeBlur ? "bg-transparent" : "bg-background";
    let base = `flex h-dvh w-screen flex-col overflow-hidden ${bg}`;
    if (platformStore.hasWindowChrome) {
      base += " rounded-lg";
    }
    return base;
  });
</script>

<div class={outerClass}>
  <!-- Header: TitleBar on desktop platforms (has window chrome), MobileHeader on native mobile -->
  {#if platformStore.hasWindowChrome}
    <TitleBar onToggleSidebar={handleToggleSidebar} />
  {:else}
    <MobileHeader />
  {/if}

  <!-- Body: sidebar + main content -->
  <div class="relative flex flex-1 overflow-hidden">
    <!-- Desktop inline sidebar -->
    {#if platformStore.isDesktop && uiStore.sidebarOpen}
      <AppSidebar />
    {/if}

    <!-- Mobile/tablet drawer overlay -->
    {#if !platformStore.isDesktop && uiStore.sidebarDrawerOpen}
      <!-- Backdrop scrim -->
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <div
        class="absolute inset-0 z-40 bg-black/50"
        onclick={handleBackdropClick}
        onkeydown={(e) => { if (e.key === "Escape") uiStore.closeDrawer(); }}
      ></div>

      <!-- Drawer panel -->
      <div class="absolute inset-y-0 left-0 z-50 w-[280px] max-w-[80vw] shadow-xl">
        <AppSidebar isDrawer onClose={() => uiStore.closeDrawer()} />
      </div>
    {/if}

    <!-- Main content area -->
    <main class="flex flex-1 flex-col overflow-hidden">
      {@render children()}
    </main>
  </div>
</div>

<script lang="ts">
  import { explorerStore } from "$lib/stores";
  import ChevronLeft from "@lucide/svelte/icons/chevron-left";
  import ChevronRight from "@lucide/svelte/icons/chevron-right";
  import Home from "@lucide/svelte/icons/home";
  import RefreshCw from "@lucide/svelte/icons/refresh-cw";
  import MessageSquare from "@lucide/svelte/icons/message-square";
  import Grid3x3 from "@lucide/svelte/icons/grid-3x3";
  import List from "@lucide/svelte/icons/list";
  import ChevronDown from "@lucide/svelte/icons/chevron-down";
  import Search from "@lucide/svelte/icons/search";
  import X from "@lucide/svelte/icons/x";
  import { cn } from "$lib/utils";
  import { onMount } from "svelte";

  let sortMenuOpen = $state(false);
  let searchInputRef = $state<HTMLInputElement | null>(null);

  onMount(() => {
    function handleFocusSearch() {
      searchInputRef?.focus();
    }
    window.addEventListener("explorer:focus-search", handleFocusSearch);
    return () => window.removeEventListener("explorer:focus-search", handleFocusSearch);
  });
</script>

<div class="flex h-11 shrink-0 items-center gap-1 border-b border-border px-3">
  <!-- Navigation buttons -->
  <button
    type="button"
    class={cn(
      "rounded-md p-1.5 transition-colors",
      explorerStore.canGoBack ? "hover:bg-muted text-foreground" : "text-muted-foreground/40 cursor-not-allowed",
    )}
    disabled={!explorerStore.canGoBack}
    onclick={() => explorerStore.goBack()}
    title="Go back"
  >
    <ChevronLeft class="h-4 w-4" />
  </button>

  <button
    type="button"
    class={cn(
      "rounded-md p-1.5 transition-colors",
      explorerStore.canGoForward ? "hover:bg-muted text-foreground" : "text-muted-foreground/40 cursor-not-allowed",
    )}
    disabled={!explorerStore.canGoForward}
    onclick={() => explorerStore.goForward()}
    title="Go forward"
  >
    <ChevronRight class="h-4 w-4" />
  </button>

  <button
    type="button"
    class="rounded-md p-1.5 text-foreground transition-colors hover:bg-muted"
    onclick={() => explorerStore.goHome()}
    title="Home"
  >
    <Home class="h-4 w-4" />
  </button>

  <!-- Breadcrumbs -->
  <div class="mx-2 flex min-w-0 flex-1 items-center gap-0.5 overflow-hidden">
    {#if explorerStore.isHome}
      <span class="text-sm font-medium text-foreground">Home</span>
    {:else}
      {#each explorerStore.breadcrumbs as crumb, i}
        {#if i > 0}
          <span class="text-xs text-muted-foreground">/</span>
        {/if}
        <button
          type="button"
          class={cn(
            "shrink-0 truncate rounded px-1 py-0.5 text-sm transition-colors",
            i === explorerStore.breadcrumbs.length - 1
              ? "font-medium text-foreground"
              : "text-muted-foreground hover:text-foreground hover:bg-muted",
          )}
          onclick={() => explorerStore.navigateTo(crumb.path)}
        >
          {crumb.name}
        </button>
      {/each}
    {/if}
  </div>

  <!-- Search input -->
  {#if !explorerStore.isHome}
    <div class="relative flex items-center">
      <Search class="pointer-events-none absolute left-2 h-3.5 w-3.5 text-muted-foreground" />
      <input
        bind:this={searchInputRef}
        type="text"
        placeholder="Filter..."
        value={explorerStore.searchQuery}
        oninput={(e) => explorerStore.setSearchQuery(e.currentTarget.value)}
        class={cn(
          "h-7 w-40 rounded-md border border-border bg-background pl-7 pr-7 text-xs text-foreground outline-none transition-all placeholder:text-muted-foreground",
          "focus:w-56 focus:border-ring focus:ring-1 focus:ring-ring/50",
        )}
      />
      {#if explorerStore.searchQuery}
        <button
          type="button"
          class="absolute right-1.5 rounded-sm p-0.5 text-muted-foreground hover:text-foreground"
          onclick={() => explorerStore.setSearchQuery("")}
        >
          <X class="h-3 w-3" />
        </button>
      {/if}
    </div>
  {/if}

  <!-- Right side controls -->
  <button
    type="button"
    class="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
    onclick={() => explorerStore.refresh()}
    title="Refresh"
  >
    <RefreshCw class={cn("h-4 w-4", explorerStore.isLoading && "animate-spin")} />
  </button>

  <!-- Sort dropdown -->
  <div class="relative">
    <button
      type="button"
      class="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
      onclick={() => (sortMenuOpen = !sortMenuOpen)}
    >
      {explorerStore.sortBy}
      <ChevronDown class="h-3 w-3" />
    </button>
    {#if sortMenuOpen}
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <div
        class="fixed inset-0 z-40"
        onclick={() => (sortMenuOpen = false)}
      ></div>
      <div class="absolute right-0 top-full z-50 mt-1 min-w-28 rounded-md border border-border bg-popover p-1 shadow-lg">
        {#each ["name", "modified", "size", "type"] as field}
          <button
            type="button"
            class={cn(
              "flex w-full items-center rounded-md px-2 py-1 text-xs transition-colors hover:bg-muted",
              explorerStore.sortBy === field ? "text-primary" : "text-foreground",
            )}
            onclick={() => {
              explorerStore.setSortBy(field as "name" | "modified" | "size" | "type");
              sortMenuOpen = false;
            }}
          >
            {field.charAt(0).toUpperCase() + field.slice(1)}
            {#if explorerStore.sortBy === field}
              <span class="ml-auto text-xs">{explorerStore.sortAsc ? "\u2191" : "\u2193"}</span>
            {/if}
          </button>
        {/each}
      </div>
    {/if}
  </div>

  <!-- View mode -->
  <div class="flex items-center rounded-md border border-border">
    <button
      type="button"
      class={cn(
        "rounded-l-md p-1.5 transition-colors",
        explorerStore.viewMode === "icon" ? "bg-muted text-foreground" : "text-muted-foreground hover:text-foreground",
      )}
      onclick={() => explorerStore.setViewMode("icon")}
      title="Icon view"
    >
      <Grid3x3 class="h-3.5 w-3.5" />
    </button>
    <button
      type="button"
      class={cn(
        "rounded-r-md p-1.5 transition-colors",
        explorerStore.viewMode === "list" ? "bg-muted text-foreground" : "text-muted-foreground hover:text-foreground",
      )}
      onclick={() => explorerStore.setViewMode("list")}
      title="List view"
    >
      <List class="h-3.5 w-3.5" />
    </button>
  </div>

  <!-- Chat toggle -->
  <button
    type="button"
    class={cn(
      "rounded-md p-1.5 transition-colors",
      explorerStore.chatSidebarOpen
        ? "bg-primary/10 text-primary"
        : "text-muted-foreground hover:bg-muted hover:text-foreground",
    )}
    onclick={() => explorerStore.toggleChatSidebar()}
    title="Toggle chat sidebar"
  >
    <MessageSquare class="h-4 w-4" />
  </button>
</div>

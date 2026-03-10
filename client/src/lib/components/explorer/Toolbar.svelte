<script lang="ts">
  import { explorerStore } from "$lib/stores";
  import ChevronLeft from "@lucide/svelte/icons/chevron-left";
  import ChevronRight from "@lucide/svelte/icons/chevron-right";
  import Home from "@lucide/svelte/icons/home";
  import RefreshCw from "@lucide/svelte/icons/refresh-cw";
  import MessageSquare from "@lucide/svelte/icons/message-square";
  import Grid3x3 from "@lucide/svelte/icons/grid-3x3";
  import List from "@lucide/svelte/icons/list";
  import { cn } from "$lib/utils";
  import * as DropdownMenu from "$lib/components/ui/dropdown-menu";
  import AddressBar from "./AddressBar.svelte";
</script>

<div class="flex h-10 shrink-0 items-center gap-1 border-b border-border px-2">
  <!-- Nav buttons -->
  <div class="flex items-center gap-0.5">
    <button
      type="button"
      class={cn(
        "rounded-md p-1.5 transition-colors",
        explorerStore.canGoBack
          ? "hover:bg-muted text-foreground"
          : "text-muted-foreground/40 cursor-not-allowed",
      )}
      disabled={!explorerStore.canGoBack}
      onclick={() => explorerStore.goBack()}
      title="Go back (Alt+Left)"
    >
      <ChevronLeft class="h-4 w-4" />
    </button>

    <button
      type="button"
      class={cn(
        "rounded-md p-1.5 transition-colors",
        explorerStore.canGoForward
          ? "hover:bg-muted text-foreground"
          : "text-muted-foreground/40 cursor-not-allowed",
      )}
      disabled={!explorerStore.canGoForward}
      onclick={() => explorerStore.goForward()}
      title="Go forward (Alt+Right)"
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
  </div>

  <!-- Address bar (flex-1) -->
  <AddressBar />

  <!-- Right controls -->
  <div class="flex items-center gap-0.5">
    <button
      type="button"
      class="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
      onclick={() => explorerStore.refresh()}
      title="Refresh"
    >
      <RefreshCw class={cn("h-4 w-4", explorerStore.isLoading && "animate-spin")} />
    </button>

    <!-- Sort dropdown -->
    <DropdownMenu.Root>
      <DropdownMenu.Trigger>
        <button
          type="button"
          class="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          title="Sort by"
        >
          {explorerStore.sortBy}
          <span class="text-[10px]">{explorerStore.sortAsc ? "\u2191" : "\u2193"}</span>
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Content align="end" class="min-w-28">
        {#each ["name", "modified", "size", "type"] as field}
          <DropdownMenu.Item
            onclick={() => explorerStore.setSortBy(field as "name" | "modified" | "size" | "type")}
            class={cn(explorerStore.sortBy === field && "text-primary")}
          >
            {field.charAt(0).toUpperCase() + field.slice(1)}
            {#if explorerStore.sortBy === field}
              <span class="ml-auto text-xs">{explorerStore.sortAsc ? "\u2191" : "\u2193"}</span>
            {/if}
          </DropdownMenu.Item>
        {/each}
      </DropdownMenu.Content>
    </DropdownMenu.Root>

    <!-- View mode -->
    <div class="flex items-center rounded-md border border-border">
      <button
        type="button"
        class={cn(
          "rounded-l-md p-1.5 transition-colors",
          explorerStore.viewMode === "icon"
            ? "bg-muted text-foreground"
            : "text-muted-foreground hover:text-foreground",
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
          explorerStore.viewMode === "list"
            ? "bg-muted text-foreground"
            : "text-muted-foreground hover:text-foreground",
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
</div>

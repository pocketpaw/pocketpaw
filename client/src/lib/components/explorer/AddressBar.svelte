<script lang="ts">
  import { explorerStore } from "$lib/stores";
  import { cn } from "$lib/utils";
  import ChevronRight from "@lucide/svelte/icons/chevron-right";
  import { onMount } from "svelte";

  let editing = $state(false);
  let editValue = $state("");
  let inputRef = $state<HTMLInputElement | null>(null);

  onMount(() => {
    function handleFocusAddress(e: Event) {
      startEditing();
    }
    window.addEventListener("explorer:focus-address", handleFocusAddress);
    return () => window.removeEventListener("explorer:focus-address", handleFocusAddress);
  });

  function startEditing() {
    editValue = explorerStore.currentPath;
    editing = true;
    // Focus input on next tick
    requestAnimationFrame(() => {
      inputRef?.focus();
      inputRef?.select();
    });
  }

  function commitNavigation() {
    const path = editValue.trim();
    editing = false;
    if (path && path !== explorerStore.currentPath) {
      explorerStore.navigateTo(path);
    }
  }

  function cancelEditing() {
    editing = false;
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === "Enter") {
      e.preventDefault();
      commitNavigation();
    } else if (e.key === "Escape") {
      e.preventDefault();
      cancelEditing();
    }
  }
</script>

<div
  class={cn(
    "flex h-7 min-w-0 flex-1 items-center rounded-lg border border-border bg-muted/40 px-2 transition-colors",
    editing ? "ring-1 ring-ring/50 border-ring" : "hover:bg-muted/60 cursor-pointer",
  )}
  role="textbox"
  tabindex="-1"
>
  {#if editing}
    <input
      bind:this={inputRef}
      type="text"
      bind:value={editValue}
      onkeydown={handleKeydown}
      onblur={commitNavigation}
      class="h-7 w-full bg-transparent text-xs text-foreground outline-none placeholder:text-muted-foreground"
      placeholder="Enter path..."
    />
  {:else}
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div
      class="flex min-w-0 flex-1 items-center gap-0.5 overflow-hidden"
      onclick={startEditing}
    >
      {#if explorerStore.isHome}
        <span class="text-xs font-medium text-foreground">Home</span>
      {:else}
        {#each explorerStore.breadcrumbs as crumb, i}
          {#if i > 0}
            <ChevronRight class="h-3 w-3 shrink-0 text-muted-foreground/60" />
          {/if}
          <button
            type="button"
            class={cn(
              "shrink-0 truncate rounded px-1 py-0.5 text-xs transition-colors",
              i === explorerStore.breadcrumbs.length - 1
                ? "font-medium text-foreground"
                : "text-muted-foreground hover:text-foreground hover:bg-muted",
            )}
            onclick={(e) => { e.stopPropagation(); explorerStore.navigateTo(crumb.path); }}
          >
            {crumb.name}
          </button>
        {/each}
      {/if}
    </div>
  {/if}
</div>

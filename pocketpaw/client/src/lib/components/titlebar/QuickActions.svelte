<script lang="ts">
  import { Plus, Search, PanelRight } from "@lucide/svelte";
  import { sessionStore, uiStore, platformStore } from "$lib/stores";
  import * as Tooltip from "$lib/components/ui/tooltip";
  import NotificationBell from "./NotificationBell.svelte";

  function newChat() {
    sessionStore.createNewSession();
  }

  function openSearch() {
    uiStore.requestSearchFocus();
  }

  async function toggleSidePanel() {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("toggle_side_panel");
    } catch {
      // Not in Tauri
    }
  }
</script>

<div class="flex items-center gap-0.5">
  <Tooltip.Root>
    <Tooltip.Trigger>
      <button
        onclick={newChat}
        class={platformStore.isTouch
          ? "flex h-10 w-10 items-center justify-center rounded-md text-muted-foreground transition-colors duration-100 hover:bg-foreground/10 hover:text-foreground"
          : "flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors duration-100 hover:bg-foreground/10 hover:text-foreground"}
      >
        <Plus class={platformStore.isTouch ? "h-5 w-5" : "h-3.5 w-3.5"} strokeWidth={2} />
      </button>
    </Tooltip.Trigger>
    <Tooltip.Content>
      <p>New Chat <kbd class="ml-1 text-[10px] text-muted-foreground">⌘N</kbd></p>
    </Tooltip.Content>
  </Tooltip.Root>

  <Tooltip.Root>
    <Tooltip.Trigger>
      <button
        onclick={openSearch}
        class={platformStore.isTouch
          ? "flex h-10 w-10 items-center justify-center rounded-md text-muted-foreground transition-colors duration-100 hover:bg-foreground/10 hover:text-foreground"
          : "flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors duration-100 hover:bg-foreground/10 hover:text-foreground"}
      >
        <Search class={platformStore.isTouch ? "h-5 w-5" : "h-3.5 w-3.5"} strokeWidth={2} />
      </button>
    </Tooltip.Trigger>
    <Tooltip.Content>
      <p>Search <kbd class="ml-1 text-[10px] text-muted-foreground">⌘K</kbd></p>
    </Tooltip.Content>
  </Tooltip.Root>

  <NotificationBell />

  <Tooltip.Root>
    <Tooltip.Trigger>
      <button
        onclick={toggleSidePanel}
        class={platformStore.isTouch
          ? "flex h-10 w-10 items-center justify-center rounded-md text-muted-foreground transition-colors duration-100 hover:bg-foreground/10 hover:text-foreground"
          : "flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors duration-100 hover:bg-foreground/10 hover:text-foreground"}
      >
        <PanelRight class={platformStore.isTouch ? "h-5 w-5" : "h-3.5 w-3.5"} strokeWidth={2} />
      </button>
    </Tooltip.Trigger>
    <Tooltip.Content>
      <p>Side Panel <kbd class="ml-1 text-[10px] text-muted-foreground">⌘⇧L</kbd></p>
    </Tooltip.Content>
  </Tooltip.Root>
</div>

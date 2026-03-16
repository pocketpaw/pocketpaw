<script lang="ts">
  import { Button } from "$lib/components/ui/button";
  import { Plus } from "@lucide/svelte";
  import { goto } from "$app/navigation";
  import { page } from "$app/state";
  import { sessionStore, platformStore } from "$lib/stores";

  let { hideLogoRow = false }: { hideLogoRow?: boolean } = $props();

  let isFilesMode = $derived(page.url.pathname === "/");

  function newChat() {
    sessionStore.createNewSession();
    // Only navigate if on the files tab - otherwise stay on current page
    if (page.url.pathname === "/") {
      goto("/chat");
    }
  }

  let btnClass = $derived(
    platformStore.isTouch
      ? "w-full justify-start gap-2 text-[13px] touch-target"
      : "w-full justify-start gap-2 text-[12px]"
  );
</script>

<div class="flex flex-col gap-2 px-3 py-3">
  {#if !hideLogoRow}
    <!-- Logo + brand -->
    <div class="flex items-center gap-2">
      <span class="text-lg">🐾</span>
      <span class="text-[13px] font-semibold text-foreground">PocketPaw</span>
    </div>
  {/if}

  {#if !isFilesMode}
    <!-- New chat button -->
    <Button
      onclick={newChat}
      variant="outline"
      size="sm"
      class={btnClass}
    >
      <Plus class="h-3.5 w-3.5" strokeWidth={2} />
      New Chat
    </Button>
  {/if}
</div>

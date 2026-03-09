<script lang="ts">
  import { Menu, Plus } from "@lucide/svelte";
  import { uiStore, sessionStore, platformStore } from "$lib/stores";
  import ConnectionBadge from "./titlebar/ConnectionBadge.svelte";
  import AgentProgressBar from "./titlebar/AgentProgressBar.svelte";

  let sessionTitle = $derived(sessionStore.activeSession?.title ?? "New Chat");
</script>

<header
  class="relative flex w-full shrink-0 items-center border-b border-border/50 px-3 safe-x"
  class:safe-top={platformStore.isNativeMobile}
  style="height: 48px;"
>
  <!-- Left: hamburger -->
  <button
    onclick={() => uiStore.openDrawer()}
    class="flex h-10 w-10 items-center justify-center rounded-md text-muted-foreground transition-colors active:bg-foreground/10"
  >
    <Menu class="h-5 w-5" strokeWidth={1.75} />
  </button>

  <!-- Center: session title -->
  <div class="flex min-w-0 flex-1 items-center justify-center px-2">
    <span class="truncate text-sm font-medium text-foreground">{sessionTitle}</span>
  </div>

  <!-- Right: new chat + connection -->
  <div class="flex items-center gap-1">
    <button
      onclick={() => sessionStore.createNewSession()}
      class="flex h-10 w-10 items-center justify-center rounded-md text-muted-foreground transition-colors active:bg-foreground/10"
    >
      <Plus class="h-5 w-5" strokeWidth={1.75} />
    </button>
    <ConnectionBadge />
  </div>

  <AgentProgressBar />
</header>

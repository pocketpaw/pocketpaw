<script lang="ts">
  import RefreshCw from "@lucide/svelte/icons/refresh-cw";
  import ExternalLink from "@lucide/svelte/icons/external-link";
  import X from "@lucide/svelte/icons/x";
  import Monitor from "@lucide/svelte/icons/monitor";
  import Tablet from "@lucide/svelte/icons/tablet";
  import Smartphone from "@lucide/svelte/icons/smartphone";
  import { cn } from "$lib/utils";
  import { explorerStore } from "$lib/stores";

  let {
    url = "http://localhost:3000",
  }: {
    url?: string;
  } = $props();

  let inputUrl = $state("");
  let currentUrl = $state("");

  // Sync from prop
  $effect(() => {
    inputUrl = url;
    currentUrl = url;
  });
  let iframeKey = $state(0);
  let responsiveMode = $state<"desktop" | "tablet" | "mobile">("desktop");

  let iframeWidth = $derived(
    responsiveMode === "mobile" ? "375px"
      : responsiveMode === "tablet" ? "768px"
      : "100%"
  );

  function navigate() {
    let target = inputUrl.trim();
    if (!target) return;
    if (!target.startsWith("http://") && !target.startsWith("https://")) {
      target = "http://" + target;
      inputUrl = target;
    }
    currentUrl = target;
    iframeKey++;
  }

  function refresh() {
    iframeKey++;
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === "Enter") navigate();
  }

  async function openExternal() {
    try {
      const { openUrl } = await import("@tauri-apps/plugin-opener");
      await openUrl(currentUrl);
    } catch {
      window.open(currentUrl, "_blank");
    }
  }

  function close() {
    explorerStore.closeWebPreview();
  }
</script>

<div class="flex h-full flex-col">
  <!-- URL bar -->
  <div class="flex items-center gap-2 border-b border-border/50 px-3 py-1.5">
    <button
      type="button"
      class="rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
      onclick={refresh}
      title="Refresh"
    >
      <RefreshCw class="h-4 w-4" />
    </button>

    <input
      type="text"
      bind:value={inputUrl}
      onkeydown={handleKeydown}
      class="flex-1 rounded-md border border-border/50 bg-muted/30 px-3 py-1 text-xs text-foreground placeholder:text-muted-foreground focus:border-primary/50 focus:outline-none"
      placeholder="http://localhost:3000"
    />

    <button
      type="button"
      class="rounded-md bg-primary/10 px-3 py-1 text-xs text-primary hover:bg-primary/20"
      onclick={navigate}
    >
      Go
    </button>

    <div class="mx-1 h-4 w-px bg-border/50"></div>

    <!-- Responsive mode buttons -->
    <button
      type="button"
      class={cn("rounded-md p-1", responsiveMode === "desktop" ? "bg-muted text-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground")}
      onclick={() => (responsiveMode = "desktop")}
      title="Desktop (100%)"
    >
      <Monitor class="h-4 w-4" />
    </button>
    <button
      type="button"
      class={cn("rounded-md p-1", responsiveMode === "tablet" ? "bg-muted text-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground")}
      onclick={() => (responsiveMode = "tablet")}
      title="Tablet (768px)"
    >
      <Tablet class="h-4 w-4" />
    </button>
    <button
      type="button"
      class={cn("rounded-md p-1", responsiveMode === "mobile" ? "bg-muted text-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground")}
      onclick={() => (responsiveMode = "mobile")}
      title="Mobile (375px)"
    >
      <Smartphone class="h-4 w-4" />
    </button>

    <div class="mx-1 h-4 w-px bg-border/50"></div>

    <button
      type="button"
      class="rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
      onclick={openExternal}
      title="Open in browser"
    >
      <ExternalLink class="h-4 w-4" />
    </button>
    <button
      type="button"
      class="rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
      onclick={close}
      title="Close preview"
    >
      <X class="h-4 w-4" />
    </button>
  </div>

  <!-- iframe -->
  <div class="flex flex-1 items-start justify-center overflow-auto bg-muted/20 p-2">
    {#key iframeKey}
      <iframe
        src={currentUrl}
        title="Web preview"
        sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
        style:width={iframeWidth}
        class="h-full rounded border border-border/30 bg-white"
      ></iframe>
    {/key}
  </div>
</div>

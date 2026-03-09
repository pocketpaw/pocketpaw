<script lang="ts">
  import type { PanelConfig } from "$lib/types/pawkit";
  import { connectionStore } from "$lib/stores";
  import { RefreshCw } from "@lucide/svelte";

  let {
    config,
    data,
  }: { config: PanelConfig; data: unknown } = $props();

  let summary = $state<string>("");
  let isLoading = $state(true);
  let error = $state<string | null>(null);

  async function loadStandup() {
    isLoading = true;
    error = null;
    try {
      const client = connectionStore.getClient();
      const res = await client.mcGetStandup();
      summary = res.summary ?? "";
    } catch (e) {
      console.error("[StandupPanel] Failed to load standup:", e);
      error = "Failed to load standup summary";
    } finally {
      isLoading = false;
    }
  }

  // Load on mount
  $effect(() => {
    loadStandup();
  });
</script>

<div class="rounded-lg border border-border/50 p-4">
  <div class="mb-3 flex items-center justify-between">
    <span class="text-xs font-medium text-muted-foreground">Daily Standup</span>
    <button
      class="flex h-5 w-5 items-center justify-center rounded text-muted-foreground transition-colors hover:text-foreground"
      title="Refresh"
      onclick={loadStandup}
      disabled={isLoading}
    >
      <RefreshCw class="h-3 w-3 {isLoading ? 'animate-spin' : ''}" />
    </button>
  </div>

  {#if isLoading && !summary}
    <div class="flex items-center justify-center py-6">
      <div class="h-5 w-5 animate-spin rounded-full border-2 border-foreground/20 border-t-foreground"></div>
    </div>
  {:else if error && !summary}
    <p class="text-xs text-muted-foreground">{error}</p>
  {:else if summary}
    <div class="prose prose-sm max-w-none text-sm text-foreground/80 whitespace-pre-wrap">
      {summary}
    </div>
  {:else}
    <p class="text-xs text-muted-foreground">No standup summary available yet.</p>
  {/if}
</div>

<script lang="ts">
  import { goto } from "$app/navigation";
  import { activityStore, settingsStore } from "$lib/stores";
  import { ArrowLeft, Trash2 } from "@lucide/svelte";
  import ActivityEntryComponent from "./ActivityEntry.svelte";

  let entries = $derived(activityStore.entries);
  let model = $derived(settingsStore.model || "unknown");
  let tokenUsage = $derived(activityStore.tokenUsage);

  let totalTokens = $derived.by(() => {
    if (!tokenUsage) return null;
    const inp = tokenUsage.input ?? 0;
    const out = tokenUsage.output ?? 0;
    return inp + out;
  });

  function clearLog() {
    activityStore.clear();
  }
</script>

<div class="flex h-full flex-col gap-4 overflow-y-auto px-4 py-4 md:px-6 md:py-6">
  <!-- Header -->
  <div class="flex items-center gap-3">
    <button
      onclick={() => goto("/")}
      class="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
    >
      <ArrowLeft class="h-4 w-4" />
    </button>
    <div>
      <h1 class="text-lg font-semibold text-foreground">Activity Log</h1>
      <p class="text-xs text-muted-foreground">Full activity feed for the current session.</p>
    </div>
    {#if entries.length > 0}
      <button
        onclick={clearLog}
        class="ml-auto flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
      >
        <Trash2 class="h-3 w-3" />
        Clear
      </button>
    {/if}
  </div>

  <!-- Content -->
  <div class="flex flex-col gap-4">
    {#if entries.length === 0}
      <p class="py-8 text-center text-sm text-muted-foreground">
        No activity recorded yet. Activity appears when the agent processes requests.
      </p>
    {:else}
      <!-- Summary -->
      <div class="flex items-center gap-4 rounded-lg border border-border/40 bg-muted/20 px-3 py-2 text-[11px] text-muted-foreground">
        <span>{entries.length} entries</span>
        <span>Model: {model}</span>
        {#if totalTokens !== null}
          <span>{totalTokens.toLocaleString()} tokens</span>
        {/if}
      </div>

      <!-- All entries -->
      <div class="flex flex-col">
        {#each entries as entry (entry.id)}
          <ActivityEntryComponent {entry} />
        {/each}
      </div>
    {/if}
  </div>
</div>

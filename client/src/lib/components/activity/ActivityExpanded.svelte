<script lang="ts">
  import { activityStore, settingsStore, platformStore } from "$lib/stores";
  import { ChevronUp } from "@lucide/svelte";
  import ActivityEntryComponent from "./ActivityEntry.svelte";

  let { onCollapse }: { onCollapse: () => void } = $props();

  let entries = $derived(activityStore.recentEntries);
  let isWorking = $derived(activityStore.isAgentWorking);
  let tokenUsage = $derived(activityStore.tokenUsage);
  let model = $derived(settingsStore.model || "unknown");

  let scrollEl: HTMLDivElement | undefined = $state();

  // Auto-scroll to bottom on new entries
  $effect(() => {
    entries.length;
    if (scrollEl) {
      queueMicrotask(() => {
        scrollEl?.scrollTo({ top: scrollEl.scrollHeight });
      });
    }
  });

  let totalTokens = $derived.by(() => {
    if (!tokenUsage) return null;
    const inp = tokenUsage.input ?? 0;
    const out = tokenUsage.output ?? 0;
    return inp + out;
  });
</script>

<div class="flex flex-col rounded-lg border border-border/40 bg-muted/20">
  <!-- Header -->
  <div class="flex items-center justify-between border-b border-border/30 px-3 py-2">
    <span class="text-xs font-medium text-foreground/80">Activity</span>
    <button
      onclick={onCollapse}
      class="rounded p-0.5 text-muted-foreground transition-colors hover:text-foreground"
    >
      <ChevronUp class="h-3.5 w-3.5" />
    </button>
  </div>

  <!-- Entries list -->
  <div
    bind:this={scrollEl}
    class={platformStore.isMobile ? "max-h-64 overflow-y-auto" : "max-h-48 overflow-y-auto"}
  >
    {#if entries.length === 0}
      <p class="px-3 py-3 text-center text-[11px] text-muted-foreground/60">
        No activity yet
      </p>
    {:else}
      {#each entries as entry (entry.id)}
        <ActivityEntryComponent {entry} />
      {/each}

      {#if isWorking}
        <div class="flex items-center gap-2 px-2 py-1.5">
          <div class="flex gap-0.5">
            <div class="h-1 w-1 animate-pulse rounded-full bg-paw-accent" style="animation-delay: 0ms"></div>
            <div class="h-1 w-1 animate-pulse rounded-full bg-paw-accent" style="animation-delay: 150ms"></div>
            <div class="h-1 w-1 animate-pulse rounded-full bg-paw-accent" style="animation-delay: 300ms"></div>
          </div>
          <span class="text-[10px] text-muted-foreground">Processing...</span>
        </div>
      {/if}
    {/if}
  </div>

  <!-- Footer summary -->
  <div class={platformStore.isTouch
    ? "flex items-center gap-3 border-t border-border/30 px-3 py-2 text-[11px] text-muted-foreground"
    : "flex items-center gap-3 border-t border-border/30 px-3 py-2 text-[10px] text-muted-foreground"}>
    <span>Model: {model}</span>
    {#if totalTokens !== null}
      <span>{totalTokens.toLocaleString()} tokens used</span>
    {/if}
  </div>
</div>

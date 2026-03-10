<script lang="ts">
  import type { ChannelStatusMap, ChannelInfo } from "$lib/api";
  import { connectionStore } from "$lib/stores";
  import { Radio, RefreshCw } from "@lucide/svelte";
  import { Skeleton } from "$lib/components/ui/skeleton";
  import ChannelConfigCard from "./ChannelConfigCard.svelte";
  import { CHANNEL_ORDER } from "./channel-schema";

  let channels = $state<ChannelStatusMap>({});
  let initialLoad = $state(true);
  let refreshing = $state(false);

  const defaultInfo: ChannelInfo = { configured: false, running: false, autostart: false };

  // Grouped channel lists preserving CHANNEL_ORDER within each group
  let running = $derived.by(() => {
    return CHANNEL_ORDER
      .filter((ch) => channels[ch]?.running)
      .map((ch): [string, ChannelInfo] => [ch, channels[ch]]);
  });

  let configured = $derived.by(() => {
    return CHANNEL_ORDER
      .filter((ch) => channels[ch]?.configured && !channels[ch]?.running)
      .map((ch): [string, ChannelInfo] => [ch, channels[ch]]);
  });

  let available = $derived.by(() => {
    return CHANNEL_ORDER
      .filter((ch) => !channels[ch]?.configured)
      .map((ch): [string, ChannelInfo] => [ch, channels[ch] ?? defaultInfo]);
  });

  // Stats
  let runningCount = $derived(running.length);
  let configuredCount = $derived(running.length + configured.length);
  let availableCount = $derived(available.length);

  $effect(() => {
    loadChannels();
  });

  async function loadChannels() {
    try {
      const client = connectionStore.getClient();
      channels = await client.getChannelStatus();
    } catch {
      // Backend unavailable
    } finally {
      initialLoad = false;
    }
  }

  async function handleRefresh() {
    refreshing = true;
    await loadChannels();
    refreshing = false;
  }
</script>

<div class="flex flex-col gap-6">
  <!-- Header -->
  <div class="flex items-center justify-between">
    <div class="flex items-center gap-2">
      <Radio class="h-4 w-4 text-muted-foreground" strokeWidth={1.75} />
      <h3 class="text-sm font-semibold text-foreground">Channels</h3>
    </div>
    {#if !initialLoad}
      <button
        onclick={handleRefresh}
        disabled={refreshing}
        class="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:opacity-40"
        title="Refresh channels"
      >
        <RefreshCw class={`h-3.5 w-3.5 ${refreshing ? "animate-spin" : ""}`} strokeWidth={1.75} />
      </button>
    {/if}
  </div>

  {#if initialLoad}
    <!-- Skeleton loading -->
    <div class="grid grid-cols-2 gap-3">
      {#each Array(4) as _}
        <div class="rounded-lg border border-border/60 bg-muted/30 p-4">
          <div class="flex items-start gap-3">
            <Skeleton class="h-8 w-8 rounded-lg" />
            <div class="flex flex-1 flex-col gap-2">
              <Skeleton class="h-4 w-24 rounded" />
              <Skeleton class="h-3 w-full rounded" />
              <Skeleton class="h-3 w-16 rounded" />
            </div>
          </div>
        </div>
      {/each}
    </div>
  {:else}
    <!-- Stats Summary Bar -->
    <div class="flex items-center gap-4 text-[11px] text-muted-foreground">
      <div class="flex items-center gap-1.5">
        <div class="h-1.5 w-1.5 rounded-full bg-paw-success"></div>
        <span>{runningCount} running</span>
      </div>
      <div class="flex items-center gap-1.5">
        <div class="h-1.5 w-1.5 rounded-full bg-muted-foreground"></div>
        <span>{configuredCount} configured</span>
      </div>
      <div class="flex items-center gap-1.5">
        <div class="h-1.5 w-1.5 rounded-full bg-muted-foreground/40"></div>
        <span>{availableCount} available</span>
      </div>
    </div>

    <!-- RUNNING section -->
    {#if running.length > 0}
      <div class="flex flex-col gap-2">
        <span class="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
          Running
        </span>
        <div class="grid grid-cols-2 gap-3">
          {#each running as [name, info] (name)}
            <ChannelConfigCard channel={name} {info} onRefresh={loadChannels} />
          {/each}
        </div>
      </div>
    {/if}

    <!-- CONFIGURED section -->
    {#if configured.length > 0}
      <div class="flex flex-col gap-2">
        <span class="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
          Configured
        </span>
        <div class="grid grid-cols-2 gap-3">
          {#each configured as [name, info] (name)}
            <ChannelConfigCard channel={name} {info} onRefresh={loadChannels} />
          {/each}
        </div>
      </div>
    {/if}

    <!-- AVAILABLE section -->
    {#if available.length > 0}
      <div class="flex flex-col gap-2">
        <span class="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
          Available
        </span>
        <div class="grid grid-cols-2 gap-3">
          {#each available as [name, info] (name)}
            <ChannelConfigCard channel={name} {info} onRefresh={loadChannels} />
          {/each}
        </div>
      </div>
    {/if}

    {#if running.length === 0 && configured.length === 0 && available.length === 0}
      <p class="py-4 text-sm text-muted-foreground">
        No channels available. Make sure the backend is running.
      </p>
    {/if}
  {/if}
</div>

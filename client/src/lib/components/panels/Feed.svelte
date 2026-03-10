<script lang="ts">
  import type { PanelConfig } from "$lib/types/pawkit";

  let { config, data }: { config: PanelConfig; data: unknown } = $props();

  let maxItems = $derived((config.max_items as number) ?? 20);

  interface FeedItem {
    message: string;
    type?: string;
    created_at?: string;
    agent_id?: string;
  }

  let items = $derived.by<FeedItem[]>(() => {
    if (!Array.isArray(data)) return [];
    return (data as FeedItem[]).slice(0, maxItems);
  });

  function relativeTime(iso: string | undefined): string {
    if (!iso) return "";
    const diff = Date.now() - new Date(iso).getTime();
    const mins = Math.floor(diff / 60_000);
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    return `${days}d ago`;
  }
</script>

<div class="flex flex-col gap-1">
  {#each items as item, i (i)}
    <div class="flex items-start gap-2 rounded-md px-2 py-1.5 text-sm">
      <span class="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-foreground/40"></span>
      <span class="flex-1 text-xs text-foreground/80">{item.message}</span>
      <span class="shrink-0 text-[10px] text-muted-foreground">{relativeTime(item.created_at)}</span>
    </div>
  {/each}
  {#if items.length === 0}
    <p class="py-4 text-center text-xs text-muted-foreground">No activity yet</p>
  {/if}
</div>

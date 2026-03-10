<script lang="ts">
  import { activityStore, platformStore } from "$lib/stores";

  let { onExpand }: { onExpand: () => void } = $props();

  let latest = $derived(activityStore.latestEntry);
  let isWorking = $derived(activityStore.isAgentWorking);

  let displayText = $derived.by(() => {
    if (!latest) return "Working...";
    const text = latest.content;
    return text.length > 60 ? text.slice(0, 60) + "..." : text;
  });
</script>

<button
  onclick={onExpand}
  class={platformStore.isTouch
    ? "flex w-full items-center gap-2 rounded-lg border border-border/40 bg-muted/20 px-3 py-3 text-left transition-colors active:bg-muted/40"
    : "flex w-full items-center gap-2 rounded-lg border border-border/40 bg-muted/20 px-3 py-2 text-left transition-colors hover:bg-muted/40"}
>
  <span
    class="h-2 w-2 shrink-0 rounded-full bg-paw-accent"
    class:animate-pulse={isWorking}
  ></span>
  <span class="min-w-0 flex-1 truncate text-xs text-muted-foreground">
    {displayText}
  </span>
  <span class="shrink-0 text-[10px] text-muted-foreground/50">
    click to expand
  </span>
</button>

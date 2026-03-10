<script lang="ts">
  import { explorerStore } from "$lib/stores";

  function formatSize(bytes: number): string {
    if (bytes === 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    const k = 1024;
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    const value = bytes / Math.pow(k, i);
    return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[i]}`;
  }

  const displayPath = $derived.by(() => {
    const p = explorerStore.currentPath;
    if (!p) return "";
    return p.replace(/\\/g, "/");
  });
</script>

<div class="flex h-7 shrink-0 items-center justify-between border-t border-border px-3 text-[11px] text-muted-foreground">
  <div class="flex items-center gap-2">
    <span>{explorerStore.totalItemCount} item{explorerStore.totalItemCount !== 1 ? "s" : ""}</span>
    {#if explorerStore.selectionCount > 0}
      <span class="h-3 w-px bg-border"></span>
      <span>
        {explorerStore.selectionCount} selected
        {#if explorerStore.selectedSize > 0}
          &middot; {formatSize(explorerStore.selectedSize)}
        {/if}
      </span>
    {/if}
  </div>

  {#if displayPath}
    <span class="min-w-0 truncate pl-4 text-right" title={displayPath}>
      {displayPath}
    </span>
  {/if}
</div>

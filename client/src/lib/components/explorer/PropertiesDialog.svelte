<script lang="ts">
  import { localFs } from "$lib/filesystem";
  import type { FileStatExtended } from "$lib/filesystem";
  import FileTypeIcon from "./FileTypeIcon.svelte";
  import X from "@lucide/svelte/icons/x";
  import Copy from "@lucide/svelte/icons/copy";
  import Check from "@lucide/svelte/icons/check";
  import Loader2 from "@lucide/svelte/icons/loader-2";
  import { onMount } from "svelte";

  let {
    filePath,
    onClose,
  }: {
    filePath: string;
    onClose: () => void;
  } = $props();

  let stat = $state<FileStatExtended | null>(null);
  let loading = $state(true);
  let error = $state<string | null>(null);
  let copiedPath = $state(false);

  onMount(() => {
    localFs.statExtended(filePath).then(
      (s) => { stat = s; loading = false; },
      (e) => { error = String(e); loading = false; },
    );

    function handleKeydown(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", handleKeydown);
    return () => document.removeEventListener("keydown", handleKeydown);
  });

  function formatSize(bytes: number): string {
    if (bytes === 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    return `${(bytes / Math.pow(1024, i)).toFixed(i > 0 ? 1 : 0)} ${units[i]} (${bytes.toLocaleString()} bytes)`;
  }

  function formatDate(timestamp: number): string {
    if (!timestamp) return "\u2014";
    return new Date(timestamp * 1000).toLocaleString(undefined, {
      year: "numeric",
      month: "long",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      second: "2-digit",
    });
  }

  async function copyPath() {
    try {
      await navigator.clipboard.writeText(filePath);
      copiedPath = true;
      setTimeout(() => { copiedPath = false; }, 2000);
    } catch {
      // noop
    }
  }
</script>

<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
  class="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
  onclick={(e) => { if (e.target === e.currentTarget) onClose(); }}
>
  <div class="w-full max-w-md rounded-xl border border-border bg-popover p-6 shadow-2xl">
    {#if loading}
      <div class="flex items-center justify-center py-8">
        <Loader2 class="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    {:else if error}
      <div class="text-center text-sm text-red-400">{error}</div>
    {:else if stat}
      <!-- Header -->
      <div class="mb-4 flex items-center gap-3">
        <FileTypeIcon extension={stat.extension} isDir={stat.isDir} size={32} />
        <div class="min-w-0 flex-1">
          <h3 class="truncate text-base font-semibold text-foreground">{stat.name}</h3>
          <p class="truncate text-xs text-muted-foreground">
            {stat.isDir ? "Folder" : stat.extension ? `.${stat.extension} file` : "File"}
          </p>
        </div>
        <button
          type="button"
          class="rounded-md p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground"
          onclick={onClose}
        >
          <X class="h-4 w-4" />
        </button>
      </div>

      <div class="h-px bg-border"></div>

      <!-- Properties grid -->
      <div class="mt-4 grid grid-cols-[auto_1fr] gap-x-4 gap-y-2.5 text-sm">
        <span class="text-muted-foreground">Location</span>
        <div class="flex min-w-0 items-center gap-1">
          <span class="truncate text-foreground" title={stat.path}>{stat.path}</span>
          <button
            type="button"
            class="shrink-0 rounded p-0.5 text-muted-foreground hover:text-foreground"
            onclick={copyPath}
            title="Copy path"
          >
            {#if copiedPath}
              <Check class="h-3 w-3 text-green-400" />
            {:else}
              <Copy class="h-3 w-3" />
            {/if}
          </button>
        </div>

        {#if !stat.isDir}
          <span class="text-muted-foreground">Size</span>
          <span class="text-foreground">{formatSize(stat.size)}</span>
        {/if}

        <span class="text-muted-foreground">Created</span>
        <span class="text-foreground">{formatDate(stat.created)}</span>

        <span class="text-muted-foreground">Modified</span>
        <span class="text-foreground">{formatDate(stat.modified)}</span>

        {#if stat.extension}
          <span class="text-muted-foreground">Type</span>
          <span class="text-foreground">.{stat.extension}</span>
        {/if}

        <span class="text-muted-foreground">Read-only</span>
        <span class="text-foreground">{stat.readonly ? "Yes" : "No"}</span>

        <span class="text-muted-foreground">Symlink</span>
        <span class="text-foreground">{stat.isSymlink ? "Yes" : "No"}</span>
      </div>
    {/if}
  </div>
</div>

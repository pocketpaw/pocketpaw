<script lang="ts">
  import { explorerStore } from "$lib/stores";
  import { connectionStore } from "$lib/stores/connection.svelte";
  import type { RecentFileEntry } from "$lib/api/types";
  import FileTypeIcon from "./FileTypeIcon.svelte";
  import StyledFileIcon from "./StyledFileIcon.svelte";
  import Folder from "@lucide/svelte/icons/folder";
  import PinOff from "@lucide/svelte/icons/pin-off";
  import Server from "@lucide/svelte/icons/server";
  import Clock from "@lucide/svelte/icons/clock";
  import { cn } from "$lib/utils";
  import { onMount } from "svelte";

  interface QuickFolder {
    name: string;
    path: string;
    source: string;
  }

  interface CloudProvider {
    name: string;
    logo: string;
  }

  const cloudProviders: CloudProvider[] = [
    { name: "Google Drive", logo: "https://cdn.iconscout.com/icon/free/png-256/free-google-drive-logo-icon-svg-download-png-2476481.png?f=webp" },
    { name: "Dropbox", logo: "https://cdn.freebiesupply.com/logos/large/2x/dropbox-2-logo-png-transparent.png" },
    { name: "OneDrive", logo: "https://upload.wikimedia.org/wikipedia/commons/thumb/5/59/Microsoft_Office_OneDrive_%282019%E2%80%932025%29.svg/1280px-Microsoft_Office_OneDrive_%282019%E2%80%932025%29.svg.png" },
    { name: "iCloud", logo: "https://upload.wikimedia.org/wikipedia/commons/thumb/1/1c/ICloud_logo.svg/3840px-ICloud_logo.svg.png" },
    { name: "Amazon S3", logo: "https://upload.wikimedia.org/wikipedia/commons/thumb/b/bc/Amazon-S3-Logo.svg/1280px-Amazon-S3-Logo.svg.png" },
  ];

  let recentFiles = $state<RecentFileEntry[]>([]);

  let localFolders = $derived.by((): QuickFolder[] => {
    const dirs = explorerStore.defaultDirs;
    if (!dirs) return [];
    const result: QuickFolder[] = [];
    if (dirs.documents) result.push({ name: "Documents", path: dirs.documents, source: "local" });
    if (dirs.downloads) result.push({ name: "Downloads", path: dirs.downloads, source: "local" });
    if (dirs.desktop) result.push({ name: "Desktop", path: dirs.desktop, source: "local" });
    if (dirs.home) result.push({ name: "Home", path: dirs.home, source: "local" });
    return result;
  });

  function formatRelativeTime(timestamp: number): string {
    if (!timestamp) return "";
    const now = Date.now() / 1000;
    const diff = now - timestamp;
    if (diff < 60) return "Just now";
    const mins = Math.floor(diff / 60);
    if (diff < 3600) return `${mins} ${mins === 1 ? "minute" : "minutes"} ago`;
    const hrs = Math.floor(diff / 3600);
    if (diff < 86400) return `${hrs} ${hrs === 1 ? "hour" : "hours"} ago`;
    const days = Math.floor(diff / 86400);
    if (diff < 604800) return `${days} ${days === 1 ? "day" : "days"} ago`;
    const weeks = Math.floor(diff / 604800);
    if (diff < 2592000) return `${weeks} ${weeks === 1 ? "week" : "weeks"} ago`;
    return new Date(timestamp * 1000).toLocaleDateString();
  }

  onMount(async () => {
    try {
      const client = connectionStore.getClient();
      recentFiles = await client.getRecentFiles(15);
    } catch {
      // Backend may not support this yet
    }
  });

  function openRecent(entry: RecentFileEntry) {
    if (entry.is_dir) {
      explorerStore.navigateTo(entry.path);
    } else {
      explorerStore.openFileByPath(entry.path);
    }
  }
</script>

<div class="space-y-6 overflow-y-auto p-5">
  <!-- Recents -->
  {#if recentFiles.length > 0}
    <section>
      <h2 class="mb-2.5 text-[11px] font-medium uppercase tracking-wider text-muted-foreground/70">
        Recent
      </h2>
      <div class="grid grid-cols-[repeat(auto-fill,minmax(200px,1fr))] gap-2">
        {#each recentFiles as entry}
          <button
            type="button"
            class="flex items-center gap-2.5 rounded-lg border border-border/50 bg-card/30 px-3 py-2.5 text-left transition-colors hover:bg-muted/50"
            onclick={() => openRecent(entry)}
          >
            <div class="flex h-5 w-5 shrink-0 items-center justify-center">
              <StyledFileIcon extension={entry.extension} isDir={entry.is_dir} size={20} />
            </div>
            <div class="min-w-0 flex-1">
              <p class="truncate text-sm text-foreground">{entry.name}</p>
              <p class="truncate text-[11px] text-muted-foreground/60">{formatRelativeTime(entry.timestamp)}</p>
            </div>
          </button>
        {/each}
      </div>
    </section>
  {/if}

  <!-- Pinned -->
  {#if explorerStore.pinnedFolders.length > 0}
    <section>
      <h2 class="mb-2.5 text-[11px] font-medium uppercase tracking-wider text-muted-foreground/70">
        Pinned
      </h2>
      <div class="grid grid-cols-[repeat(auto-fill,minmax(180px,1fr))] gap-2">
        {#each explorerStore.pinnedFolders as folder}
          <!-- svelte-ignore a11y_click_events_have_key_events -->
          <!-- svelte-ignore a11y_no_static_element_interactions -->
          <div
            class="group relative flex cursor-pointer items-center gap-2.5 rounded-lg px-3 py-2.5 transition-colors hover:bg-muted/50"
            onclick={() => explorerStore.navigateTo(folder.path)}
          >
            <FileTypeIcon isDir={true} size={22} />
            <div class="min-w-0 text-left">
              <p class="truncate text-sm text-foreground">{folder.name}</p>
              <p class="truncate text-[11px] text-muted-foreground/60">{folder.path}</p>
            </div>
            <button
              type="button"
              class="absolute -right-1 -top-1 hidden rounded-full bg-background p-0.5 text-muted-foreground shadow-sm transition-colors hover:text-foreground group-hover:flex"
              onclick={(e: MouseEvent) => { e.stopPropagation(); explorerStore.unpinFolder(folder.path); }}
              title="Unpin"
            >
              <PinOff class="h-3 w-3" />
            </button>
          </div>
        {/each}
      </div>
    </section>
  {/if}

  <!-- Local -->
  <section>
    <h2 class="mb-2.5 text-[11px] font-medium uppercase tracking-wider text-muted-foreground/70">
      Local
    </h2>
    <div class="grid grid-cols-[repeat(auto-fill,minmax(180px,1fr))] gap-2">
      {#each localFolders as folder}
        <button
          type="button"
          class="flex items-center gap-2.5 rounded-lg border border-border/50 bg-card/30 px-3 py-2.5 text-left transition-colors hover:bg-muted/50"
          onclick={() => explorerStore.navigateTo(folder.path)}
        >
          <Folder class="h-5 w-5 shrink-0 text-yellow-400" />
          <div class="min-w-0">
            <p class="truncate text-sm text-foreground">{folder.name}</p>
            <p class="truncate text-[11px] text-muted-foreground/60">{folder.path}</p>
          </div>
        </button>
      {/each}
      {#if localFolders.length === 0}
        <p class="text-xs text-muted-foreground/50">Loading directories...</p>
      {/if}
    </div>
  </section>

  <!-- Remote -->
  <section>
    <h2 class="mb-2.5 text-[11px] font-medium uppercase tracking-wider text-muted-foreground/70">
      Remote
      <span class="ml-1.5 rounded-full bg-muted/80 px-1.5 py-px text-[9px] font-normal normal-case text-muted-foreground/60">Soon</span>
    </h2>
    <div class="grid grid-cols-[repeat(auto-fill,minmax(180px,1fr))] gap-2">
      <button
        type="button"
        disabled
        class="flex items-center gap-2.5 rounded-lg border border-border/50 bg-card/30 px-3 py-2.5 text-left opacity-40 cursor-not-allowed"
      >
        <Server class="h-5 w-5 shrink-0 text-emerald-400" />
        <div class="min-w-0">
          <p class="truncate text-sm text-foreground">PocketPaw Server</p>
          <p class="truncate text-[11px] text-muted-foreground/60">Connect to hosted instance</p>
        </div>
      </button>
    </div>
  </section>

  <!-- Cloud Storage -->
  <section>
    <h2 class="mb-2.5 text-[11px] font-medium uppercase tracking-wider text-muted-foreground/70">
      Cloud Storage
      <span class="ml-1.5 rounded-full bg-muted/80 px-1.5 py-px text-[9px] font-normal normal-case text-muted-foreground/60">Soon</span>
    </h2>
    <div class="grid grid-cols-[repeat(auto-fill,minmax(180px,1fr))] gap-2">
      {#each cloudProviders as provider}
        <button
          type="button"
          disabled
          class="flex items-center gap-2.5 rounded-lg border border-border/50 bg-card/30 px-3 py-2.5 text-left opacity-40 cursor-not-allowed"
        >
          <img src={provider.logo} alt={provider.name} class="h-5 w-5 shrink-0 object-contain" />
          <div class="min-w-0">
            <p class="truncate text-sm text-foreground">{provider.name}</p>
          </div>
        </button>
      {/each}
    </div>
  </section>
</div>

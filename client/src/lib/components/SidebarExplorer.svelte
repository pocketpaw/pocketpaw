<script lang="ts">
  import {
    Clock,
    Star,
    Download,
    Home,
    Monitor,
    FileText,
    Cloud,
    Droplets,
    HardDrive,
  } from "@lucide/svelte";
  import { onMount } from "svelte";
  import type { RecentFileEntry } from "$lib/api/types";
  import SidebarSection from "./SidebarSection.svelte";
  import { explorerStore, connectionStore, platformStore } from "$lib/stores";
  import * as Tooltip from "$lib/components/ui/tooltip";

  let recentFiles = $state<RecentFileEntry[]>([]);

  let defaultDirs = $derived(explorerStore.defaultDirs);
  let pinnedFolders = $derived(explorerStore.pinnedFolders);

  onMount(() => {
    loadRecentFiles();
  });

  async function loadRecentFiles() {
    try {
      const client = connectionStore.getClient();
      recentFiles = await client.getRecentFiles(10);
    } catch {
      // Backend may not support this yet
    }
  }

  let currentPath = $derived(explorerStore.currentPath.replace(/\\/g, "/"));
  let isHome = $derived(explorerStore.isHome);

  function navigateTo(path: string) {
    explorerStore.navigateTo(path);
  }

  function normalizePath(p: string): string {
    return p.replace(/\\/g, "/").replace(/\/+$/, "");
  }

  function isActivePath(path: string): boolean {
    return normalizePath(currentPath) === normalizePath(path);
  }

  function itemClasses(active: boolean): string {
    const touch = platformStore.isTouch;
    const base = touch
      ? "flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-[13px] transition-colors"
      : "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-[12px] transition-colors";
    if (active) {
      return base + " bg-paw-accent-subtle font-medium text-foreground";
    }
    return base + (touch
      ? " text-muted-foreground active:bg-accent active:text-foreground"
      : " text-muted-foreground hover:bg-accent hover:text-foreground");
  }

  const cloudServices = [
    { label: "Google Drive", icon: Cloud },
    { label: "Dropbox", icon: Droplets },
    { label: "OneDrive", icon: HardDrive },
  ] as const;
</script>

<div class="flex min-h-0 flex-1 flex-col overflow-y-auto">
  <!-- Quick Access -->
  <SidebarSection title="Quick Access">
    {#if recentFiles.length > 0}
      <button type="button" class={itemClasses(isHome)} onclick={() => explorerStore.goHome()}>
        <Clock class="h-3.5 w-3.5 shrink-0" strokeWidth={1.75} />
        <span class="truncate">Recent Files</span>
        <span class="ml-auto text-[10px] text-muted-foreground/50">{recentFiles.length}</span>
      </button>
    {/if}

    {#if pinnedFolders.length > 0}
      {#each pinnedFolders as folder (folder.path)}
        <button type="button" class={itemClasses(isActivePath(folder.path))} onclick={() => navigateTo(folder.path)}>
          <Star class="h-3.5 w-3.5 shrink-0 text-amber-500/70" strokeWidth={1.75} />
          <span class="truncate">{folder.name}</span>
        </button>
      {/each}
    {:else}
      <span class={itemClasses(false)}>
        <Star class="h-3.5 w-3.5 shrink-0" strokeWidth={1.75} />
        <span class="truncate">Favorites</span>
        <span class="ml-auto text-[10px] text-muted-foreground/40">None</span>
      </span>
    {/if}

    {#if defaultDirs?.downloads}
      <button type="button" class={itemClasses(isActivePath(defaultDirs!.downloads))} onclick={() => navigateTo(defaultDirs!.downloads)}>
        <Download class="h-3.5 w-3.5 shrink-0" strokeWidth={1.75} />
        <span class="truncate">Downloads</span>
      </button>
    {/if}
  </SidebarSection>

  <!-- Locations -->
  {#if defaultDirs}
    <SidebarSection title="Locations">
      <button type="button" class={itemClasses(isActivePath(defaultDirs!.home))} onclick={() => navigateTo(defaultDirs!.home)}>
        <Home class="h-3.5 w-3.5 shrink-0" strokeWidth={1.75} />
        <span class="truncate">Home</span>
      </button>
      <button type="button" class={itemClasses(isActivePath(defaultDirs!.desktop))} onclick={() => navigateTo(defaultDirs!.desktop)}>
        <Monitor class="h-3.5 w-3.5 shrink-0" strokeWidth={1.75} />
        <span class="truncate">Desktop</span>
      </button>
      <button type="button" class={itemClasses(isActivePath(defaultDirs!.documents))} onclick={() => navigateTo(defaultDirs!.documents)}>
        <FileText class="h-3.5 w-3.5 shrink-0" strokeWidth={1.75} />
        <span class="truncate">Documents</span>
      </button>
    </SidebarSection>
  {/if}

  <!-- Cloud Storage (disabled / coming soon) -->
  <SidebarSection title="Cloud Storage" defaultOpen={false}>
    {#each cloudServices as service}
      {@const Icon = service.icon}
      <Tooltip.Root>
        <Tooltip.Trigger>
          <span
            class="flex w-full cursor-not-allowed items-center gap-2 rounded-md px-2 py-1.5 text-[12px] text-muted-foreground/40"
          >
            <Icon class="h-3.5 w-3.5 shrink-0" strokeWidth={1.75} />
            <span class="truncate">{service.label}</span>
          </span>
        </Tooltip.Trigger>
        <Tooltip.Content>Coming soon</Tooltip.Content>
      </Tooltip.Root>
    {/each}
  </SidebarSection>
</div>

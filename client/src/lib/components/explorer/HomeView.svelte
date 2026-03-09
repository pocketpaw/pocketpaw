<script lang="ts">
  import { explorerStore } from "$lib/stores";
  import FileTypeIcon from "./FileTypeIcon.svelte";
  import Folder from "@lucide/svelte/icons/folder";
  import PinOff from "@lucide/svelte/icons/pin-off";
  import Cloud from "@lucide/svelte/icons/cloud";
  import Server from "@lucide/svelte/icons/server";

  interface QuickFolder {
    name: string;
    path: string;
    source: string;
  }

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
</script>

<div class="space-y-8 p-6">
  <!-- Pinned Folders -->
  {#if explorerStore.pinnedFolders.length > 0}
    <section>
      <h2 class="mb-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        Pinned
      </h2>
      <div class="flex flex-wrap gap-3">
        {#each explorerStore.pinnedFolders as folder}
          <!-- svelte-ignore a11y_click_events_have_key_events -->
          <!-- svelte-ignore a11y_no_static_element_interactions -->
          <div
            class="group relative flex cursor-pointer items-center gap-3 rounded-lg border border-border bg-card p-4 transition-all hover:border-border/80 hover:bg-muted/50"
            onclick={() => explorerStore.navigateTo(folder.path)}
          >
            <FileTypeIcon isDir={true} size={28} />
            <div class="text-left">
              <p class="text-sm font-medium text-foreground">{folder.name}</p>
              <p class="max-w-48 truncate text-xs text-muted-foreground">{folder.path}</p>
            </div>
            <button
              type="button"
              class="absolute -right-1 -top-1 hidden rounded-full border border-border bg-background p-0.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground group-hover:flex"
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
    <h2 class="mb-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
      Local
    </h2>
    <div class="flex flex-wrap gap-3">
      {#each localFolders as folder}
        <button
          type="button"
          class="flex items-center gap-3 rounded-lg border border-border bg-card/20 p-4 transition-all hover:border-border/80 hover:bg-card/50"
          onclick={() => explorerStore.navigateTo(folder.path)}
        >
          <Folder class="h-7 w-7 text-yellow-400" />
          <div class="text-left">
            <p class="text-sm font-medium text-foreground">{folder.name}</p>
            <p class="max-w-48 truncate text-xs text-muted-foreground">{folder.path}</p>
          </div>
        </button>
      {/each}
      {#if localFolders.length === 0}
        <p class="text-sm text-muted-foreground">Loading directories...</p>
      {/if}
    </div>
  </section>

  <!-- Workspace (placeholder) -->
  <section class="opacity-50">
    <h2 class="mb-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
      Workspace
      <span class="ml-2 rounded bg-muted px-1.5 py-0.5 text-[10px] font-normal normal-case">Coming soon</span>
    </h2>
    <div class="flex items-center gap-3 rounded-lg border border-dashed border-border p-4">
      <Server class="h-7 w-7 text-muted-foreground/50" />
      <p class="text-sm text-muted-foreground">Connect to your PocketPaw backend workspace</p>
    </div>
  </section>

  <!-- Cloud (placeholder) -->
  <section class="opacity-50">
    <h2 class="mb-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
      Cloud
      <span class="ml-2 rounded bg-muted px-1.5 py-0.5 text-[10px] font-normal normal-case">Coming soon</span>
    </h2>
    <div class="flex items-center gap-3 rounded-lg border border-dashed border-border p-4">
      <Cloud class="h-7 w-7 text-muted-foreground/50" />
      <p class="text-sm text-muted-foreground">Connect cloud storage providers</p>
    </div>
  </section>
</div>

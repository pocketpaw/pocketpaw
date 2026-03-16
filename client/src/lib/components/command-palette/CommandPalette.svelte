<script lang="ts">
  import {
    Search,
    MessageSquare,
    FileText,
    Folder,
    ChevronLeft,
    ChevronRight,
    Home,
    RefreshCw,
    Grid3x3,
    List,
    ArrowUpDown,
    Copy,
    Plus,
    X,
    Loader2,
  } from "@lucide/svelte";
  import { explorerStore } from "$lib/stores";
  import { localFs } from "$lib/filesystem";
  import type { FileEntry } from "$lib/filesystem/types";
  import * as Command from "$lib/components/ui/command";
  import StyledFileIcon from "$lib/components/explorer/StyledFileIcon.svelte";
  import { cn } from "$lib/utils";

  let expanded = $state(false);
  let wrapperEl = $state<HTMLDivElement | null>(null);
  let justOpened = $state(false);
  let searchValue = $state("");

  // Deep search state
  let searchResults = $state<FileEntry[]>([]);
  let isSearching = $state(false);
  let totalScanned = $state(0);
  let searchTruncated = $state(false);

  // Non-reactive internals (plain let, not $state)
  let _debounceTimer: ReturnType<typeof setTimeout> | undefined;
  let _searchVersion = 0;

  function doSearch(query: string, root: string) {
    clearTimeout(_debounceTimer);
    const version = ++_searchVersion;

    if (query.length < 2 || !root) {
      searchResults = [];
      isSearching = false;
      totalScanned = 0;
      searchTruncated = false;
      return;
    }

    isSearching = true;

    _debounceTimer = setTimeout(() => {
      if (version !== _searchVersion) return;

      localFs.searchRecursive(root, query, 50, 8).then(
        (result) => {
          if (version !== _searchVersion) return;
          searchResults = result.entries;
          totalScanned = result.totalScanned;
          searchTruncated = result.truncated;
          isSearching = false;
        },
        () => {
          if (version !== _searchVersion) return;
          searchResults = [];
          isSearching = false;
        },
      );
    }, 300);
  }

  function clearSearch() {
    clearTimeout(_debounceTimer);
    ++_searchVersion;
    searchResults = [];
    isSearching = false;
    totalScanned = 0;
    searchTruncated = false;
  }

  let inputEl = $state<HTMLInputElement | null>(null);

  function open() {
    if (expanded) return;
    expanded = true;
    justOpened = true;
    requestAnimationFrame(() => {
      justOpened = false;
    });
  }

  function close() {
    expanded = false;
    searchValue = "";
    clearSearch();
    inputEl?.blur();
  }

  function runAndClose(action: () => void) {
    close();
    action();
  }

  function handleClickOutside(e: MouseEvent) {
    if (!expanded || justOpened) return;
    if (wrapperEl && !wrapperEl.contains(e.target as Node)) {
      close();
    }
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === "Escape" && expanded) {
      close();
    }
  }

  // Watch search input — only reads reactive state, delegates work to plain function
  $effect(() => {
    const q = searchValue.trim();
    const root = explorerStore.currentPath;
    const isOpen = expanded;

    if (!isOpen) return;
    doSearch(q, root);
  });

  function formatSize(bytes: number): string {
    if (bytes === 0) return "";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  function shortenPath(fullPath: string): string {
    const root = explorerStore.currentPath;
    if (root && fullPath.startsWith(root)) {
      const rel = fullPath.slice(root.length).replace(/^[/\\]/, "");
      return rel || fullPath;
    }
    return fullPath;
  }

  function openResult(entry: FileEntry) {
    if (entry.isDir) {
      runAndClose(() => explorerStore.navigateTo(entry.path));
    } else {
      runAndClose(() => explorerStore.openFileDetail(entry));
    }
  }
</script>

<svelte:document onclick={handleClickOutside} onkeydown={handleKeydown} />

<div bind:this={wrapperEl} class="relative w-xl mx-auto">
  <!-- Always-visible search input -->
  <div class={cn(
    "flex h-10 items-center gap-2 rounded-xl border bg-border px-2.5 transition-all",
    expanded
      ? "border-border  shadow-sm"
      : "border-border/50 ",
  )}>
    <Search class="h-3 w-3 shrink-0 text-muted-foreground" strokeWidth={2} />
    <input
      bind:this={inputEl}
      bind:value={searchValue}
      type="text"
      placeholder={explorerStore.isHome ? "Search files & actions..." : explorerStore.currentPath}
      class="flex-1 bg-transparent text-xs text-foreground placeholder:text-muted-foreground focus:outline-none"
      onfocus={open}
    />
    {#if expanded && searchValue}
      <button type="button" class="shrink-0 rounded-sm p-0.5 text-muted-foreground hover:text-foreground" onclick={() => { searchValue = ""; clearSearch(); inputEl?.focus(); }}>
        <X class="h-3 w-3" />
      </button>
    {:else}
      <kbd class="shrink-0 rounded border border-border/50 bg-background/50 px-1 py-px text-[9px] font-medium text-muted-foreground">⌘K</kbd>
    {/if}
  </div>

  <!-- Dropdown results — appears below the input -->
  {#if expanded}
    <div class="absolute left-0 top-[calc(100%+4px)] z-50 w-full min-w-[400px]">
      <Command.Root
        class="rounded-lg border border-border bg-background "
        shouldFilter={searchValue.trim().length < 2}
      >
        <!-- Hidden input syncs with our visible input for cmdk filtering -->
        <div class="hidden">
          <Command.Input bind:value={searchValue} />
        </div>
        <Command.List class="max-h-[360px]">
          <Command.Empty>No results found.</Command.Empty>

          <!-- Deep search results -->
          {#if searchValue.trim().length >= 2 && explorerStore.currentPath}
            {#if isSearching && searchResults.length === 0}
              <Command.Loading>
                <div class="flex items-center gap-2 px-4 py-3 text-xs text-muted-foreground">
                  <Loader2 class="h-3.5 w-3.5 animate-spin" />
                  <span>Searching in {explorerStore.currentPath.split(/[/\\]/).pop()}...</span>
                </div>
              </Command.Loading>
            {/if}

            {#if searchResults.length > 0}
              <Command.Group heading={searchTruncated ? `Files (50+ of ${totalScanned} scanned)` : `Files (${searchResults.length} found)`}>
                {#each searchResults as entry (entry.path)}
                  <Command.Item value={entry.path} onSelect={() => openResult(entry)}>
                    <div class="flex shrink-0 items-center justify-center" style="width: 20px; height: 20px;">
                      <StyledFileIcon extension={entry.extension} isDir={entry.isDir} size={20} />
                    </div>
                    <div class="flex min-w-0 flex-1 flex-col">
                      <span class="truncate text-sm">{entry.name}</span>
                      <span class="truncate text-[11px] text-muted-foreground">{shortenPath(entry.path)}</span>
                    </div>
                    {#if !entry.isDir && entry.size > 0}
                      <Command.Shortcut>{formatSize(entry.size)}</Command.Shortcut>
                    {/if}
                  </Command.Item>
                {/each}
              </Command.Group>

              {#if isSearching}
                <div class="flex items-center gap-2 px-4 py-1.5 text-[11px] text-muted-foreground">
                  <Loader2 class="h-3 w-3 animate-spin" />
                  <span>Searching for more...</span>
                </div>
              {/if}
            {/if}

            {#if !isSearching && searchResults.length === 0}
              <div class="px-4 py-3 text-xs text-muted-foreground">
                No files matching "{searchValue.trim()}"
              </div>
            {/if}
          {/if}

          <!-- Actions -->
          <Command.Group heading="Navigate">
            <Command.Item onSelect={() => runAndClose(() => explorerStore.goHome())}>
              <Home class="h-4 w-4" />
              <span>Go Home</span>
            </Command.Item>
            <Command.Item
              disabled={!explorerStore.canGoBack}
              onSelect={() => runAndClose(() => explorerStore.goBack())}
            >
              <ChevronLeft class="h-4 w-4" />
              <span>Go Back</span>
            </Command.Item>
            <Command.Item
              disabled={!explorerStore.canGoForward}
              onSelect={() => runAndClose(() => explorerStore.goForward())}
            >
              <ChevronRight class="h-4 w-4" />
              <span>Go Forward</span>
            </Command.Item>
            <Command.Item onSelect={() => runAndClose(() => explorerStore.refresh())}>
              <RefreshCw class="h-4 w-4" />
              <span>Refresh</span>
            </Command.Item>
          </Command.Group>

          <Command.Separator />

          <Command.Group heading="View">
            <Command.Item
              onSelect={() =>
                runAndClose(() =>
                  explorerStore.setViewMode(explorerStore.viewMode === "icon" ? "list" : "icon"),
                )}
            >
              {#if explorerStore.viewMode === "icon"}
                <List class="h-4 w-4" />
                <span>Switch to List View</span>
              {:else}
                <Grid3x3 class="h-4 w-4" />
                <span>Switch to Grid View</span>
              {/if}
            </Command.Item>
            <Command.Item onSelect={() => runAndClose(() => explorerStore.toggleChatSidebar())}>
              <MessageSquare class="h-4 w-4" />
              <span>Toggle Chat Sidebar</span>
            </Command.Item>
          </Command.Group>

          <Command.Separator />

          <Command.Group heading="Sort">
            <Command.Item onSelect={() => runAndClose(() => explorerStore.setSortBy("name"))}>
              <ArrowUpDown class="h-4 w-4" />
              <span>Sort by Name</span>
            </Command.Item>
            <Command.Item onSelect={() => runAndClose(() => explorerStore.setSortBy("modified"))}>
              <ArrowUpDown class="h-4 w-4" />
              <span>Sort by Modified</span>
            </Command.Item>
            <Command.Item onSelect={() => runAndClose(() => explorerStore.setSortBy("size"))}>
              <ArrowUpDown class="h-4 w-4" />
              <span>Sort by Size</span>
            </Command.Item>
            <Command.Item onSelect={() => runAndClose(() => explorerStore.setSortBy("type"))}>
              <ArrowUpDown class="h-4 w-4" />
              <span>Sort by Type</span>
            </Command.Item>
          </Command.Group>

          <Command.Separator />

          <Command.Group heading="Tabs">
            <Command.Item onSelect={() => runAndClose(() => explorerStore.createNewTab())}>
              <Plus class="h-4 w-4" />
              <span>New Tab</span>
              <Command.Shortcut>⌘T</Command.Shortcut>
            </Command.Item>
            <Command.Item
              onSelect={() => runAndClose(() => explorerStore.closeTab(explorerStore.activeTabId))}
            >
              <X class="h-4 w-4" />
              <span>Close Tab</span>
              <Command.Shortcut>⌘W</Command.Shortcut>
            </Command.Item>
            <Command.Item
              onSelect={() => runAndClose(() => explorerStore.duplicateTab(explorerStore.activeTabId))}
            >
              <Copy class="h-4 w-4" />
              <span>Duplicate Tab</span>
            </Command.Item>
            {#each explorerStore.tabs as tab (tab.id)}
              {@const label = tab.path ? (tab.path.replace(/\\/g, "/").split("/").pop() || tab.path) : "Home"}
              <Command.Item onSelect={() => runAndClose(() => explorerStore.activateTab(tab.id))}>
                {#if tab.path}
                  <FileText class="h-4 w-4" />
                {:else}
                  <Home class="h-4 w-4" />
                {/if}
                <span>{label}</span>
                {#if tab.id === explorerStore.activeTabId}
                  <Command.Shortcut>active</Command.Shortcut>
                {/if}
              </Command.Item>
            {/each}
          </Command.Group>

          {#if explorerStore.history.length > 0}
            <Command.Separator />
            <Command.Group heading="Recent">
              {#each explorerStore.history as path (path)}
                <Command.Item onSelect={() => runAndClose(() => explorerStore.navigateTo(path))}>
                  <FileText class="h-4 w-4" />
                  <span>{path.split("/").pop() || path}</span>
                  <Command.Shortcut class="max-w-[200px] truncate">{path}</Command.Shortcut>
                </Command.Item>
              {/each}
            </Command.Group>
          {/if}
        </Command.List>
      </Command.Root>
    </div>
  {/if}
</div>

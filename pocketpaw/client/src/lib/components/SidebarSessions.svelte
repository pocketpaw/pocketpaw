<script lang="ts">
  import type { Session } from "$lib/api";
  import { sessionStore, uiStore, platformStore } from "$lib/stores";
  import SessionSearch from "./SessionSearch.svelte";
  import SessionItem from "./SessionItem.svelte";

  let searchQuery = $state("");
  let searchResults = $state<Session[]>([]);
  let isSearching = $state(false);
  let debounceTimer: ReturnType<typeof setTimeout> | undefined;
  let searchComponent: ReturnType<typeof SessionSearch> | undefined;

  let sessions = $derived(sessionStore.sessions);
  let activeId = $derived(sessionStore.activeSessionId);
  let isLoading = $derived(sessionStore.isLoading);

  // Filter chips
  type FilterType = "today" | "with_files" | "starred" | null;
  let activeFilter = $state<FilterType>(null);

  function toggleFilter(filter: FilterType) {
    activeFilter = activeFilter === filter ? null : filter;
  }

  let filteredSessionList = $derived.by(() => {
    if (!activeFilter) return sessions;
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());

    switch (activeFilter) {
      case "today":
        return sessions.filter((s) => new Date(s.last_activity) >= today);
      case "starred":
        return sessions.filter((s) => sessionStore.isSessionPinned(s.id));
      case "with_files":
        return sessions;
      default:
        return sessions;
    }
  });

  // Date grouping
  type GroupedSessions = { label: string; sessions: Session[] }[];

  let groupedSessions = $derived.by((): GroupedSessions => {
    if (filteredSessionList.length === 0) return [];

    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const yesterday = new Date(today.getTime() - 86_400_000);
    const thisWeek = new Date(today.getTime() - 7 * 86_400_000);
    const thisMonth = new Date(today.getTime() - 30 * 86_400_000);

    const groups = new Map<string, Session[]>([
      ["Today", []],
      ["Yesterday", []],
      ["This Week", []],
      ["This Month", []],
      ["Older", []],
    ]);

    for (const s of filteredSessionList) {
      const d = new Date(s.last_activity);
      if (d >= today) groups.get("Today")!.push(s);
      else if (d >= yesterday) groups.get("Yesterday")!.push(s);
      else if (d >= thisWeek) groups.get("This Week")!.push(s);
      else if (d >= thisMonth) groups.get("This Month")!.push(s);
      else groups.get("Older")!.push(s);
    }

    const result: GroupedSessions = [];
    for (const [label, items] of groups) {
      if (items.length > 0) {
        result.push({ label, sessions: items });
      }
    }
    return result;
  });

  let showSearchResults = $derived(searchQuery.length > 0);

  function handleSearchInput() {
    clearTimeout(debounceTimer);
    if (!searchQuery.trim()) {
      searchResults = [];
      isSearching = false;
      return;
    }
    isSearching = true;
    debounceTimer = setTimeout(async () => {
      try {
        searchResults = await sessionStore.searchSessions(searchQuery.trim());
      } catch {
        // Fall back to local filter
        const q = searchQuery.toLowerCase();
        searchResults = sessions.filter((s) => s.title.toLowerCase().includes(q));
      }
      isSearching = false;
    }, 300);
  }

  $effect(() => {
    searchQuery;
    handleSearchInput();
  });

  let displaySessions = $derived(showSearchResults ? searchResults : []);

  // Focus search input when requested via uiStore
  $effect(() => {
    uiStore.searchFocusRequest;
    if (uiStore.searchFocusRequest > 0) {
      queueMicrotask(() => searchComponent?.focus());
    }
  });
</script>

<div class="flex min-h-0 flex-1 flex-col">
  <SessionSearch bind:this={searchComponent} bind:value={searchQuery} />

  <!-- Filter chips -->
  <div class="flex gap-1.5 px-3 pb-1.5">
    {#each [
      { key: "today", label: "Today" },
      { key: "starred", label: "Starred" },
      { key: "with_files", label: "With files" },
    ] as chip (chip.key)}
      <button
        type="button"
        onclick={() => toggleFilter(chip.key as FilterType)}
        class={activeFilter === chip.key
          ? "rounded-full bg-paw-accent-subtle px-2.5 py-0.5 text-[10px] font-medium text-foreground transition-colors"
          : "rounded-full bg-muted/60 px-2.5 py-0.5 text-[10px] text-muted-foreground transition-colors hover:bg-muted"}
      >
        {chip.label}
      </button>
    {/each}
  </div>

  <div class={platformStore.isTouch ? "flex-1 overflow-y-auto px-3 py-2" : "flex-1 overflow-y-auto px-2 py-1"}>
    {#if showSearchResults}
      <!-- Search results (flat) -->
      {#if isSearching}
        <p class="px-2 py-3 text-center text-[11px] text-muted-foreground">
          Searching...
        </p>
      {:else if displaySessions.length === 0}
        <p class="px-2 py-3 text-center text-[11px] text-muted-foreground">
          No results for "{searchQuery}"
        </p>
      {:else}
        <p class="px-2 pb-1 text-[10px] text-muted-foreground">
          {displaySessions.length} result{displaySessions.length === 1 ? "" : "s"}
        </p>
        {#each displaySessions as session (session.id)}
          <SessionItem {session} isActive={session.id === activeId} />
        {/each}
      {/if}
    {:else if isLoading && sessions.length === 0}
      <!-- Loading skeleton -->
      <div class="space-y-1 px-1">
        {#each { length: 4 } as _}
          <div class="flex items-center gap-2 rounded-md px-2 py-2">
            <div class="h-3.5 w-3.5 animate-pulse rounded bg-muted"></div>
            <div class="h-3 flex-1 animate-pulse rounded bg-muted"></div>
          </div>
        {/each}
      </div>
    {:else if sessions.length === 0}
      <p class="px-2 py-4 text-center text-[11px] text-muted-foreground">
        No conversations yet
      </p>
    {:else}
      <!-- Pinned sessions -->
      {#if !activeFilter}
        {@const pinned = sessionStore.pinnedSessions}
        {#if pinned.length > 0}
          <p class={platformStore.isTouch
            ? "mt-1 px-2 pb-0.5 text-[11px] font-medium uppercase tracking-wider text-muted-foreground/50"
            : "mt-1 px-2 pb-0.5 text-[10px] font-medium uppercase tracking-wider text-muted-foreground/50"}>
            Pinned
          </p>
          {#each pinned as session (session.id)}
            <SessionItem {session} isActive={session.id === activeId} />
          {/each}
        {/if}
      {/if}
      <!-- Grouped sessions -->
      {#each groupedSessions as group}
        <p class={platformStore.isTouch
          ? "mt-2 px-2 pb-0.5 text-[11px] font-medium uppercase tracking-wider text-muted-foreground/50 first:mt-0"
          : "mt-2 px-2 pb-0.5 text-[10px] font-medium uppercase tracking-wider text-muted-foreground/50 first:mt-0"}>
          {group.label}
        </p>
        {#each group.sessions as session (session.id)}
          <SessionItem {session} isActive={session.id === activeId} />
        {/each}
      {/each}
    {/if}
  </div>
</div>

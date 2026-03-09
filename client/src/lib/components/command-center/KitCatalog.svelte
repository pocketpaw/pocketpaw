<script lang="ts">
  import { kitStore } from "$lib/stores";
  import type { KitCatalogEntry } from "$lib/types/pawkit";
  import { Search, Loader2, Check, Download, Play } from "@lucide/svelte";

  let { onInstalled }: { onInstalled?: () => void } = $props();

  let catalogSearch = $state("");
  let activeCategory = $state("");
  let installingKit = $state<string | null>(null);
  let activatingKit = $state<string | null>(null);

  let catalog = $derived(kitStore.catalog);
  let isLoading = $derived(kitStore.isCatalogLoading);
  let activeKitId = $derived(kitStore.activeKitId);

  let categories = $derived.by(() => {
    const cats = new Set(catalog.map((k) => k.category));
    return Array.from(cats).sort();
  });

  let filteredCatalog = $derived.by(() => {
    let result = catalog;
    if (activeCategory) {
      result = result.filter((k) => k.category === activeCategory);
    }
    if (catalogSearch.trim()) {
      const q = catalogSearch.toLowerCase();
      result = result.filter(
        (k) =>
          k.name.toLowerCase().includes(q) ||
          k.description.toLowerCase().includes(q) ||
          k.tags.some((t) => t.toLowerCase().includes(q)),
      );
    }
    return result;
  });

  // Load catalog on mount
  $effect(() => {
    if (catalog.length === 0 && !isLoading) {
      kitStore.loadCatalog();
    }
  });

  async function installKit(entry: KitCatalogEntry) {
    installingKit = entry.id;
    try {
      await kitStore.installFromCatalog(entry.id);
      onInstalled?.();
    } catch (err) {
      console.error("[KitCatalog] Install failed:", err);
    } finally {
      installingKit = null;
    }
  }

  async function activateKit(kitId: string) {
    activatingKit = kitId;
    try {
      await kitStore.activate(kitId);
      onInstalled?.();
    } catch (err) {
      console.error("[KitCatalog] Activate failed:", err);
    } finally {
      activatingKit = null;
    }
  }
</script>

<div class="flex flex-col gap-4 p-4">
  <!-- Search -->
  <div class="relative">
    <Search class="absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
    <input
      bind:value={catalogSearch}
      type="text"
      placeholder="Search kits..."
      class="h-9 w-full rounded-lg border border-border bg-muted/50 pl-9 pr-3 text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
    />
  </div>

  <!-- Category Filters -->
  <div class="flex flex-wrap gap-1.5">
    <button
      onclick={() => (activeCategory = "")}
      class={[
        "rounded-full px-2.5 py-1 text-[10px] transition-colors",
        !activeCategory
          ? "bg-primary/10 font-medium text-primary"
          : "bg-muted text-muted-foreground hover:text-foreground",
      ].join(" ")}
    >
      All
    </button>
    {#each categories as cat (cat)}
      <button
        onclick={() => (activeCategory = activeCategory === cat ? "" : cat)}
        class={[
          "rounded-full px-2.5 py-1 text-[10px] capitalize transition-colors",
          activeCategory === cat
            ? "bg-primary/10 font-medium text-primary"
            : "bg-muted text-muted-foreground hover:text-foreground",
        ].join(" ")}
      >
        {cat}
      </button>
    {/each}
  </div>

  <!-- Kit Cards -->
  {#if isLoading}
    <div class="flex items-center justify-center py-12">
      <div class="h-6 w-6 animate-spin rounded-full border-2 border-foreground/20 border-t-foreground"></div>
    </div>
  {:else if filteredCatalog.length === 0}
    <p class="py-6 text-center text-xs text-muted-foreground">No kits match your search</p>
  {:else}
    <div class="flex flex-col gap-2">
      {#each filteredCatalog as entry (entry.id)}
        {@const isActive = entry.id === activeKitId}
        {@const isInstalled = entry.installed}
        <div
          class={[
            "rounded-lg border px-3 py-2.5 transition-colors",
            isActive
              ? "border-primary/30 bg-primary/5"
              : "border-border/40 bg-muted/20",
          ].join(" ")}
        >
          <div class="flex items-start gap-3">
            <!-- Left: name + badges + description -->
            <div class="flex min-w-0 flex-1 flex-col gap-0.5">
              <div class="flex items-center gap-2">
                <span class="text-sm font-medium text-foreground">{entry.name}</span>
                <span class="rounded-full bg-muted px-1.5 py-0.5 text-[10px] capitalize text-muted-foreground">
                  {entry.category}
                </span>
                {#if isActive}
                  <span class="flex items-center gap-0.5 rounded-full bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium text-primary">
                    <Check class="h-2.5 w-2.5" />
                    Active
                  </span>
                {/if}
              </div>
              <p class="text-xs text-muted-foreground">{entry.description}</p>
              {#if entry.tags.length > 0}
                <div class="mt-1 flex flex-wrap gap-1">
                  {#each entry.tags as tag (tag)}
                    <span class="rounded bg-muted/60 px-1.5 py-0.5 text-[10px] text-muted-foreground">
                      {tag}
                    </span>
                  {/each}
                </div>
              {/if}
              {#if entry.preview}
                <p class="mt-1 text-[10px] text-muted-foreground/70">{entry.preview}</p>
              {/if}
            </div>

            <!-- Right: action button -->
            <div class="flex shrink-0 items-center gap-1.5 pt-0.5">
              {#if isActive}
                <!-- Already active — no action needed -->
              {:else if isInstalled}
                <!-- Installed but not active — offer to activate -->
                <button
                  onclick={() => activateKit(entry.id)}
                  disabled={activatingKit === entry.id}
                  class="flex items-center gap-1 rounded-lg border border-border px-2.5 py-1.5 text-[10px] font-medium text-foreground transition-colors hover:bg-muted disabled:opacity-40"
                >
                  {#if activatingKit === entry.id}
                    <Loader2 class="h-3 w-3 animate-spin" />
                  {:else}
                    <Play class="h-3 w-3" />
                    Use
                  {/if}
                </button>
              {:else}
                <!-- Not installed — offer to install -->
                <button
                  onclick={() => installKit(entry)}
                  disabled={installingKit === entry.id}
                  class="flex items-center gap-1 rounded-lg bg-primary px-2.5 py-1.5 text-[10px] font-medium text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-40"
                >
                  {#if installingKit === entry.id}
                    <Loader2 class="h-3 w-3 animate-spin" />
                  {:else}
                    <Download class="h-3 w-3" />
                    Install
                  {/if}
                </button>
              {/if}
            </div>
          </div>
        </div>
      {/each}
    </div>
  {/if}
</div>

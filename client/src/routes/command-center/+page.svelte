<script lang="ts">
  import { kitStore } from "$lib/stores";
  import LayoutRenderer from "$lib/components/command-center/LayoutRenderer.svelte";
  import KitCatalog from "$lib/components/command-center/KitCatalog.svelte";
  import * as Select from "$lib/components/ui/select";
  import { LayoutDashboard, Store, ChevronRight } from "@lucide/svelte";

  let activeView = $state<"dashboard" | "store">("dashboard");
  let kit = $derived(kitStore.activeKit);
  let data = $derived(kitStore.kitData);
  let isLoading = $derived(kitStore.isLoading);
  let installedKits = $derived(kitStore.kits);

  // Load data + start polling when active kit becomes available
  $effect(() => {
    if (kit) {
      kitStore.loadData(kit.id);
      kitStore.startPolling(kit.id);
    }
    return () => {
      kitStore.stopPolling();
    };
  });

  // Ensure kits are loaded when navigating directly to this page
  $effect(() => {
    if (kitStore.kits.length === 0 && !kitStore.isLoading) {
      kitStore.load();
    }
  });

  function handleKitSwitch(kitId: string) {
    if (kitId && kitId !== kitStore.activeKitId) {
      kitStore.activate(kitId);
    }
  }

  function handleInstalled() {
    activeView = "dashboard";
  }
</script>

<div class="flex h-full flex-col overflow-auto">
  <!-- Header Bar -->
  <div class="flex items-center justify-between border-b border-border/40 px-4 py-2">
    <!-- Left: view toggle -->
    <div class="flex gap-1 rounded-lg border border-border/40 bg-muted/20 p-0.5">
      <button
        onclick={() => (activeView = "dashboard")}
        class={[
          "flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs transition-colors",
          activeView === "dashboard"
            ? "bg-background font-medium text-foreground shadow-sm"
            : "text-muted-foreground hover:text-foreground",
        ].join(" ")}
      >
        <LayoutDashboard class="h-3.5 w-3.5" />
        Dashboard
      </button>
      <button
        onclick={() => (activeView = "store")}
        class={[
          "flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs transition-colors",
          activeView === "store"
            ? "bg-background font-medium text-foreground shadow-sm"
            : "text-muted-foreground hover:text-foreground",
        ].join(" ")}
      >
        <Store class="h-3.5 w-3.5" />
        Kit Store
      </button>
    </div>

    <!-- Right: kit switcher (only when on dashboard + kits installed) -->
    {#if activeView === "dashboard" && installedKits.length > 0}
      <div class="flex items-center gap-2">
        <Select.Root
          type="single"
          value={kitStore.activeKitId ?? undefined}
          onValueChange={(v) => { if (v) handleKitSwitch(v); }}
        >
          <Select.Trigger size="sm" class="min-w-[160px]">
            {kit?.config.meta.name ?? "Select a kit..."}
          </Select.Trigger>
          <Select.Content>
            {#each installedKits as k (k.id)}
              <Select.Item value={k.id} label={k.config.meta.name} />
            {/each}
          </Select.Content>
        </Select.Root>
      </div>
    {/if}
  </div>

  <!-- Content -->
  {#if activeView === "store"}
    <KitCatalog onInstalled={handleInstalled} />
  {:else if isLoading}
    <div class="flex flex-1 items-center justify-center">
      <div class="h-6 w-6 animate-spin rounded-full border-2 border-foreground/20 border-t-foreground"></div>
    </div>
  {:else if kit}
    <LayoutRenderer config={kit.config.layout} kitData={data} onDataChanged={() => kit && kitStore.loadData(kit.id)} />
  {:else if installedKits.length > 0}
    <!-- Kits are installed but none is active -->
    <div class="flex h-full flex-col items-center justify-center gap-4 p-8 text-center">
      <div class="rounded-2xl border border-border/50 bg-muted/30 p-4">
        <LayoutDashboard class="h-10 w-10 text-muted-foreground" strokeWidth={1.5} />
      </div>
      <div>
        <h2 class="text-lg font-semibold text-foreground">No kit active</h2>
        <p class="mt-1 text-sm text-muted-foreground">
          Select a kit from the dropdown above, or browse the store.
        </p>
      </div>
      <div class="flex flex-col gap-2">
        {#each installedKits as k (k.id)}
          <button
            class="flex items-center gap-2 rounded-lg border border-border px-4 py-2 text-sm text-foreground transition-colors hover:bg-muted"
            onclick={() => kitStore.activate(k.id)}
          >
            {k.config.meta.name}
            <ChevronRight class="h-3.5 w-3.5 text-muted-foreground" />
          </button>
        {/each}
      </div>
    </div>
  {:else}
    <!-- Nothing installed — first-time experience -->
    <div class="flex h-full flex-col items-center justify-center gap-5 p-8 text-center">
      <div class="rounded-2xl border border-border/50 bg-muted/30 p-4">
        <Store class="h-10 w-10 text-muted-foreground" strokeWidth={1.5} />
      </div>
      <div>
        <h2 class="text-lg font-semibold text-foreground">Welcome to Deep Work</h2>
        <p class="mt-1 max-w-sm text-sm text-muted-foreground">
          Install a dashboard kit to track tasks, manage sprints, organize research, or run content pipelines.
        </p>
      </div>
      <button
        class="rounded-lg bg-foreground px-5 py-2.5 text-sm font-medium text-background transition-opacity hover:opacity-90"
        onclick={() => (activeView = "store")}
      >
        Browse Kit Store
      </button>
    </div>
  {/if}
</div>

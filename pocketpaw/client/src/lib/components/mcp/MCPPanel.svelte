<script lang="ts">
  import type { MCPStatusMap, MCPServerInfo, MCPPreset } from "$lib/api";
  import { goto } from "$app/navigation";
  import { connectionStore } from "$lib/stores";
  import {
    ArrowLeft,
    Plug,
    Loader2,
    Play,
    Square,
    Trash2,
    Plus,
    ExternalLink,
    CircleDot,
    Search,
  } from "@lucide/svelte";
  import { toast } from "svelte-sonner";

  // State
  let servers = $state<MCPStatusMap>({});
  let presets = $state<MCPPreset[]>([]);
  let loading = $state(true);
  let activeView = $state<"servers" | "catalog">("servers");
  let togglingServer = $state<string | null>(null);
  let removingServer = $state<string | null>(null);

  // Add server form
  let showAddForm = $state(false);
  let addName = $state("");
  let addTransport = $state<"stdio" | "http">("stdio");
  let addCommand = $state("");
  let addArgs = $state("");
  let addUrl = $state("");
  let adding = $state(false);

  // Catalog
  let catalogSearch = $state("");
  let activeCategory = $state("");
  let installingPreset = $state<string | null>(null);
  let presetEnvValues = $state<Record<string, string>>({});
  let configuringPreset = $state<string | null>(null);

  let serverEntries = $derived(
    Object.entries(servers).sort(([, a], [, b]) => {
      if (a.connected && !b.connected) return -1;
      if (!a.connected && b.connected) return 1;
      return 0;
    }),
  );

  let categories = $derived.by(() => {
    const cats = new Set(presets.map((p) => p.category));
    return Array.from(cats).sort();
  });

  let filteredPresets = $derived.by(() => {
    let result = presets;
    if (activeCategory) {
      result = result.filter((p) => p.category === activeCategory);
    }
    if (catalogSearch.trim()) {
      const q = catalogSearch.toLowerCase();
      result = result.filter(
        (p) =>
          p.name.toLowerCase().includes(q) ||
          p.description.toLowerCase().includes(q),
      );
    }
    return result;
  });

  $effect(() => {
    loadAll();
  });

  async function loadAll() {
    loading = true;
    try {
      const client = connectionStore.getClient();
      const [statusResult, presetsResult] = await Promise.allSettled([
        client.getMcpStatus(),
        client.getMcpPresets(),
      ]);
      if (statusResult.status === "fulfilled") servers = statusResult.value;
      if (presetsResult.status === "fulfilled") presets = presetsResult.value;
    } catch {
      // partial failures handled
    } finally {
      loading = false;
    }
  }

  async function toggleServer(name: string) {
    togglingServer = name;
    try {
      const client = connectionStore.getClient();
      await client.toggleMcpServer(name);
      // Refresh status
      servers = await client.getMcpStatus();
      toast.success(`Server "${name}" toggled`);
    } catch {
      toast.error(`Failed to toggle "${name}"`);
    } finally {
      togglingServer = null;
    }
  }

  async function removeServer(name: string) {
    removingServer = name;
    try {
      const client = connectionStore.getClient();
      await client.removeMcpServer(name);
      const { [name]: _, ...rest } = servers;
      servers = rest;
      toast.success(`Server "${name}" removed`);
    } catch {
      toast.error(`Failed to remove "${name}"`);
    } finally {
      removingServer = null;
    }
  }

  async function addServer() {
    if (!addName.trim()) return;
    adding = true;
    try {
      const client = connectionStore.getClient();
      const config: Parameters<typeof client.addMcpServer>[0] = {
        name: addName.trim(),
        transport: addTransport,
      };
      if (addTransport === "stdio") {
        config.command = addCommand.trim();
        config.args = addArgs
          .split(/\s+/)
          .map((a) => a.trim())
          .filter(Boolean);
      } else {
        config.url = addUrl.trim();
      }
      const result = await client.addMcpServer(config);
      if (result.error) {
        toast.error(result.error);
      } else {
        toast.success(`Server "${addName}" added`);
        showAddForm = false;
        addName = "";
        addCommand = "";
        addArgs = "";
        addUrl = "";
        servers = await client.getMcpStatus();
      }
    } catch {
      toast.error("Failed to add server");
    } finally {
      adding = false;
    }
  }

  async function installPreset(preset: MCPPreset) {
    // If preset needs env keys and we haven't collected them yet, show config form
    if (preset.env_keys.length > 0 && configuringPreset !== preset.id) {
      configuringPreset = preset.id;
      presetEnvValues = {};
      return;
    }

    installingPreset = preset.id;
    try {
      const client = connectionStore.getClient();
      const env = preset.env_keys.length > 0 ? presetEnvValues : undefined;
      const result = await client.installMcpPreset(preset.id, env);
      if (result.error) {
        toast.error(result.error);
      } else {
        toast.success(`${preset.name} installed${result.connected ? " and connected" : ""}`);
        configuringPreset = null;
        presetEnvValues = {};
        // Refresh
        const [statusResult, presetsResult] = await Promise.allSettled([
          client.getMcpStatus(),
          client.getMcpPresets(),
        ]);
        if (statusResult.status === "fulfilled") servers = statusResult.value;
        if (presetsResult.status === "fulfilled") presets = presetsResult.value;
      }
    } catch {
      toast.error(`Failed to install ${preset.name}`);
    } finally {
      installingPreset = null;
    }
  }
</script>

<div class="flex h-full flex-col gap-4 overflow-y-auto px-4 py-4 md:px-6 md:py-6">
  <!-- Header -->
  <div class="flex items-center gap-3">
    <button
      onclick={() => goto("/")}
      class="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
    >
      <ArrowLeft class="h-4 w-4" />
    </button>
    <Plug class="h-4 w-4 text-muted-foreground" strokeWidth={1.75} />
    <h1 class="text-lg font-semibold text-foreground">MCP Servers</h1>
    <span class="rounded-full bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary">Beta</span>
  </div>

  {#if loading}
    <div class="flex items-center gap-2 py-8 text-sm text-muted-foreground">
      <Loader2 class="h-4 w-4 animate-spin" />
      Loading MCP servers...
    </div>
  {:else}
    <!-- View Toggle -->
    <div class="flex gap-1 rounded-lg border border-border/40 bg-muted/20 p-1">
      <button
        onclick={() => (activeView = "servers")}
        class={[
          "flex-1 rounded-md px-3 py-1.5 text-xs transition-colors",
          activeView === "servers"
            ? "bg-background font-medium text-foreground shadow-sm"
            : "text-muted-foreground hover:text-foreground",
        ].join(" ")}
      >
        My Servers ({serverEntries.length})
      </button>
      <button
        onclick={() => (activeView = "catalog")}
        class={[
          "flex-1 rounded-md px-3 py-1.5 text-xs transition-colors",
          activeView === "catalog"
            ? "bg-background font-medium text-foreground shadow-sm"
            : "text-muted-foreground hover:text-foreground",
        ].join(" ")}
      >
        Catalog ({presets.length})
      </button>
    </div>

    {#if activeView === "servers"}
      <!-- Server List -->
      <div class="flex flex-col gap-2">
        {#if serverEntries.length === 0 && !showAddForm}
          <div class="flex flex-col items-center gap-2 py-8">
            <Plug class="h-8 w-8 text-muted-foreground/40" />
            <p class="text-sm text-muted-foreground">No MCP servers configured</p>
            <p class="text-xs text-muted-foreground/60">Add a server or browse the catalog.</p>
          </div>
        {:else}
          {#each serverEntries as [name, info] (name)}
            <div class="flex items-center gap-3 rounded-lg border border-border/40 bg-muted/20 px-3 py-2.5">
              <!-- Status dot -->
              <CircleDot
                class={[
                  "h-3.5 w-3.5 shrink-0",
                  info.connecting
                    ? "animate-pulse text-amber-500"
                    : info.connected
                      ? "text-emerald-500"
                      : "text-muted-foreground/40",
                ].join(" ")}
              />

              <!-- Info -->
              <div class="flex min-w-0 flex-1 flex-col">
                <div class="flex items-center gap-2">
                  <span class="text-sm font-medium text-foreground">{name}</span>
                  {#if info.connected && info.tool_count > 0}
                    <span class="rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                      {info.tool_count} tools
                    </span>
                  {/if}
                  <span class="rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                    {info.transport}
                  </span>
                </div>
                {#if info.error}
                  <p class="truncate text-[10px] text-red-400">{info.error}</p>
                {/if}
              </div>

              <!-- Actions -->
              <div class="flex shrink-0 items-center gap-1">
                <button
                  onclick={() => toggleServer(name)}
                  disabled={togglingServer === name}
                  class="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
                  title={info.connected ? "Stop" : "Start"}
                >
                  {#if togglingServer === name}
                    <Loader2 class="h-3.5 w-3.5 animate-spin" />
                  {:else if info.connected}
                    <Square class="h-3.5 w-3.5" />
                  {:else}
                    <Play class="h-3.5 w-3.5" />
                  {/if}
                </button>
                <button
                  onclick={() => removeServer(name)}
                  disabled={removingServer === name}
                  class="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive"
                  title="Remove"
                >
                  {#if removingServer === name}
                    <Loader2 class="h-3.5 w-3.5 animate-spin" />
                  {:else}
                    <Trash2 class="h-3.5 w-3.5" />
                  {/if}
                </button>
              </div>
            </div>
          {/each}
        {/if}
      </div>

      <!-- Add Server -->
      {#if showAddForm}
        <div class="rounded-lg border border-border/60 bg-muted/20 p-4">
          <h4 class="mb-3 text-xs font-medium text-muted-foreground">Add Server</h4>
          <div class="flex flex-col gap-3">
            <input
              bind:value={addName}
              type="text"
              placeholder="Server name"
              class="h-9 w-full rounded-lg border border-border bg-muted/50 px-3 text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
            />

            <div class="flex gap-2">
              <button
                onclick={() => (addTransport = "stdio")}
                class={[
                  "flex-1 rounded-lg border px-3 py-1.5 text-xs transition-colors",
                  addTransport === "stdio"
                    ? "border-primary bg-primary/10 text-foreground"
                    : "border-border text-muted-foreground hover:border-primary/50",
                ].join(" ")}
              >
                stdio
              </button>
              <button
                onclick={() => (addTransport = "http")}
                class={[
                  "flex-1 rounded-lg border px-3 py-1.5 text-xs transition-colors",
                  addTransport === "http"
                    ? "border-primary bg-primary/10 text-foreground"
                    : "border-border text-muted-foreground hover:border-primary/50",
                ].join(" ")}
              >
                http
              </button>
            </div>

            {#if addTransport === "stdio"}
              <input
                bind:value={addCommand}
                type="text"
                placeholder="Command (e.g. npx)"
                class="h-9 w-full rounded-lg border border-border bg-muted/50 px-3 text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
              />
              <input
                bind:value={addArgs}
                type="text"
                placeholder="Arguments (e.g. -y @playwright/mcp@latest)"
                class="h-9 w-full rounded-lg border border-border bg-muted/50 px-3 text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
              />
            {:else}
              <input
                bind:value={addUrl}
                type="text"
                placeholder="Server URL (e.g. https://api.example.com/mcp)"
                class="h-9 w-full rounded-lg border border-border bg-muted/50 px-3 text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
              />
            {/if}

            <div class="flex justify-end gap-2">
              <button
                onclick={() => (showAddForm = false)}
                class="rounded-lg border border-border px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
              >
                Cancel
              </button>
              <button
                onclick={addServer}
                disabled={adding || !addName.trim()}
                class="rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-40"
              >
                {#if adding}
                  Adding...
                {:else}
                  Add Server
                {/if}
              </button>
            </div>
          </div>
        </div>
      {:else}
        <button
          onclick={() => (showAddForm = true)}
          class="flex items-center gap-2 rounded-lg border border-dashed border-border px-3 py-2.5 text-xs text-muted-foreground transition-colors hover:border-primary hover:text-foreground"
        >
          <Plus class="h-3.5 w-3.5" />
          Add Custom Server
        </button>
      {/if}

    {:else}
      <!-- Catalog View -->
      <div class="relative">
        <Search class="absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
        <input
          bind:value={catalogSearch}
          type="text"
          placeholder="Search presets..."
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

      <!-- Preset Grid -->
      <div class="flex flex-col gap-2">
        {#if filteredPresets.length === 0}
          <p class="py-6 text-center text-xs text-muted-foreground">No presets match your search</p>
        {:else}
          {#each filteredPresets as preset (preset.id)}
            <div class="rounded-lg border border-border/40 bg-muted/20 px-3 py-2.5">
              <div class="flex items-start gap-3">
                <div class="flex min-w-0 flex-1 flex-col gap-0.5">
                  <div class="flex items-center gap-2">
                    <span class="text-sm font-medium text-foreground">{preset.name}</span>
                    {#if preset.oauth}
                      <span class="rounded-full bg-amber-500/10 px-1.5 py-0.5 text-[10px] text-amber-500">
                        OAuth
                      </span>
                    {/if}
                    <span class="rounded-full bg-muted px-1.5 py-0.5 text-[10px] capitalize text-muted-foreground">
                      {preset.category}
                    </span>
                  </div>
                  <p class="text-xs text-muted-foreground">{preset.description}</p>
                </div>

                <div class="flex shrink-0 items-center gap-1.5">
                  {#if preset.docs_url}
                    <a
                      href={preset.docs_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      class="rounded-md p-1.5 text-muted-foreground transition-colors hover:text-foreground"
                      title="Documentation"
                    >
                      <ExternalLink class="h-3.5 w-3.5" />
                    </a>
                  {/if}

                  {#if preset.installed}
                    <span class="rounded-full bg-emerald-500/10 px-2 py-1 text-[10px] font-medium text-emerald-500">
                      Installed
                    </span>
                  {:else}
                    <button
                      onclick={() => installPreset(preset)}
                      disabled={installingPreset === preset.id}
                      class="rounded-lg bg-primary px-3 py-1.5 text-[10px] font-medium text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-40"
                    >
                      {#if installingPreset === preset.id}
                        <Loader2 class="h-3 w-3 animate-spin" />
                      {:else}
                        Install
                      {/if}
                    </button>
                  {/if}
                </div>
              </div>

              <!-- Env key config form (inline) -->
              {#if configuringPreset === preset.id && !preset.installed}
                <div class="mt-3 flex flex-col gap-2 border-t border-border/30 pt-3">
                  {#each preset.env_keys as envKey (envKey.key)}
                    <label class="flex flex-col gap-1">
                      <span class="text-[10px] font-medium text-muted-foreground">
                        {envKey.label}
                        {#if envKey.required}
                          <span class="text-red-400">*</span>
                        {/if}
                      </span>
                      <input
                        type={envKey.secret ? "password" : "text"}
                        placeholder={envKey.placeholder}
                        value={presetEnvValues[envKey.key] ?? ""}
                        oninput={(e) => {
                          const target = e.target as HTMLInputElement;
                          presetEnvValues = { ...presetEnvValues, [envKey.key]: target.value };
                        }}
                        class="h-8 w-full rounded-lg border border-border bg-muted/50 px-3 text-xs text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
                      />
                    </label>
                  {/each}
                  <div class="flex justify-end gap-2">
                    <button
                      onclick={() => { configuringPreset = null; presetEnvValues = {}; }}
                      class="rounded-lg border border-border px-2.5 py-1 text-[10px] text-muted-foreground transition-colors hover:text-foreground"
                    >
                      Cancel
                    </button>
                    <button
                      onclick={() => installPreset(preset)}
                      disabled={installingPreset === preset.id}
                      class="rounded-lg bg-primary px-2.5 py-1 text-[10px] font-medium text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-40"
                    >
                      {#if installingPreset === preset.id}
                        Installing...
                      {:else}
                        Install
                      {/if}
                    </button>
                  </div>
                </div>
              {/if}
            </div>
          {/each}
        {/if}
      </div>
    {/if}
  {/if}
</div>

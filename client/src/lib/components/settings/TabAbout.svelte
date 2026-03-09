<script lang="ts">
  import type { VersionInfo } from "$lib/api";
  import { connectionStore, settingsStore } from "$lib/stores";
  import { Info, ExternalLink, Loader2 } from "@lucide/svelte";

  let version = $state<VersionInfo | null>(null);
  let loading = $state(true);

  let backend = $derived(settingsStore.settings?.agent_backend ?? version?.agent_backend ?? "unknown");
  let model = $derived(settingsStore.model || "unknown");
  let memoryBackend = $derived(settingsStore.settings?.memory_backend ?? "none");

  $effect(() => {
    loadVersion();
  });

  async function loadVersion() {
    loading = true;
    try {
      const client = connectionStore.getClient();
      version = await client.getVersion();
    } catch {
      version = null;
    } finally {
      loading = false;
    }
  }

  const links = [
    { label: "Documentation", url: "https://pocketpaw.dev/docs" },
    { label: "GitHub", url: "https://github.com/pocketpaw/pocketpaw" },
    { label: "Discord", url: "https://discord.gg/pocketpaw" },
  ];
</script>

<div class="flex flex-col gap-6">
  <div class="flex items-center gap-2">
    <Info class="h-4 w-4 text-muted-foreground" strokeWidth={1.75} />
    <h3 class="text-sm font-semibold text-foreground">About</h3>
  </div>

  {#if loading}
    <div class="flex items-center gap-2 py-4 text-sm text-muted-foreground">
      <Loader2 class="h-4 w-4 animate-spin" />
      Loading version info...
    </div>
  {:else}
    <!-- Version -->
    <div class="flex flex-col gap-3 rounded-lg border border-border/60 bg-muted/30 px-4 py-3">
      <div class="flex items-center justify-between">
        <div class="flex flex-col">
          <span class="text-sm font-medium text-foreground">PocketPaw</span>
          <span class="text-xs text-muted-foreground">
            v{version?.version ?? "unknown"}
          </span>
        </div>
      </div>
    </div>

    <!-- System Info -->
    <div class="flex flex-col gap-2">
      <h4 class="text-xs font-medium text-muted-foreground">System Info</h4>
      <div class="grid grid-cols-2 gap-2 text-xs">
        <div class="flex flex-col gap-0.5 rounded-lg border border-border/40 bg-muted/20 px-3 py-2">
          <span class="text-[10px] text-muted-foreground">Backend</span>
          <span class="text-foreground">{backend}</span>
        </div>
        <div class="flex flex-col gap-0.5 rounded-lg border border-border/40 bg-muted/20 px-3 py-2">
          <span class="text-[10px] text-muted-foreground">Model</span>
          <span class="text-foreground">{model}</span>
        </div>
        <div class="flex flex-col gap-0.5 rounded-lg border border-border/40 bg-muted/20 px-3 py-2">
          <span class="text-[10px] text-muted-foreground">Memory</span>
          <span class="text-foreground">{memoryBackend}</span>
        </div>
        <div class="flex flex-col gap-0.5 rounded-lg border border-border/40 bg-muted/20 px-3 py-2">
          <span class="text-[10px] text-muted-foreground">Python</span>
          <span class="text-foreground">{version?.python ?? "—"}</span>
        </div>
      </div>
    </div>

    <!-- Links -->
    <div class="flex flex-col gap-2">
      <h4 class="text-xs font-medium text-muted-foreground">Links</h4>
      <div class="flex flex-col gap-1">
        {#each links as link (link.label)}
          <a
            href={link.url}
            target="_blank"
            rel="noopener noreferrer"
            class="flex items-center gap-2 rounded-md px-2 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
          >
            <ExternalLink class="h-3 w-3 shrink-0" />
            <span>{link.label}</span>
          </a>
        {/each}
      </div>
    </div>
  {/if}
</div>

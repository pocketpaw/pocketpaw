<script lang="ts">
  import type { VersionInfo } from "$lib/api";
  import { connectionStore, settingsStore } from "$lib/stores";
  import { onMount } from "svelte";
  import {
    Info,
    ExternalLink,
    Loader2,
    RefreshCw,
    Download,
    CheckCircle,
    AlertCircle,
  } from "@lucide/svelte";

  let version = $state<VersionInfo | null>(null);
  let loading = $state(true);

  let backend = $derived(
    settingsStore.settings?.agent_backend ?? version?.agent_backend ?? "unknown",
  );
  let model = $derived(settingsStore.model || "unknown");
  let memoryBackend = $derived(settingsStore.settings?.memory_backend ?? "none");

  // Update state
  type UpdateStatus = "idle" | "checking" | "available" | "downloading" | "ready" | "up-to-date" | "error";
  let updateStatus = $state<UpdateStatus>("idle");
  let updateVersion = $state("");
  let updateNotes = $state("");
  let updateError = $state("");
  let downloadProgress = $state(0);

  onMount(() => {
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

  async function checkForUpdates() {
    updateStatus = "checking";
    updateError = "";

    try {
      const { check } = await import("@tauri-apps/plugin-updater");
      const update = await check();

      if (update) {
        updateVersion = update.version;
        updateNotes = update.body ?? "";
        updateStatus = "available";
      } else {
        updateStatus = "up-to-date";
      }
    } catch (err) {
      updateError = err instanceof Error ? err.message : String(err);
      updateStatus = "error";
    }
  }

  async function downloadAndInstall() {
    updateStatus = "downloading";
    downloadProgress = 0;

    try {
      const { check } = await import("@tauri-apps/plugin-updater");
      const update = await check();
      if (!update) {
        updateStatus = "up-to-date";
        return;
      }

      let totalLength = 0;
      let downloaded = 0;

      await update.downloadAndInstall((event) => {
        if (event.event === "Started") {
          totalLength = event.data.contentLength ?? 0;
        } else if (event.event === "Progress") {
          downloaded += event.data.chunkLength;
          if (totalLength > 0) {
            downloadProgress = Math.round((downloaded / totalLength) * 100);
          }
        } else if (event.event === "Finished") {
          downloadProgress = 100;
        }
      });

      updateStatus = "ready";
    } catch (err) {
      updateError = err instanceof Error ? err.message : String(err);
      updateStatus = "error";
    }
  }

  async function restartApp() {
    const { relaunch } = await import("@tauri-apps/plugin-process");
    await relaunch();
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

    <!-- Updates -->
    <div class="flex flex-col gap-3">
      <h4 class="text-xs font-medium text-muted-foreground">Updates</h4>

      <div class="flex flex-col gap-3 rounded-lg border border-border/60 bg-muted/30 px-4 py-3">
        {#if updateStatus === "idle"}
          <button
            onclick={checkForUpdates}
            class="flex items-center gap-2 rounded-md bg-primary px-3 py-2 text-xs font-medium text-primary-foreground transition-colors hover:bg-primary/90"
          >
            <RefreshCw class="h-3.5 w-3.5" />
            Check for Updates
          </button>

        {:else if updateStatus === "checking"}
          <div class="flex items-center gap-2 py-1 text-xs text-muted-foreground">
            <Loader2 class="h-3.5 w-3.5 animate-spin" />
            Checking for updates...
          </div>

        {:else if updateStatus === "up-to-date"}
          <div class="flex items-center justify-between">
            <div class="flex items-center gap-2 text-xs text-emerald-500">
              <CheckCircle class="h-3.5 w-3.5" />
              You're up to date!
            </div>
            <button
              onclick={checkForUpdates}
              class="rounded-md px-2 py-1 text-[10px] text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
            >
              Check again
            </button>
          </div>

        {:else if updateStatus === "available"}
          <div class="flex flex-col gap-2">
            <div class="flex items-center justify-between">
              <span class="text-xs font-medium text-foreground">
                v{updateVersion} available
              </span>
            </div>
            {#if updateNotes}
              <p class="text-[11px] leading-relaxed text-muted-foreground line-clamp-3">
                {updateNotes}
              </p>
            {/if}
            <button
              onclick={downloadAndInstall}
              class="flex items-center gap-2 rounded-md bg-primary px-3 py-2 text-xs font-medium text-primary-foreground transition-colors hover:bg-primary/90"
            >
              <Download class="h-3.5 w-3.5" />
              Download & Install
            </button>
          </div>

        {:else if updateStatus === "downloading"}
          <div class="flex flex-col gap-2">
            <div class="flex items-center gap-2 text-xs text-muted-foreground">
              <Loader2 class="h-3.5 w-3.5 animate-spin" />
              Downloading update... {downloadProgress}%
            </div>
            <div class="h-1.5 w-full overflow-hidden rounded-full bg-muted">
              <div
                class="h-full rounded-full bg-primary transition-all duration-300"
                style={`width: ${downloadProgress}%`}
              ></div>
            </div>
          </div>

        {:else if updateStatus === "ready"}
          <div class="flex flex-col gap-2">
            <div class="flex items-center gap-2 text-xs text-emerald-500">
              <CheckCircle class="h-3.5 w-3.5" />
              Update installed successfully
            </div>
            <button
              onclick={restartApp}
              class="flex items-center gap-2 rounded-md bg-primary px-3 py-2 text-xs font-medium text-primary-foreground transition-colors hover:bg-primary/90"
            >
              <RefreshCw class="h-3.5 w-3.5" />
              Restart Now
            </button>
          </div>

        {:else if updateStatus === "error"}
          <div class="flex flex-col gap-2">
            <div class="flex items-center gap-2 text-xs text-destructive">
              <AlertCircle class="h-3.5 w-3.5" />
              Update check failed
            </div>
            {#if updateError}
              <p class="text-[10px] text-muted-foreground">{updateError}</p>
            {/if}
            <button
              onclick={checkForUpdates}
              class="flex items-center gap-2 rounded-md border border-border px-3 py-1.5 text-xs text-foreground transition-colors hover:bg-accent"
            >
              <RefreshCw class="h-3 w-3" />
              Try Again
            </button>
          </div>
        {/if}
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

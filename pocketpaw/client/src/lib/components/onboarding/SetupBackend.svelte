<!-- SetupBackend.svelte
  Updated: 2026-03-09 — Redesigned install/start UI with stepped progress,
  elapsed timer, always-visible log, and smarter state messages. -->
<script lang="ts">
  import { onDestroy } from "svelte";
  import { Loader2, Download, Play, AlertCircle, ExternalLink, Box, Sparkles, Layers, Terminal } from "@lucide/svelte";
  import { isTauri } from "$lib/auth";

  type BackendState = "backend_missing" | "backend_stopped" | "installing" | "starting";

  let {
    backendState: initialState,
    onReady,
  }: {
    backendState: BackendState;
    onReady: () => void;
  } = $props();

  let currentState = $state<BackendState>(initialState);
  let installLogs = $state<string[]>([]);
  let error = $state<string | null>(null);
  let logContainer: HTMLDivElement | undefined = $state(undefined);
  let unlistenInstall: (() => void) | null = null;
  let selectedProfile = $state<"minimal" | "recommended" | "full">("recommended");
  let elapsedSeconds = $state(0);
  let elapsedTimer: ReturnType<typeof setInterval> | null = null;
  let installStep = $state<string>("Preparing...");
  let showLogs = $state(false);
  let pollInterval: ReturnType<typeof setInterval> | null = null;

  onDestroy(() => {
    stopTimer();
    unlistenInstall?.();
    if (pollInterval) {
      clearInterval(pollInterval);
      pollInterval = null;
    }
  });

  const profiles = [
    { id: "minimal" as const, label: "Minimal", desc: "Core agent only, no extras", icon: Box },
    { id: "recommended" as const, label: "Recommended", desc: "Dashboard, browser, channels", icon: Sparkles },
    { id: "full" as const, label: "Full", desc: "Everything including experimental", icon: Layers },
  ];

  // Sync prop changes into internal state
  $effect(() => {
    currentState = initialState;
  });

  // Auto-scroll logs
  $effect(() => {
    if (logContainer && installLogs.length) {
      logContainer.scrollTop = logContainer.scrollHeight;
    }
  });

  function startTimer() {
    elapsedSeconds = 0;
    elapsedTimer = setInterval(() => {
      elapsedSeconds++;
    }, 1000);
  }

  function stopTimer() {
    if (elapsedTimer) {
      clearInterval(elapsedTimer);
      elapsedTimer = null;
    }
  }

  function formatElapsed(s: number): string {
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    const sec = s % 60;
    return `${m}m ${sec}s`;
  }

  // Parse installer output to determine the current step
  function parseStep(line: string): string {
    const lower = line.toLowerCase();
    if (lower.includes("python") && lower.includes("not found")) return "Installing Python...";
    if (lower.includes("installing uv") || lower.includes("uv installed")) return "Setting up uv...";
    if (lower.includes("installing python") || lower.includes("python 3.")) return "Installing Python...";
    if (lower.includes("installing pocketpaw") || lower.includes("pip install")) return "Installing PocketPaw...";
    if (lower.includes("creating") && lower.includes("config")) return "Creating config...";
    if (lower.includes("claude code cli")) return "Installing Claude Code CLI...";
    if (lower.includes("complete") || lower.includes("success")) return "Finishing up...";
    if (lower.includes("download")) return "Downloading...";
    return installStep; // keep previous step if no match
  }

  async function startInstall() {
    if (!isTauri()) return;
    currentState = "installing";
    installLogs = [];
    error = null;
    installStep = "Preparing...";
    showLogs = false;
    startTimer();

    try {
      const { listen } = await import("@tauri-apps/api/event");
      const { invoke } = await import("@tauri-apps/api/core");

      unlistenInstall?.();
      unlistenInstall = await listen<{ line: string; done: boolean; success: boolean }>(
        "install-progress",
        (event) => {
          const line = event.payload.line;
          installLogs = [...installLogs, line];
          installStep = parseStep(line);

          if (event.payload.done) {
            unlistenInstall?.();
            unlistenInstall = null;
            stopTimer();
            if (event.payload.success) {
              installStep = "Installation complete!";
              startBackend();
            } else {
              error = "Installation failed. Check the log below for details.";
              showLogs = true;
              currentState = "backend_missing";
            }
          }
        },
      );

      await invoke("install_pocketpaw", { profile: selectedProfile });
    } catch (e: any) {
      stopTimer();
      error = e?.message ?? "Failed to start installer.";
      currentState = "backend_missing";
    }
  }

  async function startBackend() {
    if (!isTauri()) return;
    currentState = "starting";
    error = null;
    startTimer();

    try {
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("start_pocketpaw_backend", { port: 8888 });

      // Poll check_backend_running every 1s for up to 30s
      let attempts = 0;
      if (pollInterval) clearInterval(pollInterval);
      pollInterval = setInterval(async () => {
        attempts++;
        try {
          const running = await invoke<boolean>("check_backend_running", { port: 8888 });
          if (running) {
            clearInterval(pollInterval!);
            pollInterval = null;
            stopTimer();
            onReady();
          } else if (attempts >= 30) {
            clearInterval(pollInterval!);
            pollInterval = null;
            stopTimer();
            error = "Backend did not start within 30 seconds. Open a terminal and run: pocketpaw serve --port 8888\nIf that command is not found, try: python -m pocketpaw serve --port 8888\nor: uv run pocketpaw serve --port 8888";
            currentState = "backend_stopped";
          }
        } catch {
          if (attempts >= 30) {
            clearInterval(pollInterval!);
            pollInterval = null;
            stopTimer();
            error = "Could not verify backend status.";
            currentState = "backend_stopped";
          }
        }
      }, 1000);
    } catch (e: any) {
      stopTimer();
      const msg = typeof e === "string" ? e : e?.message ?? "Failed to start backend.";
      error = msg + "\n\nTry running manually in a terminal:\n  pocketpaw serve --port 8888\n  python -m pocketpaw serve --port 8888";
      currentState = "backend_stopped";
    }
  }

  function retry() {
    error = null;
    if (currentState === "backend_missing") {
      startInstall();
    } else {
      startBackend();
    }
  }
</script>

<div class="flex h-full w-full items-center justify-center">
  <div class="flex w-full max-w-md flex-col items-center gap-6 px-6 text-center">
    <span class="text-5xl">🐾</span>

    {#if currentState === "backend_missing"}
      <div class="flex flex-col gap-2">
        <h1 class="text-2xl font-semibold text-foreground">Install PocketPaw</h1>
        <p class="text-sm text-muted-foreground">
          Choose what to install, then hit the button below.
        </p>
      </div>

      <!-- Profile selector -->
      <div class="flex w-full flex-col gap-2">
        {#each profiles as p}
          {@const Icon = p.icon}
          <button
            onclick={() => (selectedProfile = p.id)}
            class={selectedProfile === p.id
              ? "flex items-center gap-3 rounded-xl border-2 border-primary bg-primary/5 p-3.5 text-left transition-all"
              : "flex items-center gap-3 rounded-xl border-2 border-border p-3.5 text-left transition-all hover:border-primary/30"}
          >
            <div class={selectedProfile === p.id ? "flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-primary/15" : "flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-muted"}>
              <Icon class={selectedProfile === p.id ? "h-4 w-4 text-primary" : "h-4 w-4 text-muted-foreground"} />
            </div>
            <div class="flex flex-col">
              <span class="text-sm font-medium text-foreground">{p.label}</span>
              <span class="text-xs text-muted-foreground">{p.desc}</span>
            </div>
          </button>
        {/each}
      </div>

      <button
        onclick={startInstall}
        class="inline-flex items-center gap-2 rounded-lg bg-primary px-6 py-2.5 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90"
      >
        <Download class="h-4 w-4" />
        Install PocketPaw
      </button>

      <a
        href="https://github.com/pocketpaw/pocketpaw#installation"
        target="_blank"
        rel="noopener noreferrer"
        class="inline-flex items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-foreground"
      >
        Manual installation guide
        <ExternalLink class="h-3 w-3" />
      </a>

    {:else if currentState === "installing"}
      <div class="flex flex-col gap-2">
        <h1 class="text-2xl font-semibold text-foreground">Installing PocketPaw</h1>
        <p class="text-sm text-muted-foreground">
          {installStep}
        </p>
      </div>

      <!-- Progress indicator with elapsed time -->
      <div class="flex w-full flex-col items-center gap-3">
        <div class="flex items-center gap-2">
          <Loader2 class="h-4 w-4 animate-spin text-primary" />
          <span class="text-sm text-muted-foreground">
            {formatElapsed(elapsedSeconds)} elapsed
          </span>
        </div>

        <!-- Progress bar (indeterminate) -->
        <div class="h-1.5 w-full overflow-hidden rounded-full bg-muted">
          <div class="h-full w-1/3 animate-pulse rounded-full bg-primary" style="animation: slide 2s ease-in-out infinite;"></div>
        </div>

        <!-- Latest log line preview -->
        {#if installLogs.length > 0}
          <p class="w-full truncate text-left font-mono text-[11px] text-muted-foreground/60">
            {installLogs[installLogs.length - 1]}
          </p>
        {/if}
      </div>

      <!-- Toggle full log -->
      <button
        onclick={() => showLogs = !showLogs}
        class="inline-flex items-center gap-1.5 text-xs text-muted-foreground transition-colors hover:text-foreground"
      >
        <Terminal class="h-3 w-3" />
        {showLogs ? "Hide" : "Show"} install log
      </button>

      {#if showLogs && installLogs.length > 0}
        <div
          bind:this={logContainer}
          class="max-h-48 w-full overflow-y-auto rounded-lg border border-border bg-muted/50 p-3 text-left font-mono text-xs text-muted-foreground"
        >
          {#each installLogs as line}
            <div>{line}</div>
          {/each}
        </div>
      {/if}

    {:else if currentState === "backend_stopped"}
      <div class="flex flex-col gap-2">
        <h1 class="text-2xl font-semibold text-foreground">Start PocketPaw</h1>
        <p class="text-sm text-muted-foreground">
          PocketPaw is installed. Start the backend server to begin.
        </p>
      </div>

      <button
        onclick={startBackend}
        class="inline-flex items-center gap-2 rounded-lg bg-primary px-6 py-2.5 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90"
      >
        <Play class="h-4 w-4" />
        Start PocketPaw
      </button>

      <p class="text-xs text-muted-foreground/60">
        Or run manually: <code class="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px]">pocketpaw serve</code>
      </p>

    {:else if currentState === "starting"}
      <div class="flex flex-col gap-2">
        <h1 class="text-2xl font-semibold text-foreground">Starting PocketPaw</h1>
        <p class="text-sm text-muted-foreground">Waiting for the server to come online...</p>
      </div>

      <div class="flex flex-col items-center gap-3">
        <div class="flex items-center gap-2">
          <Loader2 class="h-4 w-4 animate-spin text-primary" />
          <span class="text-sm text-muted-foreground">
            {formatElapsed(elapsedSeconds)} elapsed
          </span>
        </div>

        <!-- Progress bar (indeterminate) -->
        <div class="h-1.5 w-full max-w-xs overflow-hidden rounded-full bg-muted">
          <div class="h-full w-1/3 animate-pulse rounded-full bg-primary" style="animation: slide 2s ease-in-out infinite;"></div>
        </div>
      </div>
    {/if}

    {#if error}
      <div class="flex w-full items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-left">
        <AlertCircle class="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
        <div class="flex flex-col gap-2">
          <p class="whitespace-pre-line text-xs text-destructive">{error}</p>
          <button
            onclick={retry}
            class="self-start text-xs font-medium text-primary transition-opacity hover:opacity-80"
          >
            Try Again
          </button>
        </div>
      </div>
    {/if}
  </div>
</div>

<style>
  @keyframes slide {
    0% { transform: translateX(-100%); }
    50% { transform: translateX(200%); }
    100% { transform: translateX(-100%); }
  }
</style>

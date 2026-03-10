<script lang="ts">
  import type { HealthSummary, HealthIssue, HealthErrorEntry } from "$lib/api";
  import { goto } from "$app/navigation";
  import { connectionStore } from "$lib/stores";
  import {
    ArrowLeft,
    HeartPulse,
    RefreshCw,
    Loader2,
    CircleCheck,
    TriangleAlert,
    CircleX,
    Search,
    Trash2,
    ChevronDown,
    ChevronRight,
    ShieldCheck,
  } from "@lucide/svelte";
  import { toast } from "svelte-sonner";

  let health = $state<HealthSummary | null>(null);
  let errors = $state<HealthErrorEntry[]>([]);
  let loading = $state(true);
  let checking = $state(false);
  let errorSearch = $state("");
  let showErrors = $state(false);
  let clearingErrors = $state(false);

  let statusColor = $derived.by(() => {
    switch (health?.status) {
      case "healthy": return "text-emerald-500";
      case "degraded": return "text-amber-500";
      case "unhealthy": return "text-red-500";
      default: return "text-muted-foreground";
    }
  });

  let statusLabel = $derived.by(() => {
    switch (health?.status) {
      case "healthy": return "Healthy";
      case "degraded": return "Degraded";
      case "unhealthy": return "Unhealthy";
      default: return "Unknown";
    }
  });

  let filteredErrors = $derived.by(() => {
    if (!errorSearch.trim()) return errors;
    const q = errorSearch.toLowerCase();
    return errors.filter(
      (e) =>
        e.message.toLowerCase().includes(q) ||
        e.source.toLowerCase().includes(q),
    );
  });

  $effect(() => {
    loadHealth();
  });

  async function loadHealth() {
    loading = true;
    try {
      const client = connectionStore.getClient();
      health = await client.getHealth();
    } catch {
      health = null;
    } finally {
      loading = false;
    }
  }

  async function runCheck() {
    checking = true;
    try {
      const client = connectionStore.getClient();
      health = await client.runHealthCheck();
      toast.success("Health check complete");
    } catch {
      toast.error("Failed to run health check");
    } finally {
      checking = false;
    }
  }

  async function loadErrors() {
    showErrors = true;
    try {
      const client = connectionStore.getClient();
      errors = await client.getHealthErrors(50);
    } catch {
      errors = [];
    }
  }

  async function clearErrors() {
    clearingErrors = true;
    try {
      const client = connectionStore.getClient();
      await client.clearHealthErrors();
      errors = [];
      toast.success("Error log cleared");
    } catch {
      toast.error("Failed to clear error log");
    } finally {
      clearingErrors = false;
    }
  }

  function issueIcon(status: string) {
    switch (status) {
      case "critical": return CircleX;
      case "warning": return TriangleAlert;
      default: return CircleCheck;
    }
  }

  function issueColor(status: string) {
    switch (status) {
      case "critical": return "text-red-500";
      case "warning": return "text-amber-500";
      default: return "text-emerald-500";
    }
  }

  function severityColor(severity: string) {
    switch (severity) {
      case "critical": return "bg-red-500/10 text-red-500";
      case "error": return "bg-red-500/10 text-red-400";
      case "warning": return "bg-amber-500/10 text-amber-500";
      default: return "bg-muted text-muted-foreground";
    }
  }

  function formatTime(dateStr: string): string {
    try {
      const d = new Date(dateStr);
      return d.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      });
    } catch {
      return dateStr;
    }
  }
</script>

<div class="flex h-full flex-col gap-6 overflow-y-auto px-4 py-4 md:px-6 md:py-6">
  <!-- Header -->
  <div class="flex items-center gap-3">
    <button
      onclick={() => goto("/")}
      class="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
    >
      <ArrowLeft class="h-4 w-4" />
    </button>
    <HeartPulse class="h-4 w-4 text-muted-foreground" strokeWidth={1.75} />
    <h1 class="text-lg font-semibold text-foreground">Health</h1>

    {#if health}
      <span class={["ml-2 text-sm font-medium", statusColor].join(" ")}>
        {statusLabel}
      </span>
    {/if}

    <div class="ml-auto">
      <button
        onclick={runCheck}
        disabled={checking}
        class="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-40"
      >
        {#if checking}
          <Loader2 class="h-3 w-3 animate-spin" />
        {:else}
          <RefreshCw class="h-3 w-3" />
        {/if}
        Run Check
      </button>
    </div>
  </div>

  {#if loading}
    <div class="flex items-center gap-2 py-8 text-sm text-muted-foreground">
      <Loader2 class="h-4 w-4 animate-spin" />
      Loading health status...
    </div>
  {:else if !health}
    <div class="flex flex-col items-center gap-2 py-12">
      <HeartPulse class="h-8 w-8 text-muted-foreground/40" />
      <p class="text-sm text-muted-foreground">Could not load health status</p>
      <button
        onclick={runCheck}
        class="mt-2 text-xs text-primary transition-opacity hover:opacity-80"
      >
        Try again
      </button>
    </div>
  {:else}
    <!-- Status Card -->
    <div class="flex items-center gap-4 rounded-lg border border-border/60 bg-muted/20 px-4 py-3">
      <div class={["shrink-0", statusColor].join(" ")}>
        {#if health.status === "healthy"}
          <CircleCheck class="h-8 w-8" />
        {:else if health.status === "degraded"}
          <TriangleAlert class="h-8 w-8" />
        {:else if health.status === "unhealthy"}
          <CircleX class="h-8 w-8" />
        {:else}
          <HeartPulse class="h-8 w-8" />
        {/if}
      </div>
      <div class="flex flex-col">
        <span class="text-sm font-medium text-foreground">
          {health.check_count} checks ran
        </span>
        <span class="text-[10px] text-muted-foreground">
          {health.issues.length === 0
            ? "All systems operational"
            : `${health.issues.length} issue${health.issues.length !== 1 ? "s" : ""} found`}
        </span>
        {#if health.last_check}
          <span class="text-[10px] text-muted-foreground/60">
            Last check: {formatTime(health.last_check)}
          </span>
        {/if}
      </div>
    </div>

    <!-- Diagnostics -->
    {#if health.issues.length > 0}
      <div class="flex flex-col gap-3">
        <div class="flex items-center gap-2">
          <ShieldCheck class="h-3.5 w-3.5 text-muted-foreground" strokeWidth={1.75} />
          <h3 class="text-xs font-medium text-muted-foreground">Diagnostics</h3>
        </div>
        <div class="flex flex-col gap-2">
          {#each health.issues as issue (issue.check_id)}
            {@const Icon = issueIcon(issue.status)}
            <div class="rounded-lg border border-border/40 bg-muted/20 px-3 py-2.5">
              <div class="flex items-start gap-2.5">
                <Icon class={["mt-0.5 h-4 w-4 shrink-0", issueColor(issue.status)].join(" ")} />
                <div class="flex min-w-0 flex-1 flex-col gap-0.5">
                  <div class="flex items-center gap-2">
                    <span class="text-sm font-medium text-foreground">{issue.name}</span>
                    <span class="rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                      {issue.category}
                    </span>
                  </div>
                  <p class="text-xs text-muted-foreground">{issue.message}</p>
                  {#if issue.fix_hint}
                    <p class="mt-1 text-[10px] text-primary/80">{issue.fix_hint}</p>
                  {/if}
                </div>
              </div>
            </div>
          {/each}
        </div>
      </div>
    {/if}

    <!-- Error Log -->
    <div class="border-t border-border/50 pt-4">
      <button
        onclick={showErrors ? () => (showErrors = false) : loadErrors}
        class="flex w-full items-center gap-2 text-left"
      >
        {#if showErrors}
          <ChevronDown class="h-3.5 w-3.5 text-muted-foreground" />
        {:else}
          <ChevronRight class="h-3.5 w-3.5 text-muted-foreground" />
        {/if}
        <span class="text-xs font-medium text-muted-foreground">Recent Errors</span>
      </button>

      {#if showErrors}
        <div class="mt-3 flex flex-col gap-3">
          <!-- Search + Clear -->
          <div class="flex gap-2">
            <div class="relative flex-1">
              <Search class="absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
              <input
                bind:value={errorSearch}
                type="text"
                placeholder="Search errors..."
                class="h-8 w-full rounded-lg border border-border bg-muted/50 pl-9 pr-3 text-xs text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
              />
            </div>
            {#if errors.length > 0}
              <button
                onclick={clearErrors}
                disabled={clearingErrors}
                class="inline-flex items-center gap-1 rounded-lg border border-border px-2 py-1 text-[10px] text-muted-foreground transition-colors hover:border-destructive hover:text-destructive"
              >
                {#if clearingErrors}
                  <Loader2 class="h-3 w-3 animate-spin" />
                {:else}
                  <Trash2 class="h-3 w-3" />
                {/if}
                Clear
              </button>
            {/if}
          </div>

          <!-- Error List -->
          <div class="flex max-h-72 flex-col gap-1.5 overflow-y-auto">
            {#if filteredErrors.length === 0}
              <p class="py-4 text-center text-xs text-muted-foreground">
                {errorSearch ? "No errors match your search" : "No recent errors"}
              </p>
            {:else}
              {#each filteredErrors as error (error.id)}
                <div class="rounded-lg border border-border/40 bg-muted/20 px-3 py-2">
                  <div class="flex items-center gap-2">
                    <span class={["rounded-full px-1.5 py-0.5 text-[10px] font-medium", severityColor(error.severity)].join(" ")}>
                      {error.severity}
                    </span>
                    <span class="text-[10px] text-muted-foreground">{error.source}</span>
                    <span class="ml-auto text-[10px] text-muted-foreground/60">
                      {formatTime(error.timestamp)}
                    </span>
                  </div>
                  <p class="mt-1 text-xs text-foreground">{error.message}</p>
                </div>
              {/each}
            {/if}
          </div>
        </div>
      {/if}
    </div>
  {/if}
</div>

<script lang="ts">
  import { onMount } from "svelte";
  import { metricsStore } from "$lib/stores";
  import { cn } from "$lib/utils";
  import * as Chart from "$lib/components/ui/chart/index.js";
  import { BarChart, PieChart, AreaChart } from "layerchart";
  import {
    RefreshCw,
    Loader2,
    ShieldCheck,
    AlertTriangle,
    CircleCheck,
    CircleX,
    Trash2,
  } from "@lucide/svelte";
  import type { ChartConfig } from "$lib/components/ui/chart/index.js";

  let activeTab = $state<"overview" | "system" | "usage" | "health">("overview");

  const tabs = [
    { id: "overview" as const, label: "Overview" },
    { id: "system" as const, label: "System" },
    { id: "usage" as const, label: "Usage" },
    { id: "health" as const, label: "Health" },
  ];

  // Rolling history for system metrics (last 30 data points, ~2.5 min at 5s intervals)
  let cpuHistory = $state<{ time: string; value: number }[]>([]);
  let memHistory = $state<{ time: string; value: number }[]>([]);

  const MAX_HISTORY = 30;
  let lastSystemTs = 0;

  onMount(() => {
    metricsStore.startAutoRefresh();
    return () => metricsStore.stopAutoRefresh();
  });

  // Push new data points when system metrics update
  // Use a timestamp guard to avoid re-triggering when cpuHistory/memHistory are written
  $effect(() => {
    const s = metricsStore.system;
    if (!s?.available) return;
    const now = Date.now();
    // Only push a new point at most every 3 seconds to avoid effect loops
    if (now - lastSystemTs < 3000) return;
    lastSystemTs = now;
    const t = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    cpuHistory.push({ time: t, value: s.cpu.percent });
    if (cpuHistory.length > MAX_HISTORY) cpuHistory.splice(0, cpuHistory.length - MAX_HISTORY);
    memHistory.push({ time: t, value: s.memory.percent });
    if (memHistory.length > MAX_HISTORY) memHistory.splice(0, memHistory.length - MAX_HISTORY);
  });

  function formatBytes(bytes: number): string {
    const gb = bytes / (1024 ** 3);
    if (gb >= 1) return `${gb.toFixed(1)} GB`;
    return `${(bytes / (1024 ** 2)).toFixed(0)} MB`;
  }

  function formatUptime(seconds: number): string {
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    if (d > 0) return `${d}d ${h}h ${m}m`;
    if (h > 0) return `${h}h ${m}m`;
    return `${m}m`;
  }

  function formatTime(iso: string): string {
    return new Date(iso).toLocaleTimeString();
  }

  function formatCost(usd: number): string {
    if (usd < 0.01) return `$${usd.toFixed(4)}`;
    return `$${usd.toFixed(2)}`;
  }

  function formatTokens(n: number): string {
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
    if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
    return n.toString();
  }

  function percentColor(pct: number): string {
    if (pct >= 90) return "text-red-500";
    if (pct >= 70) return "text-amber-500";
    return "text-emerald-500";
  }

  function healthColor(status: string): string {
    if (status === "healthy") return "text-emerald-500";
    if (status === "degraded") return "text-amber-500";
    return "text-red-500";
  }

  let sys = $derived(metricsStore.system);
  let health = $derived(metricsStore.health);
  let errors = $derived(metricsStore.errors);
  let version = $derived(metricsStore.version);
  let audit = $derived(metricsStore.securityAudit);
  let usage = $derived(metricsStore.usage);
  let recentUsage = $derived(metricsStore.recentUsage);

  // Chart configs
  const cpuChartConfig: ChartConfig = {
    value: { label: "CPU %", color: "var(--chart-1)" },
  };
  const memChartConfig: ChartConfig = {
    value: { label: "Memory %", color: "var(--chart-2)" },
  };
  const diskChartConfig: ChartConfig = {
    used: { label: "Used", color: "var(--chart-1)" },
    free: { label: "Free", color: "var(--chart-3)" },
  };

  let diskData = $derived(
    sys?.available
      ? [
          { name: "Used", value: sys.disk.used_bytes, key: "used" },
          { name: "Free", value: sys.disk.total_bytes - sys.disk.used_bytes, key: "free" },
        ]
      : []
  );

  // Usage chart data
  let modelChartData = $derived(
    usage
      ? Object.entries(usage.by_model).map(([model, data]) => ({
          name: model.length > 20 ? model.slice(0, 18) + ".." : model,
          tokens: data.input_tokens + data.output_tokens,
          cost: data.cost_usd,
        }))
      : []
  );

  const modelChartConfig: ChartConfig = {
    tokens: { label: "Tokens", color: "var(--chart-1)" },
  };

  const costChartConfig: ChartConfig = {
    cost: { label: "Cost ($)", color: "var(--chart-4)" },
  };
</script>

<div class="flex h-full flex-col overflow-hidden">
  <!-- Header with tabs -->
  <div class="shrink-0 border-b border-border">
    <div class="flex items-center justify-between px-4 pt-3 pb-0">
      <div>
        <h1 class="text-sm font-semibold text-foreground">Metrics</h1>
        {#if metricsStore.lastRefresh}
          <p class="text-[10px] text-muted-foreground">Updated {formatTime(metricsStore.lastRefresh)}</p>
        {/if}
      </div>
      <button
        onclick={() => metricsStore.fetchAll()}
        disabled={metricsStore.loading}
        class={cn("rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:opacity-50")}
        title="Refresh"
      >
        {#if metricsStore.loading}
          <Loader2 class="h-3.5 w-3.5 animate-spin" />
        {:else}
          <RefreshCw class="h-3.5 w-3.5" />
        {/if}
      </button>
    </div>
    <div class="flex gap-0 px-4 mt-2">
      {#each tabs as tab (tab.id)}
        <button
          onclick={() => activeTab = tab.id}
          class={cn(
            "px-3 py-1.5 text-[12px] font-medium transition-colors border-b-2 -mb-px",
            activeTab === tab.id
              ? "border-foreground text-foreground"
              : "border-transparent text-muted-foreground hover:text-foreground"
          )}
        >
          {tab.label}
        </button>
      {/each}
    </div>
  </div>

  <!-- Tab content -->
  <div class="flex-1 overflow-y-auto p-4">

    {#if activeTab === "overview"}
      <div class="flex flex-col gap-5">
        <!-- Key stats row -->
        <div class="grid grid-cols-4 gap-3 text-center">
          <div>
            <p class={cn("text-2xl font-bold tabular-nums", sys?.available ? percentColor(sys.cpu.percent) : "text-muted-foreground")}>
              {sys?.available ? `${sys.cpu.percent}%` : "--"}
            </p>
            <p class="text-[10px] text-muted-foreground mt-0.5">CPU</p>
          </div>
          <div>
            <p class={cn("text-2xl font-bold tabular-nums", sys?.available ? percentColor(sys.memory.percent) : "text-muted-foreground")}>
              {sys?.available ? `${sys.memory.percent}%` : "--"}
            </p>
            <p class="text-[10px] text-muted-foreground mt-0.5">Memory</p>
          </div>
          <div>
            <p class="text-2xl font-bold tabular-nums text-foreground">
              {usage ? formatCost(usage.total_cost_usd) : "$0"}
            </p>
            <p class="text-[10px] text-muted-foreground mt-0.5">Spend</p>
          </div>
          <div>
            <p class="text-2xl font-bold tabular-nums text-foreground">
              {usage ? formatTokens(usage.total_tokens) : "0"}
            </p>
            <p class="text-[10px] text-muted-foreground mt-0.5">Tokens</p>
          </div>
        </div>

        <!-- CPU area chart -->
        {#if cpuHistory.length > 1}
          <div>
            <p class="text-[11px] font-medium text-muted-foreground mb-1">CPU Usage</p>
            <Chart.Container config={cpuChartConfig} class="h-[120px] w-full">
              <AreaChart
                data={cpuHistory}
                x="time"
                series={[{ key: "value", color: "var(--chart-1)" }]}
                yDomain={[0, 100]}
              >
                {#snippet tooltip()}
                  <Chart.Tooltip indicator="line" />
                {/snippet}
              </AreaChart>
            </Chart.Container>
          </div>
        {/if}

        <!-- Bottom status row -->
        <div class="flex items-center justify-between text-[11px] text-muted-foreground">
          <span>
            Agent: <span class={cn("font-medium", metricsStore.isAgentWorking ? "text-amber-500" : "text-emerald-500")}>
              {metricsStore.isAgentWorking ? "Working" : "Idle"}
            </span>
          </span>
          <span>
            Health: <span class={cn("font-medium capitalize", health ? healthColor(health.status) : "")}>
              {health?.status ?? "..."}
            </span>
          </span>
          <span>{metricsStore.sessions.length} sessions</span>
          <span>{sys?.available ? formatUptime(sys.uptime_seconds) : "..."} uptime</span>
        </div>

        {#if version}
          <p class="text-[10px] text-muted-foreground">
            PocketPaw v{version.version} / {version.agent_backend}
          </p>
        {/if}
      </div>

    {:else if activeTab === "system"}
      {#if sys?.available}
        <div class="flex flex-col gap-5">
          <!-- CPU chart -->
          <div>
            <div class="flex items-baseline justify-between mb-1">
              <p class="text-[11px] font-medium text-muted-foreground">CPU</p>
              <p class={cn("text-lg font-bold tabular-nums", percentColor(sys.cpu.percent))}>
                {sys.cpu.percent}%
                <span class="text-[10px] font-normal text-muted-foreground ml-1">
                  {sys.cpu.cores} cores{sys.cpu.freq_mhz ? ` @ ${sys.cpu.freq_mhz} MHz` : ""}
                </span>
              </p>
            </div>
            {#if cpuHistory.length > 1}
              <Chart.Container config={cpuChartConfig} class="h-[140px] w-full">
                <AreaChart
                  data={cpuHistory}
                  x="time"
                  series={[{ key: "value", color: "var(--chart-1)" }]}
                  yDomain={[0, 100]}
                >
                  {#snippet tooltip()}
                    <Chart.Tooltip indicator="line" />
                  {/snippet}
                </AreaChart>
              </Chart.Container>
            {/if}
          </div>

          <!-- Memory chart -->
          <div>
            <div class="flex items-baseline justify-between mb-1">
              <p class="text-[11px] font-medium text-muted-foreground">Memory</p>
              <p class={cn("text-lg font-bold tabular-nums", percentColor(sys.memory.percent))}>
                {sys.memory.percent}%
                <span class="text-[10px] font-normal text-muted-foreground ml-1">
                  {formatBytes(sys.memory.used_bytes)} / {formatBytes(sys.memory.total_bytes)}
                </span>
              </p>
            </div>
            {#if memHistory.length > 1}
              <Chart.Container config={memChartConfig} class="h-[140px] w-full">
                <AreaChart
                  data={memHistory}
                  x="time"
                  series={[{ key: "value", color: "var(--chart-2)" }]}
                  yDomain={[0, 100]}
                >
                  {#snippet tooltip()}
                    <Chart.Tooltip indicator="line" />
                  {/snippet}
                </AreaChart>
              </Chart.Container>
            {/if}
          </div>

          <!-- Disk pie -->
          <div>
            <div class="flex items-baseline justify-between mb-1">
              <p class="text-[11px] font-medium text-muted-foreground">Disk</p>
              <p class={cn("text-lg font-bold tabular-nums", percentColor(sys.disk.percent))}>
                {sys.disk.percent}%
                <span class="text-[10px] font-normal text-muted-foreground ml-1">
                  {formatBytes(sys.disk.used_bytes)} / {formatBytes(sys.disk.total_bytes)}
                </span>
              </p>
            </div>
            {#if diskData.length > 0}
              <Chart.Container config={diskChartConfig} class="h-[160px] w-full">
                <PieChart
                  data={diskData}
                  value="value"
                  label="name"
                  series={[
                    { key: "Used", color: "var(--chart-1)" },
                    { key: "Free", color: "var(--chart-3)" },
                  ]}
                  legend
                >
                  {#snippet tooltip()}
                    <Chart.Tooltip />
                  {/snippet}
                </PieChart>
              </Chart.Container>
            {/if}
          </div>

          <!-- Info line -->
          <div class="flex items-center justify-between text-[11px] text-muted-foreground border-t border-border pt-3">
            <span>Uptime: <span class="font-medium text-foreground tabular-nums">{formatUptime(sys.uptime_seconds)}</span></span>
            {#if sys.battery}
              <span>Battery: <span class="font-medium text-foreground tabular-nums">{sys.battery.percent}%{sys.battery.plugged ? " (plugged)" : ""}</span></span>
            {/if}
            <span>Platform: <span class="font-medium text-foreground">{sys.os} ({sys.arch})</span></span>
          </div>
        </div>
      {:else}
        <p class="text-[13px] text-muted-foreground">{sys?.error ?? "Loading system metrics..."}</p>
      {/if}

    {:else if activeTab === "usage"}
      {#if usage && usage.request_count > 0}
        <div class="flex flex-col gap-5">
          <!-- Key numbers -->
          <div class="grid grid-cols-4 gap-3 text-center">
            <div>
              <p class="text-2xl font-bold tabular-nums text-foreground">{formatCost(usage.total_cost_usd)}</p>
              <p class="text-[10px] text-muted-foreground mt-0.5">Total Cost</p>
            </div>
            <div>
              <p class="text-2xl font-bold tabular-nums text-foreground">{formatTokens(usage.total_tokens)}</p>
              <p class="text-[10px] text-muted-foreground mt-0.5">Tokens</p>
            </div>
            <div>
              <p class="text-2xl font-bold tabular-nums text-foreground">{usage.request_count}</p>
              <p class="text-[10px] text-muted-foreground mt-0.5">Requests</p>
            </div>
            <div>
              <p class="text-2xl font-bold tabular-nums text-foreground">{formatTokens(usage.total_cached_input_tokens)}</p>
              <p class="text-[10px] text-muted-foreground mt-0.5">Cached</p>
            </div>
          </div>

          <!-- Token usage by model bar chart -->
          {#if modelChartData.length > 0}
            <div>
              <p class="text-[11px] font-medium text-muted-foreground mb-1">Tokens by Model</p>
              <Chart.Container config={modelChartConfig} class="h-[180px] w-full">
                <BarChart
                  data={modelChartData}
                  x="name"
                  series={[{ key: "tokens", color: "var(--chart-1)" }]}
                >
                  {#snippet tooltip()}
                    <Chart.Tooltip />
                  {/snippet}
                </BarChart>
              </Chart.Container>
            </div>
          {/if}

          <!-- Cost by model bar chart -->
          {#if modelChartData.length > 0}
            <div>
              <p class="text-[11px] font-medium text-muted-foreground mb-1">Cost by Model</p>
              <Chart.Container config={costChartConfig} class="h-[180px] w-full">
                <BarChart
                  data={modelChartData}
                  x="name"
                  series={[{ key: "cost", color: "var(--chart-4)" }]}
                >
                  {#snippet tooltip()}
                    <Chart.Tooltip />
                  {/snippet}
                </BarChart>
              </Chart.Container>
            </div>
          {/if}

          <!-- By backend -->
          {#if Object.keys(usage.by_backend).length > 0}
            <div>
              <p class="text-[11px] font-medium text-muted-foreground mb-2">By Backend</p>
              {#each Object.entries(usage.by_backend) as [backend, data] (backend)}
                <div class="flex items-center justify-between py-1 text-[12px] border-b border-border last:border-0">
                  <span class="text-foreground">{backend}</span>
                  <span class="tabular-nums text-muted-foreground">{data.count} reqs, {formatCost(data.cost_usd)}</span>
                </div>
              {/each}
            </div>
          {/if}

          <!-- Recent requests -->
          {#if recentUsage.length > 0}
            <div>
              <p class="text-[11px] font-medium text-muted-foreground mb-2">Recent Requests</p>
              {#each recentUsage as record, i (record.timestamp)}
                <div class={cn("flex items-center justify-between gap-2 py-1 text-[11px]", i > 0 && "border-t border-border")}>
                  <div class="min-w-0 flex-1">
                    <span class="text-foreground">{record.model || record.backend}</span>
                    <span class="ml-1.5 tabular-nums text-muted-foreground">
                      {formatTokens(record.input_tokens)} in, {formatTokens(record.output_tokens)} out
                    </span>
                  </div>
                  <div class="flex items-center gap-2 shrink-0">
                    {#if record.cost_usd !== null}
                      <span class="tabular-nums font-medium text-foreground">{formatCost(record.cost_usd)}</span>
                    {/if}
                    <span class="tabular-nums text-muted-foreground text-[10px]">{formatTime(record.timestamp)}</span>
                  </div>
                </div>
              {/each}
            </div>
          {/if}

          <button
            onclick={() => metricsStore.clearUsage()}
            class="flex items-center gap-2 text-[12px] text-muted-foreground transition-colors hover:text-foreground"
          >
            <Trash2 class="h-3.5 w-3.5" />
            Clear Usage History
          </button>
        </div>
      {:else}
        <p class="text-[13px] text-muted-foreground">No usage data yet. Token tracking starts when you send messages.</p>
      {/if}

    {:else if activeTab === "health"}
      <div class="flex flex-col gap-4">
        {#if health}
          <!-- Status -->
          <div class="flex items-center gap-3">
            {#if health.status === "healthy"}
              <CircleCheck class="h-5 w-5 text-emerald-500" />
            {:else if health.status === "degraded"}
              <AlertTriangle class="h-5 w-5 text-amber-500" />
            {:else}
              <CircleX class="h-5 w-5 text-red-500" />
            {/if}
            <div>
              <p class={cn("text-lg font-semibold capitalize", healthColor(health.status))}>
                {health.status}
              </p>
              <p class="text-[11px] text-muted-foreground">
                {health.check_count} checks, {health.issues.length} issue{health.issues.length !== 1 ? "s" : ""}
              </p>
            </div>
          </div>

          <!-- Issues -->
          {#if health.issues.length > 0}
            <div>
              <p class="text-[11px] font-medium text-muted-foreground mb-2">Issues</p>
              {#each health.issues as issue (issue.check_id)}
                <div class="flex items-start gap-2 py-1.5 border-b border-border last:border-0">
                  <AlertTriangle class={cn("mt-0.5 h-3.5 w-3.5 shrink-0", issue.status === "critical" ? "text-red-500" : "text-amber-500")} />
                  <div class="min-w-0">
                    <p class="text-[12px] font-medium text-foreground">{issue.name}</p>
                    <p class="text-[11px] text-muted-foreground">{issue.message}</p>
                  </div>
                </div>
              {/each}
            </div>
          {/if}
        {/if}

        <!-- Recent Errors -->
        {#if errors.length > 0}
          <div>
            <p class="text-[11px] font-medium text-muted-foreground mb-2">Recent Errors</p>
            {#each errors as error (error.id)}
              <div class="py-1.5 border-b border-border last:border-0">
                <div class="flex items-baseline justify-between gap-2">
                  <span class={cn("text-[11px] font-medium", error.severity === "critical" ? "text-red-500" : error.severity === "error" ? "text-red-400" : "text-amber-500")}>
                    {error.severity}
                  </span>
                  <span class="shrink-0 text-[10px] tabular-nums text-muted-foreground">{formatTime(error.timestamp)}</span>
                </div>
                <p class="mt-0.5 text-[11px] text-muted-foreground line-clamp-2">{error.message}</p>
              </div>
            {/each}
          </div>
        {/if}

        <!-- Security Audit -->
        <div class="border-t border-border pt-3">
          <button
            onclick={() => metricsStore.runSecurityAudit()}
            class="flex items-center gap-2 text-[13px] text-foreground transition-colors hover:text-muted-foreground"
          >
            <ShieldCheck class="h-4 w-4" />
            Run Security Audit
          </button>

          {#if audit}
            <div class="flex items-center gap-2 mt-2 text-[12px]">
              {#if audit.issues === 0}
                <CircleCheck class="h-4 w-4 text-emerald-500" />
              {:else}
                <AlertTriangle class="h-4 w-4 text-amber-500" />
              {/if}
              <span class="text-foreground">
                {audit.passed}/{audit.total} passed, {audit.issues} issue{audit.issues !== 1 ? "s" : ""}
              </span>
            </div>
          {/if}
        </div>
      </div>
    {/if}
  </div>
</div>

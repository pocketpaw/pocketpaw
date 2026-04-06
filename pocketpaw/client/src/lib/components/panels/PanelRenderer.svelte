<script lang="ts">
  import type { PanelConfig } from "$lib/types/pawkit";
  import MetricsRow from "./MetricsRow.svelte";
  import KanbanBoard from "./KanbanBoard.svelte";
  import Feed from "./Feed.svelte";
  import DataTable from "./DataTable.svelte";
  import AgentRoster from "./AgentRoster.svelte";
  import StandupPanel from "./StandupPanel.svelte";

  let {
    panel,
    data,
    onDataChanged,
  }: { panel: PanelConfig; data: unknown; onDataChanged?: () => void } = $props();
</script>

{#if panel.type === "metrics-row"}
  <MetricsRow config={panel} {data} />
{:else if panel.type === "kanban"}
  <KanbanBoard config={panel} {data} {onDataChanged} />
{:else if panel.type === "feed"}
  <Feed config={panel} {data} />
{:else if panel.type === "table"}
  <DataTable config={panel} {data} {onDataChanged} />
{:else if panel.type === "agent-roster"}
  <AgentRoster config={panel} {data} {onDataChanged} />
{:else if panel.type === "standup"}
  <StandupPanel config={panel} {data} />
{:else}
  <div class="rounded-lg border border-border/50 p-4 text-sm text-muted-foreground">
    Unknown panel type: {panel.type}
  </div>
{/if}

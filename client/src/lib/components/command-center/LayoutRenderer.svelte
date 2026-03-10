<script lang="ts">
  import type { LayoutConfig } from "$lib/types/pawkit";
  import PanelRenderer from "$lib/components/panels/PanelRenderer.svelte";
  import TaskExecutionPanel from "$lib/components/command-center/TaskExecutionPanel.svelte";

  let {
    config,
    kitData,
    onDataChanged,
  }: { config: LayoutConfig; kitData: Record<string, unknown>; onDataChanged?: () => void } =
    $props();

  function sectionSpanClass(span: string): string {
    if (span === "full") return "col-span-2";
    return "col-span-2 sm:col-span-1";
  }

  function resolveData(panel: { source?: unknown; [k: string]: unknown }, data: Record<string, unknown>): unknown {
    const source = panel.source as string | undefined;
    if (source && source in data) return data[source];
    // For metrics-row, pass the whole data object so items can resolve by source
    if (panel.type === "metrics-row") return data["api:stats"] ?? data;
    return data;
  }
</script>

<div class="grid grid-cols-2 gap-4 p-4">
  {#each config.sections as section (section.title)}
    <div class={sectionSpanClass(section.span)}>
      <h3 class="mb-2 text-sm font-medium text-foreground">{section.title}</h3>
      <div class="flex flex-col gap-3">
        {#each section.panels as panel (panel.id)}
          <PanelRenderer {panel} data={resolveData(panel, kitData)} {onDataChanged} />
        {/each}
      </div>
    </div>
  {/each}
</div>

<TaskExecutionPanel />

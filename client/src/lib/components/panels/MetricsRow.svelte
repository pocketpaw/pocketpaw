<script lang="ts">
  import type { PanelConfig } from "$lib/types/pawkit";

  let { config, data }: { config: PanelConfig; data: unknown } = $props();

  interface MetricDef {
    label: string;
    source: string;
    field: string;
    format: string;
    trend?: boolean;
  }

  let items = $derived((config.items as MetricDef[]) ?? []);

  function resolve(obj: unknown, path: string): unknown {
    let cur = obj;
    for (const key of path.split(".")) {
      if (cur == null || typeof cur !== "object") return undefined;
      cur = (cur as Record<string, unknown>)[key];
    }
    return cur;
  }

  function formatValue(val: unknown, fmt: string): string {
    if (val == null) return "—";
    const num = Number(val);
    if (isNaN(num)) return String(val);
    if (fmt === "currency") return `$${num.toLocaleString()}`;
    if (fmt === "percent") return `${num}%`;
    return num.toLocaleString();
  }
</script>

<div class="grid auto-cols-fr grid-flow-col gap-3">
  {#each items as item (item.label)}
    {@const value = resolve(data, item.field)}
    <div class="rounded-xl border border-border bg-card p-4">
      <p class="text-xs text-muted-foreground">{item.label}</p>
      <p class="mt-1 text-2xl font-semibold text-foreground">
        {formatValue(value, item.format)}
      </p>
    </div>
  {/each}
</div>

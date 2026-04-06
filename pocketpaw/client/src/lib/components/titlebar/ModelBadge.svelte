<script lang="ts">
  import { settingsStore } from "$lib/stores";

  const displayModel = $derived.by(() => {
    const m = settingsStore.model;
    if (!m) return settingsStore.agentBackend;
    if (m.includes("claude")) return m.replace("claude-", "Claude ").replace("-latest", "");
    if (m.includes("gpt-")) return m.toUpperCase().replace("GPT-", "GPT-");
    return m;
  });
</script>

{#if displayModel}
  <span
    class="shrink-0 rounded-full bg-muted px-1.5 py-0.5 text-[11px] font-medium text-muted-foreground"
  >
    {displayModel}
  </span>
{/if}

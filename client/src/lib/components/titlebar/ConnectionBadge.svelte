<script lang="ts">
  import { connectionStore, activityStore, settingsStore } from "$lib/stores";
  import * as Tooltip from "$lib/components/ui/tooltip";

  const dotColor = $derived.by(() => {
    if (connectionStore.status === "disconnected") return "bg-paw-error";
    if (activityStore.isAgentWorking) return "bg-paw-warning animate-pulse-dot";
    return "bg-paw-success";
  });

  const tooltipText = $derived.by(() => {
    if (connectionStore.isOffline) {
      return "Offline · No network connection";
    }
    if (connectionStore.status === "disconnected") {
      if (connectionStore.consecutiveFailures >= 5) {
        return "Disconnected · Backend may not be running";
      }
      return connectionStore.error
        ? `Disconnected · ${connectionStore.error}`
        : "Disconnected · Reconnecting...";
    }
    if (connectionStore.status === "connecting") {
      return "Connecting...";
    }
    if (activityStore.isAgentWorking) {
      const latest = activityStore.latestEntry;
      return latest
        ? `Working · ${latest.content}`
        : "Working...";
    }
    const model = settingsStore.model || settingsStore.agentBackend;
    return `Connected · ${model}`;
  });

  const dotClass = $derived(`h-[6px] w-[6px] rounded-full ${dotColor}`);
</script>

<Tooltip.Root>
  <Tooltip.Trigger>
    <div class="flex h-7 w-7 items-center justify-center">
      <div class={dotClass}></div>
    </div>
  </Tooltip.Trigger>
  <Tooltip.Content>
    <p class="text-xs">{tooltipText}</p>
  </Tooltip.Content>
</Tooltip.Root>

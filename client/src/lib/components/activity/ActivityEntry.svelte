<script lang="ts">
  import type { ActivityEntry } from "$lib/stores";
  import { platformStore } from "$lib/stores";

  let { entry }: { entry: ActivityEntry } = $props();

  let expanded = $state(false);
  let isLong = $derived(entry.content.length > 100);
  let displayContent = $derived(
    !expanded && isLong ? entry.content.slice(0, 100) + "..." : entry.content,
  );

  let timeStr = $derived.by(() => {
    try {
      const d = new Date(entry.timestamp);
      return d.toLocaleTimeString("en-US", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
    } catch {
      return "--:--:--";
    }
  });

  let statusIcon = $derived.by(() => {
    switch (entry.type) {
      case "tool_start":
        return { icon: "\u25CF", color: "text-paw-warning", animate: true };
      case "tool_result":
        return { icon: "\u2713", color: "text-paw-success", animate: false };
      case "thinking":
        return { icon: "\u25CF", color: "text-paw-info", animate: true };
      case "error":
        return { icon: "\u2715", color: "text-paw-error", animate: false };
      case "status":
        return { icon: "\u2139", color: "text-muted-foreground", animate: false };
      default:
        return { icon: "\u25CF", color: "text-muted-foreground", animate: false };
    }
  });

  let typeLabel = $derived.by(() => {
    switch (entry.type) {
      case "tool_start": return "Tool:";
      case "tool_result": return "Result:";
      case "thinking": return "Thinking:";
      case "error": return "Error:";
      case "status": return "Status:";
      default: return "";
    }
  });
</script>

<button
  onclick={() => { if (isLong) expanded = !expanded; }}
  class={platformStore.isTouch
    ? "flex w-full items-start gap-2 rounded px-3 py-2 text-left transition-colors active:bg-muted/40"
    : "flex w-full items-start gap-2 rounded px-2 py-1 text-left transition-colors hover:bg-muted/40"}
  class:cursor-pointer={isLong}
  class:cursor-default={!isLong}
>
  <span class="shrink-0 font-mono text-[10px] text-muted-foreground/60">{timeStr}</span>
  <span
    class={`shrink-0 text-[10px] ${statusIcon.color}`}
    class:animate-pulse={statusIcon.animate}
  >
    {statusIcon.icon}
  </span>
  <span class={platformStore.isTouch ? "min-w-0 flex-1 text-xs leading-relaxed text-muted-foreground" : "min-w-0 flex-1 text-[11px] leading-relaxed text-muted-foreground"}>
    <span class="font-medium text-foreground/70">{typeLabel}</span>
    {displayContent}
  </span>
</button>

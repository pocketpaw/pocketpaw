<script lang="ts">
  import { BrainCircuit, Fingerprint, HeartPulse, Plug, Wand, Activity, Settings } from "@lucide/svelte";
  import type { Component } from "svelte";
  import { page } from "$app/state";
  import { platformStore } from "$lib/stores";

  const tools: { href: string; label: string; icon: Component<any> }[] = [
    { href: "/memory", label: "Memory", icon: BrainCircuit },
    { href: "/identity", label: "Identity", icon: Fingerprint },
    { href: "/explore", label: "Skills", icon: Wand },
    { href: "/health", label: "Health", icon: HeartPulse },
    { href: "/mcp", label: "MCP", icon: Plug },
    { href: "/settings", label: "Settings", icon: Settings },
  ];

  let pathname = $derived(page.url.pathname);
  let activityActive = $derived(pathname === "/activity");
</script>

<div class="border-t border-sidebar-border px-2 py-2">
  <!-- Activity link -->
  <a
    href="/activity"
    class={[
      "mb-1.5 flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-[12px] transition-colors duration-100",
      activityActive
        ? "bg-paw-accent-subtle font-medium text-foreground"
        : platformStore.isTouch
          ? "text-muted-foreground active:bg-accent active:text-foreground"
          : "text-muted-foreground hover:bg-accent hover:text-foreground",
    ].join(" ")}
  >
    <Activity class="h-3.5 w-3.5 shrink-0" strokeWidth={1.75} />
    <span>Activity</span>
  </a>

  <!-- Tool grid -->
  <div class="grid grid-cols-3 gap-1">
    {#each tools as tool (tool.href)}
      {@const Icon = tool.icon}
      {@const isActive = pathname === tool.href}
      <a
        href={tool.href}
        class={[
          "flex flex-col items-center gap-1 rounded-lg py-1.5 transition-colors duration-100",
          isActive
            ? "bg-paw-accent-subtle text-foreground"
            : platformStore.isTouch
              ? "text-muted-foreground active:bg-accent active:text-foreground"
              : "text-muted-foreground hover:bg-accent hover:text-foreground",
        ].join(" ")}
        title={tool.label}
      >
        <Icon class="h-3.5 w-3.5" strokeWidth={1.75} />
        <span class="text-[9px] leading-none">{tool.label}</span>
      </a>
    {/each}
  </div>
</div>

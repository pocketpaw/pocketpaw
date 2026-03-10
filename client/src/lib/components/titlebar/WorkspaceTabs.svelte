<script lang="ts">
  import { page } from "$app/state";
  import { goto } from "$app/navigation";
  import { MessageSquare, FolderOpen, Rocket, FolderKanban } from "@lucide/svelte";
  import { Tabs, TabsList, TabsTrigger } from "$lib/components/ui/tabs";
  import type { Component } from "svelte";

  const tabs: { value: string; label: string; icon: Component<any>; match: (p: string) => boolean; disabled?: boolean }[] = [
    { value: "/chat", label: "Chat", icon: MessageSquare, match: (p) => p.startsWith("/chat") },
    { value: "/", label: "Files", icon: FolderOpen, match: (p) => p === "/" },
    { value: "/command-center", label: "PawKits", icon: Rocket, match: (p) => p.startsWith("/command-center"), disabled: true },
    { value: "/projects", label: "Deep Work", icon: FolderKanban, match: (p) => p.startsWith("/projects"), disabled: true },
  ];

  let { visible = true }: { visible?: boolean } = $props();

  let pathname = $derived(page.url.pathname);
  let activeTab = $derived(tabs.find((t) => t.match(pathname))?.value ?? "/chat");
</script>

{#if visible}
<Tabs
  value={activeTab}
  onValueChange={(v) => { if (v) goto(v, { noScroll: true }); }}
  class="flex items-center"
>
  <TabsList class="bg-transparent  data-[state=active]:border-foreground/80">
    {#each tabs as tab (tab.value)}
      {@const Icon = tab.icon}
      <TabsTrigger
        value={tab.value}
        class="h-6 px-2.5 text-xs"
        disabled={tab.disabled}
      >
        <Icon class="size-2" strokeWidth={2} />
        {tab.label}
      </TabsTrigger>
    {/each}
  </TabsList>
</Tabs>
{/if}

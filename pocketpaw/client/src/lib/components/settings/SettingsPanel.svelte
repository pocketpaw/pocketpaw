<script lang="ts">
  import { goto } from "$app/navigation";
  import { toggleMode, mode } from "mode-watcher";
  import { ArrowLeft, Bot, Radio, Shield, Info, Sun, Moon } from "@lucide/svelte";
  import type { Component } from "svelte";
  import TabAIModel from "./TabAIModel.svelte";
  import TabChannels from "./TabChannels.svelte";
  import TabSecurity from "./TabSecurity.svelte";
  import TabAbout from "./TabAbout.svelte";

  interface Section {
    id: string;
    label: string;
    icon: Component<any>;
    component: Component<any>;
  }

  const sections: Section[] = [
    { id: "ai-model", label: "AI Model", icon: Bot, component: TabAIModel },
    { id: "channels", label: "Channels", icon: Radio, component: TabChannels },
    { id: "security", label: "Security", icon: Shield, component: TabSecurity },
    { id: "about", label: "About", icon: Info, component: TabAbout },
  ];

  let activeSection = $state("ai-model");
  let isDark = $derived(mode.current === "dark");

  // Mobile: show section list or content
  let mobileShowContent = $state(false);

  let currentSection = $derived(sections.find((s) => s.id === activeSection)!);

  function selectSection(id: string) {
    activeSection = id;
    mobileShowContent = true;
  }

  function mobileBack() {
    mobileShowContent = false;
  }

  async function handleToggleMode() {
    const willBeDark = !isDark;
    toggleMode();
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("set_vibrancy_theme", { dark: willBeDark });
    } catch {
      // Not in Tauri or command not available
    }
  }
</script>

<div class="flex h-full flex-col overflow-hidden px-4 py-4 md:px-6 md:py-6">
  <!-- Header -->
  <div class="mb-4 flex items-center gap-3">
    <button
      onclick={() => goto("/")}
      class="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
    >
      <ArrowLeft class="h-4 w-4" />
    </button>
    <h1 class="text-lg font-semibold text-foreground">Settings</h1>
    <div class="ml-auto">
      <button
        onclick={handleToggleMode}
        class="flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
      >
        {#if isDark}
          <Sun class="h-4 w-4" />
        {:else}
          <Moon class="h-4 w-4" />
        {/if}
      </button>
    </div>
  </div>

  <!-- Desktop: two-column layout -->
  <div class="hidden flex-1 gap-6 overflow-hidden md:flex">
    <!-- Left nav -->
    <nav class="flex w-44 shrink-0 flex-col gap-1">
      {#each sections as section (section.id)}
        {@const Icon = section.icon}
        <button
          onclick={() => (activeSection = section.id)}
          class={[
            "flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left text-sm transition-colors",
            activeSection === section.id
              ? "bg-card font-medium text-foreground"
              : "text-muted-foreground hover:bg-card/50 hover:text-foreground",
          ].join(" ")}
        >
          <Icon class="h-4 w-4 shrink-0" strokeWidth={1.75} />
          {section.label}
        </button>
      {/each}
    </nav>

    <!-- Right content -->
    <div class="flex-1 overflow-y-auto rounded-lg border border-border/40 bg-muted/10 p-6">
      {#each sections as section (section.id)}
        {#if section.id === activeSection}
          {@const Content = section.component}
          <Content />
        {/if}
      {/each}
    </div>
  </div>

  <!-- Mobile: list / content toggle -->
  <div class="flex flex-1 flex-col overflow-hidden md:hidden">
    {#if !mobileShowContent}
      <!-- Section list -->
      <nav class="flex flex-col gap-1">
        {#each sections as section (section.id)}
          {@const Icon = section.icon}
          <button
            onclick={() => selectSection(section.id)}
            class="flex w-full items-center gap-3 rounded-lg px-3 py-3 text-left text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
          >
            <Icon class="h-4 w-4 shrink-0" strokeWidth={1.75} />
            <span>{section.label}</span>
            <span class="ml-auto text-muted-foreground/40">&rsaquo;</span>
          </button>
        {/each}
      </nav>
    {:else}
      <!-- Section content with back -->
      <div class="flex flex-col gap-4 overflow-hidden">
        <button
          onclick={mobileBack}
          class="flex items-center gap-1.5 self-start text-xs text-muted-foreground transition-colors hover:text-foreground"
        >
          <ArrowLeft class="h-3 w-3" />
          Back
        </button>
        <div class="flex-1 overflow-y-auto">
          {#each sections as section (section.id)}
            {#if section.id === activeSection}
              {@const Content = section.component}
              <Content />
            {/if}
          {/each}
        </div>
      </div>
    {/if}
  </div>
</div>

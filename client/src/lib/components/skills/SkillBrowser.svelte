<script lang="ts">
  import type { Skill } from "$lib/api";
  import type { Component } from "svelte";
  import { goto } from "$app/navigation";
  import { skillStore } from "$lib/stores";
  import { Zap, BarChart3, PenTool, Wrench, FolderOpen, Package, ArrowLeft, Search, Download, Trash2, Loader2, X } from "@lucide/svelte";
  import SkillCategory from "./SkillCategory.svelte";
  import SkillCard from "./SkillCard.svelte";
  import SkillForm from "./SkillForm.svelte";
  import { toast } from "svelte-sonner";

  let {
    initialCategory = "",
  }: {
    initialCategory?: string;
  } = $props();

  let activeView = $state<"my-skills" | "library">("my-skills");
  let searchQuery = $state("");
  let selectedSkill = $state<Skill | null>(null);
  let formOpen = $state(false);

  // Library state
  let libraryQuery = $state("");
  let librarySearching = $state(false);
  let libraryResults = $state<Skill[]>([]);
  let installingSkill = $state<string | null>(null);
  let removingSkill = $state<string | null>(null);
  let searchDebounce: ReturnType<typeof setTimeout> | undefined;

  // Default category definitions with skill name patterns
  const CATEGORIES: { id: string; name: string; icon: Component<any>; patterns: string[] }[] = [
    { id: "quick", name: "Quick Tasks", icon: Zap, patterns: ["summarize", "translate", "draft", "email", "quick"] },
    { id: "analyze", name: "Analyze Data", icon: BarChart3, patterns: ["analyze", "expense", "csv", "data", "chart", "compare", "report"] },
    { id: "write", name: "Write Content", icon: PenTool, patterns: ["write", "blog", "social", "content", "post", "article"] },
    { id: "dev", name: "Dev Tools", icon: Wrench, patterns: ["debug", "code", "review", "explain", "test", "dev", "git"] },
    { id: "files", name: "File Management", icon: FolderOpen, patterns: ["file", "organize", "convert", "rename", "folder"] },
  ];

  let skills = $derived(skillStore.skills);
  let installedNames = $derived(new Set(skills.map((s) => s.name)));

  // Find the active category definition (if one is selected via URL)
  let activeCategoryDef = $derived(
    initialCategory ? CATEGORIES.find((c) => c.id === initialCategory) ?? null : null,
  );

  // Categorize skills
  let categorizedSkills = $derived.by(() => {
    const result: { id: string; name: string; icon: Component<any>; skills: Skill[] }[] = [];
    const assigned = new Set<string>();

    const categoriesToShow = activeCategoryDef ? [activeCategoryDef] : CATEGORIES;

    for (const cat of categoriesToShow) {
      const matched = skills.filter((s) => {
        const lower = (s.name + " " + s.description).toLowerCase();
        return cat.patterns.some((p) => lower.includes(p));
      });
      if (matched.length > 0 || cat.id === initialCategory) {
        result.push({ ...cat, skills: matched });
        for (const s of matched) assigned.add(s.name);
      }
    }

    if (!activeCategoryDef) {
      const other = skills.filter((s) => !assigned.has(s.name));
      if (other.length > 0) {
        result.push({ id: "other", name: "Other", icon: Package, skills: other });
      }
    }

    return result;
  });

  // Filtered by local search
  let filteredSkills = $derived.by(() => {
    if (!searchQuery.trim()) return [];
    const q = searchQuery.toLowerCase();
    return skills.filter(
      (s) => s.name.toLowerCase().includes(q) || s.description.toLowerCase().includes(q),
    );
  });

  let isSearchActive = $derived(searchQuery.trim().length > 0);

  function handleUseSkill(skill: Skill) {
    selectedSkill = skill;
    formOpen = true;
  }

  // Load skills on mount
  $effect(() => {
    if (skills.length === 0) {
      skillStore.load();
    }
  });

  // Library search with debounce
  function handleLibrarySearch() {
    clearTimeout(searchDebounce);
    if (!libraryQuery.trim()) {
      libraryResults = [];
      return;
    }
    searchDebounce = setTimeout(async () => {
      librarySearching = true;
      try {
        await skillStore.search(libraryQuery);
        libraryResults = skillStore.searchResults;
      } catch {
        libraryResults = [];
      } finally {
        librarySearching = false;
      }
    }, 300);
  }

  async function installSkill(identifier: string) {
    installingSkill = identifier;
    try {
      await skillStore.install(identifier);
      toast.success(`Installed "${identifier}"`);
    } catch {
      toast.error(`Failed to install "${identifier}"`);
    } finally {
      installingSkill = null;
    }
  }

  async function removeSkill(name: string) {
    removingSkill = name;
    try {
      await skillStore.remove(name);
      toast.success(`Removed "${name}"`);
    } catch {
      toast.error(`Failed to remove "${name}"`);
    } finally {
      removingSkill = null;
    }
  }
</script>

<div class="flex h-full flex-col gap-4 overflow-y-auto px-4 py-4 md:px-6 md:py-6">
  <!-- Header -->
  <div class="flex items-center gap-3">
    <button
      onclick={() => activeCategoryDef ? goto("/explore") : goto("/")}
      class="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
    >
      <ArrowLeft class="h-4 w-4" />
    </button>
    <div>
      {#if activeCategoryDef}
        <h1 class="text-lg font-semibold text-foreground">{activeCategoryDef.name}</h1>
        <p class="text-xs text-muted-foreground">Skills in this category</p>
      {:else}
        <h1 class="text-lg font-semibold text-foreground">Skills</h1>
        <p class="text-xs text-muted-foreground">{skills.length} installed</p>
      {/if}
    </div>
  </div>

  <!-- View Toggle -->
  {#if !activeCategoryDef}
    <div class="flex gap-1 rounded-lg border border-border/40 bg-muted/20 p-1">
      <button
        onclick={() => (activeView = "my-skills")}
        class={[
          "flex-1 rounded-md px-3 py-1.5 text-xs transition-colors",
          activeView === "my-skills"
            ? "bg-background font-medium text-foreground shadow-sm"
            : "text-muted-foreground hover:text-foreground",
        ].join(" ")}
      >
        My Skills
      </button>
      <button
        onclick={() => (activeView = "library")}
        class={[
          "flex-1 rounded-md px-3 py-1.5 text-xs transition-colors",
          activeView === "library"
            ? "bg-background font-medium text-foreground shadow-sm"
            : "text-muted-foreground hover:text-foreground",
        ].join(" ")}
      >
        Library
      </button>
    </div>
  {/if}

  {#if activeView === "my-skills" || activeCategoryDef}
    <!-- My Skills: Search -->
    <div class="relative">
      <Search class="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
      <input
        bind:value={searchQuery}
        placeholder="Search installed skills..."
        class="h-9 w-full rounded-lg border border-border bg-muted/50 pl-9 pr-9 text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
      />
      {#if searchQuery}
        <button
          onclick={() => (searchQuery = "")}
          class="absolute right-2 top-1/2 -translate-y-1/2 rounded-sm p-1 text-muted-foreground transition-colors hover:text-foreground"
        >
          <X class="h-3.5 w-3.5" />
        </button>
      {/if}
    </div>

    <!-- My Skills: Content -->
    <div class="flex flex-col gap-6">
      {#if skillStore.isLoading}
        <div class="flex items-center justify-center py-8">
          <Loader2 class="mr-2 h-4 w-4 animate-spin text-muted-foreground" />
          <p class="text-sm text-muted-foreground">Loading skills...</p>
        </div>
      {:else if isSearchActive}
        {#if filteredSkills.length === 0}
          <p class="py-8 text-center text-sm text-muted-foreground">
            No skills matching "{searchQuery}"
          </p>
        {:else}
          <div class="flex flex-col gap-2">
            <p class="text-xs text-muted-foreground">
              {filteredSkills.length} result{filteredSkills.length === 1 ? "" : "s"}
            </p>
            <div class="grid grid-cols-2 gap-2">
              {#each filteredSkills as skill (skill.name)}
                <SkillCard {skill} onUse={handleUseSkill} />
              {/each}
            </div>
          </div>
        {/if}
      {:else if skills.length === 0}
        <div class="flex flex-col items-center gap-3 py-8 text-center">
          <Package class="h-8 w-8 text-muted-foreground/40" strokeWidth={1.5} />
          <p class="text-sm text-muted-foreground">No skills installed yet.</p>
          <button
            onclick={() => (activeView = "library")}
            class="text-xs text-primary transition-opacity hover:opacity-80"
          >
            Browse the library to install skills
          </button>
        </div>
      {:else}
        {#each categorizedSkills as cat (cat.id)}
          <SkillCategory
            name={cat.name}
            icon={cat.icon}
            skills={cat.skills}
            onUseSkill={handleUseSkill}
          />
        {/each}
      {/if}
    </div>

  {:else}
    <!-- Library View -->
    <div class="relative">
      <Search class="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
      <input
        bind:value={libraryQuery}
        oninput={handleLibrarySearch}
        placeholder="Search skills.sh..."
        class="h-9 w-full rounded-lg border border-border bg-muted/50 pl-9 pr-9 text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
      />
      {#if libraryQuery}
        <button
          onclick={() => { libraryQuery = ""; libraryResults = []; }}
          class="absolute right-2 top-1/2 -translate-y-1/2 rounded-sm p-1 text-muted-foreground transition-colors hover:text-foreground"
        >
          <X class="h-3.5 w-3.5" />
        </button>
      {/if}
    </div>

    {#if librarySearching}
      <div class="flex items-center justify-center py-8">
        <Loader2 class="mr-2 h-4 w-4 animate-spin text-muted-foreground" />
        <p class="text-sm text-muted-foreground">Searching...</p>
      </div>
    {:else if libraryResults.length > 0}
      <div class="flex flex-col gap-2">
        <p class="text-xs text-muted-foreground">{libraryResults.length} results</p>
        {#each libraryResults as skill (skill.name)}
          <div class="flex items-center gap-3 rounded-lg border border-border/40 bg-muted/20 px-3 py-2.5">
            <div class="flex min-w-0 flex-1 flex-col gap-0.5">
              <div class="flex items-center gap-2">
                <span class="text-sm font-medium text-foreground">{skill.name}</span>
                {#if skill.argument_hint}
                  <span class="rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                    {skill.argument_hint}
                  </span>
                {/if}
              </div>
              <p class="text-xs text-muted-foreground">{skill.description}</p>
            </div>
            <div class="shrink-0">
              {#if installedNames.has(skill.name)}
                <span class="rounded-full bg-emerald-500/10 px-2 py-1 text-[10px] font-medium text-emerald-500">
                  Installed
                </span>
              {:else}
                <button
                  onclick={() => installSkill(skill.name)}
                  disabled={installingSkill === skill.name}
                  class="inline-flex items-center gap-1 rounded-lg bg-primary px-3 py-1.5 text-[10px] font-medium text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-40"
                >
                  {#if installingSkill === skill.name}
                    <Loader2 class="h-3 w-3 animate-spin" />
                  {:else}
                    <Download class="h-3 w-3" />
                  {/if}
                  Install
                </button>
              {/if}
            </div>
          </div>
        {/each}
      </div>
    {:else if libraryQuery.trim()}
      <p class="py-8 text-center text-sm text-muted-foreground">
        No results for "{libraryQuery}"
      </p>
    {:else}
      <div class="flex flex-col items-center gap-3 py-8 text-center">
        <Search class="h-8 w-8 text-muted-foreground/40" strokeWidth={1.5} />
        <p class="text-sm text-muted-foreground">Search the skills.sh registry</p>
        <p class="text-xs text-muted-foreground/60">
          Find and install community skills for your agent.
        </p>
      </div>
    {/if}

    <!-- Installed skills (removable) -->
    {#if skills.length > 0}
      <div class="border-t border-border/50 pt-4">
        <h4 class="mb-2 text-xs font-medium text-muted-foreground">Installed ({skills.length})</h4>
        <div class="flex flex-col gap-1.5">
          {#each skills as skill (skill.name)}
            <div class="group flex items-center gap-3 rounded-lg border border-border/30 bg-muted/10 px-3 py-2">
              <div class="flex min-w-0 flex-1 flex-col">
                <span class="text-xs font-medium text-foreground">{skill.name}</span>
                <span class="truncate text-[10px] text-muted-foreground">{skill.description}</span>
              </div>
              <button
                onclick={() => removeSkill(skill.name)}
                disabled={removingSkill === skill.name}
                class="shrink-0 rounded-md p-1 text-muted-foreground opacity-0 transition-all hover:bg-destructive/10 hover:text-destructive group-hover:opacity-100"
                title="Remove skill"
              >
                {#if removingSkill === skill.name}
                  <Loader2 class="h-3 w-3 animate-spin" />
                {:else}
                  <Trash2 class="h-3 w-3" />
                {/if}
              </button>
            </div>
          {/each}
        </div>
      </div>
    {/if}
  {/if}
</div>

<SkillForm skill={selectedSkill} bind:open={formOpen} />

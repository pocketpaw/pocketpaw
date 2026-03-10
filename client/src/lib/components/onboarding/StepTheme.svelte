<script lang="ts">
  import { setMode } from "mode-watcher";
  import { Sun, Moon, Monitor } from "@lucide/svelte";
  import Typewriter from "./Typewriter.svelte";

  let {
    initialTheme = "system",
    onNext,
  }: {
    initialTheme?: string;
    onNext: (theme: string) => void;
  } = $props();

  let selected = $state(initialTheme);
  let showCards = $state(false);

  const themes = [
    { id: "light", label: "Light", icon: Sun, preview: "bg-white", previewAccent: "bg-gray-200" },
    { id: "dark", label: "Dark", icon: Moon, preview: "bg-zinc-900", previewAccent: "bg-zinc-700" },
    { id: "system", label: "System", icon: Monitor, preview: "bg-gradient-to-r from-white to-zinc-900", previewAccent: "bg-gray-400" },
  ];

  function pick(id: string) {
    selected = id;
    if (id === "light") setMode("light");
    else if (id === "dark") setMode("dark");
    else setMode("system");
  }
</script>

<div class="flex w-full max-w-lg flex-col items-center gap-8">
  <h2 class="text-center text-xl font-semibold text-foreground">
    <Typewriter text="How do you like it?" speed={40} onDone={() => (showCards = true)} />
  </h2>

  {#if showCards}
    <div class="grid w-full grid-cols-3 gap-4 animate-in fade-in slide-in-from-bottom-4 duration-500">
      {#each themes as theme}
        {@const Icon = theme.icon}
        <button
          onclick={() => pick(theme.id)}
          class="group flex flex-col items-center gap-3 rounded-2xl border-2 p-5 transition-all duration-200
            {selected === theme.id
              ? 'border-primary bg-primary/5 shadow-lg shadow-primary/10'
              : 'border-border hover:border-primary/30 hover:bg-accent/30'}"
        >
          <div class="h-20 w-full rounded-lg {theme.preview} flex flex-col gap-1.5 p-2.5 shadow-inner">
            <div class="h-1.5 w-8 rounded-full {theme.previewAccent}"></div>
            <div class="h-1.5 w-12 rounded-full {theme.previewAccent}"></div>
            <div class="h-1.5 w-6 rounded-full {theme.previewAccent}"></div>
          </div>

          <div class="flex items-center gap-2">
            <Icon class="h-4 w-4 text-muted-foreground" />
            <span class="text-sm font-medium text-foreground">{theme.label}</span>
          </div>
        </button>
      {/each}
    </div>

    <button
      onclick={() => onNext(selected)}
      class="rounded-xl bg-primary px-6 py-3 text-sm font-medium text-primary-foreground transition-all hover:opacity-90"
    >
      Looks great! →
    </button>
  {/if}
</div>

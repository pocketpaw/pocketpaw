<script lang="ts">
  import { FileText, Code, Bug, FolderOpen, Terminal, Search } from "@lucide/svelte";

  let { onSuggestionClick }: { onSuggestionClick: (text: string) => void } = $props();

  const suggestions = [
    { text: "Summarize a file", icon: FileText },
    { text: "Write some code", icon: Code },
    { text: "Debug an error", icon: Bug },
    { text: "Browse my files", icon: FolderOpen },
    { text: "Run a command", icon: Terminal },
    { text: "Research a topic", icon: Search },
  ];
</script>

<div class="flex flex-col items-center gap-6 px-4 text-center">
  <div class="flex flex-col items-center gap-3">
    <span class="text-4xl">🐾</span>
    <h2 class="text-lg font-semibold text-foreground">What can I help you with?</h2>
    <p class="max-w-sm text-xs text-muted-foreground">
      Ask anything, drop files into chat, or explore your files from the sidebar.
    </p>
  </div>

  <div class="grid grid-cols-2 gap-2 sm:grid-cols-3">
    {#each suggestions as { text, icon }}
      {@const Icon = icon}
      <button
        onclick={() => onSuggestionClick(text)}
        class="flex flex-col items-center gap-2 rounded-xl border border-border px-4 py-3 text-xs text-muted-foreground transition-colors hover:border-primary/30 hover:bg-accent hover:text-foreground active:bg-accent active:text-foreground touch-target"
      >
        <Icon class="h-4 w-4" strokeWidth={1.75} />
        <span>{text}</span>
      </button>
    {/each}
  </div>
</div>

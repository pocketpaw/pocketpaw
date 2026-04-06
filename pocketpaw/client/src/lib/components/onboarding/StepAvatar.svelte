<script lang="ts">
  import Typewriter from "./Typewriter.svelte";

  let {
    initialEmoji = "🐾",
    onNext,
  }: {
    initialEmoji?: string;
    onNext: (emoji: string) => void;
  } = $props();

  const emojis = [
    "🐾", "😎", "🚀", "🦊", "🐱", "🐶", "🦁", "🐼",
    "🦄", "🐲", "🤖", "👾", "🎮", "🧑‍💻", "🧙", "🥷",
    "🦉", "🐸", "🌟", "⚡", "🔥", "💎", "🎯", "🍕",
  ];

  let selected = $state(initialEmoji);
  let showGrid = $state(false);
  let justSelected = $state("");

  function pick(emoji: string) {
    selected = emoji;
    justSelected = emoji;
    setTimeout(() => (justSelected = ""), 300);
  }
</script>

<div class="flex w-full max-w-md flex-col items-center gap-8">
  <div class="text-7xl transition-transform duration-300" class:scale-110={!!justSelected}>
    {selected}
  </div>

  <h2 class="text-center text-xl font-semibold text-foreground">
    <Typewriter text="Pick a face!" speed={40} onDone={() => (showGrid = true)} />
  </h2>

  {#if showGrid}
    <div class="grid grid-cols-8 gap-2 animate-in fade-in slide-in-from-bottom-4 duration-500">
      {#each emojis as emoji}
        <button
          onclick={() => pick(emoji)}
          class="flex h-11 w-11 items-center justify-center rounded-xl text-2xl transition-all hover:scale-110 hover:bg-accent/60
            {selected === emoji ? 'bg-primary/15 ring-2 ring-primary scale-110' : 'bg-card'}"
        >
          {emoji}
        </button>
      {/each}
    </div>

    <button
      onclick={() => onNext(selected)}
      class="mt-2 rounded-xl bg-primary px-6 py-3 text-sm font-medium text-primary-foreground transition-all hover:opacity-90"
    >
      Love it! →
    </button>
  {/if}
</div>

<script lang="ts">
  import Typewriter from "./Typewriter.svelte";

  let {
    initialName = "",
    onNext,
  }: {
    initialName?: string;
    onNext: (name: string) => void;
  } = $props();

  let name = $state(initialName);
  let showInput = $state(false);

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === "Enter" && name.trim()) onNext(name.trim());
  }
</script>

<div class="flex w-full max-w-md flex-col items-center gap-8">
  <span class="text-6xl">👋</span>

  <h2 class="text-center text-2xl font-semibold text-foreground">
    <Typewriter text="Hey there! What should I call you?" speed={35} onDone={() => (showInput = true)} />
  </h2>

  {#if showInput}
    <div class="flex w-full flex-col gap-4 animate-in fade-in slide-in-from-bottom-4 duration-500">
      <input
        bind:value={name}
        onkeydown={handleKeydown}
        type="text"
        placeholder="Your name"
        maxlength={30}
        class="w-full rounded-xl border border-border bg-card px-4 py-3 text-center text-lg text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary/20"
      />

      <button
        onclick={() => name.trim() && onNext(name.trim())}
        disabled={!name.trim()}
        class="rounded-xl bg-primary px-6 py-3 text-sm font-medium text-primary-foreground transition-all hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed"
      >
        That's me! →
      </button>
    </div>
  {/if}
</div>

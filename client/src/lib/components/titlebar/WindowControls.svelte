<script lang="ts">
  import { getCurrentWindow } from "@tauri-apps/api/window";
  import { Minus, Square, X, Copy } from "@lucide/svelte";

  let { platform = "linux" }: { platform?: string } = $props();
  let isMaximized = $state(false);

  async function checkMaximized() {
    try {
      isMaximized = await getCurrentWindow().isMaximized();
    } catch {
      // not in Tauri context
    }
  }

  async function minimize() {
    try {
      await getCurrentWindow().minimize();
    } catch {}
  }

  async function toggleMaximize() {
    try {
      await getCurrentWindow().toggleMaximize();
      await checkMaximized();
    } catch {}
  }

  async function close() {
    try {
      await getCurrentWindow().hide();
    } catch {}
  }

  $effect(() => {
    checkMaximized();
  });
</script>

{#if platform === "windows"}
  <!-- Windows: Fluent-style rectangular buttons -->
  <div class="flex h-8">
    <button
      onclick={minimize}
      class="flex h-full w-[46px] items-center justify-center text-muted-foreground transition-colors duration-100 hover:bg-foreground/10"
    >
      <Minus class="h-3 w-3" strokeWidth={1.5} />
    </button>
    <button
      onclick={toggleMaximize}
      class="flex h-full w-[46px] items-center justify-center text-muted-foreground transition-colors duration-100 hover:bg-foreground/10"
    >
      {#if isMaximized}
        <Copy class="h-2.5 w-2.5" strokeWidth={1.5} />
      {:else}
        <Square class="h-2.5 w-2.5" strokeWidth={1.5} />
      {/if}
    </button>
    <button
      onclick={close}
      class="flex h-full w-[46px] items-center justify-center text-muted-foreground transition-colors duration-100 hover:bg-destructive hover:text-destructive-foreground"
    >
      <X class="h-3 w-3" strokeWidth={1.5} />
    </button>
  </div>
{:else}
  <!-- Linux: Neutral rounded circles -->
  <div class="flex items-center gap-1.5 px-2">
    <button
      onclick={minimize}
      class="flex h-6 w-6 items-center justify-center rounded-full bg-muted text-muted-foreground transition-colors duration-100 hover:bg-foreground/15"
    >
      <Minus class="h-2.5 w-2.5" strokeWidth={2} />
    </button>
    <button
      onclick={toggleMaximize}
      class="flex h-6 w-6 items-center justify-center rounded-full bg-muted text-muted-foreground transition-colors duration-100 hover:bg-foreground/15"
    >
      {#if isMaximized}
        <Copy class="h-2.5 w-2.5" strokeWidth={2} />
      {:else}
        <Square class="h-2.5 w-2.5" strokeWidth={2} />
      {/if}
    </button>
    <button
      onclick={close}
      class="flex size-6 items-center justify-center rounded-full bg-muted text-muted-foreground transition-colors duration-100 hover:bg-destructive/80 hover:text-destructive-foreground"
    >
      <X class="h-2.5 w-2.5" strokeWidth={2} />
    </button>
  </div>
{/if}

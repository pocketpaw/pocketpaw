<script lang="ts">
  import { Check, Copy } from "@lucide/svelte";
  import { platformStore } from "$lib/stores";

  let { code, language = "" }: { code: string; language?: string } = $props();

  let copied = $state(false);

  async function copyCode() {
    try {
      await navigator.clipboard.writeText(code);
      copied = true;
      setTimeout(() => { copied = false; }, 2000);
    } catch {
      // fallback: noop
    }
  }

  let displayLang = $derived(language || "text");
</script>

<div class="group relative my-2 overflow-hidden rounded-lg border border-border bg-muted/50">
  <!-- Header -->
  <div class="flex items-center justify-between border-b border-border px-3 py-1.5">
    <span class="text-[11px] font-medium text-muted-foreground">{displayLang}</span>
    <button
      onclick={copyCode}
      class={platformStore.isTouch
        ? "flex items-center gap-1 rounded-sm px-2 py-1 text-[11px] text-muted-foreground transition-colors active:bg-accent active:text-foreground"
        : "flex items-center gap-1 rounded-sm px-1.5 py-0.5 text-[11px] text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"}
    >
      {#if copied}
        <Check class="h-3 w-3" />
        <span>Copied</span>
      {:else}
        <Copy class="h-3 w-3" />
        <span>Copy</span>
      {/if}
    </button>
  </div>

  <!-- Code -->
  <div class="overflow-x-auto p-3">
    <pre class="text-[12px] leading-relaxed"><code>{code}</code></pre>
  </div>
</div>

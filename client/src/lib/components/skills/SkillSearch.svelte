<script lang="ts">
  import { Search, X } from "@lucide/svelte";

  let {
    value = $bindable(""),
  }: {
    value?: string;
  } = $props();

  let hasValue = $derived(value.length > 0);

  function clear() {
    value = "";
  }
</script>

<div class="relative">
  <Search class="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
  <input
    bind:value={value}
    placeholder="Search skills..."
    class="h-9 w-full rounded-lg border border-border bg-muted/50 pl-9 pr-9 text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
  />
  {#if hasValue}
    <button
      onclick={clear}
      class="absolute right-2 top-1/2 -translate-y-1/2 rounded-sm p-1 text-muted-foreground transition-colors hover:text-foreground"
    >
      <X class="h-3.5 w-3.5" />
    </button>
  {/if}
</div>

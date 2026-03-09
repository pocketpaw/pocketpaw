<script lang="ts">
  import { Search, X } from "@lucide/svelte";
  import { platformStore } from "$lib/stores";

  let {
    value = $bindable(""),
    onFocus,
  }: {
    value?: string;
    onFocus?: () => void;
  } = $props();

  let inputEl: HTMLInputElement | undefined = $state();

  export function focus() {
    inputEl?.focus();
  }

  function clear() {
    value = "";
    inputEl?.focus();
  }

  let hasValue = $derived(value.length > 0);
</script>

<div class="relative px-2 pb-1">
  <div class="relative">
    <Search class="pointer-events-none absolute left-2 top-1/2 h-3 w-3 -translate-y-1/2 text-muted-foreground" />
    <input
      bind:this={inputEl}
      bind:value={value}
      onfocus={onFocus}
      placeholder="Search..."
      class={platformStore.isTouch
        ? "h-9 w-full rounded-md border border-border bg-muted/50 pl-7 pr-7 text-xs text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
        : "h-7 w-full rounded-md border border-border bg-muted/50 pl-7 pr-7 text-[11px] text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"}
    />
    {#if hasValue}
      <button
        onclick={clear}
        class="absolute right-1.5 top-1/2 -translate-y-1/2 rounded-sm p-0.5 text-muted-foreground transition-colors hover:text-foreground"
      >
        <X class="h-3 w-3" />
      </button>
    {/if}
  </div>
</div>

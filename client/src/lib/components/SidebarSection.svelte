<script lang="ts">
  import { ChevronRight } from "@lucide/svelte";
  import type { Snippet } from "svelte";

  let {
    title,
    defaultOpen = true,
    children,
  }: {
    title: string;
    defaultOpen?: boolean;
    children: Snippet;
  } = $props();

  let open = $state(defaultOpen);
</script>

<div class="mb-1">
  <button
    type="button"
    onclick={() => (open = !open)}
    class="flex w-full items-center gap-1.5 px-3 py-1.5 text-[10px] font-medium uppercase tracking-wider text-muted-foreground/60 transition-colors hover:text-muted-foreground"
  >
    <ChevronRight
      class="h-3 w-3 shrink-0 transition-transform duration-150"
      style={open ? "transform: rotate(90deg)" : ""}
      strokeWidth={2}
    />
    {title}
  </button>
  {#if open}
    <div class="px-2 py-0.5">
      {@render children()}
    </div>
  {/if}
</div>

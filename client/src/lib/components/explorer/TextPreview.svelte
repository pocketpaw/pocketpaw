<script lang="ts">
  import { getCategoryColor } from "./file-icon-colors";

  let {
    text,
    extension = "",
  }: {
    text: string;
    extension?: string;
  } = $props();

  let color = $derived(getCategoryColor(extension));
  let badgeText = $derived(color.text || extension.toUpperCase().slice(0, 4));
</script>

<div class="relative h-full w-full overflow-hidden bg-[var(--color-muted)]/20">
  <!-- Text content -->
  <pre
    class="h-full w-full overflow-hidden whitespace-pre-wrap break-all p-1.5 font-mono text-[7px] leading-[1.3] text-muted-foreground/80"
  >{text}</pre>

  <!-- Bottom fade gradient -->
  <div
    class="pointer-events-none absolute inset-x-0 bottom-0 h-5"
    style="background: linear-gradient(to bottom, transparent, var(--color-card, hsl(var(--card))))"
  ></div>

  <!-- Extension badge in top-right corner -->
  {#if badgeText}
    <span
      class="absolute right-1 top-1 rounded px-1 py-0.5 text-[10px] font-bold leading-none text-white"
      style:background-color={color.fill}
    >
      {badgeText}
    </span>
  {/if}
</div>

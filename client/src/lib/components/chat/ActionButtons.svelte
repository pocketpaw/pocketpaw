<script lang="ts">
  import { chatStore } from "$lib/stores";

  let { actions }: { actions: string[] } = $props();

  function handleClick(text: string) {
    chatStore.sendMessage(text);
  }
</script>

{#if actions.length > 0}
  <div class="mt-2 flex flex-wrap gap-2">
    {#each actions as action}
      <button
        onclick={() => handleClick(action)}
        class="rounded-lg border border-border px-3 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
      >
        {action}
      </button>
    {/each}
  </div>
{/if}

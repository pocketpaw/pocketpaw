<script lang="ts">
  import { mcStore, connectionStore } from "$lib/stores";
  import { X, Square } from "@lucide/svelte";

  let execution = $derived(mcStore.activeExecution);
  let logContainer = $state<HTMLDivElement | null>(null);

  // Auto-scroll to bottom when new log entries arrive
  $effect(() => {
    if (execution?.log && logContainer) {
      // Access log.length to trigger reactivity
      const _ = execution.log.length;
      requestAnimationFrame(() => {
        if (logContainer) logContainer.scrollTop = logContainer.scrollHeight;
      });
    }
  });

  async function handleStop() {
    if (!execution) return;
    try {
      const client = connectionStore.getClient();
      await client.mcStopTask(execution.taskId);
    } catch (e) {
      console.error("[TaskExecutionPanel] Failed to stop:", e);
    }
  }

  function handleClose() {
    mcStore.closeExecution();
  }
</script>

{#if execution}
  <!-- Backdrop -->
  <button
    class="fixed inset-0 z-40 bg-black/20"
    onclick={handleClose}
    aria-label="Close execution panel"
  ></button>

  <!-- Slide-over -->
  <div class="fixed top-0 right-0 z-50 flex h-full w-96 flex-col border-l border-border bg-background shadow-xl">
    <!-- Header -->
    <div class="flex items-center justify-between border-b border-border px-4 py-3">
      <div class="min-w-0 flex-1">
        <h3 class="truncate text-sm font-medium text-foreground">{execution.taskTitle}</h3>
        <p class="text-xs text-muted-foreground">
          Agent: <span class="font-medium">{execution.agentName}</span>
          {#if execution.status === "running"}
            <span class="ml-1.5 inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-green-500"></span>
          {:else}
            <span class="ml-1.5 text-[10px] font-medium {execution.status === 'completed' ? 'text-green-500' : 'text-red-500'}">{execution.status}</span>
          {/if}
        </p>
      </div>
      <button
        onclick={handleClose}
        class="flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground"
      >
        <X class="h-4 w-4" />
      </button>
    </div>

    <!-- Log body -->
    <div bind:this={logContainer} class="flex-1 overflow-y-auto p-4">
      {#if execution.log.length === 0 && execution.status === "running"}
        <div class="flex items-center gap-2 text-xs text-muted-foreground">
          <div class="h-3 w-3 animate-spin rounded-full border border-muted-foreground/30 border-t-muted-foreground"></div>
          Waiting for output...
        </div>
      {/if}

      <div class="flex flex-col gap-2">
        {#each execution.log as entry, i (i)}
          {#if entry.output_type === "message"}
            <p class="text-xs leading-relaxed text-foreground/80 whitespace-pre-wrap">{entry.content}</p>
          {:else if entry.output_type === "tool_use"}
            <details class="rounded border border-border/50 bg-muted/30">
              <summary class="cursor-pointer px-2 py-1 text-[10px] font-medium text-muted-foreground">
                Tool call
              </summary>
              <pre class="overflow-x-auto px-2 py-1 text-[10px] text-foreground/70">{entry.content}</pre>
            </details>
          {:else if entry.output_type === "tool_result"}
            <div class="rounded border border-border/50 bg-muted/20 px-2 py-1">
              <pre class="overflow-x-auto text-[10px] text-foreground/70">{entry.content}</pre>
            </div>
          {/if}
        {/each}
      </div>
    </div>

    <!-- Footer -->
    {#if execution.status === "running"}
      <div class="border-t border-border px-4 py-3">
        <button
          onclick={handleStop}
          class="flex w-full items-center justify-center gap-1.5 rounded-md border border-red-300 px-3 py-1.5 text-xs text-red-500 transition-colors hover:bg-red-500/10"
        >
          <Square class="h-3 w-3" />
          Stop Execution
        </button>
      </div>
    {/if}
  </div>
{/if}

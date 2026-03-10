<script lang="ts">
  import { explorerStore } from "$lib/stores";
  import { onMount } from "svelte";

  let {
    fileName,
    filePath,
  }: {
    fileName: string;
    filePath: string;
  } = $props();

  let inputRef = $state<HTMLInputElement | null>(null);
  let value = $state(fileName);

  onMount(() => {
    if (!inputRef) return;
    inputRef.focus();
    const dotIdx = value.lastIndexOf(".");
    if (dotIdx > 0) {
      inputRef.setSelectionRange(0, dotIdx);
    } else {
      inputRef.select();
    }
  });

  function commit() {
    const trimmed = value.trim();
    if (trimmed && trimmed !== fileName) {
      explorerStore.commitRename(filePath, trimmed);
    } else {
      explorerStore.cancelRename();
    }
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === "Enter") {
      e.preventDefault();
      commit();
    } else if (e.key === "Escape") {
      e.preventDefault();
      explorerStore.cancelRename();
    }
    e.stopPropagation();
  }
</script>

<input
  bind:this={inputRef}
  bind:value
  type="text"
  class="h-5 w-full min-w-0 rounded border border-primary bg-background px-1 text-sm text-foreground outline-none ring-1 ring-primary/50"
  onkeydown={handleKeydown}
  onblur={commit}
  onclick={(e) => e.stopPropagation()}
  ondblclick={(e) => e.stopPropagation()}
/>

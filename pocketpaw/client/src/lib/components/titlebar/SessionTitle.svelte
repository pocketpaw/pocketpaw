<script lang="ts">
  import { sessionStore } from "$lib/stores";

  let isEditing = $state(false);
  let editValue = $state("");
  let inputEl: HTMLInputElement | undefined = $state();

  const title = $derived(sessionStore.activeSession?.title ?? "New Chat");

  function startEditing() {
    editValue = title;
    isEditing = true;
    // Focus after DOM update
    requestAnimationFrame(() => {
      inputEl?.select();
    });
  }

  function save() {
    const trimmed = editValue.trim();
    if (trimmed && trimmed !== title && sessionStore.activeSessionId) {
      sessionStore.renameSession(sessionStore.activeSessionId, trimmed);
    }
    isEditing = false;
  }

  function cancel() {
    isEditing = false;
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === "Enter") {
      e.preventDefault();
      save();
    } else if (e.key === "Escape") {
      cancel();
    }
  }
</script>

<div class="flex min-w-0 items-center gap-1.5">
  <span class="shrink-0 text-sm">🐾</span>

  {#if isEditing}
    <input
      bind:this={inputEl}
      bind:value={editValue}
      onblur={save}
      onkeydown={handleKeydown}
      class="h-6 min-w-[100px] max-w-[200px] rounded-sm border border-border bg-background px-1.5 text-[13px] font-medium text-foreground outline-none focus:ring-1 focus:ring-ring"
    />
  {:else}
    <button
      onclick={startEditing}
      class="max-w-[200px] truncate text-[13px] font-medium text-foreground/90 transition-colors duration-100 hover:text-foreground"
    >
      {title}
    </button>
  {/if}
</div>

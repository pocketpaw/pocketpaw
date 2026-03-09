<script lang="ts">
  import { sessionStore } from "$lib/stores";
  import * as DropdownMenu from "$lib/components/ui/dropdown-menu";
  import { ChevronDown, SquarePen, MessageSquare, Check } from "@lucide/svelte";

  let sessions = $derived(sessionStore.sessions);
  let active = $derived(sessionStore.activeSession);

  function switchTo(id: string) {
    sessionStore.switchSession(id);
  }

  function newChat() {
    sessionStore.createNewSession();
  }
</script>

<div class="flex items-center gap-1 px-2 py-1.5">
  <!-- Session dropdown -->
  <DropdownMenu.Root>
    <DropdownMenu.Trigger>
      {#snippet child({ props })}
        <button
          {...props}
          class="flex min-w-0 flex-1 items-center gap-2 rounded-md px-2 py-1.5 text-left text-[13px] font-medium text-foreground transition-colors hover:bg-accent/50"
        >
          <MessageSquare class="h-3.5 w-3.5 shrink-0 text-muted-foreground" strokeWidth={1.75} />
          <span class="min-w-0 flex-1 truncate">
            {active?.title ?? "New Chat"}
          </span>
          <ChevronDown class="h-3 w-3 shrink-0 text-muted-foreground" />
        </button>
      {/snippet}
    </DropdownMenu.Trigger>

    <DropdownMenu.Content class="w-[320px] max-h-80 overflow-y-auto" align="start" side="bottom">
      {#each sessions as session (session.id)}
        <DropdownMenu.Item
          onclick={() => switchTo(session.id)}
          class="gap-2 text-xs"
        >
          <MessageSquare class="h-3.5 w-3.5 shrink-0" strokeWidth={1.75} />
          <span class="min-w-0 flex-1 truncate">{session.title}</span>
          {#if session.id === active?.id}
            <Check class="h-3 w-3 shrink-0 text-paw-accent" />
          {/if}
        </DropdownMenu.Item>
      {/each}

      {#if sessions.length === 0}
        <p class="px-2 py-3 text-center text-[11px] text-muted-foreground">
          No conversations yet
        </p>
      {/if}
    </DropdownMenu.Content>
  </DropdownMenu.Root>

  <!-- New chat button -->
  <button
    onclick={newChat}
    class="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent/50 hover:text-foreground"
    title="New Chat"
  >
    <SquarePen class="h-4 w-4" />
  </button>
</div>

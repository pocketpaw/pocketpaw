<script lang="ts">
  import { sessionStore } from "$lib/stores";
  import * as Popover from "$lib/components/ui/popover";
  import * as Command from "$lib/components/ui/command";
  import { ChevronsUpDown, SquarePen, MessageSquare, Check } from "@lucide/svelte";
  import type { Session } from "$lib/api";

  let sessions = $derived(sessionStore.sessions);
  let active = $derived(sessionStore.activeSession);

  let open = $state(false);

  // Date grouping
  type GroupedSessions = { label: string; sessions: Session[] }[];

  let groupedSessions = $derived.by((): GroupedSessions => {
    if (sessions.length === 0) return [];

    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const yesterday = new Date(today.getTime() - 86_400_000);
    const thisWeek = new Date(today.getTime() - 7 * 86_400_000);
    const thisMonth = new Date(today.getTime() - 30 * 86_400_000);

    const groups = new Map<string, Session[]>([
      ["Today", []],
      ["Yesterday", []],
      ["This Week", []],
      ["This Month", []],
      ["Older", []],
    ]);

    for (const s of sessions) {
      const d = new Date(s.last_activity);
      if (d >= today) groups.get("Today")!.push(s);
      else if (d >= yesterday) groups.get("Yesterday")!.push(s);
      else if (d >= thisWeek) groups.get("This Week")!.push(s);
      else if (d >= thisMonth) groups.get("This Month")!.push(s);
      else groups.get("Older")!.push(s);
    }

    const result: GroupedSessions = [];
    for (const [label, items] of groups) {
      if (items.length > 0) {
        result.push({ label, sessions: items });
      }
    }
    return result;
  });

  function switchTo(id: string) {
    open = false;
    sessionStore.switchSession(id);
  }

  function newChat() {
    sessionStore.createNewSession();
  }
</script>

<div class="flex items-center gap-1 px-2 py-1.5">
  <!-- Session combobox -->
  <Popover.Root bind:open>
    <Popover.Trigger>
      {#snippet child({ props })}
        <button
          {...props}
          class="flex min-w-0 flex-1 items-center gap-2 rounded-md px-2 py-1.5 text-left text-[13px] font-medium text-foreground transition-colors hover:bg-accent/50"
        >
          <MessageSquare class="h-3.5 w-3.5 shrink-0 text-muted-foreground" strokeWidth={1.75} />
          <span class="min-w-0 flex-1 truncate">
            {active?.title ?? "New Chat"}
          </span>
          <ChevronsUpDown class="h-3 w-3 shrink-0 text-muted-foreground/50" />
        </button>
      {/snippet}
    </Popover.Trigger>

    <Popover.Content class="w-[320px] p-0" align="start" side="bottom" sideOffset={4}>
      <Command.Root>
        <Command.Input placeholder="Search conversations..." />
        <Command.List class="max-h-[300px]">
          <Command.Empty>No conversations found.</Command.Empty>
          {#each groupedSessions as group}
            <Command.Group heading={group.label}>
              {#each group.sessions as session (session.id)}
                <Command.Item
                  value={session.title}
                  onSelect={() => switchTo(session.id)}
                  class="gap-2"
                >
                  <MessageSquare class="h-3.5 w-3.5 shrink-0" strokeWidth={1.75} />
                  <span class="min-w-0 flex-1 truncate text-xs">{session.title}</span>
                  {#if session.id === active?.id}
                    <Check class="h-3 w-3 shrink-0 text-primary" />
                  {/if}
                </Command.Item>
              {/each}
            </Command.Group>
          {/each}
        </Command.List>
      </Command.Root>
    </Popover.Content>
  </Popover.Root>

  <!-- New chat button -->
  <button
    onclick={newChat}
    class="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent/50 hover:text-foreground"
    title="New Chat"
  >
    <SquarePen class="h-4 w-4" />
  </button>
</div>

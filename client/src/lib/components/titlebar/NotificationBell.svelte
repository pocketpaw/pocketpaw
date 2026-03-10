<script lang="ts">
  import { mcStore } from "$lib/stores";
  import { Bell } from "@lucide/svelte";
  import * as Tooltip from "$lib/components/ui/tooltip";

  let open = $state(false);
  let unreadCount = $derived(mcStore.unreadCount);
  let notifications = $derived(mcStore.notifications);

  function toggle() {
    open = !open;
  }

  function close() {
    open = false;
  }

  async function handleClickNotification(id: string) {
    await mcStore.markRead(id);
  }

  function relativeTime(iso: string): string {
    try {
      const diff = Date.now() - new Date(iso).getTime();
      const mins = Math.floor(diff / 60_000);
      if (mins < 1) return "just now";
      if (mins < 60) return `${mins}m ago`;
      const hours = Math.floor(mins / 60);
      if (hours < 24) return `${hours}h ago`;
      return `${Math.floor(hours / 24)}d ago`;
    } catch {
      return "";
    }
  }
</script>

<div class="relative">
  <Tooltip.Root>
    <Tooltip.Trigger>
      <button
        onclick={toggle}
        class="relative flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors duration-100 hover:bg-foreground/10 hover:text-foreground"
      >
        <Bell class="h-3.5 w-3.5" strokeWidth={2} />
        {#if unreadCount > 0}
          <span class="absolute -top-0.5 -right-0.5 flex h-3.5 min-w-3.5 items-center justify-center rounded-full bg-red-500 px-0.5 text-[8px] font-bold text-white">
            {unreadCount > 99 ? "99+" : unreadCount}
          </span>
        {/if}
      </button>
    </Tooltip.Trigger>
    <Tooltip.Content>
      <p>Notifications</p>
    </Tooltip.Content>
  </Tooltip.Root>

  {#if open}
    <!-- Backdrop -->
    <button class="fixed inset-0 z-40" onclick={close} aria-label="Close notifications"></button>

    <!-- Dropdown -->
    <div class="absolute right-0 top-full z-50 mt-1 w-72 rounded-lg border border-border bg-background shadow-lg">
      <div class="flex items-center justify-between border-b border-border px-3 py-2">
        <span class="text-xs font-medium text-foreground">Notifications</span>
        {#if unreadCount > 0}
          <span class="text-[10px] text-muted-foreground">{unreadCount} unread</span>
        {/if}
      </div>
      <div class="max-h-64 overflow-y-auto">
        {#if notifications.length === 0}
          <p class="py-6 text-center text-xs text-muted-foreground">No notifications</p>
        {:else}
          {#each notifications.slice(0, 50) as notif (notif.id)}
            <button
              class={[
                "flex w-full flex-col gap-0.5 border-b border-border/50 px-3 py-2 text-left transition-colors last:border-0 hover:bg-muted/50",
                notif.read ? "opacity-60" : "",
              ].join(" ")}
              onclick={() => handleClickNotification(notif.id)}
            >
              <div class="flex items-start justify-between gap-2">
                <p class="flex-1 text-xs text-foreground {notif.read ? '' : 'font-medium'}">{notif.content}</p>
                {#if !notif.read}
                  <span class="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-blue-500"></span>
                {/if}
              </div>
              <span class="text-[10px] text-muted-foreground">{relativeTime(notif.created_at)}</span>
            </button>
          {/each}
        {/if}
      </div>
    </div>
  {/if}
</div>

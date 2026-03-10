<script lang="ts">
  import type { Session } from "$lib/api";
  import { toast } from "svelte-sonner";
  import { goto } from "$app/navigation";
  import { page } from "$app/state";
  import { sessionStore, platformStore } from "$lib/stores";
  import { MessageSquare, MoreHorizontal, Pencil, Trash2, FileDown, FileJson } from "@lucide/svelte";
  import * as DropdownMenu from "$lib/components/ui/dropdown-menu";
  import * as Dialog from "$lib/components/ui/dialog";
  import { Button } from "$lib/components/ui/button";

  let { session, isActive = false }: { session: Session; isActive?: boolean } = $props();

  let isEditing = $state(false);
  let editValue = $state("");
  let inputEl: HTMLInputElement | undefined = $state();
  let deleteDialogOpen = $state(false);

  let relativeTime = $derived.by(() => {
    if (!session.last_activity) return "";
    const diff = Date.now() - new Date(session.last_activity).getTime();
    const mins = Math.floor(diff / 60_000);
    if (mins < 1) return "now";
    if (mins < 60) return `${mins}m`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h`;
    const days = Math.floor(hrs / 24);
    if (days < 7) return `${days}d`;
    return `${Math.floor(days / 7)}w`;
  });

  let itemClass = $derived.by(() => {
    const touch = platformStore.isTouch;
    const pad = touch ? "px-3 py-2.5" : "px-2 py-1.5";
    const font = touch ? "text-[13px]" : "text-[12px]";
    if (isActive) {
      return `group flex w-full items-center gap-2 rounded-md ${pad} text-left ${font} transition-colors duration-100 bg-paw-accent-subtle text-foreground font-medium`;
    }
    return `group flex w-full items-center gap-2 rounded-md ${pad} text-left ${font} transition-colors duration-100 text-muted-foreground hover:bg-accent hover:text-foreground`;
  });

  function handleClick() {
    if (!isEditing) {
      sessionStore.switchSession(session.id);
      if (page.url.pathname !== "/") {
        goto("/");
      }
    }
  }

  function startRename() {
    editValue = session.title;
    isEditing = true;
    requestAnimationFrame(() => inputEl?.select());
  }

  function saveRename() {
    const trimmed = editValue.trim();
    if (trimmed && trimmed !== session.title) {
      sessionStore.renameSession(session.id, trimmed);
    }
    isEditing = false;
  }

  function cancelRename() {
    isEditing = false;
  }

  function handleRenameKeydown(e: KeyboardEvent) {
    if (e.key === "Enter") { e.preventDefault(); saveRename(); }
    if (e.key === "Escape") cancelRename();
  }

  function handleDelete() {
    deleteDialogOpen = true;
  }

  function confirmDelete() {
    sessionStore.deleteSession(session.id);
    deleteDialogOpen = false;
  }

  async function handleExport(format: "md" | "json") {
    try {
      const content = await sessionStore.exportSession(session.id, format);
      const blob = new Blob([content], { type: format === "json" ? "application/json" : "text/markdown" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${session.title.replace(/[^a-zA-Z0-9]/g, "_")}.${format === "json" ? "json" : "md"}`;
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      toast.error("Export failed");
    }
  }
</script>

{#if isEditing}
  <div class="flex items-center gap-2 rounded-md px-2 py-1">
    <MessageSquare class="h-3.5 w-3.5 shrink-0 text-muted-foreground" strokeWidth={1.75} />
    <input
      bind:this={inputEl}
      bind:value={editValue}
      onblur={saveRename}
      onkeydown={handleRenameKeydown}
      class="min-w-0 flex-1 rounded-sm border border-border bg-background px-1 py-0.5 text-[12px] text-foreground outline-none focus:ring-1 focus:ring-ring"
    />
  </div>
{:else}
  <div class="relative">
    <button
      onclick={handleClick}
      ondblclick={startRename}
      class={itemClass}
    >
      <MessageSquare class="h-3.5 w-3.5 shrink-0" strokeWidth={1.75} />
      <span class="min-w-0 flex-1 truncate">{session.title}</span>
      <span class="shrink-0 text-[10px] text-muted-foreground/60 group-hover:hidden">{relativeTime}</span>
    </button>

    <!-- More button (visible on hover / always on touch) -->
    <div class={platformStore.isTouch
      ? "absolute right-1 top-1/2 block -translate-y-1/2"
      : "absolute right-1 top-1/2 hidden -translate-y-1/2 group-hover:block"}>
      <DropdownMenu.Root>
        <DropdownMenu.Trigger>
          <button
            class="flex h-5 w-5 items-center justify-center rounded-sm text-muted-foreground transition-colors hover:bg-foreground/10 hover:text-foreground"
            onclick={(e: MouseEvent) => e.stopPropagation()}
          >
            <MoreHorizontal class="h-3 w-3" />
          </button>
        </DropdownMenu.Trigger>
        <DropdownMenu.Content class="w-44" align="start" side="right">
          <DropdownMenu.Item onclick={startRename} class="gap-2 text-xs">
            <Pencil class="h-3.5 w-3.5" />
            Rename
          </DropdownMenu.Item>
          <DropdownMenu.Item onclick={() => handleExport("md")} class="gap-2 text-xs">
            <FileDown class="h-3.5 w-3.5" />
            Export as Markdown
          </DropdownMenu.Item>
          <DropdownMenu.Item onclick={() => handleExport("json")} class="gap-2 text-xs">
            <FileJson class="h-3.5 w-3.5" />
            Export as JSON
          </DropdownMenu.Item>
          <DropdownMenu.Separator />
          <DropdownMenu.Item onclick={handleDelete} class="gap-2 text-xs text-paw-error focus:text-paw-error">
            <Trash2 class="h-3.5 w-3.5" />
            Delete
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Root>
    </div>
  </div>
{/if}

<Dialog.Root bind:open={deleteDialogOpen}>
  <Dialog.Content class="max-w-sm">
    <Dialog.Header>
      <Dialog.Title>Delete conversation</Dialog.Title>
      <Dialog.Description>
        Are you sure you want to delete "<span class="font-medium">{session.title}</span>"? This action cannot be undone.
      </Dialog.Description>
    </Dialog.Header>
    <Dialog.Footer>
      <Button variant="outline" size="sm" onclick={() => (deleteDialogOpen = false)}>Cancel</Button>
      <Button variant="destructive" size="sm" onclick={confirmDelete}>Delete</Button>
    </Dialog.Footer>
  </Dialog.Content>
</Dialog.Root>

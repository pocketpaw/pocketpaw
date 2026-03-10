<script lang="ts">
  import type { FileEntry } from "$lib/filesystem";
  import { localFs } from "$lib/filesystem";
  import { explorerStore } from "$lib/stores";
  import { onMount } from "svelte";
  import FolderOpen from "@lucide/svelte/icons/folder-open";
  import Eye from "@lucide/svelte/icons/eye";
  import Trash2 from "@lucide/svelte/icons/trash-2";
  import ExternalLink from "@lucide/svelte/icons/external-link";
  import RefreshCw from "@lucide/svelte/icons/refresh-cw";
  import Terminal from "@lucide/svelte/icons/terminal";
  import { cn } from "$lib/utils";

  let {
    x,
    y,
    file,
    onClose,
  }: {
    x: number;
    y: number;
    file: FileEntry | null;
    onClose: () => void;
  } = $props();

  let menuRef = $state<HTMLDivElement | null>(null);

  onMount(() => {
    function handleClickOutside(e: MouseEvent) {
      if (menuRef && !menuRef.contains(e.target as Node)) {
        onClose();
      }
    }
    function handleEscape(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("mousedown", handleClickOutside);
    document.addEventListener("keydown", handleEscape);
    return () => {
      document.removeEventListener("mousedown", handleClickOutside);
      document.removeEventListener("keydown", handleEscape);
    };
  });

  interface MenuItem {
    label: string;
    icon: typeof FolderOpen;
    action: () => void;
    danger?: boolean;
  }

  type MenuEntry = MenuItem | "separator";

  // Adjust position to keep menu on screen
  let adjustedPos = $derived.by(() => {
    let ax = x;
    let ay = y;
    if (typeof window !== "undefined") {
      const menuWidth = 200;
      const menuHeight = file ? 200 : 100;
      if (ax + menuWidth > window.innerWidth) ax = window.innerWidth - menuWidth - 8;
      if (ay + menuHeight > window.innerHeight) ay = window.innerHeight - menuHeight - 8;
    }
    return { x: ax, y: ay };
  });

  let items = $derived.by((): MenuEntry[] => {
    if (!file) {
      // Background context menu
      return [
        {
          label: "Refresh",
          icon: RefreshCw,
          action: () => {
            explorerStore.refresh();
            onClose();
          },
        },
        "separator",
        {
          label: "Open in Terminal",
          icon: Terminal,
          action: async () => {
            await localFs.openInTerminal(explorerStore.currentPath);
            onClose();
          },
        },
      ];
    }

    const result: MenuEntry[] = [];

    // Open action
    if (file.isDir) {
      result.push({
        label: "Open",
        icon: FolderOpen,
        action: () => {
          explorerStore.navigateTo(file.path);
          onClose();
        },
      });
    } else {
      result.push({
        label: "Open",
        icon: Eye,
        action: () => {
          explorerStore.openFileDetail(file);
          onClose();
        },
      });
    }

    result.push({
      label: "Open in System App",
      icon: ExternalLink,
      action: async () => {
        try {
          const { openPath } = await import("@tauri-apps/plugin-opener");
          await openPath(file.path);
        } catch {
          // Fallback or not in Tauri
        }
        onClose();
      },
    });

    if (file.isDir) {
      result.push("separator");
      result.push({
        label: "Open in Terminal",
        icon: Terminal,
        action: async () => {
          await localFs.openInTerminal(file.path);
          onClose();
        },
      });
    }

    result.push("separator");

    // Delete
    result.push({
      label: "Delete",
      icon: Trash2,
      action: async () => {
        if (confirm(`Delete "${file.name}"?`)) {
          try {
            const { localFs } = await import("$lib/filesystem");
            await localFs.deleteFile(file.path, file.isDir);
            explorerStore.refresh();
          } catch {
            // Error handling
          }
        }
        onClose();
      },
      danger: true,
    });

    return result;
  });
</script>

<div
  bind:this={menuRef}
  class="fixed z-50 min-w-48 rounded-lg border border-border bg-popover p-1 shadow-lg"
  style="left: {adjustedPos.x}px; top: {adjustedPos.y}px;"
>
  {#each items as item}
    {#if item === "separator"}
      <div class="my-1 h-px bg-border"></div>
    {:else}
      {@const Icon = item.icon}
      <button
        type="button"
        class={cn(
          "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-sm transition-colors",
          item.danger ? "text-red-400 hover:bg-red-500/10" : "text-foreground hover:bg-muted",
        )}
        onclick={item.action}
      >
        <Icon class="h-4 w-4" />
        {item.label}
      </button>
    {/if}
  {/each}
</div>

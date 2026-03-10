<script lang="ts">
  import { Minus, X, ChevronsLeft, ChevronsRight, Link, Unlink } from "@lucide/svelte";
  import ContextBar from "./ContextBar.svelte";
  import SidePanelChat from "./SidePanelChat.svelte";
  import { onMount, onDestroy } from "svelte";
  import { initializeStores, connectionStore, sessionStore, chatStore, settingsStore, platformStore } from "$lib/stores";
  import { isTauri, getValidToken } from "$lib/auth";
  import {
    emitSidePanelReady,
    onSessionSwitch,
    onChatSync,
    onSettingsUpdate,
    onAttachChanged,
    onAttachError,
    disposeAllBridgeListeners,
    type AttachMode,
    type AttachChangedPayload,
  } from "$lib/tauri/bridge";

  let isDragging = $state(false);
  let collapsed = $state(false);
  let attachMode = $state<AttachMode>("docked");
  let isAttached = $state(false);
  let tokenUpdateUnsub: (() => void) | null = null;

  let panelBg = $derived(platformStore.hasNativeBlur ? "bg-transparent" : "");

  // Set data-native-blur attribute and transparent body for side panel window
  $effect(() => {
    const active = platformStore.hasNativeBlur;
    if (active) {
      document.documentElement.setAttribute("data-native-blur", "");
      document.body.style.background = "transparent";
    } else {
      document.documentElement.removeAttribute("data-native-blur");
      document.body.style.background = "";
    }
  });

  onMount(async () => {
    let token = "dev-token";
    let wsToken: string | undefined;
    if (isTauri()) {
      const storedToken = await getValidToken();
      if (storedToken) token = storedToken;
      try {
        const { invoke } = await import("@tauri-apps/api/core");
        wsToken = await invoke<string>("read_access_token");
      } catch {
        // Fall back to OAuth token for WS
      }
    }
    await initializeStores(token, undefined, wsToken);

    // Listen for token updates from the main window
    if (isTauri()) {
      try {
        const { listen } = await import("@tauri-apps/api/event");
        const unsub = await listen<{ token: string }>("token-updated", (event) => {
          connectionStore.updateToken(event.payload.token);
        });
        tokenUpdateUnsub = unsub;
      } catch {
        // Not in Tauri
      }

      // Check initial collapsed state
      try {
        const { invoke } = await import("@tauri-apps/api/core");
        collapsed = await invoke<boolean>("is_side_panel_collapsed");
      } catch {
        // Not in Tauri
      }

      // Cross-window bridge: listen for sync events from main window
      onSessionSwitch(({ sessionId }) => {
        sessionStore.switchSession(sessionId);
      });

      onChatSync((payload) => {
        if (!chatStore.isStreaming) {
          chatStore.loadHistory(payload.messages);
        }
      });

      onSettingsUpdate((settings) => {
        settingsStore.settings = settings;
      });

      // Listen for window attach events
      onAttachChanged((payload: AttachChangedPayload) => {
        attachMode = payload.mode;
        isAttached = payload.attached;
      });

      onAttachError((payload) => {
        console.warn("Attach error:", payload.message);
      });

      // Check initial attach mode
      try {
        const { invoke } = await import("@tauri-apps/api/core");
        attachMode = await invoke<AttachMode>("get_attach_mode");
      } catch {
        // Not in Tauri
      }

      // Tell main window we're ready to receive state
      emitSidePanelReady();
    }
  });

  onDestroy(() => {
    tokenUpdateUnsub?.();
    disposeAllBridgeListeners();
  });

  async function startDrag(e: MouseEvent) {
    // Only drag on the title bar area
    if ((e.target as HTMLElement).closest("button")) return;
    isDragging = true;
    try {
      const { getCurrentWindow } = await import("@tauri-apps/api/window");
      await getCurrentWindow().startDragging();
    } catch {
      // Not in Tauri
    } finally {
      isDragging = false;
    }
  }

  async function minimize() {
    try {
      const { getCurrentWindow } = await import("@tauri-apps/api/window");
      await getCurrentWindow().hide();
    } catch {
      // Not in Tauri
    }
  }

  async function close() {
    try {
      const { getCurrentWindow } = await import("@tauri-apps/api/window");
      await getCurrentWindow().hide();
    } catch {
      // Not in Tauri
    }
  }

  async function collapse() {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("collapse_side_panel");
      collapsed = true;
    } catch {
      // Not in Tauri
    }
  }

  async function expand() {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("expand_side_panel");
      collapsed = false;
    } catch {
      // Not in Tauri
    }
  }

  async function toggleAttach() {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      const newMode: AttachMode = attachMode === "auto" ? "docked" : "auto";
      await invoke("set_attach_mode", { mode: newMode });
      attachMode = newMode;
    } catch {
      // Not in Tauri
    }
  }
</script>

{#if collapsed}
  <!-- Collapsed strip: just icon + expand button -->
  <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
  <div
    class={`flex h-dvh w-full flex-col items-center gap-2 overflow-hidden rounded-xl border border-border/40 ${panelBg} py-2`}
    onmousedown={startDrag}
    role="none"
    data-tauri-drag-region
  >
    <span class="text-lg">🐾</span>
    <button
      onclick={expand}
      class="rounded p-1 text-muted-foreground/60 transition-colors hover:bg-accent hover:text-foreground"
      title="Expand"
    >
      <ChevronsRight class="h-4 w-4" />
    </button>
    <span
      class={connectionStore.isConnected
        ? "h-1.5 w-1.5 rounded-full bg-green-500"
        : "h-1.5 w-1.5 rounded-full bg-red-500"}
    ></span>
  </div>
{:else}
  <!-- Expanded layout -->
  <div class={`flex h-dvh w-full flex-col overflow-hidden rounded-xl border border-border/40 ${panelBg}`}>
    <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
    <!-- Slim title bar (drag area for Tauri window) -->
    <div
      class="flex shrink-0 items-center justify-between px-3 py-1.5"
      onmousedown={startDrag}
      role="none"
      data-tauri-drag-region
    >
      <div class="flex items-center gap-1.5">
        <span class="text-sm">🐾</span>
        <span class="text-xs font-medium text-foreground/80">PocketPaw</span>
        <span class="rounded-full bg-primary/15 px-1.5 py-0.5 text-[9px] font-medium text-primary">Side Panel</span>
        <span
          class={connectionStore.isConnected
            ? "h-1.5 w-1.5 rounded-full bg-green-500"
            : "h-1.5 w-1.5 rounded-full bg-red-500"}
        ></span>
      </div>
      <div class="flex items-center gap-0.5">
        <button
          onclick={toggleAttach}
          class={`rounded p-1 transition-colors hover:bg-accent hover:text-foreground ${attachMode === "auto" ? "text-primary" : "text-muted-foreground/60"}`}
          title={attachMode === "auto" ? "Unsnap from window" : "Snap to window"}
        >
          {#if attachMode === "auto"}
            <Unlink class="h-3 w-3" />
          {:else}
            <Link class="h-3 w-3" />
          {/if}
        </button>
        <button
          onclick={collapse}
          class="rounded p-1 text-muted-foreground/60 transition-colors hover:bg-accent hover:text-foreground"
          title="Collapse"
        >
          <ChevronsLeft class="h-3 w-3" />
        </button>
        <button
          onclick={minimize}
          class="rounded p-1 text-muted-foreground/60 transition-colors hover:bg-accent hover:text-foreground"
        >
          <Minus class="h-3 w-3" />
        </button>
        <button
          onclick={close}
          class="rounded p-1 text-muted-foreground/60 transition-colors hover:bg-accent hover:text-foreground"
        >
          <X class="h-3 w-3" />
        </button>
      </div>
    </div>

    <!-- Context bar -->
    <ContextBar />

    <!-- Chat area -->
    <SidePanelChat />
  </div>
{/if}

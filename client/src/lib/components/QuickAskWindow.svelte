<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { Send } from "@lucide/svelte";
  import { initializeStores, chatStore, connectionStore, platformStore } from "$lib/stores";
  import { isTauri, getValidToken } from "$lib/auth";

  let input = $state("");
  let inputEl: HTMLInputElement | undefined = $state();
  let tokenUpdateUnsub: (() => void) | null = null;
  let shownUnsub: (() => void) | null = null;

  let innerClass = $derived(
    platformStore.hasNativeBlur
      ? "flex w-full items-center gap-3 rounded-2xl border border-border/60 bg-background/40 px-4 py-3 shadow-2xl"
      : "flex w-full items-center gap-3 rounded-2xl border border-border/60 bg-background/95 px-4 py-3 shadow-2xl backdrop-blur-xl"
  );

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
    }

    // Transparent background for floating window
    document.body.style.background = 'transparent';

    // Focus on mount
    inputEl?.focus();

    // Re-focus and clear input each time the window is shown
    listenForShown();
  });

  onDestroy(() => {
    tokenUpdateUnsub?.();
    shownUnsub?.();
  });

  async function listenForShown() {
    try {
      const { listen } = await import("@tauri-apps/api/event");
      const unsub = await listen("quickask-shown", () => {
        input = "";
        queueMicrotask(() => inputEl?.focus());
      });
      shownUnsub = unsub;
    } catch {
      // Not in Tauri
    }
  }

  async function hideWindow() {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("hide_quick_ask");
    } catch {
      // Not in Tauri
    }
  }

  async function handleSubmit() {
    if (!input.trim()) return;
    const message = input.trim();
    input = "";
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("quickask_to_sidepanel", { message });
    } catch {
      // Fallback: send directly if not in Tauri
      chatStore.sendMessage(message);
      hideWindow();
    }
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === "Escape") {
      hideWindow();
    } else if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  }
</script>

<div class="flex h-dvh w-full items-center justify-center bg-transparent p-2">
  <div class={innerClass}>
    <span class="text-lg">&#x1f43e;</span>
    <input
      bind:this={inputEl}
      bind:value={input}
      onkeydown={handleKeydown}
      placeholder="What can I help with?"
      class="flex-1 bg-transparent text-sm text-foreground placeholder:text-muted-foreground focus:outline-none"
    />
    <button
      onclick={handleSubmit}
      disabled={!input.trim()}
      class="rounded-lg bg-primary p-2 text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-30"
    >
      <Send class="h-4 w-4" />
    </button>
  </div>
</div>

<script lang="ts">
  import "../styles/global.css";
  import type { Snippet } from "svelte";
  import { onMount, onDestroy } from "svelte";
  import { goto } from "$app/navigation";
  import { page } from "$app/state";
  import { ModeWatcher } from "mode-watcher";
  import { Toaster } from "$lib/components/ui/sonner";
  import { Provider as TooltipProvider } from "$lib/components/ui/tooltip";
  import AppShell from "$lib/components/AppShell.svelte";
  import SetupBackend from "$lib/components/onboarding/SetupBackend.svelte";
  import { initializeStores, activityStore, connectionStore, platformStore, sessionStore, chatStore, settingsStore } from "$lib/stores";
  import {
    isTauri,
    getValidToken,
    startOAuthFlow,
    scheduleTokenRefresh,
    cancelScheduledRefresh,
    type OAuthTokens,
  } from "$lib/auth";
  import {
    registerHotkeys,
    unregisterHotkeys,
    setupTrayListeners,
    cleanupTrayListeners,
    requestNotificationPermission,
    notifyAgentComplete,
    emitSessionSwitch,
    emitChatSync,
    emitSettingsUpdate,
    onSidePanelReady,
    onChatSync,
    disposeAllBridgeListeners,
  } from "$lib/tauri";

  let { children }: { children: Snippet } = $props();

  let isOnboarding = $derived(page.url.pathname.startsWith("/onboarding"));
  let isSidePanel = $derived(page.url.pathname.startsWith("/sidepanel"));
  let isQuickAsk = $derived(page.url.pathname.startsWith("/quickask"));
  let isOAuthCallback = $derived(page.url.pathname.startsWith("/oauth-callback"));

  type AuthState = "loading" | "checking_backend" | "backend_missing" | "backend_stopped" | "installing" | "starting" | "authenticating" | "authenticated" | "error";
  let authState = $state<AuthState>("loading");
  let authError = $state<string | null>(null);

  // Set data-native-blur attribute and transparent body when native vibrancy is active
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

  // Notify when agent finishes while window is unfocused
  let prevWorking = $state(false);
  $effect(() => {
    const working = activityStore.isAgentWorking;
    if (prevWorking && !working) {
      const latest = activityStore.latestEntry;
      notifyAgentComplete(latest?.content ?? "Task completed");
    }
    prevWorking = working;
  });

  // Cross-window bridge: emit session switch when active session changes
  let prevSessionId = $state<string | null>(null);
  $effect(() => {
    const sid = sessionStore.activeSessionId;
    if (sid && sid !== prevSessionId && authState === "authenticated") {
      emitSessionSwitch(sid);
    }
    prevSessionId = sid;
  });

  // Cross-window bridge: emit chat sync when streaming ends
  let prevStreaming = $state(false);
  $effect(() => {
    const streaming = chatStore.isStreaming;
    if (prevStreaming && !streaming && authState === "authenticated") {
      emitChatSync({
        messages: chatStore.messages,
        streaming: false,
        streamingContent: "",
      });
    }
    prevStreaming = streaming;
  });

  // Cross-window bridge: emit settings update when settings change
  $effect(() => {
    const settings = settingsStore.settings;
    if (settings && authState === "authenticated") {
      emitSettingsUpdate(settings);
    }
  });

  function onTokenRefreshed(newTokens: OAuthTokens) {
    connectionStore.updateToken(newTokens.access_token);
    // Notify other windows (side panel, quick ask) about the new token
    import("@tauri-apps/api/event").then(({ emit }) => {
      emit("token-updated", { token: newTokens.access_token }).catch(() => {});
    }).catch(() => {});
  }

  function onTokenRefreshFailed(_error: Error) {
    authState = "error";
    authError = "Session expired. Please sign in again.";
  }

  async function readMasterToken(): Promise<string | null> {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      return await invoke<string>("read_access_token");
    } catch {
      return null;
    }
  }

  async function authenticate() {
    // Side panel and quick ask handle their own auth
    const pathname = window.location.pathname;
    if (pathname.startsWith("/sidepanel") || pathname.startsWith("/quickask") || pathname.startsWith("/oauth-callback")) return;

    if (!isTauri()) {
      // Browser dev fallback — skip backend check entirely
      const token = "dev-token";
      await initializeStores(token);
      authState = "authenticated";
      finishSetup();
      return;
    }

    // Step 1: Check if backend is running (TCP check)
    authState = "checking_backend";
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      const running = await invoke<boolean>("check_backend_running", { port: 8888 });

      if (!running) {
        // Backend not running — check if PocketPaw is installed
        const status = await invoke<{
          installed: boolean;
          has_config_dir: boolean;
          has_cli: boolean;
          config_dir: string;
        }>("check_pocketpaw_installed");

        if (status.has_cli) {
          authState = "backend_stopped";
        } else {
          authState = "backend_missing";
        }
        return; // SetupBackend component takes over
      }
    } catch {
      // If Tauri commands fail, fall through to normal auth
    }

    // Step 2: Backend is running — proceed with auth
    // Read master token for WebSocket (WS only accepts master/session tokens, not OAuth)
    const masterToken = await readMasterToken();

    // Try stored OAuth token first
    authState = "loading";
    const existingToken = await getValidToken();

    if (existingToken) {
      await initializeStores(existingToken, undefined, masterToken ?? undefined);
      authState = "authenticated";

      // Schedule auto-refresh using the stored tokens
      const { readTokens } = await import("$lib/auth/token-store");
      const tokens = await readTokens();
      if (tokens) {
        scheduleTokenRefresh(tokens, onTokenRefreshed, onTokenRefreshFailed);
      }

      finishSetup();
      return;
    }

    // No valid token — start OAuth flow
    authState = "authenticating";
    const result = await startOAuthFlow();

    if (result.success && result.tokens) {
      await initializeStores(result.tokens.access_token, undefined, masterToken ?? undefined);
      authState = "authenticated";
      scheduleTokenRefresh(result.tokens, onTokenRefreshed, onTokenRefreshFailed);
      finishSetup();
    } else {
      authState = "error";
      authError = result.error ?? "Authentication failed.";
    }
  }

  function finishSetup() {
    const pathname = window.location.pathname;

    // Redirect to onboarding if not completed
    const onboarded = localStorage.getItem("pocketpaw_onboarded");
    if (!onboarded && !pathname.startsWith("/onboarding")) {
      goto("/onboarding");
    }

    // Request notification permission
    requestNotificationPermission();

    // Desktop-only: hotkeys, tray, side panel (skip on native mobile)
    if (!platformStore.isNativeMobile) {
      registerHotkeys({
        onQuickAsk: async () => {
          try {
            const { invoke } = await import("@tauri-apps/api/core");
            await invoke("toggle_quick_ask");
          } catch {
            // Not in Tauri
          }
        },
        onToggleSidePanel: async () => {
          try {
            const { invoke } = await import("@tauri-apps/api/core");
            await invoke("toggle_side_panel");
          } catch {
            // Not in Tauri
          }
        },
      });

      setupTrayListeners({
        onNavigate: (path: string) => {
          goto(path);
        },
      });

      // Cross-window bridge: respond when side panel is ready
      onSidePanelReady(() => {
        // Send current state to the side panel
        if (sessionStore.activeSessionId) {
          emitSessionSwitch(sessionStore.activeSessionId);
        }
        emitChatSync({
          messages: chatStore.messages,
          streaming: chatStore.isStreaming,
          streamingContent: chatStore.streamingContent,
        });
        if (settingsStore.settings) {
          emitSettingsUpdate(settingsStore.settings);
        }
      });

      // Cross-window bridge: receive chat sync from side panel
      onChatSync((payload) => {
        // Only apply if we're not currently streaming ourselves
        if (!chatStore.isStreaming) {
          chatStore.loadHistory(payload.messages);
        }
      });
    }
  }

  async function retryAuth() {
    authError = null;
    await authenticate();
  }

  onMount(() => {
    authenticate().catch((err) => {
      console.error("[layout] authenticate() failed:", err);
      authState = "error";
      authError = err?.message ?? "Failed to connect. Check that the backend is running.";
    });
  });

  onDestroy(() => {
    cancelScheduledRefresh();
    unregisterHotkeys();
    cleanupTrayListeners();
    disposeAllBridgeListeners();
  });
</script>

<ModeWatcher />
<div>
  <TooltipProvider>
    {#if isSidePanel || isQuickAsk || isOAuthCallback}
      {@render children()}
    {:else if authState === "checking_backend" || authState === "loading"}
      <div class="flex h-dvh w-screen items-center justify-center bg-background">
        <div class="flex flex-col items-center gap-3">
          <div class="h-8 w-8 animate-spin rounded-full border-2 border-primary border-t-transparent"></div>
          <p class="text-sm text-muted-foreground">
            {authState === "checking_backend" ? "Checking backend..." : "Connecting..."}
          </p>
        </div>
      </div>
    {:else if authState === "backend_missing" || authState === "backend_stopped" || authState === "installing" || authState === "starting"}
      <SetupBackend backendState={authState} onReady={() => {
        // Fresh install/start — force onboarding to re-run
        localStorage.removeItem("pocketpaw_onboarded");
        authenticate();
      }} />
    {:else if authState === "authenticating"}
      <div class="flex h-dvh w-screen items-center justify-center bg-background">
        <div class="flex flex-col items-center gap-3">
          <div class="h-8 w-8 animate-spin rounded-full border-2 border-primary border-t-transparent"></div>
          <p class="text-sm text-muted-foreground">Waiting for sign-in...</p>
        </div>
      </div>
    {:else if authState === "error"}
      <div class="flex h-dvh w-screen items-center justify-center bg-background">
        <div class="flex flex-col items-center gap-4 text-center">
          <p class="text-sm text-destructive">{authError ?? "Authentication failed."}</p>
          <button
            onclick={retryAuth}
            class="rounded-md bg-primary px-4 py-2 text-sm text-primary-foreground hover:opacity-90"
          >
            Try again
          </button>
        </div>
      </div>
    {:else if isOnboarding}
      <div class="flex h-dvh w-screen items-center justify-center bg-background">
        {@render children()}
      </div>
    {:else}
      <AppShell>
        {@render children()}
      </AppShell>
    {/if}
  </TooltipProvider>
  {#if !isSidePanel && !isQuickAsk}
    <Toaster />
  {/if}
</div>

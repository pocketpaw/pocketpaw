<script lang="ts">
  import { onMount } from "svelte";
  import { page } from "$app/state";

  let status = $state("Processing sign-in...");

  onMount(async () => {
    const code = page.url.searchParams.get("code");
    const state = page.url.searchParams.get("state");
    const error = page.url.searchParams.get("error");

    if (error) {
      status = `Authorization denied: ${error}`;
      emitAndClose("oauth-callback-error", { error });
      return;
    }

    if (!code || !state) {
      status = "Missing authorization parameters.";
      emitAndClose("oauth-callback-error", { error: "missing_params" });
      return;
    }

    status = "Signing in...";
    emitAndClose("oauth-callback", { code, state });
  });

  async function emitAndClose(event: string, payload: Record<string, string>) {
    try {
      const { emit } = await import("@tauri-apps/api/event");
      await emit(event, payload);
    } catch {
      // Not in Tauri — nothing to emit to
    }

    // Close this window after a short delay
    setTimeout(async () => {
      try {
        const { getCurrentWindow } = await import("@tauri-apps/api/window");
        await getCurrentWindow().close();
      } catch {
        // Not in Tauri
      }
    }, 500);
  }
</script>

<div class="flex h-dvh w-screen items-center justify-center bg-background">
  <div class="flex flex-col items-center gap-3">
    <div class="h-8 w-8 animate-spin rounded-full border-2 border-primary border-t-transparent"></div>
    <p class="text-sm text-muted-foreground">{status}</p>
  </div>
</div>

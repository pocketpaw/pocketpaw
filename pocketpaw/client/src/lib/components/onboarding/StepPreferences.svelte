<script lang="ts">
  import { Switch } from "$lib/components/ui/switch";
  import { Bell, Volume2, Wrench, FolderOpen } from "@lucide/svelte";
  import { isTauri } from "$lib/auth";
  import Typewriter from "./Typewriter.svelte";

  let {
    initialPrefs = {
      notifications_enabled: true,
      sound_enabled: true,
      tool_notifications_enabled: true,
      default_workspace_dir: "",
    },
    onNext,
  }: {
    initialPrefs?: {
      notifications_enabled: boolean;
      sound_enabled: boolean;
      tool_notifications_enabled: boolean;
      default_workspace_dir: string;
    };
    onNext: (prefs: {
      notifications_enabled: boolean;
      sound_enabled: boolean;
      tool_notifications_enabled: boolean;
      default_workspace_dir: string;
    }) => void;
  } = $props();

  let notifs = $state(initialPrefs.notifications_enabled);
  let sound = $state(initialPrefs.sound_enabled);
  let toolNotifs = $state(initialPrefs.tool_notifications_enabled);
  let workspaceDir = $state(initialPrefs.default_workspace_dir);
  let showContent = $state(false);

  async function pickFolder() {
    if (!isTauri()) return;
    try {
      const { open } = await import("@tauri-apps/plugin-dialog");
      const selected = await open({ directory: true, multiple: false, title: "Choose workspace" });
      if (selected) workspaceDir = selected as string;
    } catch (e) {
      console.error("Folder picker failed:", e);
    }
  }

  const toggles = [
    { icon: Bell, label: "Desktop notifications", desc: "Get notified when tasks complete", get: () => notifs, set: (v: boolean) => (notifs = v) },
    { icon: Volume2, label: "Sound effects", desc: "Play sounds for events", get: () => sound, set: (v: boolean) => (sound = v) },
    { icon: Wrench, label: "Tool notifications", desc: "Show when tools are running", get: () => toolNotifs, set: (v: boolean) => (toolNotifs = v) },
  ];
</script>

<div class="flex w-full max-w-md flex-col items-center gap-8">
  <h2 class="text-center text-xl font-semibold text-foreground">
    <Typewriter text="Let's set things up your way" speed={35} onDone={() => (showContent = true)} />
  </h2>

  {#if showContent}
    <div class="flex w-full flex-col gap-3 animate-in fade-in slide-in-from-bottom-4 duration-500">
      {#each toggles as toggle}
        {@const Icon = toggle.icon}
        <div class="flex items-center justify-between rounded-xl border border-border bg-card p-4">
          <div class="flex items-center gap-3">
            <div class="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10">
              <Icon class="h-4 w-4 text-primary" />
            </div>
            <div class="flex flex-col">
              <span class="text-sm font-medium text-foreground">{toggle.label}</span>
              <span class="text-xs text-muted-foreground">{toggle.desc}</span>
            </div>
          </div>
          <Switch checked={toggle.get()} onCheckedChange={toggle.set} />
        </div>
      {/each}

      <div class="flex flex-col gap-2 rounded-xl border border-border bg-card p-4">
        <div class="flex items-center gap-3">
          <div class="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10">
            <FolderOpen class="h-4 w-4 text-primary" />
          </div>
          <div class="flex flex-col">
            <span class="text-sm font-medium text-foreground">Workspace folder</span>
            <span class="text-xs text-muted-foreground">Where the agent operates by default</span>
          </div>
        </div>
        <div class="mt-2 flex gap-2">
          <input
            bind:value={workspaceDir}
            type="text"
            placeholder="~/projects"
            class="flex-1 rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary/20"
          />
          {#if isTauri()}
            <button
              onclick={pickFolder}
              class="rounded-lg border border-border px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
            >
              Browse
            </button>
          {/if}
        </div>
      </div>
    </div>

    <button
      onclick={() => onNext({
        notifications_enabled: notifs,
        sound_enabled: sound,
        tool_notifications_enabled: toolNotifs,
        default_workspace_dir: workspaceDir,
      })}
      class="rounded-xl bg-primary px-6 py-3 text-sm font-medium text-primary-foreground transition-all hover:opacity-90"
    >
      Almost done! →
    </button>
  {/if}
</div>

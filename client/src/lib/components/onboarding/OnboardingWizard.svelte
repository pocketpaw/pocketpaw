<script lang="ts">
  import { settingsStore } from "$lib/stores";
  import StepWelcome from "./StepWelcome.svelte";
  import StepAvatar from "./StepAvatar.svelte";
  import StepTheme from "./StepTheme.svelte";
  import StepPreferences from "./StepPreferences.svelte";
  import StepChooseAI from "./StepChooseAI.svelte";
  import StepReady from "./StepReady.svelte";

  const TOTAL_STEPS = 6;
  let currentStep = $state(1);

  // Collected data
  let userName = $state("");
  let avatarEmoji = $state("🐾");
  let theme = $state("system");
  let prefs = $state({
    notifications_enabled: true,
    sound_enabled: true,
    tool_notifications_enabled: true,
    default_workspace_dir: "",
  });

  function next() {
    currentStep++;
  }

  function onWelcomeDone(name: string) {
    userName = name;
    next();
  }

  function onAvatarDone(emoji: string) {
    avatarEmoji = emoji;
    next();
  }

  function onThemeDone(t: string) {
    theme = t;
    next();
  }

  async function onPrefsDone(p: typeof prefs) {
    prefs = p;
    // Persist user preferences to backend now (before AI setup)
    try {
      await settingsStore.update({
        user_display_name: userName,
        user_avatar_emoji: avatarEmoji,
        theme_preference: theme,
        ...p,
      });
    } catch (e) {
      console.error("[Onboarding] Failed to save preferences:", e);
    }
    next();
  }

  function onAIDone() {
    next();
  }
</script>

<div class="flex h-full w-full flex-col items-center justify-center px-6">
  <!-- Progress dots -->
  <div class="mb-8 flex items-center gap-2">
    {#each Array(TOTAL_STEPS) as _, i}
      <div
        class={i + 1 === currentStep
          ? "h-1.5 w-6 rounded-full bg-primary transition-all duration-300"
          : i + 1 < currentStep
            ? "h-1.5 w-6 rounded-full bg-primary/40 transition-all duration-300"
            : "h-1.5 w-2 rounded-full bg-muted transition-all duration-300"}
      ></div>
    {/each}
  </div>

  <!-- Step content with key to force remount for animations -->
  {#key currentStep}
    <div class="animate-in fade-in slide-in-from-right-8 duration-400">
      {#if currentStep === 1}
        <StepWelcome initialName={userName} onNext={onWelcomeDone} />
      {:else if currentStep === 2}
        <StepAvatar initialEmoji={avatarEmoji} onNext={onAvatarDone} />
      {:else if currentStep === 3}
        <StepTheme initialTheme={theme} onNext={onThemeDone} />
      {:else if currentStep === 4}
        <StepPreferences initialPrefs={prefs} onNext={onPrefsDone} />
      {:else if currentStep === 5}
        <StepChooseAI onNext={onAIDone} />
      {:else if currentStep === 6}
        <StepReady {userName} {avatarEmoji} {theme} />
      {/if}
    </div>
  {/key}
</div>

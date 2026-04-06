<script lang="ts">
  import { goto } from "$app/navigation";
  import Confetti from "./Confetti.svelte";
  import Typewriter from "./Typewriter.svelte";

  let {
    userName = "",
    avatarEmoji = "🐾",
    theme = "system",
  }: {
    userName?: string;
    avatarEmoji?: string;
    theme?: string;
  } = $props();

  let showContent = $state(false);

  function startChatting() {
    localStorage.setItem("pocketpaw_onboarded", "true");
    goto("/");
  }
</script>

<Confetti />

<div class="flex w-full max-w-md flex-col items-center gap-8">
  <div class="text-7xl">{avatarEmoji}</div>

  <h2 class="text-center text-2xl font-semibold text-foreground">
    <Typewriter
      text={userName ? `You're all set, ${userName}!` : "You're all set!"}
      speed={35}
      onDone={() => (showContent = true)}
    />
  </h2>

  {#if showContent}
    <div class="flex flex-col items-center gap-6 animate-in fade-in slide-in-from-bottom-4 duration-500">
      <div class="flex flex-wrap justify-center gap-2">
        <span class="rounded-full bg-primary/10 px-3 py-1 text-xs font-medium text-primary">
          {avatarEmoji} {userName || "User"}
        </span>
        <span class="rounded-full bg-primary/10 px-3 py-1 text-xs font-medium text-primary">
          🎨 {theme === "system" ? "System theme" : theme === "dark" ? "Dark mode" : "Light mode"}
        </span>
      </div>

      <p class="text-center text-sm text-muted-foreground">
        Your AI assistant is ready. Let's get to work.
      </p>

      <button
        onclick={startChatting}
        class="rounded-xl bg-primary px-8 py-3 text-sm font-medium text-primary-foreground transition-all hover:opacity-90 hover:scale-[1.02]"
      >
        Start Chatting 🚀
      </button>
    </div>
  {/if}
</div>

<script lang="ts">
  import { goto } from "$app/navigation";
  import { Check } from "@lucide/svelte";

  function startChatting() {
    localStorage.setItem("pocketpaw_onboarded", "true");
    goto("/");
  }
</script>

<div class="flex flex-col items-center gap-6 text-center">
  <div class="flex h-12 w-12 items-center justify-center rounded-full bg-paw-success/10">
    <Check class="h-6 w-6 text-paw-success" strokeWidth={2} />
  </div>

  <div class="flex flex-col gap-2">
    <h1 class="text-2xl font-semibold text-foreground">You're all set!</h1>
    <p class="text-sm text-muted-foreground">Your AI assistant is ready to go.</p>
  </div>

  <button
    onclick={startChatting}
    class="mt-2 rounded-lg bg-primary px-6 py-2.5 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90"
  >
    Start Chatting
  </button>
</div>

<script lang="ts">
  import { Cpu, Cloud } from "@lucide/svelte";
  import OllamaSetup from "./OllamaSetup.svelte";
  import ApiKeySetup from "./ApiKeySetup.svelte";
  import Typewriter from "./Typewriter.svelte";

  let { onNext }: { onNext: () => void } = $props();

  let subflow = $state<"none" | "ollama" | "api">("none");

  function openOllama() { subflow = "ollama"; }
  function openApi() { subflow = "api"; }
  function backToCards() { subflow = "none"; }
</script>

{#if subflow === "ollama"}
  <OllamaSetup onComplete={onNext} onBack={backToCards} onSwitchToApi={openApi} />
{:else if subflow === "api"}
  <ApiKeySetup onComplete={onNext} onBack={backToCards} />
{:else}
  <div class="flex w-full max-w-lg flex-col items-center gap-6">
    <div class="flex flex-col gap-1 text-center">
      <h2 class="text-lg font-semibold text-foreground">
        <Typewriter text="Now, let's pick your AI brain!" speed={35} />
      </h2>
      <p class="text-sm text-muted-foreground">Pick how you'd like to power your assistant.</p>
    </div>

    <div class="grid w-full grid-cols-2 gap-3">
      <!-- Ollama card -->
      <button
        onclick={openOllama}
        class="flex flex-col items-start gap-3 rounded-xl border border-border p-5 text-left transition-colors hover:border-primary/50 hover:bg-accent/50"
      >
        <div class="flex h-10 w-10 items-center justify-center rounded-lg bg-paw-success/10">
          <Cpu class="h-5 w-5 text-paw-success" strokeWidth={1.75} />
        </div>
        <div class="flex flex-col gap-1">
          <span class="text-sm font-semibold text-foreground">Free &amp; Local</span>
          <span class="text-xs text-muted-foreground">
            Runs 100% on your machine using Ollama. No account needed.
          </span>
        </div>
        <span class="mt-auto text-xs font-medium text-primary">Set up &rarr;</span>
      </button>

      <!-- API card -->
      <button
        onclick={openApi}
        class="flex flex-col items-start gap-3 rounded-xl border border-border p-5 text-left transition-colors hover:border-primary/50 hover:bg-accent/50"
      >
        <div class="flex h-10 w-10 items-center justify-center rounded-lg bg-paw-info/10">
          <Cloud class="h-5 w-5 text-paw-info" strokeWidth={1.75} />
        </div>
        <div class="flex flex-col gap-1">
          <span class="text-sm font-semibold text-foreground">Powerful</span>
          <span class="text-xs text-muted-foreground">
            Use Claude, OpenAI, or Google. Smarter models, needs an API key.
          </span>
        </div>
        <span class="mt-auto text-xs font-medium text-primary">Set up &rarr;</span>
      </button>
    </div>

    <button
      onclick={onNext}
      class="text-xs text-muted-foreground transition-colors hover:text-foreground"
    >
      Skip for now
    </button>
  </div>
{/if}

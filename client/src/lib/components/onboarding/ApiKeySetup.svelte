<script lang="ts">
  import { settingsStore } from "$lib/stores";
  import { ExternalLink, Eye, EyeOff, Shield } from "@lucide/svelte";
  import ModelSearch from "./ModelSearch.svelte";

  let { onComplete, onBack }: { onComplete: () => void; onBack: () => void } = $props();

  const providers = [
    {
      id: "anthropic",
      name: "Anthropic (Claude)",
      prefix: "sk-ant-",
      keyUrl: "https://console.anthropic.com/settings/keys",
      backend: "claude_agent_sdk",
      providerField: "anthropic",
      models: [
        { id: "claude-sonnet-4-6", name: "Claude Sonnet 4.6", desc: "Fast, best balance of speed & intelligence" },
        { id: "claude-opus-4-6", name: "Claude Opus 4.6", desc: "Most intelligent, best for agents & coding" },
        { id: "claude-haiku-4-5", name: "Claude Haiku 4.5", desc: "Fastest, near-frontier intelligence" },
        { id: "claude-sonnet-4-5", name: "Claude Sonnet 4.5", desc: "Previous gen, still strong" },
      ],
    },
    {
      id: "openai",
      name: "OpenAI",
      prefix: "sk-",
      keyUrl: "https://platform.openai.com/api-keys",
      backend: "openai_agents",
      providerField: "openai",
      models: [
        { id: "gpt-5.2", name: "GPT-5.2", desc: "Latest flagship, best overall" },
        { id: "gpt-5.2-pro", name: "GPT-5.2 Pro", desc: "Smarter, more precise responses" },
        { id: "gpt-5.1", name: "GPT-5.1", desc: "Flagship coding & agentic tasks" },
        { id: "gpt-5-mini", name: "GPT-5 Mini", desc: "Fast, cost-efficient" },
        { id: "gpt-5-nano", name: "GPT-5 Nano", desc: "Ultra-fast, lightweight" },
        { id: "o4-mini", name: "o4-mini", desc: "Fast reasoning" },
        { id: "o3-pro", name: "o3-pro", desc: "Best reasoning, more compute" },
        { id: "gpt-4.1", name: "GPT-4.1", desc: "Great coding & long-context" },
        { id: "gpt-4.1-mini", name: "GPT-4.1 Mini", desc: "Budget-friendly" },
        { id: "gpt-4.1-nano", name: "GPT-4.1 Nano", desc: "Smallest GPT-4.1" },
      ],
    },
    {
      id: "google",
      name: "Google (Gemini)",
      prefix: "AI",
      keyUrl: "https://aistudio.google.com/app/apikey",
      backend: "google_adk",
      providerField: "google",
      models: [
        { id: "gemini-3.1-pro-preview", name: "Gemini 3.1 Pro", desc: "Latest, most capable" },
        { id: "gemini-3-flash-preview", name: "Gemini 3 Flash", desc: "Frontier-class, affordable" },
        { id: "gemini-2.5-flash", name: "Gemini 2.5 Flash", desc: "Best price-performance, stable" },
        { id: "gemini-2.5-pro", name: "Gemini 2.5 Pro", desc: "Advanced reasoning & coding" },
        { id: "gemini-2.5-flash-lite", name: "Gemini 2.5 Flash Lite", desc: "Fastest, most budget-friendly" },
      ],
    },
  ] as const;

  type ProviderId = (typeof providers)[number]["id"];

  let selectedProvider = $state<ProviderId>("anthropic");
  let apiKey = $state("");
  let showKey = $state(false);
  let error = $state("");
  let subStep = $state<"key" | "model">("key");
  let selectedModel = $state("");

  let currentProvider = $derived(providers.find((p) => p.id === selectedProvider)!);

  function validate(): boolean {
    const trimmed = apiKey.trim();
    if (!trimmed) {
      error = "Please enter your API key.";
      return false;
    }
    if (selectedProvider === "anthropic" && !trimmed.startsWith("sk-ant-")) {
      error = "Anthropic keys start with sk-ant-";
      return false;
    }
    if (selectedProvider === "openai" && !trimmed.startsWith("sk-")) {
      error = "OpenAI keys start with sk-";
      return false;
    }
    error = "";
    return true;
  }

  function handleKeySubmit() {
    if (!validate()) return;
    // Advance to model selection
    selectedModel = currentProvider.models[0].id;
    subStep = "model";
  }

  function handleModelSelect(modelId: string) {
    selectedModel = modelId;
    saveAndContinue();
  }

  function saveAndContinue() {
    const provider = currentProvider;
    // Build the settings patch: API key + backend + model + provider fields
    const keyFieldMap: Record<string, string> = {
      anthropic: "anthropic_api_key",
      openai: "openai_api_key",
      google: "google_api_key",
    };
    const modelFieldMap: Record<string, string> = {
      claude_agent_sdk: "claude_sdk_model",
      openai_agents: "openai_agents_model",
      google_adk: "google_adk_model",
    };

    const patch: Record<string, string> = {
      agent_backend: provider.backend,
      [keyFieldMap[provider.id]]: apiKey.trim(),
      [modelFieldMap[provider.backend]]: selectedModel,
    };

    settingsStore.update(patch);
    onComplete();
  }

  function backToKey() {
    subStep = "key";
    error = "";
  }
</script>

<div class="flex w-full max-w-md flex-col gap-5">
  {#if subStep === "key"}
    <button
      onclick={onBack}
      class="self-start text-xs text-muted-foreground transition-colors hover:text-foreground"
    >
      &larr; Back
    </button>

    <div class="flex flex-col gap-1">
      <h2 class="text-lg font-semibold text-foreground">Connect an AI Provider</h2>
      <p class="text-sm text-muted-foreground">
        Paste your API key to use a cloud AI model.
      </p>
    </div>

    <!-- Provider selector -->
    <div class="flex gap-2">
      {#each providers as provider}
        <button
          onclick={() => { selectedProvider = provider.id; apiKey = ""; error = ""; }}
          class={selectedProvider === provider.id
            ? "rounded-lg border-2 border-primary px-3 py-2 text-sm font-medium text-foreground"
            : "rounded-lg border border-border px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"}
        >
          {provider.name}
        </button>
      {/each}
    </div>

    <!-- API key input -->
    <div class="flex flex-col gap-2">
      <div class="relative">
        <input
          type={showKey ? "text" : "password"}
          bind:value={apiKey}
          placeholder="Paste your API key"
          onkeydown={(e) => { if (e.key === "Enter") handleKeySubmit(); }}
          class="w-full rounded-lg border border-border bg-muted/50 px-3 py-2.5 pr-10 text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
        />
        <button
          onclick={() => showKey = !showKey}
          class="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-muted-foreground transition-colors hover:text-foreground"
        >
          {#if showKey}
            <EyeOff class="h-4 w-4" />
          {:else}
            <Eye class="h-4 w-4" />
          {/if}
        </button>
      </div>

      {#if error}
        <p class="text-xs text-paw-error">{error}</p>
      {/if}

      <a
        href={currentProvider.keyUrl}
        target="_blank"
        rel="noopener noreferrer"
        class="inline-flex items-center gap-1 text-xs text-primary transition-opacity hover:opacity-80"
      >
        Where do I get one?
        <ExternalLink class="h-3 w-3" />
      </a>
    </div>

    <!-- Trust message -->
    <div class="flex items-start gap-2 rounded-lg bg-muted/30 px-3 py-2.5">
      <Shield class="mt-0.5 h-3.5 w-3.5 shrink-0 text-paw-success" />
      <p class="text-xs text-muted-foreground">
        Your key is encrypted and stored locally. We never see it.
      </p>
    </div>

    <!-- Submit -->
    <button
      onclick={handleKeySubmit}
      disabled={!apiKey.trim()}
      class="rounded-lg bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-40"
    >
      Continue
    </button>

  {:else}
    <!-- Model selection sub-step -->
    <button
      onclick={backToKey}
      class="self-start text-xs text-muted-foreground transition-colors hover:text-foreground"
    >
      &larr; Back
    </button>

    <div class="flex flex-col gap-1">
      <h2 class="text-lg font-semibold text-foreground">Choose a Model</h2>
      <p class="text-sm text-muted-foreground">
        Pick a default model for {currentProvider.name}.
      </p>
    </div>

    <ModelSearch
      models={currentProvider.models.map((m) => ({ ...m }))}
      bind:selectedModel
      placeholder="Search models..."
      onSelect={handleModelSelect}
    />
  {/if}
</div>

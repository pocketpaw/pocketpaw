<script lang="ts">
  import { settingsStore } from "$lib/stores";
  import { Loader2, RefreshCw, ExternalLink, Check, Cloud } from "@lucide/svelte";
  import { Progress } from "$lib/components/ui/progress";
  import ModelSearch from "./ModelSearch.svelte";

  let { onComplete, onBack, onSwitchToApi }: {
    onComplete: () => void;
    onBack: () => void;
    onSwitchToApi?: () => void;
  } = $props();

  type OllamaModel = { name: string; size: number };

  let status = $state<"checking" | "not_found" | "found" | "pulling" | "done">("checking");
  let rawModels = $state<OllamaModel[]>([]);
  let selectedModel = $state("");
  let pullProgress = $state(0);
  let error = $state("");

  const suggestedModels = [
    { id: "deepseek-r1:8b", name: "DeepSeek R1 8B", desc: "Open reasoning model, compact" },
    { id: "qwen3:8b", name: "Qwen 3 8B", desc: "Strong all-around, multilingual" },
    { id: "llama3.1:8b", name: "Llama 3.1 8B", desc: "Meta, versatile" },
    { id: "devstral", name: "Devstral", desc: "Mistral, optimized for coding" },
    { id: "gemma2:2b", name: "Gemma 2 2B", desc: "Google, lightweight" },
    { id: "phi3:mini", name: "Phi-3 Mini", desc: "Microsoft, compact 3B" },
  ];

  // Merge installed models + suggested models into a single ModelSearch list
  let searchModels = $derived.by(() => {
    const installedNames = new Set(rawModels.map((m) => m.name));
    const installed = rawModels.map((m) => ({
      id: m.name,
      name: m.name,
      size: `${Math.round((m.size / 1e9) * 10) / 10} GB`,
    }));
    const suggested = suggestedModels
      .filter((m) => !installedNames.has(m.id))
      .map((m) => ({ id: m.id, name: m.name, desc: m.desc }));
    return [...installed, ...suggested];
  });

  let installedNames = $derived(new Set(rawModels.map((m) => m.name)));

  async function checkOllama() {
    status = "checking";
    error = "";
    try {
      const res = await fetch("http://localhost:11434/api/tags");
      if (res.ok) {
        const data = await res.json();
        rawModels = data.models ?? [];
        status = "found";
      } else {
        status = "not_found";
      }
    } catch {
      status = "not_found";
    }
  }

  async function pullModel() {
    status = "pulling";
    pullProgress = 0;
    error = "";

    try {
      const res = await fetch("http://localhost:11434/api/pull", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: selectedModel }),
      });

      if (!res.ok || !res.body) {
        error = "Failed to start model download";
        status = "found";
        return;
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        const lines = decoder.decode(value, { stream: true }).split("\n");
        for (const line of lines) {
          if (!line.trim()) continue;
          try {
            const data = JSON.parse(line);
            if (data.total && data.completed) {
              pullProgress = Math.round((data.completed / data.total) * 100);
            }
            if (data.status === "success") {
              pullProgress = 100;
            }
          } catch {
            // skip
          }
        }
      }

      status = "done";
      saveAndContinue();
    } catch {
      error = "Download failed. Check that Ollama is running.";
      status = "found";
    }
  }

  function saveAndContinue() {
    settingsStore.update({
      agent_backend: "claude_agent_sdk",
      llm_provider: "ollama",
      ollama_model: selectedModel,
    });
    onComplete();
  }

  function handleModelSelect(id: string) {
    selectedModel = id;
    if (installedNames.has(id)) {
      // Already installed — save and continue immediately
      saveAndContinue();
    } else {
      // Not installed — start pull
      pullModel();
    }
  }

  // Check on mount
  $effect(() => {
    checkOllama();
  });
</script>

<div class="flex w-full max-w-md flex-col gap-5">
  <button
    onclick={onBack}
    class="self-start text-xs text-muted-foreground transition-colors hover:text-foreground"
  >
    &larr; Back
  </button>

  <div class="flex flex-col gap-1">
    <h2 class="text-lg font-semibold text-foreground">Set Up Local AI</h2>
    <p class="text-sm text-muted-foreground">
      PocketPaw uses Ollama to run AI models on your machine.
    </p>
  </div>

  {#if status === "checking"}
    <div class="flex items-center gap-2 rounded-lg border border-border bg-muted/50 p-4">
      <Loader2 class="h-4 w-4 animate-spin text-muted-foreground" />
      <span class="text-sm text-muted-foreground">Looking for Ollama...</span>
    </div>
  {:else if status === "not_found"}
    <div class="flex flex-col gap-3 rounded-lg border border-border bg-muted/50 p-4">
      <p class="text-sm text-foreground">Ollama is not running on this machine.</p>
      <p class="text-sm text-muted-foreground">
        Install Ollama to run AI models locally, for free.
      </p>
      <div class="flex items-center gap-2">
        <a
          href="https://ollama.ai/download"
          target="_blank"
          rel="noopener noreferrer"
          class="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90"
        >
          Download Ollama
          <ExternalLink class="h-3 w-3" />
        </a>
        <button
          onclick={checkOllama}
          class="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
        >
          <RefreshCw class="h-3 w-3" />
          Check Again
        </button>
      </div>
    </div>

    {#if onSwitchToApi}
      <button
        onclick={onSwitchToApi}
        class="inline-flex items-center gap-1.5 self-start text-xs text-muted-foreground transition-colors hover:text-foreground"
      >
        <Cloud class="h-3 w-3" />
        Use an API key instead
      </button>
    {/if}
  {:else if status === "found"}
    <ModelSearch
      models={searchModels}
      bind:selectedModel
      placeholder="Search models..."
      onSelect={handleModelSelect}
    />
  {:else if status === "pulling"}
    <div class="flex flex-col gap-3 rounded-lg border border-border bg-muted/50 p-4">
      <div class="flex items-center gap-2">
        <Loader2 class="h-4 w-4 animate-spin text-primary" />
        <span class="text-sm text-foreground">
          Downloading {selectedModel}...
        </span>
      </div>
      <Progress value={pullProgress} max={100} class="h-2" />
      <p class="text-xs text-muted-foreground">{pullProgress}% complete</p>
    </div>
  {:else if status === "done"}
    <div class="flex items-center gap-2 rounded-lg border border-paw-success/30 bg-paw-success/10 p-4">
      <Check class="h-4 w-4 text-paw-success" />
      <span class="text-sm text-foreground">Model ready!</span>
    </div>
  {/if}

  {#if error}
    <p class="text-xs text-paw-error">{error}</p>
  {/if}
</div>

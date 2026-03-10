<script lang="ts">
  import type { BackendInfo, Settings } from "$lib/api";
  import { connectionStore, settingsStore } from "$lib/stores";
  import * as Select from "$lib/components/ui/select";
  import { Switch } from "$lib/components/ui/switch";
  import { Badge } from "$lib/components/ui/badge";
  import * as Alert from "$lib/components/ui/alert";
  import {
    Eye,
    EyeOff,
    Loader2,
    Download,
    RefreshCw,
    ExternalLink,
    Copy,
    AlertTriangle,
  } from "@lucide/svelte";
  import { toast } from "svelte-sonner";

  let backends = $state<BackendInfo[]>([]);
  let loading = $state(true);
  let saving = $state(false);
  let installing = $state<string | null>(null);

  // Local form state
  let selectedBackend = $state("");
  let selectedProvider = $state("");
  let selectedModel = $state("");
  let customModel = $state("");
  let ollamaHost = $state("http://localhost:11434");
  let ollamaModels = $state<string[]>([]);
  let ollamaFetching = $state(false);
  let openaiCompatBaseUrl = $state("");
  let opencodeBaseUrl = $state("");
  let apiKeyInput = $state("");
  let apiKeyMasked = $state(true);
  let changingKey = $state(false);
  let smartRouting = $state(false);
  let maxTurns = $state(25);

  // Known models per provider (matching Python config defaults)
  const PROVIDER_MODELS: Record<string, string[]> = {
    anthropic: ["claude-sonnet-4-6", "claude-opus-4-6", "claude-haiku-4-5-20251001"],
    openai: ["gpt-5.2", "gpt-4o", "gpt-4o-mini", "o1-preview"],
    google: ["gemini-3-pro-preview", "gemini-2.0-flash", "gemini-1.5-pro"],
    ollama: ["llama3.2", "mistral", "codellama", "gemma2", "phi3"],
    openrouter: [], // 300+ models; user types the slug directly
    openai_compatible: [],
    copilot: [],
    azure: [],
  };

  const PROVIDER_LABELS: Record<string, string> = {
    anthropic: "Anthropic",
    openai: "OpenAI",
    google: "Google",
    ollama: "Ollama (Local)",
    openrouter: "OpenRouter",
    openai_compatible: "OpenAI Compatible",
    copilot: "GitHub Copilot",
    azure: "Azure OpenAI",
  };

  const API_KEY_CONFIG: Record<string, { label: string; placeholder: string }> = {
    anthropic: { label: "Anthropic API Key", placeholder: "sk-ant-..." },
    openai: { label: "OpenAI API Key", placeholder: "sk-..." },
    google: { label: "Google API Key", placeholder: "AIza..." },
    azure: { label: "Azure API Key", placeholder: "Enter Azure key..." },
    openrouter: { label: "OpenRouter API Key", placeholder: "sk-or-v1-..." },
    openai_compatible: { label: "API Key", placeholder: "Enter API key..." },
  };

  // Providers that don't require an API key
  const NO_KEY_PROVIDERS = new Set(["ollama", "copilot"]);

  // Per-backend field mapping
  type BackendFields = {
    providerField?: keyof Settings;
    modelField: keyof Settings;
    maxTurnsField: keyof Settings;
  };

  const BACKEND_FIELDS: Record<string, BackendFields> = {
    claude_agent_sdk: {
      providerField: "claude_sdk_provider",
      modelField: "claude_sdk_model",
      maxTurnsField: "claude_sdk_max_turns",
    },
    openai_agents: {
      providerField: "openai_agents_provider",
      modelField: "openai_agents_model",
      maxTurnsField: "openai_agents_max_turns",
    },
    google_adk: {
      modelField: "google_adk_model",
      maxTurnsField: "google_adk_max_turns",
    },
    codex_cli: {
      modelField: "codex_cli_model",
      maxTurnsField: "codex_cli_max_turns",
    },
    copilot_sdk: {
      providerField: "copilot_sdk_provider",
      modelField: "copilot_sdk_model",
      maxTurnsField: "copilot_sdk_max_turns",
    },
    opencode: {
      modelField: "opencode_model",
      maxTurnsField: "opencode_max_turns",
    },
  };

  const CAPABILITY_LABELS: Record<string, string> = {
    streaming: "Streaming",
    tools: "Tools",
    mcp: "MCP",
    multi_turn: "Multi-turn",
    custom_system_prompt: "System Prompt",
  };

  let currentBackend = $derived(backends.find((b) => b.name === selectedBackend));
  let providers = $derived(currentBackend?.supportedProviders ?? []);
  let models = $derived.by(() => {
    if (selectedProvider === "ollama" && ollamaModels.length > 0) {
      return ollamaModels;
    }
    return PROVIDER_MODELS[selectedProvider] ?? [];
  });
  let effectiveModel = $derived(customModel || selectedModel);
  let needsApiKey = $derived(!NO_KEY_PROVIDERS.has(selectedProvider) && selectedProvider !== "");
  let keyConfig = $derived(API_KEY_CONFIG[selectedProvider] ?? { label: "API Key", placeholder: "Enter API key..." });
  let missingKeys = $derived.by(() => {
    if (!currentBackend) return [];
    return currentBackend.requiredKeys.filter((k) => {
      // Check if we might be missing this key based on provider
      if (selectedProvider === "ollama" || selectedProvider === "copilot") return false;
      return true;
    });
  });

  // Whether form has been initialized from settings (only sync once)
  let initialized = $state(false);

  function syncFromSettings(s: Settings | null) {
    if (!s) return;
    selectedBackend = s.agent_backend ?? "";
    smartRouting = s.smart_routing_enabled ?? false;
    ollamaHost = (s.ollama_host as string) ?? "http://localhost:11434";
    openaiCompatBaseUrl = (s.openai_compatible_base_url as string) ?? "";
    opencodeBaseUrl = (s.opencode_base_url as string) ?? "";

    const fields = BACKEND_FIELDS[s.agent_backend];
    if (fields) {
      selectedProvider = fields.providerField ? ((s[fields.providerField] as string) ?? "") : "";
      const model = (s[fields.modelField] as string) ?? "";
      selectedModel = model;
      customModel = "";
      maxTurns = (s[fields.maxTurnsField] as number) ?? 25;
    } else {
      selectedProvider = "";
      selectedModel = "";
      customModel = "";
      maxTurns = 25;
    }

    // Fallback: if no per-backend provider, check global
    if (!selectedProvider && s.llm_provider) {
      selectedProvider = s.llm_provider;
    }
    initialized = true;
  }

  // Only sync from settings once on initial load (or when settings first arrive)
  $effect(() => {
    if (!initialized) {
      syncFromSettings(settingsStore.settings);
    }
  });

  $effect(() => {
    loadBackends();
  });

  // Auto-switch provider + model when backend changes
  let prevBackend = $state("");
  $effect(() => {
    const current = selectedBackend;
    if (current && current !== prevBackend) {
      if (prevBackend !== "") {
        // User switched backend — pick first supported provider & reset model
        const backend = backends.find((b) => b.name === current);
        const provs = backend?.supportedProviders ?? [];
        selectedProvider = provs[0] ?? "";
        const available = PROVIDER_MODELS[selectedProvider] ?? [];
        selectedModel = available[0] ?? "";
        customModel = "";
      }
      prevBackend = current;
    }
  });

  // Auto-fetch Ollama models when provider switches to ollama
  $effect(() => {
    if (selectedProvider === "ollama") {
      fetchOllamaModels();
    }
  });

  async function loadBackends() {
    try {
      const client = connectionStore.getClient();
      backends = await client.listBackends();
    } catch {
      // Backend not available — show empty
    } finally {
      loading = false;
    }
  }

  async function fetchOllamaModels() {
    ollamaFetching = true;
    try {
      const client = connectionStore.getClient();
      const models = await client.fetchOllamaModels(ollamaHost);
      ollamaModels = models;
    } catch {
      ollamaModels = [];
    } finally {
      ollamaFetching = false;
    }
  }

  // Reset model when provider changes (after initial sync)
  let prevProvider = $state("");
  $effect(() => {
    const current = selectedProvider;
    if (current && current !== prevProvider) {
      if (prevProvider !== "") {
        const available = PROVIDER_MODELS[current] ?? [];
        selectedModel = available[0] ?? "";
        customModel = "";
      }
      prevProvider = current;
    }
  });

  async function handleInstall(backend: BackendInfo) {
    const hint = backend.installHint;

    // Only pip-installable backends can be auto-installed
    if (!hint.pip_spec) {
      toast.info("This backend requires manual installation");
      return;
    }

    installing = backend.name;
    try {
      const client = connectionStore.getClient();
      await client.installBackend(backend.name);
      toast.success(`Installing ${backend.displayName}...`);
      setTimeout(loadBackends, 3000);
    } catch {
      toast.error(`Failed to install ${backend.displayName}`);
    } finally {
      installing = null;
    }
  }

  function copyToClipboard(text: string) {
    navigator.clipboard.writeText(text);
    toast.success("Copied to clipboard");
  }

  async function handleSave() {
    saving = true;
    try {
      const patch: Partial<Settings> = {
        agent_backend: selectedBackend,
        smart_routing_enabled: smartRouting,
        llm_provider: selectedProvider,
      };

      const fields = BACKEND_FIELDS[selectedBackend];
      if (fields) {
        if (fields.providerField) {
          patch[fields.providerField] = selectedProvider;
        }
        patch[fields.modelField] = effectiveModel;
        patch[fields.maxTurnsField] = maxTurns;
      }

      // Provider-specific fields
      if (selectedProvider === "ollama") {
        patch.ollama_host = ollamaHost;
        patch.ollama_model = effectiveModel;
      }

      if (selectedProvider === "openrouter") {
        patch.openrouter_model = effectiveModel;
      }

      if (selectedProvider === "openai_compatible") {
        patch.openai_compatible_base_url = openaiCompatBaseUrl;
        patch.openai_compatible_model = effectiveModel;
      }

      if (selectedBackend === "opencode") {
        patch.opencode_base_url = opencodeBaseUrl;
      }

      await settingsStore.update(patch);
      toast.success("Settings saved");
    } catch {
      toast.error("Failed to save settings");
    } finally {
      saving = false;
    }
  }

  function handleSaveApiKey() {
    if (!apiKeyInput.trim()) return;
    settingsStore.saveApiKey(selectedProvider, apiKeyInput.trim());
    apiKeyInput = "";
    changingKey = false;
    toast.success("API key saved");
  }
</script>

<div class="flex flex-col gap-6">
  <h3 class="text-sm font-semibold text-foreground">AI Model</h3>

  {#if loading}
    <div class="flex items-center gap-2 py-4 text-sm text-muted-foreground">
      <Loader2 class="h-4 w-4 animate-spin" />
      Loading backends...
    </div>
  {:else}
    <!-- Backend -->
    <div class="flex flex-col gap-1.5">
      <span class="text-xs font-medium text-muted-foreground">Backend</span>
      <Select.Root type="single" bind:value={selectedBackend}>
        <Select.Trigger class="w-full">
          {selectedBackend
            ? (backends.find((b) => b.name === selectedBackend)?.displayName ?? selectedBackend)
            : "Select backend..."}
        </Select.Trigger>
        <Select.Content>
          {#each backends as backend (backend.name)}
            {#if backend.available}
              <Select.Item value={backend.name} label={backend.displayName} />
            {/if}
          {/each}
          {#if backends.filter((b) => b.available).length === 0}
            <div class="px-2 py-1.5 text-xs text-muted-foreground">
              No backends available
            </div>
          {/if}
        </Select.Content>
      </Select.Root>
    </div>

    <!-- Backend capabilities -->
    {#if currentBackend}
      <div class="flex flex-wrap gap-1.5">
        {#each currentBackend.capabilities as cap (cap)}
          <Badge variant="secondary" class="text-[10px]">
            {CAPABILITY_LABELS[cap] ?? cap}
          </Badge>
        {/each}
        {#if currentBackend.beta}
          <Badge variant="outline" class="text-[10px] border-yellow-500/40 text-yellow-500">
            Beta
          </Badge>
        {/if}
      </div>
    {/if}

    <!-- Required keys warning -->
    {#if currentBackend && missingKeys.length > 0 && needsApiKey}
      <Alert.Root variant="destructive">
        <AlertTriangle class="h-4 w-4" />
        <Alert.Title>API key required</Alert.Title>
        <Alert.Description>
          This backend requires: {missingKeys.join(", ")}. Set the key below or via environment variable.
        </Alert.Description>
      </Alert.Root>
    {/if}

    <!-- Unavailable backends — install section -->
    {#if backends.filter((b) => !b.available).length > 0}
      <div class="flex flex-col gap-1.5">
        <span class="text-xs font-medium text-muted-foreground">
          Install Additional Backends
        </span>
        <div class="flex flex-col gap-2">
          {#each backends.filter((b) => !b.available) as backend (backend.name)}
            {@const hint = backend.installHint}
            <div class="flex items-center gap-2 rounded-lg border border-border p-2">
              <div class="flex flex-1 flex-col gap-0.5">
                <div class="flex items-center gap-1.5">
                  <span class="text-xs font-medium text-foreground">
                    {backend.displayName}
                  </span>
                  {#if backend.beta}
                    <Badge variant="outline" class="text-[9px] border-yellow-500/40 text-yellow-500">
                      beta
                    </Badge>
                  {/if}
                </div>
                {#if hint.external_cmd && !hint.pip_spec}
                  <code class="text-[10px] text-muted-foreground font-mono">
                    {hint.external_cmd}
                  </code>
                {/if}
              </div>
              <div class="flex items-center gap-1.5">
                {#if hint.pip_spec}
                  <!-- pip-installable: show Install button -->
                  <button
                    onclick={() => handleInstall(backend)}
                    disabled={installing === backend.name}
                    class="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:border-primary hover:text-foreground disabled:opacity-40"
                  >
                    {#if installing === backend.name}
                      <Loader2 class="h-3 w-3 animate-spin" />
                    {:else}
                      <Download class="h-3 w-3" />
                    {/if}
                    Install
                  </button>
                {:else if hint.external_cmd}
                  <!-- External cmd: show Copy button -->
                  <button
                    onclick={() => copyToClipboard(hint.external_cmd ?? "")}
                    class="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:border-primary hover:text-foreground"
                  >
                    <Copy class="h-3 w-3" />
                    Copy
                  </button>
                {/if}
                {#if hint.docs_url}
                  <a
                    href={hint.docs_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    class="inline-flex items-center gap-1 rounded-lg border border-border px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:border-primary hover:text-foreground"
                  >
                    <ExternalLink class="h-3 w-3" />
                    Docs
                  </a>
                {/if}
              </div>
            </div>
          {/each}
        </div>
      </div>
    {/if}

    <!-- Provider -->
    {#if providers.length > 0}
      <div class="flex flex-col gap-1.5">
        <span class="text-xs font-medium text-muted-foreground">Provider</span>
        <Select.Root type="single" bind:value={selectedProvider}>
          <Select.Trigger class="w-full">
            {selectedProvider
              ? (PROVIDER_LABELS[selectedProvider] ?? selectedProvider)
              : "Select provider..."}
          </Select.Trigger>
          <Select.Content>
            {#each providers as p (p)}
              <Select.Item value={p} label={PROVIDER_LABELS[p] ?? p} />
            {/each}
          </Select.Content>
        </Select.Root>
      </div>
    {/if}

    <!-- Ollama Host + Fetch -->
    {#if selectedProvider === "ollama"}
      <div class="flex flex-col gap-1.5">
        <label for="ollama-host" class="text-xs font-medium text-muted-foreground">
          Ollama Host
        </label>
        <div class="flex gap-2">
          <input
            id="ollama-host"
            bind:value={ollamaHost}
            type="text"
            placeholder="http://localhost:11434"
            class="h-9 flex-1 rounded-lg border border-border bg-muted/50 px-3 text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
          />
          <button
            onclick={fetchOllamaModels}
            disabled={ollamaFetching}
            class="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:border-primary hover:text-foreground disabled:opacity-40"
          >
            <RefreshCw class={ollamaFetching ? "h-3 w-3 animate-spin" : "h-3 w-3"} />
            Fetch Models
          </button>
        </div>
      </div>
    {/if}

    <!-- OpenAI Compatible fields -->
    {#if selectedProvider === "openai_compatible"}
      <div class="flex flex-col gap-1.5">
        <label for="compat-base-url" class="text-xs font-medium text-muted-foreground">
          Base URL
        </label>
        <input
          id="compat-base-url"
          bind:value={openaiCompatBaseUrl}
          type="text"
          placeholder="https://api.example.com/v1"
          class="h-9 w-full rounded-lg border border-border bg-muted/50 px-3 text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
        />
      </div>
    {/if}

    <!-- OpenCode Base URL -->
    {#if selectedBackend === "opencode"}
      <div class="flex flex-col gap-1.5">
        <label for="opencode-url" class="text-xs font-medium text-muted-foreground">
          OpenCode Base URL
        </label>
        <input
          id="opencode-url"
          bind:value={opencodeBaseUrl}
          type="text"
          placeholder="http://localhost:3000"
          class="h-9 w-full rounded-lg border border-border bg-muted/50 px-3 text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
        />
      </div>
    {/if}

    <!-- API Key -->
    {#if needsApiKey}
      <div class="flex flex-col gap-1.5">
        <span class="text-xs font-medium text-muted-foreground">
          {keyConfig.label}
        </span>
        {#if changingKey}
          <div class="flex gap-2">
            <div class="relative flex-1">
              <input
                bind:value={apiKeyInput}
                type={apiKeyMasked ? "password" : "text"}
                placeholder={keyConfig.placeholder}
                class="h-9 w-full rounded-lg border border-border bg-muted/50 px-3 pr-9 text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
              />
              <button
                onclick={() => (apiKeyMasked = !apiKeyMasked)}
                class="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground transition-colors hover:text-foreground"
              >
                {#if apiKeyMasked}
                  <Eye class="h-3.5 w-3.5" />
                {:else}
                  <EyeOff class="h-3.5 w-3.5" />
                {/if}
              </button>
            </div>
            <button
              onclick={handleSaveApiKey}
              disabled={!apiKeyInput.trim()}
              class="rounded-lg bg-primary px-3 py-2 text-xs font-medium text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-40"
            >
              Save
            </button>
            <button
              onclick={() => {
                changingKey = false;
                apiKeyInput = "";
              }}
              class="rounded-lg border border-border px-3 py-2 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
            >
              Cancel
            </button>
          </div>
          <p class="text-[10px] text-muted-foreground">
            Encrypted and stored locally
          </p>
        {:else}
          <div class="flex items-center gap-2">
            <span class="text-sm text-muted-foreground">
              {keyConfig.placeholder.slice(0, 6)}•••••••••
            </span>
            <button
              onclick={() => (changingKey = true)}
              class="text-xs text-primary transition-opacity hover:opacity-80"
            >
              Change
            </button>
          </div>
        {/if}
      </div>
    {/if}

    <!-- Model -->
    {#if selectedProvider || selectedBackend}
      <div class="flex flex-col gap-1.5">
        <div class="flex items-center justify-between">
          <span class="text-xs font-medium text-muted-foreground">Model</span>
          {#if selectedProvider === "ollama"}
            <button
              onclick={fetchOllamaModels}
              disabled={ollamaFetching}
              class="inline-flex items-center gap-1 text-[10px] text-muted-foreground transition-colors hover:text-foreground"
            >
              <RefreshCw class={ollamaFetching ? "h-2.5 w-2.5 animate-spin" : "h-2.5 w-2.5"} />
              Refresh
            </button>
          {/if}
        </div>
        {#if models.length > 0}
          <Select.Root type="single" bind:value={selectedModel}>
            <Select.Trigger class="w-full">
              {selectedModel || "Select model..."}
            </Select.Trigger>
            <Select.Content>
              {#each models as m (m)}
                <Select.Item value={m} label={m} />
              {/each}
            </Select.Content>
          </Select.Root>
        {:else if selectedProvider === "ollama"}
          <p class="text-[10px] text-muted-foreground">
            No models found — pull one with <code class="rounded bg-muted px-1">ollama pull llama3.2</code>
          </p>
        {/if}
        <input
          bind:value={customModel}
          type="text"
          placeholder={models.length > 0
            ? "Or type a custom model name..."
            : "Enter model name..."}
          class="h-9 w-full rounded-lg border border-border bg-muted/50 px-3 text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
        />
        {#if customModel}
          <p class="text-[10px] text-muted-foreground">
            Custom model overrides dropdown selection
          </p>
        {/if}
      </div>
    {/if}

    <!-- Switch to Ollama shortcut -->
    {#if selectedProvider !== "ollama" && providers.includes("ollama")}
      <div class="border-t border-border/50"></div>
      <div class="flex flex-col gap-1.5">
        <p class="text-xs text-muted-foreground">Or use a free local model:</p>
        <button
          onclick={() => { selectedProvider = "ollama"; }}
          class="w-fit rounded-lg border border-border px-4 py-2 text-xs text-muted-foreground transition-colors hover:border-primary hover:text-foreground"
        >
          Switch to Ollama (free, offline)
        </button>
      </div>
    {/if}

    <!-- Advanced -->
    <div class="border-t border-border/50"></div>
    <p class="text-xs font-medium text-muted-foreground">Advanced</p>

    <div class="flex items-center justify-between">
      <div class="flex flex-col">
        <span class="text-sm text-foreground">Smart Routing</span>
        <span class="text-[10px] text-muted-foreground">
          Routes simple queries to cheaper models
        </span>
      </div>
      <Switch bind:checked={smartRouting} />
    </div>

    <div class="flex flex-col gap-1.5">
      <label for="max-turns" class="text-xs font-medium text-muted-foreground">
        Max Turns
      </label>
      <input
        id="max-turns"
        bind:value={maxTurns}
        type="number"
        min={1}
        max={100}
        class="h-9 w-24 rounded-lg border border-border bg-muted/50 px-3 text-sm text-foreground focus:border-primary focus:outline-none"
      />
    </div>

    <!-- Save button -->
    <div class="flex justify-end pt-2">
      <button
        onclick={handleSave}
        disabled={saving}
        class="rounded-lg bg-primary px-6 py-2 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-40"
      >
        {#if saving}
          Saving...
        {:else}
          Save
        {/if}
      </button>
    </div>
  {/if}
</div>

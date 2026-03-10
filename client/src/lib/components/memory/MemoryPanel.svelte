<script lang="ts">
  import type { MemoryEntry, MemorySettings, MemoryStats } from "$lib/api";
  import { goto } from "$app/navigation";
  import { connectionStore, settingsStore } from "$lib/stores";
  import * as Select from "$lib/components/ui/select";
  import { Switch } from "$lib/components/ui/switch";
  import {
    ArrowLeft,
    BrainCircuit,
    Search,
    Trash2,
    Loader2,
    ChevronDown,
    ChevronRight,
    Database,
  } from "@lucide/svelte";
  import { toast } from "svelte-sonner";

  let memories = $state<MemoryEntry[]>([]);
  let stats = $state<MemoryStats | null>(null);
  let loading = $state(true);
  let deletingId = $state<string | null>(null);
  let searchQuery = $state("");
  let showConfig = $state(false);
  let savingConfig = $state(false);

  // Local form state for config
  let memoryBackend = $state("file");
  let autoLearn = $state(false);
  let llmProvider = $state("");
  let llmModel = $state("");
  let embedderProvider = $state("");
  let embedderModel = $state("");
  let vectorStore = $state("");
  let ollamaUrl = $state("http://localhost:11434");

  let filteredMemories = $derived.by(() => {
    if (!searchQuery.trim()) return memories;
    const q = searchQuery.toLowerCase();
    return memories.filter(
      (m) =>
        m.content.toLowerCase().includes(q) ||
        m.tags.some((t) => t.toLowerCase().includes(q)),
    );
  });

  let isMem0 = $derived(memoryBackend === "mem0");

  const LLM_PROVIDERS = ["anthropic", "openai", "ollama"];
  const EMBEDDER_PROVIDERS = ["openai", "ollama", "huggingface"];
  const VECTOR_STORES = ["qdrant", "chroma"];

  $effect(() => {
    loadAll();
  });

  async function loadAll() {
    loading = true;
    try {
      const client = connectionStore.getClient();
      const [memResult, settingsResult, statsResult] = await Promise.allSettled([
        client.getLongTermMemory(200),
        client.getMemorySettings(),
        client.getMemoryStats(),
      ]);
      if (memResult.status === "fulfilled") memories = memResult.value;
      if (settingsResult.status === "fulfilled") {
        syncConfigFromSettings(settingsResult.value);
      }
      if (statsResult.status === "fulfilled") stats = statsResult.value;
    } catch {
      // partial failures handled above
    } finally {
      loading = false;
    }
  }

  function syncConfigFromSettings(s: MemorySettings) {
    memoryBackend = s.memory_backend || "file";
    autoLearn = s.mem0_auto_learn ?? false;
    llmProvider = s.mem0_llm_provider || "";
    llmModel = s.mem0_llm_model || "";
    embedderProvider = s.mem0_embedder_provider || "";
    embedderModel = s.mem0_embedder_model || "";
    vectorStore = s.mem0_vector_store || "";
    ollamaUrl = s.mem0_ollama_base_url || "http://localhost:11434";
  }

  async function deleteMemory(id: string) {
    deletingId = id;
    try {
      const client = connectionStore.getClient();
      await client.deleteMemory(id);
      memories = memories.filter((m) => m.id !== id);
      toast.success("Memory forgotten");
    } catch {
      toast.error("Failed to delete memory");
    } finally {
      deletingId = null;
    }
  }

  async function saveConfig() {
    savingConfig = true;
    try {
      const client = connectionStore.getClient();
      const payload: Partial<MemorySettings> = {
        memory_backend: memoryBackend,
      };
      if (memoryBackend === "mem0") {
        payload.mem0_auto_learn = autoLearn;
        payload.mem0_llm_provider = llmProvider;
        payload.mem0_llm_model = llmModel;
        payload.mem0_embedder_provider = embedderProvider;
        payload.mem0_embedder_model = embedderModel;
        payload.mem0_vector_store = vectorStore;
        if (llmProvider === "ollama" || embedderProvider === "ollama") {
          payload.mem0_ollama_base_url = ollamaUrl;
        }
      }
      await client.saveMemorySettings(payload);
      await settingsStore.update({ memory_backend: memoryBackend });
      toast.success("Memory configuration saved");
    } catch {
      toast.error("Failed to save memory configuration");
    } finally {
      savingConfig = false;
    }
  }

  function formatDate(dateStr: string): string {
    try {
      const d = new Date(dateStr);
      return d.toLocaleDateString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch {
      return dateStr;
    }
  }
</script>

<div class="flex h-full flex-col gap-6 overflow-y-auto px-4 py-4 md:px-6 md:py-6">
  <!-- Header -->
  <div class="flex items-center gap-3">
    <button
      onclick={() => goto("/")}
      class="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
    >
      <ArrowLeft class="h-4 w-4" />
    </button>
    <BrainCircuit class="h-4 w-4 text-muted-foreground" strokeWidth={1.75} />
    <h1 class="text-lg font-semibold text-foreground">Memory</h1>
    {#if stats && typeof stats.total_memories === "number"}
      <span class="ml-auto rounded-full bg-muted px-2 py-0.5 text-[10px] text-muted-foreground">
        {stats.total_memories} memories
      </span>
    {/if}
  </div>

  {#if loading}
    <div class="flex items-center gap-2 py-8 text-sm text-muted-foreground">
      <Loader2 class="h-4 w-4 animate-spin" />
      Loading memories...
    </div>
  {:else}
    <!-- Search -->
    <div class="relative">
      <Search class="absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
      <input
        bind:value={searchQuery}
        type="text"
        placeholder="Search memories..."
        class="h-9 w-full rounded-lg border border-border bg-muted/50 pl-9 pr-3 text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
      />
    </div>

    <!-- Memory List -->
    <div class="flex flex-1 flex-col gap-1.5 overflow-y-auto">
      {#if filteredMemories.length === 0}
        <div class="flex flex-col items-center gap-2 py-12">
          <BrainCircuit class="h-8 w-8 text-muted-foreground/40" />
          <p class="text-sm text-muted-foreground">
            {searchQuery ? "No memories match your search" : "No memories yet"}
          </p>
          {#if !searchQuery}
            <p class="text-xs text-muted-foreground/60">
              Start chatting to build memory, or enable Auto-Learn below.
            </p>
          {/if}
        </div>
      {:else}
        {#each filteredMemories as memory (memory.id)}
          <div class="group flex items-start gap-3 rounded-lg border border-border/40 bg-muted/20 px-3 py-2.5 transition-colors hover:bg-muted/40">
            <div class="flex min-w-0 flex-1 flex-col gap-1">
              <p class="text-sm leading-relaxed text-foreground">{memory.content}</p>
              <div class="flex flex-wrap items-center gap-2">
                <span class="text-[10px] text-muted-foreground">
                  {formatDate(memory.timestamp)}
                </span>
                {#each memory.tags as tag (tag)}
                  <span class="rounded-full bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary">
                    {tag}
                  </span>
                {/each}
              </div>
            </div>
            <button
              onclick={() => deleteMemory(memory.id)}
              disabled={deletingId === memory.id}
              class="shrink-0 rounded-md p-1 text-muted-foreground opacity-0 transition-all hover:bg-destructive/10 hover:text-destructive group-hover:opacity-100"
              title="Forget this memory"
            >
              {#if deletingId === memory.id}
                <Loader2 class="h-3.5 w-3.5 animate-spin" />
              {:else}
                <Trash2 class="h-3.5 w-3.5" />
              {/if}
            </button>
          </div>
        {/each}
      {/if}
    </div>

    <!-- Configuration -->
    <div class="border-t border-border/50 pt-4">
      <button
        onclick={() => (showConfig = !showConfig)}
        class="flex w-full items-center gap-2 text-left"
      >
        {#if showConfig}
          <ChevronDown class="h-3.5 w-3.5 text-muted-foreground" />
        {:else}
          <ChevronRight class="h-3.5 w-3.5 text-muted-foreground" />
        {/if}
        <Database class="h-3.5 w-3.5 text-muted-foreground" strokeWidth={1.75} />
        <span class="text-xs font-medium text-muted-foreground">Configuration</span>
      </button>

      {#if showConfig}
        <div class="mt-4 flex flex-col gap-4">
          <!-- Backend Selector -->
          <div class="flex flex-col gap-1.5">
            <span class="text-xs font-medium text-muted-foreground">Memory Backend</span>
            <Select.Root type="single" bind:value={memoryBackend}>
              <Select.Trigger class="w-full">
                {memoryBackend === "mem0" ? "Mem0 (Semantic)" : "File (Simple)"}
              </Select.Trigger>
              <Select.Content>
                <Select.Item value="file" label="File (Simple)" />
                <Select.Item value="mem0" label="Mem0 (Semantic)" />
              </Select.Content>
            </Select.Root>
            <p class="text-[10px] text-muted-foreground">
              {memoryBackend === "mem0"
                ? "LLM-powered semantic memory with vector search"
                : "Simple markdown file storage, human-readable"}
            </p>
          </div>

          {#if isMem0}
            <!-- Auto-Learn -->
            <div class="flex items-center justify-between">
              <div class="flex flex-col">
                <span class="text-sm text-foreground">Auto-Learn</span>
                <span class="text-[10px] text-muted-foreground">
                  Automatically extract facts from conversations
                </span>
              </div>
              <Switch bind:checked={autoLearn} />
            </div>

            <!-- LLM Provider + Model -->
            <div class="grid grid-cols-2 gap-3">
              <div class="flex flex-col gap-1.5">
                <span class="text-xs font-medium text-muted-foreground">LLM Provider</span>
                <Select.Root type="single" bind:value={llmProvider}>
                  <Select.Trigger class="w-full">
                    {llmProvider || "Select..."}
                  </Select.Trigger>
                  <Select.Content>
                    {#each LLM_PROVIDERS as p (p)}
                      <Select.Item value={p} label={p.charAt(0).toUpperCase() + p.slice(1)} />
                    {/each}
                  </Select.Content>
                </Select.Root>
              </div>
              <div class="flex flex-col gap-1.5">
                <span class="text-xs font-medium text-muted-foreground">LLM Model</span>
                <input
                  bind:value={llmModel}
                  type="text"
                  placeholder="e.g. claude-haiku-4-5-20251001"
                  class="h-9 w-full rounded-lg border border-border bg-muted/50 px-3 text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
                />
              </div>
            </div>

            <!-- Embedder Provider + Model -->
            <div class="grid grid-cols-2 gap-3">
              <div class="flex flex-col gap-1.5">
                <span class="text-xs font-medium text-muted-foreground">Embedder Provider</span>
                <Select.Root type="single" bind:value={embedderProvider}>
                  <Select.Trigger class="w-full">
                    {embedderProvider || "Select..."}
                  </Select.Trigger>
                  <Select.Content>
                    {#each EMBEDDER_PROVIDERS as p (p)}
                      <Select.Item value={p} label={p.charAt(0).toUpperCase() + p.slice(1)} />
                    {/each}
                  </Select.Content>
                </Select.Root>
              </div>
              <div class="flex flex-col gap-1.5">
                <span class="text-xs font-medium text-muted-foreground">Embedder Model</span>
                <input
                  bind:value={embedderModel}
                  type="text"
                  placeholder="e.g. text-embedding-3-small"
                  class="h-9 w-full rounded-lg border border-border bg-muted/50 px-3 text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
                />
              </div>
            </div>

            <!-- Vector Store -->
            <div class="flex flex-col gap-1.5">
              <span class="text-xs font-medium text-muted-foreground">Vector Store</span>
              <Select.Root type="single" bind:value={vectorStore}>
                <Select.Trigger class="w-full">
                  {vectorStore
                    ? vectorStore === "qdrant" ? "Qdrant" : "ChromaDB"
                    : "Select..."}
                </Select.Trigger>
                <Select.Content>
                  {#each VECTOR_STORES as vs (vs)}
                    <Select.Item
                      value={vs}
                      label={vs === "qdrant" ? "Qdrant" : "ChromaDB"}
                    />
                  {/each}
                </Select.Content>
              </Select.Root>
            </div>

            <!-- Ollama URL -->
            {#if llmProvider === "ollama" || embedderProvider === "ollama"}
              <div class="flex flex-col gap-1.5">
                <label for="mem0-ollama-url" class="text-xs font-medium text-muted-foreground">
                  Ollama URL
                </label>
                <input
                  id="mem0-ollama-url"
                  bind:value={ollamaUrl}
                  type="text"
                  placeholder="http://localhost:11434"
                  class="h-9 w-full rounded-lg border border-border bg-muted/50 px-3 text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
                />
              </div>
            {/if}
          {/if}

          <!-- Save Config -->
          <div class="flex justify-end">
            <button
              onclick={saveConfig}
              disabled={savingConfig}
              class="rounded-lg bg-primary px-6 py-2 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-40"
            >
              {#if savingConfig}
                Saving...
              {:else}
                Save Configuration
              {/if}
            </button>
          </div>
        </div>
      {/if}
    </div>
  {/if}
</div>

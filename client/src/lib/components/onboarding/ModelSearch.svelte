<script lang="ts">
  import { Search } from "@lucide/svelte";

  type ModelItem = {
    id: string;
    name: string;
    desc?: string;
    size?: string;
  };

  let {
    models,
    selectedModel = $bindable(""),
    placeholder = "Search models...",
    onSelect,
  }: {
    models: ModelItem[];
    selectedModel?: string;
    placeholder?: string;
    onSelect?: (id: string) => void;
  } = $props();

  let query = $state("");
  let open = $state(false);

  let filtered = $derived(
    query.trim()
      ? models.filter(
          (m) =>
            m.name.toLowerCase().includes(query.toLowerCase()) ||
            m.id.toLowerCase().includes(query.toLowerCase()) ||
            (m.desc?.toLowerCase().includes(query.toLowerCase()) ?? false),
        )
      : models,
  );

  function select(id: string) {
    selectedModel = id;
    query = "";
    open = false;
    onSelect?.(id);
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === "Enter" && query.trim()) {
      e.preventDefault();
      select(query.trim());
    }
    if (e.key === "Escape") {
      open = false;
    }
  }

  function handleBlur() {
    // Delay to allow onmousedown on items to fire first
    setTimeout(() => {
      open = false;
    }, 150);
  }
</script>

<div class="relative w-full">
  <div class="relative">
    <Search class="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
    <input
      type="text"
      bind:value={query}
      {placeholder}
      onfocus={() => (open = true)}
      onblur={handleBlur}
      onkeydown={handleKeydown}
      class="w-full rounded-lg border border-border bg-muted/50 py-2.5 pl-9 pr-3 text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
    />
  </div>

  {#if selectedModel && !open}
    <p class="mt-1.5 text-xs text-muted-foreground">
      Selected: <span class="font-medium text-foreground">{selectedModel}</span>
    </p>
  {/if}

  {#if open}
    <div class="absolute z-10 mt-1 max-h-56 w-full overflow-y-auto rounded-lg border border-border bg-popover shadow-md">
      {#if filtered.length === 0}
        <div class="px-3 py-2 text-xs text-muted-foreground">
          No matches.
          {#if query.trim()}
            Press <kbd class="rounded border border-border px-1 py-0.5 text-[10px]">Enter</kbd> to use "{query.trim()}"
          {/if}
        </div>
      {:else}
        {#each filtered as model}
          <button
            onmousedown={() => select(model.id)}
            class="flex w-full items-center justify-between px-3 py-2 text-left text-sm transition-colors hover:bg-accent {selectedModel === model.id ? 'bg-accent/50' : ''}"
          >
            <div class="flex flex-col">
              <span class="font-medium text-foreground">{model.name}</span>
              {#if model.desc}
                <span class="text-xs text-muted-foreground">{model.desc}</span>
              {/if}
            </div>
            {#if model.size}
              <span class="shrink-0 text-xs text-muted-foreground">{model.size}</span>
            {/if}
          </button>
        {/each}
      {/if}
      {#if query.trim() && filtered.length > 0}
        <div class="border-t border-border px-3 py-1.5 text-[10px] text-muted-foreground">
          Press <kbd class="rounded border border-border px-1 py-0.5">Enter</kbd> to use custom: "{query.trim()}"
        </div>
      {/if}
    </div>
  {/if}
</div>

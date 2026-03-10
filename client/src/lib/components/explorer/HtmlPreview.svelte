<script lang="ts">
  import Globe from "@lucide/svelte/icons/globe";
  import Code from "@lucide/svelte/icons/code";
  import SquareSplitHorizontal from "@lucide/svelte/icons/columns-2";
  import Copy from "@lucide/svelte/icons/copy";
  import Check from "@lucide/svelte/icons/check";
  import RefreshCw from "@lucide/svelte/icons/refresh-cw";
  import { cn } from "$lib/utils";
  import { localFs, joinPath, isAbsolute } from "$lib/filesystem";

  let {
    content = "",
    extension = "html",
    parentDir = "",
  }: {
    content?: string;
    extension?: string;
    parentDir?: string;
  } = $props();

  let mode = $state<"preview" | "source" | "split">("preview");
  let copied = $state(false);
  let iframeRef = $state<HTMLIFrameElement | null>(null);

  async function copyContent() {
    try {
      await navigator.clipboard.writeText(content);
      copied = true;
      setTimeout(() => { copied = false; }, 2000);
    } catch {
      // noop
    }
  }

  function refresh() {
    if (iframeRef) {
      // Force reload by re-assigning srcdoc
      const doc = iframeRef.srcdoc;
      iframeRef.srcdoc = "";
      requestAnimationFrame(() => {
        if (iframeRef) iframeRef.srcdoc = doc;
      });
    }
  }

  let lineCount = $derived(content.split("\n").length);
  let charCount = $derived(content.length);

  // Pre-process HTML to resolve relative image paths
  let processedContent = $state("");

  $effect(() => {
    if (!parentDir || !content) {
      processedContent = content;
      return;
    }

    // Find all relative src attributes in img/video/audio/source tags
    const srcRegex = /(<(?:img|video|audio|source)\s[^>]*?(?:src|poster)\s*=\s*["'])([^"']+)(["'])/gi;
    const relativeSrcs: { full: string; prefix: string; path: string; suffix: string }[] = [];

    let match;
    while ((match = srcRegex.exec(content)) !== null) {
      const srcPath = match[2];
      if (srcPath && !srcPath.startsWith("http") && !srcPath.startsWith("data:") && !isAbsolute(srcPath)) {
        relativeSrcs.push({ full: match[0], prefix: match[1], path: srcPath, suffix: match[3] });
      }
    }

    if (relativeSrcs.length === 0) {
      processedContent = content;
      return;
    }

    // Load all relative images as data URLs
    Promise.all(
      relativeSrcs.map(async (entry) => {
        const absPath = joinPath(parentDir, entry.path);
        try {
          const dataUrl = await localFs.readFileBase64(absPath);
          return { ...entry, dataUrl };
        } catch {
          return { ...entry, dataUrl: "" };
        }
      }),
    ).then((results) => {
      let html = content;
      for (const r of results) {
        if (r.dataUrl) {
          html = html.replace(r.full, `${r.prefix}${r.dataUrl}${r.suffix}`);
        }
      }
      processedContent = html;
    });
  });
</script>

<div class="flex h-full flex-col">
  <!-- Toolbar -->
  <div class="flex items-center justify-between border-b border-border/50 px-3 py-1.5">
    <div class="flex items-center gap-3">
      <div class="flex rounded-md border border-border/50 p-0.5">
        <button
          type="button"
          class={cn(
            "rounded px-2 py-0.5 text-xs transition-colors",
            mode === "preview"
              ? "bg-muted text-foreground"
              : "text-muted-foreground hover:text-foreground",
          )}
          onclick={() => (mode = "preview")}
        >
          <span class="flex items-center gap-1">
            <Globe class="h-3 w-3" />
            Preview
          </span>
        </button>
        <button
          type="button"
          class={cn(
            "rounded px-2 py-0.5 text-xs transition-colors",
            mode === "source"
              ? "bg-muted text-foreground"
              : "text-muted-foreground hover:text-foreground",
          )}
          onclick={() => (mode = "source")}
        >
          <span class="flex items-center gap-1">
            <Code class="h-3 w-3" />
            Source
          </span>
        </button>
        <button
          type="button"
          class={cn(
            "rounded px-2 py-0.5 text-xs transition-colors",
            mode === "split"
              ? "bg-muted text-foreground"
              : "text-muted-foreground hover:text-foreground",
          )}
          onclick={() => (mode = "split")}
        >
          <span class="flex items-center gap-1">
            <SquareSplitHorizontal class="h-3 w-3" />
            Split
          </span>
        </button>
      </div>
      <span class="text-xs text-muted-foreground">
        {lineCount.toLocaleString()} lines &middot; {charCount.toLocaleString()} chars
      </span>
    </div>

    <div class="flex items-center gap-1">
      {#if mode !== "source"}
        <button
          type="button"
          class="rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
          onclick={refresh}
          title="Refresh preview"
        >
          <RefreshCw class="h-3.5 w-3.5" />
        </button>
      {/if}
      <button
        type="button"
        class="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground"
        onclick={copyContent}
      >
        {#if copied}
          <Check class="h-3 w-3" />
          Copied
        {:else}
          <Copy class="h-3 w-3" />
          Copy
        {/if}
      </button>
    </div>
  </div>

  <!-- Content -->
  <div class="flex flex-1 overflow-hidden">
    {#if mode === "source"}
      <div class="flex-1 overflow-hidden">
        {#await import("./CodeEditor.svelte") then mod}
          <mod.default {content} {extension} readonly={true} />
        {/await}
      </div>
    {:else if mode === "preview"}
      <div class="flex-1 overflow-hidden bg-white">
        <iframe
          bind:this={iframeRef}
          srcdoc={processedContent}
          title="HTML Preview"
          class="h-full w-full border-0"
          sandbox="allow-scripts"
        ></iframe>
      </div>
    {:else}
      <!-- Split view -->
      <div class="flex-1 overflow-hidden border-r border-border/50">
        {#await import("./CodeEditor.svelte") then mod}
          <mod.default {content} {extension} readonly={true} />
        {/await}
      </div>
      <div class="flex-1 overflow-hidden bg-white">
        <iframe
          srcdoc={processedContent}
          title="HTML Preview"
          class="h-full w-full border-0"
          sandbox="allow-scripts"
        ></iframe>
      </div>
    {/if}
  </div>
</div>

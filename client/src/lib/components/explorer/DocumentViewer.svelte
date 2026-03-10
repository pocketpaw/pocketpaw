<script lang="ts">
  import DOMPurify from "dompurify";
  import Code from "@lucide/svelte/icons/code";
  import BookOpen from "@lucide/svelte/icons/book-open";
  import Copy from "@lucide/svelte/icons/copy";
  import Check from "@lucide/svelte/icons/check";
  import Loader2 from "@lucide/svelte/icons/loader-2";
  import AlertCircle from "@lucide/svelte/icons/alert-circle";
  import { cn } from "$lib/utils";
  import { base64DataUrlToArrayBuffer } from "$lib/filesystem";

  let {
    base64Url,
    filename = "",
  }: {
    base64Url: string;
    filename?: string;
  } = $props();

  let mode = $state<"preview" | "source">("preview");
  let copied = $state(false);
  let htmlContent = $state<string | null>(null);
  let rawHtml = $state("");
  let error = $state<string | null>(null);
  let loading = $state(true);

  $effect(() => {
    loading = true;
    error = null;
    htmlContent = null;
    rawHtml = "";

    const url = base64Url;

    (async () => {
      try {
        const mammoth = await import("mammoth");
        const arrayBuffer = base64DataUrlToArrayBuffer(url);
        const result = await mammoth.convertToHtml({ arrayBuffer });
        rawHtml = result.value;
        htmlContent = DOMPurify.sanitize(rawHtml, {
          ADD_ATTR: ["target", "rel", "class"],
          ALLOWED_TAGS: [
            "p", "br", "strong", "em", "del", "a", "ul", "ol", "li",
            "h1", "h2", "h3", "h4", "h5", "h6", "blockquote",
            "table", "thead", "tbody", "tr", "th", "td",
            "code", "pre", "hr", "img", "sup", "sub", "span", "div",
          ],
        });
        loading = false;
      } catch (e) {
        error = e instanceof Error ? e.message : String(e);
        loading = false;
      }
    })();
  });

  let wordCount = $derived.by(() => {
    if (!htmlContent) return 0;
    const text = htmlContent.replace(/<[^>]*>/g, "").trim();
    return text.split(/\s+/).filter(Boolean).length;
  });

  async function copyContent() {
    try {
      const text = htmlContent?.replace(/<[^>]*>/g, "") ?? "";
      await navigator.clipboard.writeText(text);
      copied = true;
      setTimeout(() => { copied = false; }, 2000);
    } catch {
      // noop
    }
  }
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
            <BookOpen class="h-3 w-3" />
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
      </div>
      <span class="text-xs text-muted-foreground">
        {wordCount.toLocaleString()} words
      </span>
    </div>
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

  {#if loading}
    <div class="flex flex-1 items-center justify-center">
      <div class="flex flex-col items-center gap-2 text-muted-foreground">
        <Loader2 class="h-8 w-8 animate-spin" />
        <span class="text-sm">Converting document...</span>
      </div>
    </div>
  {:else if error}
    <div class="flex flex-1 items-center justify-center">
      <div class="flex flex-col items-center gap-3 text-muted-foreground">
        <AlertCircle class="h-8 w-8 text-red-400" />
        <span class="text-sm">Failed to convert document</span>
        <p class="max-w-md text-center text-xs text-red-400/80">{error}</p>
      </div>
    </div>
  {:else if mode === "source"}
    <div class="flex-1 overflow-hidden">
      {#await import("./CodeEditor.svelte") then mod}
        <mod.default content={rawHtml} extension="html" readonly={true} />
      {/await}
    </div>
  {:else}
    <div class="flex-1 overflow-y-auto">
      <article class="md-document mx-auto max-w-3xl px-8 py-6">
        {@html htmlContent}
      </article>
    </div>
  {/if}
</div>

<style>
  .md-document {
    font-size: 15px;
    line-height: 1.7;
    color: var(--foreground);
  }
  .md-document :global(h1) {
    font-size: 2em; font-weight: 700; margin: 1.5em 0 0.5em;
    padding-bottom: 0.3em; border-bottom: 1px solid var(--border); line-height: 1.3;
  }
  .md-document :global(h1:first-child) { margin-top: 0; }
  .md-document :global(h2) {
    font-size: 1.5em; font-weight: 650; margin: 1.25em 0 0.4em;
    padding-bottom: 0.25em;
    border-bottom: 1px solid color-mix(in oklab, var(--border) 50%, transparent);
    line-height: 1.35;
  }
  .md-document :global(h3) { font-size: 1.2em; font-weight: 600; margin: 1em 0 0.35em; }
  .md-document :global(h4) { font-size: 1.05em; font-weight: 600; margin: 1em 0 0.3em; }
  .md-document :global(h5),
  .md-document :global(h6) {
    font-size: 0.95em; font-weight: 600; margin: 0.75em 0 0.25em;
    color: var(--muted-foreground);
  }
  .md-document :global(p) { margin-bottom: 0.85em; }
  .md-document :global(p:last-child) { margin-bottom: 0; }
  .md-document :global(ul), .md-document :global(ol) { padding-left: 1.75em; margin-bottom: 0.85em; }
  .md-document :global(li) { margin-bottom: 0.3em; }
  .md-document :global(table) { border-collapse: collapse; width: 100%; margin: 0.85em 0; font-size: 0.9em; }
  .md-document :global(th), .md-document :global(td) {
    border: 1px solid var(--border); padding: 0.55em 0.85em; text-align: left;
  }
  .md-document :global(th) {
    font-weight: 600; background: color-mix(in oklab, var(--foreground) 5%, transparent);
  }
  .md-document :global(a) {
    color: oklch(0.7 0.15 250); text-decoration: underline;
    text-underline-offset: 3px; text-decoration-thickness: 1px;
  }
  .md-document :global(a:hover) { opacity: 0.75; }
  .md-document :global(img) { max-width: 100%; border-radius: 8px; margin: 0.85em 0; }
</style>

<script lang="ts">
  import { marked } from "marked";
  import DOMPurify from "dompurify";
  import Copy from "@lucide/svelte/icons/copy";
  import Check from "@lucide/svelte/icons/check";
  import ChevronDown from "@lucide/svelte/icons/chevron-down";
  import ChevronRight from "@lucide/svelte/icons/chevron-right";
  import AlertCircle from "@lucide/svelte/icons/alert-circle";
  import { cn } from "$lib/utils";
  import hljs from "highlight.js/lib/core";
  import javascript from "highlight.js/lib/languages/javascript";
  import typescript from "highlight.js/lib/languages/typescript";
  import python from "highlight.js/lib/languages/python";
  import rust from "highlight.js/lib/languages/rust";
  import css from "highlight.js/lib/languages/css";
  import json from "highlight.js/lib/languages/json";
  import bash from "highlight.js/lib/languages/bash";
  import yaml from "highlight.js/lib/languages/yaml";
  import sql from "highlight.js/lib/languages/sql";
  import xml from "highlight.js/lib/languages/xml";
  import go from "highlight.js/lib/languages/go";
  import java from "highlight.js/lib/languages/java";

  hljs.registerLanguage("javascript", javascript);
  hljs.registerLanguage("js", javascript);
  hljs.registerLanguage("typescript", typescript);
  hljs.registerLanguage("ts", typescript);
  hljs.registerLanguage("python", python);
  hljs.registerLanguage("py", python);
  hljs.registerLanguage("rust", rust);
  hljs.registerLanguage("rs", rust);
  hljs.registerLanguage("css", css);
  hljs.registerLanguage("json", json);
  hljs.registerLanguage("bash", bash);
  hljs.registerLanguage("sh", bash);
  hljs.registerLanguage("shell", bash);
  hljs.registerLanguage("yaml", yaml);
  hljs.registerLanguage("yml", yaml);
  hljs.registerLanguage("sql", sql);
  hljs.registerLanguage("xml", xml);
  hljs.registerLanguage("html", xml);
  hljs.registerLanguage("go", go);
  hljs.registerLanguage("java", java);

  let {
    content = "",
  }: {
    content?: string;
  } = $props();

  let copied = $state(false);
  let error = $state<string | null>(null);

  interface NotebookCell {
    cell_type: "code" | "markdown" | "raw";
    source: string[];
    execution_count?: number | null;
    outputs?: NotebookOutput[];
  }

  interface NotebookOutput {
    output_type: "stream" | "execute_result" | "display_data" | "error";
    text?: string[];
    data?: Record<string, string | string[]>;
    name?: string;
    ename?: string;
    evalue?: string;
    traceback?: string[];
  }

  interface Notebook {
    cells: NotebookCell[];
    metadata?: {
      kernelspec?: {
        language?: string;
        display_name?: string;
      };
      language_info?: {
        name?: string;
      };
    };
  }

  let notebook = $derived.by((): Notebook | null => {
    try {
      return JSON.parse(content);
    } catch (e) {
      error = "Failed to parse notebook JSON";
      return null;
    }
  });

  let kernelLanguage = $derived(
    notebook?.metadata?.kernelspec?.language ||
    notebook?.metadata?.language_info?.name ||
    "python"
  );

  let kernelDisplayName = $derived(
    notebook?.metadata?.kernelspec?.display_name || kernelLanguage
  );

  let cells = $derived(notebook?.cells ?? []);

  let collapsedOutputs = $state<Set<number>>(new Set());

  function toggleOutput(idx: number) {
    const next = new Set(collapsedOutputs);
    if (next.has(idx)) next.delete(idx);
    else next.add(idx);
    collapsedOutputs = next;
  }

  function joinSource(source: string | string[]): string {
    return Array.isArray(source) ? source.join("") : source;
  }

  function highlightCode(code: string, lang: string): string {
    const language = lang.toLowerCase();
    if (hljs.getLanguage(language)) {
      return hljs.highlight(code, { language }).value;
    }
    return code.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function renderMarkdown(source: string): string {
    const raw = marked.parse(source, { async: false }) as string;
    return DOMPurify.sanitize(raw, {
      ADD_ATTR: ["target", "rel", "id", "class"],
      ALLOWED_TAGS: [
        "p", "br", "strong", "em", "del", "a", "ul", "ol", "li",
        "h1", "h2", "h3", "h4", "h5", "h6", "blockquote",
        "table", "thead", "tbody", "tr", "th", "td",
        "code", "pre", "hr", "img", "sup", "sub", "span", "div",
      ],
    });
  }

  function joinText(text: string | string[] | undefined): string {
    if (!text) return "";
    return Array.isArray(text) ? text.join("") : text;
  }

  async function copyContent() {
    try {
      await navigator.clipboard.writeText(content);
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
      <span class="text-xs text-muted-foreground">
        {cells.length} cell{cells.length !== 1 ? "s" : ""}
      </span>
      <span class="rounded-full bg-primary/10 px-2 py-0.5 text-[10px] font-medium text-primary">
        {kernelDisplayName}
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
        Copy JSON
      {/if}
    </button>
  </div>

  {#if error}
    <div class="flex flex-1 items-center justify-center">
      <div class="flex flex-col items-center gap-3 text-muted-foreground">
        <AlertCircle class="h-8 w-8 text-red-400" />
        <span class="text-sm">{error}</span>
      </div>
    </div>
  {:else}
    <div class="flex-1 overflow-y-auto">
      <div class="mx-auto max-w-4xl space-y-1 py-4 px-4">
        {#each cells as cell, idx}
          <div class="notebook-cell rounded-lg border border-border/40 overflow-hidden">
            {#if cell.cell_type === "markdown"}
              <!-- Markdown cell -->
              <div class="md-document px-5 py-4">
                {@html renderMarkdown(joinSource(cell.source))}
              </div>
            {:else if cell.cell_type === "code"}
              <!-- Code cell -->
              <div class="flex">
                <!-- Execution count gutter -->
                <div class="flex w-14 shrink-0 items-start justify-end border-r border-border/30 bg-muted/20 px-2 pt-3">
                  <span class="text-[10px] font-mono text-muted-foreground/60">
                    [{cell.execution_count ?? " "}]
                  </span>
                </div>
                <!-- Code -->
                <div class="flex-1 overflow-x-auto">
                  <pre class="code-block px-4 py-3 text-[13px] leading-relaxed"><code>{@html highlightCode(joinSource(cell.source), kernelLanguage)}</code></pre>
                </div>
              </div>

              <!-- Outputs -->
              {#if cell.outputs && cell.outputs.length > 0}
                <div class="border-t border-border/30">
                  <button
                    type="button"
                    class="flex w-full items-center gap-1 px-3 py-1 text-[10px] text-muted-foreground hover:bg-muted/30"
                    onclick={() => toggleOutput(idx)}
                  >
                    {#if collapsedOutputs.has(idx)}
                      <ChevronRight class="h-3 w-3" />
                    {:else}
                      <ChevronDown class="h-3 w-3" />
                    {/if}
                    {cell.outputs.length} output{cell.outputs.length !== 1 ? "s" : ""}
                  </button>

                  {#if !collapsedOutputs.has(idx)}
                    <div class="output-area border-t border-border/20 bg-muted/10 px-4 py-2">
                      {#each cell.outputs as output}
                        {#if output.output_type === "stream"}
                          <pre class="output-text text-[12px] leading-relaxed whitespace-pre-wrap">{joinText(output.text)}</pre>
                        {:else if output.output_type === "execute_result" || output.output_type === "display_data"}
                          {#if output.data}
                            {#if output.data["text/html"]}
                              <div class="output-html">
                                {@html DOMPurify.sanitize(joinText(output.data["text/html"]))}
                              </div>
                            {:else if output.data["image/png"]}
                              <img
                                src="data:image/png;base64,{joinText(output.data['image/png'])}"
                                alt="Output"
                                class="max-w-full rounded"
                              />
                            {:else if output.data["text/plain"]}
                              <pre class="output-text text-[12px] leading-relaxed whitespace-pre-wrap">{joinText(output.data["text/plain"])}</pre>
                            {/if}
                          {/if}
                        {:else if output.output_type === "error"}
                          <pre class="error-output text-[12px] leading-relaxed whitespace-pre-wrap text-red-400">{#if output.traceback}{output.traceback.join("\n").replace(/\x1b\[[0-9;]*m/g, "")}{:else}{output.ename}: {output.evalue}{/if}</pre>
                        {/if}
                      {/each}
                    </div>
                  {/if}
                </div>
              {/if}
            {:else}
              <!-- Raw cell -->
              <pre class="px-4 py-3 text-[13px] text-muted-foreground">{joinSource(cell.source)}</pre>
            {/if}
          </div>
        {/each}
      </div>
    </div>
  {/if}
</div>

<style>
  .notebook-cell {
    background: color-mix(in oklab, var(--background) 95%, var(--foreground));
  }

  .code-block {
    font-family: "JetBrains Mono Variable", "JetBrains Mono", monospace;
    background: color-mix(in oklab, var(--foreground) 4%, transparent);
    margin: 0;
  }
  .code-block code { font-size: inherit; }

  .output-text {
    font-family: "JetBrains Mono Variable", "JetBrains Mono", monospace;
    color: var(--foreground);
  }

  .error-output {
    font-family: "JetBrains Mono Variable", "JetBrains Mono", monospace;
  }

  /* Markdown document styles (subset of MarkdownViewer) */
  .md-document { font-size: 15px; line-height: 1.7; color: var(--foreground); }
  .md-document :global(h1) { font-size: 1.8em; font-weight: 700; margin: 1em 0 0.4em; }
  .md-document :global(h2) { font-size: 1.4em; font-weight: 650; margin: 1em 0 0.35em; }
  .md-document :global(h3) { font-size: 1.15em; font-weight: 600; margin: 0.8em 0 0.3em; }
  .md-document :global(p) { margin-bottom: 0.7em; }
  .md-document :global(p:last-child) { margin-bottom: 0; }
  .md-document :global(ul), .md-document :global(ol) { padding-left: 1.75em; margin-bottom: 0.7em; }
  .md-document :global(li) { margin-bottom: 0.2em; }
  .md-document :global(code) {
    font-family: "JetBrains Mono Variable", "JetBrains Mono", monospace;
    font-size: 0.85em; padding: 0.2em 0.45em; border-radius: 5px;
    background: color-mix(in oklab, var(--foreground) 8%, transparent);
  }
  .md-document :global(pre) {
    margin: 0.7em 0; padding: 0.8em 1em; border-radius: 8px;
    background: color-mix(in oklab, var(--foreground) 6%, transparent);
    border: 1px solid var(--border); overflow-x: auto; font-size: 0.88em;
  }
  .md-document :global(pre code) { padding: 0; background: none; border-radius: 0; }
  .md-document :global(a) {
    color: oklch(0.7 0.15 250); text-decoration: underline; text-underline-offset: 3px;
  }
  .md-document :global(blockquote) {
    border-left: 4px solid var(--primary); padding: 0.5em 1em; margin: 0.7em 0;
    background: color-mix(in oklab, var(--primary) 5%, transparent); border-radius: 0 6px 6px 0;
  }
  .md-document :global(table) { border-collapse: collapse; width: 100%; margin: 0.7em 0; font-size: 0.9em; }
  .md-document :global(th), .md-document :global(td) { border: 1px solid var(--border); padding: 0.4em 0.7em; }
  .md-document :global(th) { font-weight: 600; background: color-mix(in oklab, var(--foreground) 5%, transparent); }
  .md-document :global(img) { max-width: 100%; border-radius: 8px; margin: 0.5em 0; }

  /* highlight.js token colors */
  .code-block :global(.hljs-keyword) { color: #c678dd; }
  .code-block :global(.hljs-string) { color: #98c379; }
  .code-block :global(.hljs-number) { color: #d19a66; }
  .code-block :global(.hljs-comment) { color: #5c6370; font-style: italic; }
  .code-block :global(.hljs-function),
  .code-block :global(.hljs-title) { color: #61afef; }
  .code-block :global(.hljs-built_in) { color: #e6c07b; }
  .code-block :global(.hljs-literal) { color: #d19a66; }
  .code-block :global(.hljs-attr) { color: #d19a66; }
  .code-block :global(.hljs-type) { color: #e6c07b; }
  .code-block :global(.hljs-params) { color: #abb2bf; }
  .code-block :global(.hljs-variable) { color: #e06c75; }
  .code-block :global(.hljs-meta) { color: #61afef; }
</style>

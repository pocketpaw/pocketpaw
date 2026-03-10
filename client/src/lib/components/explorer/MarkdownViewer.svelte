<script lang="ts">
  import { marked } from "marked";
  import DOMPurify from "dompurify";
  import Code from "@lucide/svelte/icons/code";
  import BookOpen from "@lucide/svelte/icons/book-open";
  import Copy from "@lucide/svelte/icons/copy";
  import Check from "@lucide/svelte/icons/check";
  import { cn } from "$lib/utils";
  import { localFs, joinPath, isAbsolute, isImageFile } from "$lib/filesystem";
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
    extension = "md",
    parentDir = "",
  }: {
    content?: string;
    extension?: string;
    parentDir?: string;
  } = $props();

  let mode = $state<"preview" | "source">("preview");
  let copied = $state(false);

  // Parse headings for table of contents
  interface TocEntry {
    level: number;
    text: string;
    id: string;
  }

  let toc = $derived.by((): TocEntry[] => {
    const entries: TocEntry[] = [];
    const headingRegex = /^(#{1,6})\s+(.+)$/gm;
    let match;
    while ((match = headingRegex.exec(content)) !== null) {
      const text = match[2].replace(/[*_`~\[\]]/g, "");
      const id = text
        .toLowerCase()
        .replace(/[^\w\s-]/g, "")
        .replace(/\s+/g, "-");
      entries.push({ level: match[1].length, text, id });
    }
    return entries;
  });

  let showToc = $derived(toc.length >= 3);

  // Configure marked with heading IDs
  const renderer = new marked.Renderer();
  renderer.heading = function ({ text, depth }: { text: string; depth: number }) {
    const raw = text.replace(/<[^>]*>/g, "");
    const id = raw
      .toLowerCase()
      .replace(/[^\w\s-]/g, "")
      .replace(/\s+/g, "-");
    return `<h${depth} id="${id}">${text}</h${depth}>`;
  };

  // Make links open in new tab
  renderer.link = function ({ href, title, text }: { href: string; title?: string | null; text: string }) {
    const titleAttr = title ? ` title="${title}"` : "";
    return `<a href="${href}"${titleAttr} target="_blank" rel="noopener noreferrer">${text}</a>`;
  };

  // Images — resolve relative paths against parentDir
  renderer.image = function ({ href, title, text }: { href: string; title?: string | null; text: string }) {
    const titleAttr = title ? ` title="${title}"` : "";
    const altAttr = text ? ` alt="${text}"` : "";
    // If relative and we have a parentDir, mark it for async resolution
    if (parentDir && href && !href.startsWith("http") && !href.startsWith("data:") && !isAbsolute(href)) {
      const absPath = joinPath(parentDir, href);
      return `<img src="" data-resolve-path="${absPath}"${altAttr}${titleAttr} />`;
    }
    return `<img src="${href}"${altAttr}${titleAttr} />`;
  };

  // Task list items
  renderer.listitem = function ({ text, task, checked }: { text: string; task: boolean; checked?: boolean }) {
    if (task) {
      const checkbox = checked
        ? '<input type="checkbox" checked disabled class="task-checkbox" />'
        : '<input type="checkbox" disabled class="task-checkbox" />';
      return `<li class="task-item">${checkbox} ${text}</li>`;
    }
    return `<li>${text}</li>`;
  };

  renderer.code = function ({ text, lang }: { text: string; lang?: string }) {
    if (lang && hljs.getLanguage(lang)) {
      const highlighted = hljs.highlight(text, { language: lang }).value;
      return `<pre><code class="hljs language-${lang}">${highlighted}</code></pre>`;
    }
    return `<pre><code>${text}</code></pre>`;
  };

  marked.setOptions({ renderer, gfm: true, breaks: false });

  let renderedHtml = $derived.by(() => {
    const raw = marked.parse(content, { async: false }) as string;
    return DOMPurify.sanitize(raw, {
      ADD_ATTR: ["target", "rel", "id", "checked", "disabled", "class"],
      ALLOWED_TAGS: [
        "p", "br", "strong", "em", "del", "a", "ul", "ol", "li",
        "h1", "h2", "h3", "h4", "h5", "h6", "blockquote",
        "table", "thead", "tbody", "tr", "th", "td",
        "code", "pre", "hr", "img", "sup", "sub", "span", "div",
        "input", "details", "summary",
      ],
    });
  });

  let wordCount = $derived.by(() => {
    const words = content.trim().split(/\s+/).filter(Boolean);
    return words.length;
  });

  let readingTime = $derived(Math.max(1, Math.ceil(wordCount / 200)));

  async function copyContent() {
    try {
      await navigator.clipboard.writeText(content);
      copied = true;
      setTimeout(() => { copied = false; }, 2000);
    } catch {
      // noop
    }
  }

  function scrollToHeading(id: string) {
    const el = document.getElementById(id);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // Resolve relative images after render
  let articleRef = $state<HTMLElement | null>(null);

  $effect(() => {
    // Re-run when rendered HTML changes
    void renderedHtml;
    if (!articleRef || !parentDir) return;

    // Wait for DOM to update
    requestAnimationFrame(() => {
      if (!articleRef) return;
      const imgs = articleRef.querySelectorAll<HTMLImageElement>("img[data-resolve-path]");
      for (const img of imgs) {
        const absPath = img.getAttribute("data-resolve-path");
        if (!absPath) continue;
        img.removeAttribute("data-resolve-path");
        localFs.readFileBase64(absPath).then(
          (dataUrl) => { if (dataUrl) img.src = dataUrl; },
          () => { /* ignore load errors */ },
        );
      }
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
        {wordCount.toLocaleString()} words &middot; {readingTime} min read
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

  {#if mode === "source"}
    <div class="flex-1 overflow-hidden">
      {#await import("./CodeEditor.svelte") then mod}
        <mod.default {content} {extension} readonly={true} />
      {/await}
    </div>
  {:else}
    <!-- Rendered markdown -->
    <div class="flex flex-1 overflow-hidden">
      <!-- TOC sidebar -->
      {#if showToc}
        <nav class="hidden w-56 shrink-0 overflow-y-auto border-r border-border/30 p-3 lg:block">
          <p class="mb-2 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
            On this page
          </p>
          <ul class="space-y-0.5">
            {#each toc as entry}
              <li style:padding-left="{(entry.level - 1) * 12}px">
                <button
                  type="button"
                  class="w-full truncate text-left text-xs text-muted-foreground transition-colors hover:text-foreground"
                  onclick={() => scrollToHeading(entry.id)}
                  title={entry.text}
                >
                  {entry.text}
                </button>
              </li>
            {/each}
          </ul>
        </nav>
      {/if}

      <!-- Document body -->
      <div class="flex-1 overflow-y-auto">
        <article bind:this={articleRef} class="md-document mx-auto max-w-3xl px-8 py-6">
          {@html renderedHtml}
        </article>
      </div>
    </div>
  {/if}
</div>

<style>
  /* Document typography — larger, more spacious than chat markdown */
  .md-document {
    font-size: 15px;
    line-height: 1.7;
    color: var(--foreground);
  }

  .md-document :global(h1) {
    font-size: 2em;
    font-weight: 700;
    margin: 1.5em 0 0.5em;
    padding-bottom: 0.3em;
    border-bottom: 1px solid var(--border);
    line-height: 1.3;
  }
  .md-document :global(h1:first-child) {
    margin-top: 0;
  }
  .md-document :global(h2) {
    font-size: 1.5em;
    font-weight: 650;
    margin: 1.25em 0 0.4em;
    padding-bottom: 0.25em;
    border-bottom: 1px solid color-mix(in oklab, var(--border) 50%, transparent);
    line-height: 1.35;
  }
  .md-document :global(h3) {
    font-size: 1.2em;
    font-weight: 600;
    margin: 1em 0 0.35em;
  }
  .md-document :global(h4) {
    font-size: 1.05em;
    font-weight: 600;
    margin: 1em 0 0.3em;
  }
  .md-document :global(h5),
  .md-document :global(h6) {
    font-size: 0.95em;
    font-weight: 600;
    margin: 0.75em 0 0.25em;
    color: var(--muted-foreground);
  }

  .md-document :global(p) {
    margin-bottom: 0.85em;
  }
  .md-document :global(p:last-child) {
    margin-bottom: 0;
  }

  .md-document :global(ul),
  .md-document :global(ol) {
    padding-left: 1.75em;
    margin-bottom: 0.85em;
  }
  .md-document :global(li) {
    margin-bottom: 0.3em;
  }
  .md-document :global(li > ul),
  .md-document :global(li > ol) {
    margin-top: 0.3em;
    margin-bottom: 0;
  }

  .md-document :global(.task-item) {
    list-style: none;
    margin-left: -1.5em;
  }
  .md-document :global(.task-checkbox) {
    margin-right: 0.5em;
    vertical-align: middle;
    accent-color: var(--primary);
  }

  .md-document :global(code) {
    font-family: "JetBrains Mono Variable", "JetBrains Mono", monospace;
    font-size: 0.85em;
    padding: 0.2em 0.45em;
    border-radius: 5px;
    background: color-mix(in oklab, var(--foreground) 8%, transparent);
  }
  .md-document :global(pre) {
    margin: 0.85em 0;
    padding: 1em 1.25em;
    border-radius: 8px;
    background: color-mix(in oklab, var(--foreground) 6%, transparent);
    border: 1px solid var(--border);
    overflow-x: auto;
    font-size: 0.88em;
    line-height: 1.6;
  }
  .md-document :global(pre code) {
    padding: 0;
    background: none;
    border-radius: 0;
    font-size: inherit;
  }

  .md-document :global(blockquote) {
    border-left: 4px solid var(--primary);
    padding: 0.5em 1em;
    margin: 0.85em 0;
    background: color-mix(in oklab, var(--primary) 5%, transparent);
    border-radius: 0 6px 6px 0;
    color: var(--muted-foreground);
  }
  .md-document :global(blockquote p:last-child) {
    margin-bottom: 0;
  }

  .md-document :global(table) {
    border-collapse: collapse;
    width: 100%;
    margin: 0.85em 0;
    font-size: 0.9em;
  }
  .md-document :global(th),
  .md-document :global(td) {
    border: 1px solid var(--border);
    padding: 0.55em 0.85em;
    text-align: left;
  }
  .md-document :global(th) {
    font-weight: 600;
    background: color-mix(in oklab, var(--foreground) 5%, transparent);
  }
  .md-document :global(tr:nth-child(even) td) {
    background: color-mix(in oklab, var(--foreground) 2%, transparent);
  }

  .md-document :global(a) {
    color: oklch(0.7 0.15 250);
    text-decoration: underline;
    text-underline-offset: 3px;
    text-decoration-thickness: 1px;
    transition: opacity 0.15s;
  }
  .md-document :global(a:hover) {
    opacity: 0.75;
  }

  .md-document :global(hr) {
    border: none;
    border-top: 1px solid var(--border);
    margin: 1.5em 0;
  }

  .md-document :global(img) {
    max-width: 100%;
    border-radius: 8px;
    margin: 0.85em 0;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.15);
  }

  .md-document :global(details) {
    margin: 0.85em 0;
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0.5em 1em;
  }
  .md-document :global(summary) {
    cursor: pointer;
    font-weight: 600;
  }

  /* highlight.js One Dark token colors */
  .md-document :global(.hljs-keyword) { color: #c678dd; }
  .md-document :global(.hljs-string) { color: #98c379; }
  .md-document :global(.hljs-number) { color: #d19a66; }
  .md-document :global(.hljs-comment) { color: #5c6370; font-style: italic; }
  .md-document :global(.hljs-function),
  .md-document :global(.hljs-title) { color: #61afef; }
  .md-document :global(.hljs-built_in) { color: #e6c07b; }
  .md-document :global(.hljs-literal) { color: #d19a66; }
  .md-document :global(.hljs-attr) { color: #d19a66; }
  .md-document :global(.hljs-type) { color: #e6c07b; }
  .md-document :global(.hljs-params) { color: #abb2bf; }
  .md-document :global(.hljs-variable) { color: #e06c75; }
  .md-document :global(.hljs-meta) { color: #61afef; }
</style>

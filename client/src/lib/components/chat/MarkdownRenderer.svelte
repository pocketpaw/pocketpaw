<script lang="ts">
  import { marked } from "marked";
  import DOMPurify from "dompurify";
  import CodeBlock from "./CodeBlock.svelte";

  let { content }: { content: string } = $props();

  // Extract code blocks before markdown parsing to render them as Svelte components
  type Segment = { type: "html"; html: string } | { type: "code"; code: string; lang: string };

  let segments = $derived.by((): Segment[] => {
    if (!content) return [];

    const result: Segment[] = [];
    const codeBlockRegex = /```(\w*)\n([\s\S]*?)```/g;
    let lastIndex = 0;
    let match;

    while ((match = codeBlockRegex.exec(content)) !== null) {
      // Text before this code block
      if (match.index > lastIndex) {
        const textBefore = content.slice(lastIndex, match.index);
        const html = renderMarkdown(textBefore);
        if (html.trim()) {
          result.push({ type: "html", html });
        }
      }

      result.push({
        type: "code",
        lang: match[1] || "",
        code: match[2].trimEnd(),
      });

      lastIndex = match.index + match[0].length;
    }

    // Remaining text after last code block
    if (lastIndex < content.length) {
      const remaining = content.slice(lastIndex);
      const html = renderMarkdown(remaining);
      if (html.trim()) {
        result.push({ type: "html", html });
      }
    }

    return result;
  });

  function renderMarkdown(text: string): string {
    const raw = marked.parse(text, { async: false }) as string;
    const sanitized = DOMPurify.sanitize(raw, {
      ADD_ATTR: ["target", "rel"],
      ALLOWED_TAGS: [
        "p", "br", "strong", "em", "del", "a", "ul", "ol", "li",
        "h1", "h2", "h3", "h4", "h5", "h6", "blockquote",
        "table", "thead", "tbody", "tr", "th", "td",
        "code", "pre", "hr", "img", "sup", "sub", "span", "div",
      ],
    });
    // Wrap tables in scrollable container for mobile
    return sanitized.replace(/<table/g, '<div class="table-wrapper"><table').replace(/<\/table>/g, '</table></div>');
  }
</script>

<div class="markdown-content">
  {#each segments as segment}
    {#if segment.type === "code"}
      <CodeBlock code={segment.code} language={segment.lang} />
    {:else}
      {@html segment.html}
    {/if}
  {/each}
</div>

<style>
  .markdown-content :global(p) {
    margin-bottom: 0.5em;
  }
  .markdown-content :global(p:last-child) {
    margin-bottom: 0;
  }
  .markdown-content :global(ul),
  .markdown-content :global(ol) {
    padding-left: 1.5em;
    margin-bottom: 0.5em;
  }
  .markdown-content :global(li) {
    margin-bottom: 0.25em;
  }
  .markdown-content :global(code) {
    font-family: var(--font-mono);
    font-size: 0.85em;
    padding: 0.15em 0.4em;
    border-radius: 4px;
    background: color-mix(in oklab, var(--foreground) 8%, transparent);
  }
  .markdown-content :global(pre code) {
    padding: 0;
    background: none;
  }
  .markdown-content :global(blockquote) {
    border-left: 3px solid var(--border);
    padding-left: 0.75em;
    margin: 0.5em 0;
    color: var(--muted-foreground);
  }
  .markdown-content :global(.table-wrapper) {
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
  }
  .markdown-content :global(table) {
    border-collapse: collapse;
    width: 100%;
    margin: 0.5em 0;
    font-size: 0.9em;
  }
  .markdown-content :global(th),
  .markdown-content :global(td) {
    border: 1px solid var(--border);
    padding: 0.4em 0.75em;
    text-align: left;
  }
  .markdown-content :global(th) {
    font-weight: 600;
    background: color-mix(in oklab, var(--foreground) 5%, transparent);
  }
  .markdown-content :global(a) {
    color: var(--paw-accent);
    text-decoration: underline;
    text-underline-offset: 2px;
  }
  .markdown-content :global(a:hover) {
    opacity: 0.8;
  }
  .markdown-content :global(h1),
  .markdown-content :global(h2),
  .markdown-content :global(h3) {
    font-weight: 600;
    margin: 0.75em 0 0.25em;
  }
  .markdown-content :global(h1) { font-size: 1.25em; }
  .markdown-content :global(h2) { font-size: 1.1em; }
  .markdown-content :global(h3) { font-size: 1em; }
  .markdown-content :global(hr) {
    border: none;
    border-top: 1px solid var(--border);
    margin: 0.75em 0;
  }
  .markdown-content :global(img) {
    max-width: 100%;
    border-radius: 8px;
    margin: 0.5em 0;
  }
</style>

<script lang="ts">
  import { onMount } from "svelte";
  import { EditorView, lineNumbers, highlightActiveLineGutter, highlightSpecialChars, drawSelection, highlightActiveLine, keymap } from "@codemirror/view";
  import { EditorState } from "@codemirror/state";
  import { defaultHighlightStyle, syntaxHighlighting, indentOnInput, bracketMatching, foldGutter, foldKeymap } from "@codemirror/language";
  import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
  import { highlightSelectionMatches, searchKeymap } from "@codemirror/search";
  import { closeBrackets, closeBracketsKeymap } from "@codemirror/autocomplete";
  import { oneDark } from "@codemirror/theme-one-dark";

  let {
    content = "",
    extension = "",
    readonly = true,
    resetContent = 0,
    onsave,
    ondirtychange,
  }: {
    content?: string;
    extension?: string;
    readonly?: boolean;
    resetContent?: number;
    onsave?: (content: string) => void;
    ondirtychange?: (dirty: boolean) => void;
  } = $props();

  let editorContainer = $state<HTMLDivElement | null>(null);
  let view: EditorView | null = null;
  let lastResetContent = 0;
  let isDirtyRef = { current: false };

  // Watch resetContent counter — replace document when incremented
  $effect(() => {
    if (resetContent !== lastResetContent) {
      lastResetContent = resetContent;
      if (view) {
        view.dispatch({
          changes: {
            from: 0,
            to: view.state.doc.length,
            insert: content,
          },
        });
        isDirtyRef.current = false;
        ondirtychange?.(false);
      }
    }
  });

  async function getLanguageExtension(ext: string) {
    const e = ext.toLowerCase();
    try {
      switch (e) {
        case "js":
        case "mjs":
        case "cjs": {
          const { javascript } = await import("@codemirror/lang-javascript");
          return javascript();
        }
        case "ts":
        case "mts":
        case "cts": {
          const { javascript } = await import("@codemirror/lang-javascript");
          return javascript({ typescript: true });
        }
        case "jsx": {
          const { javascript } = await import("@codemirror/lang-javascript");
          return javascript({ jsx: true });
        }
        case "tsx": {
          const { javascript } = await import("@codemirror/lang-javascript");
          return javascript({ typescript: true, jsx: true });
        }
        case "py":
        case "pyw":
        case "pyi": {
          const { python } = await import("@codemirror/lang-python");
          return python();
        }
        case "json":
        case "jsonc":
        case "json5": {
          const { json } = await import("@codemirror/lang-json");
          return json();
        }
        case "html":
        case "htm":
        case "svelte":
        case "vue": {
          const { html } = await import("@codemirror/lang-html");
          return html();
        }
        case "css":
        case "scss":
        case "less": {
          const { css } = await import("@codemirror/lang-css");
          return css();
        }
        case "md":
        case "mdx":
        case "markdown": {
          const { markdown } = await import("@codemirror/lang-markdown");
          return markdown();
        }
        case "rs": {
          const { rust } = await import("@codemirror/lang-rust");
          return rust();
        }
        case "c":
        case "h":
        case "cpp":
        case "cxx":
        case "cc":
        case "hpp":
        case "hxx": {
          const { cpp } = await import("@codemirror/lang-cpp");
          return cpp();
        }
        case "java":
        case "kt":
        case "kts": {
          const { java } = await import("@codemirror/lang-java");
          return java();
        }
        case "go": {
          const { go } = await import("@codemirror/lang-go");
          return go();
        }
        case "php": {
          const { php } = await import("@codemirror/lang-php");
          return php();
        }
        case "sql": {
          const { sql } = await import("@codemirror/lang-sql");
          return sql();
        }
        case "xml":
        case "xsl":
        case "xslt":
        case "xsd":
        case "wsdl": {
          const { xml } = await import("@codemirror/lang-xml");
          return xml();
        }
        case "yaml":
        case "yml": {
          const { yaml } = await import("@codemirror/lang-yaml");
          return yaml();
        }
        default:
          return null;
      }
    } catch {
      return null;
    }
  }

  onMount(() => {
    if (!editorContainer) return;

    let destroyed = false;

    (async () => {
      const langExt = await getLanguageExtension(extension);
      if (destroyed || !editorContainer) return;

      const saveKeyBinding = {
        key: "Mod-s",
        run: (editorView: EditorView) => {
          if (onsave) {
            onsave(editorView.state.doc.toString());
            isDirtyRef.current = false;
            ondirtychange?.(false);
          }
          return true;
        },
      };

      const extensions = [
        lineNumbers(),
        highlightActiveLineGutter(),
        highlightSpecialChars(),
        foldGutter(),
        drawSelection(),
        indentOnInput(),
        bracketMatching(),
        closeBrackets(),
        highlightActiveLine(),
        highlightSelectionMatches(),
        syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
        oneDark,
        keymap.of([
          saveKeyBinding,
          ...closeBracketsKeymap,
          ...defaultKeymap,
          ...searchKeymap,
          ...historyKeymap,
          ...foldKeymap,
        ]),
        history(),
        EditorView.lineWrapping,
      ];

      if (!readonly) {
        extensions.push(
          EditorView.updateListener.of((update) => {
            if (update.docChanged && !isDirtyRef.current) {
              isDirtyRef.current = true;
              ondirtychange?.(true);
            }
          }),
        );
      }

      if (langExt) extensions.push(langExt);
      if (readonly) extensions.push(EditorState.readOnly.of(true));

      view = new EditorView({
        state: EditorState.create({
          doc: content,
          extensions,
        }),
        parent: editorContainer,
      });
    })();

    return () => {
      destroyed = true;
      view?.destroy();
      view = null;
    };
  });
</script>

<div
  bind:this={editorContainer}
  class="code-editor h-full w-full overflow-auto"
></div>

<style>
  .code-editor :global(.cm-editor) {
    height: 100%;
    font-family: "JetBrains Mono Variable", "JetBrains Mono", monospace;
    font-size: 13px;
  }

  .code-editor :global(.cm-scroller) {
    overflow: auto;
  }

  .code-editor :global(.cm-gutters) {
    border-right: 1px solid rgba(255, 255, 255, 0.06);
  }
</style>

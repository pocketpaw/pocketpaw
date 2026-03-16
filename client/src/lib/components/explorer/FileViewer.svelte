<script lang="ts">
  import type { FileEntry } from "$lib/filesystem";
  import { localFs, parentDir } from "$lib/filesystem";
  import { explorerStore } from "$lib/stores";
  import ChevronLeft from "@lucide/svelte/icons/chevron-left";
  import ChevronRight from "@lucide/svelte/icons/chevron-right";
  import X from "@lucide/svelte/icons/x";
  import ExternalLink from "@lucide/svelte/icons/external-link";
  import Loader2 from "@lucide/svelte/icons/loader-2";
  import AlertCircle from "@lucide/svelte/icons/alert-circle";
  import FileQuestion from "@lucide/svelte/icons/file-question";
  import Save from "@lucide/svelte/icons/save";
  import { cn } from "$lib/utils";

  let {
    file,
  }: {
    file: FileEntry;
  } = $props();

  let textContent = $state<string | null>(null);
  let base64Url = $state<string | null>(null);
  let mediaSrc = $state<string | null>(null);
  let loading = $state(true);
  let error = $state<string | null>(null);
  let unsavedChanges = $state(false);
  let diskModifiedBanner = $state(false);
  let editorReloadKey = $state(0);
  let justSaved = $state(false);
  let justSavedTimer: ReturnType<typeof setTimeout> | null = null;

  const IMAGE_EXTS = new Set([
    "png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "ico", "tiff", "tif",
  ]);
  const AUDIO_EXTS = new Set([
    "mp3", "wav", "ogg", "flac", "aac", "m4a", "weba", "opus",
  ]);
  const VIDEO_EXTS = new Set([
    "mp4", "webm", "mov", "avi", "mkv", "ogv", "m4v",
  ]);
  const MARKDOWN_EXTS = new Set(["md", "mdx", "markdown"]);
  const SPREADSHEET_EXTS = new Set(["csv", "tsv"]);
  const HTML_EXTS = new Set(["html", "htm"]);
  const PDF_EXTS = new Set(["pdf"]);
  const NOTEBOOK_EXTS = new Set(["ipynb"]);
  const MODEL_EXTS = new Set(["stl", "obj", "gltf", "glb"]);
  const DOCUMENT_EXTS = new Set(["docx"]);
  const XLSX_EXTS = new Set(["xlsx", "xls", "ods"]);
  const CODE_EXTS = new Set([
    "js", "mjs", "cjs", "ts", "mts", "cts", "jsx", "tsx",
    "py", "pyw", "pyi", "rs", "go", "c", "h", "cpp", "cxx", "cc", "hpp", "hxx",
    "java", "kt", "kts", "swift", "rb", "php", "cs", "dart",
    "svelte", "vue", "css", "scss", "less",
    "json", "jsonc", "json5", "yaml", "yml", "toml", "xml", "xsl",
    "sql", "sh", "bash", "zsh", "ps1", "bat", "cmd",
    "txt", "log",
    "env", "ini", "cfg", "conf", "gitignore", "dockerfile",
    "makefile", "cmake", "gradle", "sbt",
    "r", "lua", "perl", "pl", "ex", "exs", "erl", "hs",
    "lock", "editorconfig", "prettierrc",
  ]);

  type FileCategory =
    | "image"
    | "audio"
    | "video"
    | "markdown"
    | "spreadsheet"
    | "htmlpreview"
    | "code"
    | "pdf"
    | "notebook"
    | "model3d"
    | "document"
    | "xlsx"
    | "unknown";

  let ext = $derived(file.extension.toLowerCase());
  let fileParentDir = $derived(parentDir(file.path));
  let fileCategory = $derived.by((): FileCategory => {
    if (IMAGE_EXTS.has(ext)) return "image";
    if (AUDIO_EXTS.has(ext)) return "audio";
    if (VIDEO_EXTS.has(ext)) return "video";
    if (MARKDOWN_EXTS.has(ext)) return "markdown";
    if (SPREADSHEET_EXTS.has(ext)) return "spreadsheet";
    if (HTML_EXTS.has(ext)) return "htmlpreview";
    if (PDF_EXTS.has(ext)) return "pdf";
    if (NOTEBOOK_EXTS.has(ext)) return "notebook";
    if (DOCUMENT_EXTS.has(ext)) return "document";
    if (XLSX_EXTS.has(ext)) return "xlsx";
    if (MODEL_EXTS.has(ext)) return "model3d";
    if (CODE_EXTS.has(ext)) return "code";
    // Files without extension — try as text
    if (!ext && !file.isDir) return "code";
    return "unknown";
  });

  // Load file content based on category
  $effect(() => {
    loading = true;
    error = null;
    textContent = null;
    base64Url = null;
    mediaSrc = null;
    diskModifiedBanner = false;
    editorReloadKey = 0;

    const cat = fileCategory;
    const path = file.path;

    if (cat === "image") {
      localFs.readFileBase64(path).then(
        (url) => { base64Url = url; loading = false; },
        (e) => { error = String(e); loading = false; },
      );
    } else if (cat === "audio" || cat === "video") {
      localFs.getFileSrc(path).then(
        (url) => { mediaSrc = url; loading = false; },
        (e) => { error = String(e); loading = false; },
      );
    } else if (cat === "pdf") {
      localFs.readFileBase64(path).then(
        (url) => { base64Url = url; loading = false; },
        (e) => { error = String(e); loading = false; },
      );
    } else if (cat === "notebook") {
      localFs.readFileText(path).then(
        (text) => { textContent = text; loading = false; },
        (e) => { error = String(e); loading = false; },
      );
    } else if (cat === "model3d") {
      localFs.getFileSrc(path).then(
        (url) => { mediaSrc = url; loading = false; },
        (e) => { error = String(e); loading = false; },
      );
    } else if (cat === "document" || cat === "xlsx") {
      localFs.readFileBase64(path).then(
        (url) => { base64Url = url; loading = false; },
        (e) => { error = String(e); loading = false; },
      );
    } else if (
      cat === "code" ||
      cat === "markdown" ||
      cat === "spreadsheet" ||
      cat === "htmlpreview"
    ) {
      localFs.readFileText(path).then(
        (text) => { textContent = text; loading = false; },
        (e) => { error = String(e); loading = false; },
      );
    } else {
      loading = false;
    }
  });

  // Track disk changes to the open file
  let lastDiskCounter = $state(explorerStore.openFileChangedOnDisk);

  $effect(() => {
    const counter = explorerStore.openFileChangedOnDisk;
    if (counter !== lastDiskCounter) {
      lastDiskCounter = counter;

      // Suppress reload if we just saved (our own write triggered the OS event)
      if (justSaved) return;

      const cat = fileCategory;
      const path = file.path;

      if (cat === "code") {
        if (unsavedChanges) {
          // Show banner — don't clobber user edits
          diskModifiedBanner = true;
        } else {
          // Silently reload
          localFs.readFileText(path).then((text) => {
            textContent = text;
            editorReloadKey++;
          });
        }
      } else if (cat === "image") {
        localFs.readFileBase64(path).then((url) => { base64Url = url; });
      } else if (cat === "notebook") {
        localFs.readFileText(path).then((text) => { textContent = text; });
      } else if (cat === "document" || cat === "xlsx") {
        localFs.readFileBase64(path).then((url) => { base64Url = url; });
      } else if (cat === "markdown" || cat === "spreadsheet" || cat === "htmlpreview") {
        localFs.readFileText(path).then((text) => { textContent = text; });
      } else if (cat === "pdf") {
        localFs.readFileBase64(path).then((url) => { base64Url = url; });
      }
      // audio/video use asset URLs — the OS updates the file on disk, and the
      // webview will pick up the change on next seek/reload. No explicit action needed.
    }
  });

  function loadDiskVersion() {
    diskModifiedBanner = false;
    localFs.readFileText(file.path).then((text) => {
      textContent = text;
      editorReloadKey++;
      unsavedChanges = false;
    });
  }

  function dismissBanner() {
    diskModifiedBanner = false;
  }

  function close() {
    explorerStore.closeDetail();
  }

  async function openInSystem() {
    try {
      const { openPath } = await import("@tauri-apps/plugin-opener");
      await openPath(file.path);
    } catch {
      // Not in Tauri
    }
  }

  async function handleSave(content: string) {
    try {
      // Set justSaved guard to suppress our own modify event
      justSaved = true;
      if (justSavedTimer) clearTimeout(justSavedTimer);
      justSavedTimer = setTimeout(() => { justSaved = false; }, 1000);

      await localFs.writeFile(file.path, content);
      unsavedChanges = false;
    } catch (e) {
      console.error("Save failed:", e);
    }
  }

  // Prev / next file navigation
  let siblingFiles = $derived(explorerStore.sortedFiles.filter((f) => !f.isDir));
  let currentIndex = $derived(siblingFiles.findIndex((f) => f.path === file.path));
  let canGoPrev = $derived(currentIndex > 0);
  let canGoNext = $derived(currentIndex >= 0 && currentIndex < siblingFiles.length - 1);

  function goPrev() {
    if (canGoPrev) explorerStore.openFileDetail(siblingFiles[currentIndex - 1]);
  }

  function goNext() {
    if (canGoNext) explorerStore.openFileDetail(siblingFiles[currentIndex + 1]);
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === "Escape") close();
    if (e.key === "ArrowLeft" && !e.ctrlKey && !e.metaKey) goPrev();
    if (e.key === "ArrowRight" && !e.ctrlKey && !e.metaKey) goNext();
  }

  function formatSize(bytes: number): string {
    if (bytes === 0) return "0 B";
    const units = ["B", "KB", "MB", "GB"];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    return `${(bytes / Math.pow(1024, i)).toFixed(i > 0 ? 1 : 0)} ${units[i]}`;
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<div class="flex h-full flex-col">
  <!-- Header -->
  <div class="flex h-9 items-center gap-1 px-2">
    <!-- Prev / Next -->
    <button
      type="button"
      class={cn(
        "rounded-md p-1 transition-colors",
        canGoPrev
          ? "text-muted-foreground hover:bg-muted hover:text-foreground"
          : "text-muted-foreground/30 cursor-not-allowed",
      )}
      disabled={!canGoPrev}
      onclick={goPrev}
      title="Previous file (←)"
    >
      <ChevronLeft class="h-4 w-4" />
    </button>
    <button
      type="button"
      class={cn(
        "rounded-md p-1 transition-colors",
        canGoNext
          ? "text-muted-foreground hover:bg-muted hover:text-foreground"
          : "text-muted-foreground/30 cursor-not-allowed",
      )}
      disabled={!canGoNext}
      onclick={goNext}
      title="Next file (→)"
    >
      <ChevronRight class="h-4 w-4" />
    </button>

    <div class="mx-1 h-4 w-px bg-border/50"></div>

    <!-- File info -->
    <div class="flex min-w-0 flex-1 items-center gap-2">
      <span class="truncate text-xs text-foreground" title={file.path}>
        {file.name}
      </span>
      {#if unsavedChanges}
        <span class="text-orange-400 text-xs" title="Unsaved changes">●</span>
      {/if}
      <span class="shrink-0 text-[10px] text-muted-foreground/60">
        {formatSize(file.size)}
      </span>
    </div>

    <!-- Actions -->
    <div class="flex items-center gap-0.5">
      {#if unsavedChanges}
        <button
          type="button"
          class="rounded-md p-1 text-primary hover:bg-primary/10"
          onclick={() => {
            if (textContent !== null) handleSave(textContent);
          }}
          title="Save (Ctrl+S)"
        >
          <Save class="h-3.5 w-3.5" />
        </button>
      {/if}
      <button
        type="button"
        class="rounded-md p-1 text-muted-foreground/60 hover:bg-muted hover:text-foreground"
        onclick={openInSystem}
        title="Open externally"
      >
        <ExternalLink class="h-3.5 w-3.5" />
      </button>
      <button
        type="button"
        class="rounded-md p-1 text-muted-foreground/60 hover:bg-muted hover:text-foreground"
        onclick={close}
        title="Close (Esc)"
      >
        <X class="h-3.5 w-3.5" />
      </button>
    </div>
  </div>

  <!-- Disk modified banner -->
  {#if diskModifiedBanner}
    <div class="flex items-center gap-2 border-b border-yellow-500/30 bg-yellow-500/10 px-4 py-1.5">
      <span class="text-xs text-yellow-300">File modified on disk</span>
      <button
        type="button"
        class="rounded px-2 py-0.5 text-xs font-medium text-yellow-300 hover:bg-yellow-500/20"
        onclick={loadDiskVersion}
      >
        Load disk version
      </button>
      <button
        type="button"
        class="rounded px-2 py-0.5 text-xs text-muted-foreground hover:bg-muted"
        onclick={dismissBanner}
      >
        Dismiss
      </button>
    </div>
  {/if}

  <!-- Content area -->
  <div class="flex-1 overflow-hidden">
    {#if loading}
      <div class="flex h-full items-center justify-center">
        <div class="flex flex-col items-center gap-2 text-muted-foreground">
          <Loader2 class="h-8 w-8 animate-spin" />
          <span class="text-sm">Loading file...</span>
        </div>
      </div>
    {:else if error}
      <div class="flex h-full items-center justify-center">
        <div class="flex flex-col items-center gap-3 text-muted-foreground">
          <AlertCircle class="h-8 w-8 text-red-400" />
          <span class="text-sm">Failed to load file</span>
          <p class="max-w-md text-center text-xs text-red-400/80">{error}</p>
          <button
            type="button"
            class="mt-1 rounded-md bg-primary/10 px-3 py-1.5 text-xs text-primary hover:bg-primary/20"
            onclick={openInSystem}
          >
            Open in System App
          </button>
        </div>
      </div>
    {:else if fileCategory === "image"}
      {#await import("./ImageViewer.svelte") then mod}
        <mod.default src={base64Url} alt={file.name} />
      {/await}
    {:else if fileCategory === "audio" && mediaSrc}
      {#await import("./AudioPlayer.svelte") then mod}
        <mod.default src={mediaSrc} filename={file.name} size={file.size} />
      {/await}
    {:else if fileCategory === "video" && mediaSrc}
      {#await import("./VideoPlayer.svelte") then mod}
        <mod.default src={mediaSrc} filename={file.name} />
      {/await}
    {:else if fileCategory === "markdown" && textContent !== null}
      {#await import("./MarkdownViewer.svelte") then mod}
        <mod.default content={textContent} extension={ext} parentDir={fileParentDir} />
      {/await}
    {:else if fileCategory === "spreadsheet" && textContent !== null}
      {#await import("./SpreadsheetViewer.svelte") then mod}
        <mod.default content={textContent} extension={ext} />
      {/await}
    {:else if fileCategory === "htmlpreview" && textContent !== null}
      {#await import("./HtmlPreview.svelte") then mod}
        <mod.default content={textContent} extension={ext} parentDir={fileParentDir} />
      {/await}
    {:else if fileCategory === "code" && textContent !== null}
      {#await import("./CodeEditor.svelte") then mod}
        <mod.default
          content={textContent}
          extension={ext}
          readonly={false}
          resetContent={editorReloadKey}
          onsave={handleSave}
          ondirtychange={(d) => { unsavedChanges = d; }}
        />
      {/await}
    {:else if fileCategory === "pdf" && base64Url}
      {#await import("./PdfViewer.svelte") then mod}
        <mod.default base64Url={base64Url} filename={file.name} />
      {/await}
    {:else if fileCategory === "notebook" && textContent !== null}
      {#await import("./NotebookViewer.svelte") then mod}
        <mod.default content={textContent} />
      {/await}
    {:else if fileCategory === "model3d" && mediaSrc}
      {#await import("./ModelViewer.svelte") then mod}
        <mod.default src={mediaSrc} extension={ext} />
      {/await}
    {:else if fileCategory === "document" && base64Url}
      {#await import("./DocumentViewer.svelte") then mod}
        <mod.default base64Url={base64Url} filename={file.name} />
      {/await}
    {:else if fileCategory === "xlsx" && base64Url}
      {#await import("./SpreadsheetViewer.svelte") then mod}
        <mod.default base64Url={base64Url} extension={ext} isBinary={true} />
      {/await}
    {:else}
      <!-- Unknown file type -->
      <div class="flex h-full items-center justify-center">
        <div class="flex flex-col items-center gap-4 text-muted-foreground">
          <FileQuestion class="h-16 w-16 opacity-30" />
          <div class="text-center">
            <p class="text-sm font-medium text-foreground">{file.name}</p>
            <p class="mt-1 text-xs">
              {formatSize(file.size)}
              {#if file.extension}
                &middot; .{file.extension} file
              {/if}
            </p>
          </div>
          <button
            type="button"
            class="flex items-center gap-2 rounded-md bg-primary/10 px-4 py-2 text-sm text-primary hover:bg-primary/20"
            onclick={openInSystem}
          >
            <ExternalLink class="h-4 w-4" />
            Open in System App
          </button>
        </div>
      </div>
    {/if}
  </div>
</div>

<script lang="ts">
  import { chatStore, connectionStore, settingsStore, skillStore, explorerStore } from "$lib/stores";
  import { X, Plus, ArrowUp, Globe, Brain, Wrench, Search, FolderOpen, FileText } from "@lucide/svelte";
  import * as InputGroup from "$lib/components/ui/input-group";
  import * as DropdownMenu from "$lib/components/ui/dropdown-menu";
  import { Separator } from "$lib/components/ui/separator";
  import FilePreview from "./FilePreview.svelte";
  import { localFs, getFileName, getExtension, isImageFile } from "$lib/filesystem";

  let { initialValue = "" }: { initialValue?: string } = $props();

  let inputValue = $state("");
  let textareaEl: HTMLTextAreaElement | null = $state(null);
  let attachedFiles = $state<{ name: string; type: string; size: number; file: File; path?: string; data?: string }[]>([]);

  // Sync initialValue prop into state
  $effect(() => {
    if (initialValue) {
      inputValue = initialValue;
    }
  });

  let isStreaming = $derived(chatStore.isStreaming);
  let isConnected = $derived(connectionStore.isConnected);
  let canSend = $derived(
    (inputValue.trim().length > 0 || attachedFiles.length > 0) && !isStreaming && isConnected,
  );

  // Active model display — short name from settings
  let activeModel = $derived.by(() => {
    const m = settingsStore.model;
    if (!m) return "";
    // Shorten common model names: "claude-sonnet-4-..." → "Sonnet 4"
    if (m.includes("sonnet")) return "Sonnet";
    if (m.includes("opus")) return "Opus";
    if (m.includes("haiku")) return "Haiku";
    if (m.includes("gpt-4o")) return "GPT-4o";
    if (m.includes("gpt-4")) return "GPT-4";
    if (m.includes("o3")) return "o3";
    if (m.includes("o1")) return "o1";
    if (m.includes("gemini")) return "Gemini";
    // Fallback: last segment after last slash or dash, capped
    const short = m.split(/[/-]/).pop() ?? m;
    return short.length > 12 ? short.slice(0, 12) : short;
  });

  let activeBackend = $derived(settingsStore.agentBackend);
  let skillCount = $derived(skillStore.skills.length);
  let planMode = $derived(settingsStore.settings?.plan_mode ?? false);
  let webSearch = $derived(settingsStore.settings?.smart_routing_enabled ?? false);

  // File context indicator — show what explorer state will be sent with the message
  let fileContextLabel = $derived.by(() => {
    const file = explorerStore.openFile;
    if (file) return file.name;
    const dir = explorerStore.currentPath;
    if (dir) {
      const parts = dir.replace(/\\/g, "/").split("/").filter(Boolean);
      return parts.at(-1) ?? dir;
    }
    return "";
  });

  function handleSubmit() {
    const text = inputValue.trim();
    if (!text && attachedFiles.length === 0) return;
    if (isStreaming) return;

    const media: import("$lib/api").MediaAttachment[] = attachedFiles.map((f) => ({
      type: "file" as const,
      filename: f.name,
      mime_type: f.type,
    }));

    chatStore.sendMessage(text || "(files attached)", media.length > 0 ? media : undefined);
    inputValue = "";
    attachedFiles = [];
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  }

  function openFilePicker() {
    const input = document.createElement("input");
    input.type = "file";
    input.multiple = true;
    input.accept = "image/*,.pdf,.txt,.md,.csv,.json,.js,.ts,.py,.html,.css";
    input.onchange = () => {
      if (input.files) {
        addFiles(Array.from(input.files));
      }
    };
    input.click();
  }

  export function addFiles(files: File[]) {
    for (const file of files) {
      if (attachedFiles.length >= 10) break;
      attachedFiles.push({
        name: file.name,
        type: file.type || "application/octet-stream",
        size: file.size,
        file,
      });
    }
    attachedFiles = attachedFiles;
  }

  function removeFile(index: number) {
    attachedFiles = attachedFiles.filter((_, i) => i !== index);
  }

  function mimeFromExtension(ext: string): string {
    const map: Record<string, string> = {
      png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg",
      gif: "image/gif", webp: "image/webp", svg: "image/svg+xml",
      bmp: "image/bmp", ico: "image/x-icon", avif: "image/avif",
      pdf: "application/pdf",
      txt: "text/plain", md: "text/markdown", csv: "text/csv",
      json: "application/json", js: "text/javascript", ts: "text/typescript",
      py: "text/x-python", html: "text/html", css: "text/css",
      xml: "text/xml", yaml: "text/yaml", yml: "text/yaml",
    };
    return map[ext.toLowerCase()] ?? "application/octet-stream";
  }

  export async function addExplorerFiles(paths: string[]) {
    for (const filePath of paths) {
      if (attachedFiles.length >= 10) break;
      try {
        const stat = await localFs.stat(filePath);
        const name = getFileName(filePath);

        if (stat.isDir) {
          const file = new File([], name, { type: "inode/directory" });
          attachedFiles.push({ name, type: "inode/directory", size: 0, file, path: filePath });
          continue;
        }

        const ext = getExtension(filePath);
        const mime = mimeFromExtension(ext);

        let data: string | undefined;
        if (isImageFile(ext)) {
          try {
            data = await localFs.readFileBase64(filePath);
          } catch {
            // Skip preview if read fails
          }
        } else {
          // Load text snippet for previewable files
          try {
            const { isTextPreviewable } = await import("$lib/components/explorer/file-icon-colors");
            if (isTextPreviewable(ext)) {
              const text = await localFs.readFileText(filePath);
              if (text) {
                data = text.slice(0, 500);
              }
            }
          } catch {
            // Skip preview
          }
        }

        const file = new File([], name, { type: mime });
        attachedFiles.push({ name, type: mime, size: stat.size ?? 0, file, path: filePath, data });
      } catch {
        console.error("Failed to attach explorer file:", filePath);
      }
    }
    attachedFiles = attachedFiles;
  }

  function stopGeneration() {
    chatStore.stopGeneration();
  }

  async function togglePlanMode() {
    const next = !planMode;
    try {
      await settingsStore.update({ plan_mode: next });
    } catch {
      // Failed to update
    }
  }

  async function toggleWebSearch() {
    const next = !webSearch;
    try {
      await settingsStore.update({ smart_routing_enabled: next });
    } catch {
      // Failed to update
    }
  }
</script>

<div class="p-2 pt-0">
  <div class="mx-auto max-w-3xl bg-transparent">
    <!-- Attached files -->
    {#if attachedFiles.length > 0}
      <div class="mb-2 flex flex-wrap gap-1.5">
        {#each attachedFiles as file, i}
          <FilePreview
            file={{ name: file.name, type: file.type, size: file.size, data: file.data }}
            removable
            onRemove={() => removeFile(i)}
          />
        {/each}
      </div>
    {/if}

    <InputGroup.Root class="border-border/50 shadow-none backdrop-blur-md">
      <InputGroup.Textarea
        bind:ref={textareaEl}
        bind:value={inputValue}
        onkeydown={handleKeydown}
        placeholder={isConnected ? "Ask, search, or run a skill..." : "Connecting..."}
        disabled={!isConnected}
      />
      <InputGroup.Addon align="block-end">
        <InputGroup.Button
          variant="outline"
          class="rounded-full"
          size="icon-xs"
          onclick={openFilePicker}
          disabled={isStreaming}
        >
          <Plus />
        </InputGroup.Button>
        <!-- <DropdownMenu.Root>
          <DropdownMenu.Trigger>
            {#snippet child({ props })}
              <InputGroup.Button {...props} variant="ghost">
                <Brain class="mr-1 h-3 w-3" />
                {#if planMode}Plan{:else}Chat{/if}
              </InputGroup.Button>
            {/snippet}
          </DropdownMenu.Trigger>
          <DropdownMenu.Content side="top" align="start" class="[--radius:0.95rem]">
            <DropdownMenu.Item onclick={togglePlanMode}>
              <Brain class="mr-2 h-3.5 w-3.5" />
              {planMode ? "Disable" : "Enable"} Plan Mode
            </DropdownMenu.Item>
            <DropdownMenu.Item onclick={toggleWebSearch}>
              <Search class="mr-2 h-3.5 w-3.5" />
              {webSearch ? "Disable" : "Enable"} Smart Routing
            </DropdownMenu.Item>
          </DropdownMenu.Content>
          </DropdownMenu.Root> -->
          <div class="ms-auto flex items-center gap-1">
            {#if activeModel}
              <InputGroup.Text class="text-muted-foreground">
                {activeModel}
              </InputGroup.Text>
            {/if}
            {#if skillCount > 0}
              <InputGroup.Text class="text-muted-foreground">
                <Wrench class="mr-0.5 h-3 w-3" />
                {skillCount}
              </InputGroup.Text>
            {/if}
          </div>
        {#if fileContextLabel}
          <InputGroup.Text class=" flex items-center gap-0.5 bg-card/50 p-2 rounded">
            {#if explorerStore.openFile}
              <FileText class="mr-0.5 h-3 w-3" />
            {:else}
              <FolderOpen class="mr-0.5 h-3 w-3" />
            {/if}
            <span class="max-w-20 truncate line-clamp-1">{fileContextLabel}</span>
          </InputGroup.Text>
        {/if}
        <Separator orientation="vertical" class="!h-4" />
        {#if isStreaming}
          <InputGroup.Button
            variant="outline"
            class="rounded-full"
            size="icon-xs"
            onclick={stopGeneration}
          >
            <X />
            <span class="sr-only">Stop</span>
          </InputGroup.Button>
        {:else}
          <InputGroup.Button
            variant="default"
            class="rounded-full"
            size="icon-xs"
            disabled={!canSend}
            onclick={handleSubmit}
          >
            <ArrowUp />
            <span class="sr-only">Send</span>
          </InputGroup.Button>
        {/if}
      </InputGroup.Addon>
    </InputGroup.Root>
  </div>
</div>

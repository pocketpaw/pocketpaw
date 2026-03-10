<script lang="ts">
  import { chatStore } from "$lib/stores";
  import { AlertCircle, X } from "@lucide/svelte";
  import { onMount } from "svelte";
  import MessageList from "./MessageList.svelte";
  import ChatInput from "./ChatInput.svelte";
  import EmptyState from "./EmptyState.svelte";

  const DRAG_MIME = "application/x-pocketpaw-files";

  let { initialValue = "" }: { initialValue?: string } = $props();

  let chatInput: ChatInput | undefined = $state();
  let panelEl: HTMLDivElement | undefined = $state();
  let isDragOver = $state(false);
  let isEmpty = $derived(chatStore.isEmpty && !chatStore.isStreaming);
  let error = $derived(chatStore.error);

  function handleSuggestion(text: string) {
    chatStore.sendMessage(text);
  }

  function dismissError() {
    chatStore.error = null;
  }

  // Use document-level listeners to reliably catch drag events regardless of
  // DOM propagation quirks in Tauri's WebView2 on Windows.
  onMount(() => {
    function onDragOver(e: DragEvent) {
      if (!panelEl?.contains(e.target as Node)) {
        // Cursor left our panel — clear highlight
        if (isDragOver) isDragOver = false;
        return;
      }
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
      isDragOver = true;
    }

    function onDragLeave(e: DragEvent) {
      if (!panelEl) return;
      // Use bounding rect — relatedTarget is often null in WebView2
      const rect = panelEl.getBoundingClientRect();
      if (
        e.clientX <= rect.left || e.clientX >= rect.right ||
        e.clientY <= rect.top || e.clientY >= rect.bottom
      ) {
        isDragOver = false;
      }
    }

    function onDrop(e: DragEvent) {
      if (!panelEl?.contains(e.target as Node)) return;
      e.preventDefault();
      isDragOver = false;
      if (!e.dataTransfer || !chatInput) return;

      // Check for in-app explorer drag first
      const raw = e.dataTransfer.getData(DRAG_MIME);
      if (raw) {
        try {
          const { paths } = JSON.parse(raw) as { paths: string[] };
          chatInput.addExplorerFiles(paths);
          return;
        } catch {
          // Fall through to native file handling
        }
      }

      // Native OS file drop
      if (e.dataTransfer.files.length > 0) {
        chatInput.addFiles(Array.from(e.dataTransfer.files));
      }
    }

    function onDragEnd() {
      isDragOver = false;
    }

    document.addEventListener("dragover", onDragOver);
    document.addEventListener("dragleave", onDragLeave);
    document.addEventListener("drop", onDrop);
    document.addEventListener("dragend", onDragEnd);

    return () => {
      document.removeEventListener("dragover", onDragOver);
      document.removeEventListener("dragleave", onDragLeave);
      document.removeEventListener("drop", onDrop);
      document.removeEventListener("dragend", onDragEnd);
    };
  });
</script>

<div bind:this={panelEl} class="relative flex h-full flex-col">
  {#if isDragOver}
    <div class="pointer-events-none absolute inset-0 z-50 flex items-center justify-center rounded-lg border-2 border-dashed border-primary/50 bg-background/80 backdrop-blur-sm">
      <p class="text-sm font-medium text-primary">Drop files here</p>
    </div>
  {/if}

  {#if isEmpty}
    <div class="flex flex-1 items-center justify-center">
      <EmptyState onSuggestionClick={handleSuggestion} />
    </div>
  {:else}
    <MessageList />
  {/if}

  {#if error}
    <div class="mx-4 mb-2 flex items-center gap-2 rounded-md border border-red-500/20 bg-red-500/10 px-3 py-2 text-sm text-red-400">
      <AlertCircle class="h-4 w-4 shrink-0" />
      <span class="flex-1">{error}</span>
      <button onclick={dismissError} class="shrink-0 rounded-sm p-0.5 transition-colors hover:bg-red-500/20">
        <X class="h-3.5 w-3.5" />
      </button>
    </div>
  {/if}

  <ChatInput bind:this={chatInput} {initialValue} />
</div>

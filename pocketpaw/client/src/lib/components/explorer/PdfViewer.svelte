<script lang="ts">
  import ZoomIn from "@lucide/svelte/icons/zoom-in";
  import ZoomOut from "@lucide/svelte/icons/zoom-out";
  import Maximize2 from "@lucide/svelte/icons/maximize-2";
  import ScanLine from "@lucide/svelte/icons/scan-line";
  import ChevronLeft from "@lucide/svelte/icons/chevron-left";
  import ChevronRight from "@lucide/svelte/icons/chevron-right";
  import Loader2 from "@lucide/svelte/icons/loader-2";
  import AlertCircle from "@lucide/svelte/icons/alert-circle";
  import { base64DataUrlToArrayBuffer } from "$lib/filesystem";
  import { onMount } from "svelte";

  let {
    base64Url,
    filename,
  }: {
    base64Url: string;
    filename: string;
  } = $props();

  let zoom = $state(100);
  let page = $state(1);
  let pageInput = $state("1");
  let totalPages = $state(0);
  let loading = $state(true);
  let error = $state<string | null>(null);
  let containerEl = $state<HTMLDivElement | null>(null);

  const ZOOM_STEP = 25;
  const MIN_ZOOM = 25;
  const MAX_ZOOM = 400;

  let pdfDoc: any = null;
  let renderedPages = new Map<number, HTMLCanvasElement>();
  let observer: IntersectionObserver | null = null;
  let pageElements: HTMLDivElement[] = [];

  function zoomIn() {
    zoom = Math.min(MAX_ZOOM, zoom + ZOOM_STEP);
    rerenderAll();
  }

  function zoomOut() {
    zoom = Math.max(MIN_ZOOM, zoom - ZOOM_STEP);
    rerenderAll();
  }

  function fitWidth() {
    zoom = 100;
    rerenderAll();
  }

  function fitPage() {
    zoom = 75;
    rerenderAll();
  }

  function prevPage() {
    if (page > 1) {
      page--;
      pageInput = String(page);
      scrollToPage(page);
    }
  }

  function nextPage() {
    if (page < totalPages) {
      page++;
      pageInput = String(page);
      scrollToPage(page);
    }
  }

  function handlePageInput(e: KeyboardEvent) {
    if (e.key === "Enter") {
      const val = parseInt(pageInput);
      if (!isNaN(val) && val >= 1 && val <= totalPages) {
        page = val;
        pageInput = String(page);
        scrollToPage(page);
      } else {
        pageInput = String(page);
      }
    }
  }

  function scrollToPage(pageNum: number) {
    const el = pageElements[pageNum - 1];
    if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  async function renderPage(pageNum: number, container: HTMLDivElement) {
    if (!pdfDoc || renderedPages.has(pageNum)) return;

    try {
      const pdfPage = await pdfDoc.getPage(pageNum);
      const scale = zoom / 100;
      const dpr = window.devicePixelRatio || 1;
      const viewport = pdfPage.getViewport({ scale: scale * dpr });

      const canvas = document.createElement("canvas");
      canvas.width = viewport.width;
      canvas.height = viewport.height;
      canvas.style.width = `${viewport.width / dpr}px`;
      canvas.style.height = `${viewport.height / dpr}px`;

      const ctx = canvas.getContext("2d");
      if (!ctx) return;

      await pdfPage.render({ canvasContext: ctx, viewport }).promise;

      container.innerHTML = "";
      container.appendChild(canvas);
      renderedPages.set(pageNum, canvas);
    } catch (e) {
      console.error(`Failed to render page ${pageNum}:`, e);
    }
  }

  function rerenderAll() {
    renderedPages.clear();
    if (!containerEl) return;

    const containers = containerEl.querySelectorAll<HTMLDivElement>("[data-page]");
    containers.forEach((el) => {
      el.innerHTML = "";
    });

    if (observer) {
      observer.disconnect();
      setupObserver();
    }
  }

  function setupObserver() {
    if (!containerEl) return;

    observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            const pageNum = parseInt((entry.target as HTMLElement).dataset.page ?? "0");
            if (pageNum > 0) {
              renderPage(pageNum, entry.target as HTMLDivElement);
              // Pre-render next page
              const nextContainer = containerEl?.querySelector<HTMLDivElement>(`[data-page="${pageNum + 1}"]`);
              if (nextContainer) renderPage(pageNum + 1, nextContainer);
            }
          }
        }

        // Update current page indicator based on most visible
        const visible = entries.filter((e) => e.isIntersecting);
        if (visible.length > 0) {
          const topMost = visible.reduce((a, b) =>
            a.boundingClientRect.top < b.boundingClientRect.top ? a : b
          );
          const pageNum = parseInt((topMost.target as HTMLElement).dataset.page ?? "0");
          if (pageNum > 0) {
            page = pageNum;
            pageInput = String(page);
          }
        }
      },
      { root: containerEl, rootMargin: "200px 0px", threshold: 0.1 },
    );

    pageElements.forEach((el) => {
      if (observer) observer.observe(el);
    });
  }

  onMount(() => {
    (async () => {
      try {
        const pdfjsLib = await import("pdfjs-dist");

        // pdfjs v5.x uses Uint8Array.toHex() which Tauri's WebView lacks.
        // static/pdf.worker.min.mjs is the stock pdfjs worker with a toHex
        // polyfill prepended, served as a plain static file (no Vite transforms).
        pdfjsLib.GlobalWorkerOptions.workerSrc = "/pdf.worker.min.mjs";

        // Convert base64 data URL → ArrayBuffer → Uint8Array for pdfjs
        const arrayBuffer = base64DataUrlToArrayBuffer(base64Url);
        const data = new Uint8Array(arrayBuffer);

        const loadingTask = pdfjsLib.getDocument({ data });
        pdfDoc = await loadingTask.promise;
        totalPages = pdfDoc.numPages;
        loading = false;

        // Wait for DOM to update with page containers
        await new Promise((r) => requestAnimationFrame(r));

        if (containerEl) {
          pageElements = Array.from(containerEl.querySelectorAll<HTMLDivElement>("[data-page]"));
          setupObserver();
        }
      } catch (e) {
        error = e instanceof Error ? e.message : String(e);
        loading = false;
      }
    })();

    return () => {
      if (observer) observer.disconnect();
      if (pdfDoc) pdfDoc.destroy();
    };
  });
</script>

<div class="flex h-full flex-col">
  <!-- Toolbar -->
  <div class="flex items-center gap-1 border-b border-border/50 px-3 py-1.5">
    <!-- Page navigation -->
    <button
      type="button"
      class="rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-30"
      onclick={prevPage}
      disabled={page <= 1}
      title="Previous page"
    >
      <ChevronLeft class="h-4 w-4" />
    </button>
    <div class="flex items-center gap-1">
      <input
        type="text"
        bind:value={pageInput}
        onkeydown={handlePageInput}
        class="w-10 rounded border border-border/50 bg-muted/30 px-1.5 py-0.5 text-center text-xs text-foreground"
      />
      <span class="text-xs text-muted-foreground">/ {totalPages}</span>
    </div>
    <button
      type="button"
      class="rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-30"
      onclick={nextPage}
      disabled={page >= totalPages}
      title="Next page"
    >
      <ChevronRight class="h-4 w-4" />
    </button>

    <div class="mx-2 h-4 w-px bg-border/50"></div>

    <!-- Zoom controls -->
    <button
      type="button"
      class="rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-30"
      onclick={zoomOut}
      disabled={zoom <= MIN_ZOOM}
      title="Zoom out"
    >
      <ZoomOut class="h-4 w-4" />
    </button>
    <span class="w-10 text-center text-xs tabular-nums text-muted-foreground">{zoom}%</span>
    <button
      type="button"
      class="rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-30"
      onclick={zoomIn}
      disabled={zoom >= MAX_ZOOM}
      title="Zoom in"
    >
      <ZoomIn class="h-4 w-4" />
    </button>

    <div class="mx-2 h-4 w-px bg-border/50"></div>

    <!-- Fit options -->
    <button
      type="button"
      class="rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
      onclick={fitWidth}
      title="Fit width"
    >
      <ScanLine class="h-4 w-4" />
    </button>
    <button
      type="button"
      class="rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
      onclick={fitPage}
      title="Fit page"
    >
      <Maximize2 class="h-4 w-4" />
    </button>
  </div>

  <!-- PDF content -->
  <div class="flex-1 overflow-auto bg-muted/20" bind:this={containerEl}>
    {#if loading}
      <div class="flex h-full items-center justify-center">
        <div class="flex flex-col items-center gap-2 text-muted-foreground">
          <Loader2 class="h-8 w-8 animate-spin" />
          <span class="text-sm">Loading PDF...</span>
        </div>
      </div>
    {:else if error}
      <div class="flex h-full items-center justify-center">
        <div class="flex flex-col items-center gap-3 text-muted-foreground">
          <AlertCircle class="h-8 w-8 text-red-400" />
          <span class="text-sm">Failed to load PDF</span>
          <p class="max-w-md text-center text-xs text-red-400/80">{error}</p>
        </div>
      </div>
    {:else}
      <div class="flex flex-col items-center gap-4 py-4">
        {#each Array(totalPages) as _, i}
          <div
            data-page={i + 1}
            class="pdf-page flex items-center justify-center rounded border border-border/30 bg-white shadow-sm"
            style:min-height="400px"
            style:min-width="300px"
          >
            <!-- Canvas will be inserted here by renderPage -->
          </div>
        {/each}
      </div>
    {/if}
  </div>
</div>

<style>
  .pdf-page :global(canvas) {
    display: block;
  }
</style>

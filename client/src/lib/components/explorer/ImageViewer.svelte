<script lang="ts">
  import Loader2 from "@lucide/svelte/icons/loader-2";
  import AlertCircle from "@lucide/svelte/icons/alert-circle";
  import ZoomIn from "@lucide/svelte/icons/zoom-in";
  import ZoomOut from "@lucide/svelte/icons/zoom-out";
  import RotateCcw from "@lucide/svelte/icons/rotate-ccw";
  import { cn } from "$lib/utils";

  let {
    src,
    alt = "Image",
  }: {
    src: string | null;
    alt?: string;
  } = $props();

  let loaded = $state(false);
  let errored = $state(false);
  let scale = $state(1);
  let naturalWidth = $state(0);
  let naturalHeight = $state(0);
  let offsetX = $state(0);
  let offsetY = $state(0);
  let isDragging = $state(false);
  let dragStartX = $state(0);
  let dragStartY = $state(0);
  let dragStartOffsetX = $state(0);
  let dragStartOffsetY = $state(0);

  function zoomIn() {
    scale = Math.min(scale * 1.25, 5);
  }

  function zoomOut() {
    scale = Math.max(scale / 1.25, 0.1);
  }

  function resetZoom() {
    scale = 1;
    offsetX = 0;
    offsetY = 0;
  }

  function handleLoad(e: Event) {
    const img = e.target as HTMLImageElement;
    naturalWidth = img.naturalWidth;
    naturalHeight = img.naturalHeight;
    loaded = true;
  }

  function handleError() {
    errored = true;
  }

  function handleWheel(e: WheelEvent) {
    if (e.ctrlKey || e.metaKey) {
      e.preventDefault();
      if (e.deltaY < 0) zoomIn();
      else zoomOut();
    }
  }

  function handleMouseDown(e: MouseEvent) {
    if (scale <= 1) return;
    isDragging = true;
    dragStartX = e.clientX;
    dragStartY = e.clientY;
    dragStartOffsetX = offsetX;
    dragStartOffsetY = offsetY;
  }

  function handleMouseMove(e: MouseEvent) {
    if (!isDragging) return;
    offsetX = dragStartOffsetX + (e.clientX - dragStartX);
    offsetY = dragStartOffsetY + (e.clientY - dragStartY);
  }

  function handleMouseUp() {
    isDragging = false;
  }
</script>

<div class="flex h-full flex-col">
  <!-- Zoom controls -->
  {#if loaded}
    <div class="flex items-center justify-between border-b border-border/50 px-3 py-1.5">
      <span class="text-xs text-muted-foreground">
        {naturalWidth} x {naturalHeight} &middot; {Math.round(scale * 100)}%
      </span>
      <div class="flex items-center gap-1">
        <button
          type="button"
          class="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
          onclick={zoomOut}
          title="Zoom out"
        >
          <ZoomOut class="h-4 w-4" />
        </button>
        <button
          type="button"
          class="rounded px-2 py-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground"
          onclick={resetZoom}
          title="Reset zoom"
        >
          <RotateCcw class="h-3.5 w-3.5" />
        </button>
        <button
          type="button"
          class="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
          onclick={zoomIn}
          title="Zoom in"
        >
          <ZoomIn class="h-4 w-4" />
        </button>
      </div>
    </div>
  {/if}

  <!-- Image area -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div
    class={cn(
      "flex flex-1 items-center justify-center overflow-hidden bg-[#1a1a1a]/50 p-4",
      scale > 1 ? (isDragging ? "cursor-grabbing" : "cursor-grab") : "",
    )}
    onwheel={handleWheel}
    onmousedown={handleMouseDown}
    onmousemove={handleMouseMove}
    onmouseup={handleMouseUp}
    onmouseleave={handleMouseUp}
  >
    {#if !src}
      <div class="flex flex-col items-center gap-2 text-muted-foreground">
        <Loader2 class="h-8 w-8 animate-spin" />
        <span class="text-sm">Loading image...</span>
      </div>
    {:else if errored}
      <div class="flex flex-col items-center gap-2 text-muted-foreground">
        <AlertCircle class="h-8 w-8 text-red-400" />
        <span class="text-sm">Failed to load image</span>
      </div>
    {:else}
      <img
        {src}
        {alt}
        class={cn(
          "max-h-full max-w-full select-none object-contain",
          !loaded && "invisible",
          !isDragging && "transition-transform duration-150",
        )}
        style:transform="scale({scale}) translate({offsetX / scale}px, {offsetY / scale}px)"
        onload={handleLoad}
        onerror={handleError}
        draggable="false"
      />
      {#if !loaded}
        <div class="absolute flex flex-col items-center gap-2 text-muted-foreground">
          <Loader2 class="h-8 w-8 animate-spin" />
          <span class="text-sm">Loading image...</span>
        </div>
      {/if}
    {/if}
  </div>
</div>

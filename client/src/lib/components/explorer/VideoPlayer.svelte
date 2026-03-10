<script lang="ts">
  import { onMount } from "svelte";
  import Play from "@lucide/svelte/icons/play";
  import Pause from "@lucide/svelte/icons/pause";
  import Volume2 from "@lucide/svelte/icons/volume-2";
  import VolumeX from "@lucide/svelte/icons/volume-x";
  import Maximize from "@lucide/svelte/icons/maximize";
  import PictureInPicture from "@lucide/svelte/icons/picture-in-picture";

  let {
    src,
    filename,
  }: {
    src: string;
    filename: string;
  } = $props();

  let videoEl = $state<HTMLVideoElement | null>(null);
  let containerEl = $state<HTMLDivElement | null>(null);
  let playing = $state(false);
  let currentTime = $state(0);
  let duration = $state(0);
  let volume = $state(1);
  let muted = $state(false);
  let seeking = $state(false);
  let showControls = $state(true);
  let playbackRate = $state(1);
  let showSpeedMenu = $state(false);

  let hideTimer: ReturnType<typeof setTimeout> | null = null;

  const SPEEDS = [0.5, 1, 1.5, 2];

  function formatTime(s: number): string {
    if (!isFinite(s) || s < 0) return "0:00";
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = Math.floor(s % 60);
    if (h > 0) return `${h}:${m.toString().padStart(2, "0")}:${sec.toString().padStart(2, "0")}`;
    return `${m}:${sec.toString().padStart(2, "0")}`;
  }

  function togglePlay() {
    if (!videoEl) return;
    if (playing) {
      videoEl.pause();
    } else {
      videoEl.play();
    }
  }

  function handleSeek(e: Event) {
    if (!videoEl) return;
    videoEl.currentTime = parseFloat((e.target as HTMLInputElement).value);
    seeking = false;
  }

  function handleSeekInput(e: Event) {
    seeking = true;
    currentTime = parseFloat((e.target as HTMLInputElement).value);
  }

  function handleVolumeChange(e: Event) {
    if (!videoEl) return;
    volume = parseFloat((e.target as HTMLInputElement).value);
    videoEl.volume = volume;
    muted = volume === 0;
  }

  function toggleMute() {
    if (!videoEl) return;
    muted = !muted;
    videoEl.muted = muted;
  }

  function toggleFullscreen() {
    if (!containerEl) return;
    if (document.fullscreenElement) {
      document.exitFullscreen();
    } else {
      containerEl.requestFullscreen();
    }
  }

  async function togglePiP() {
    if (!videoEl) return;
    try {
      if (document.pictureInPictureElement) {
        await document.exitPictureInPicture();
      } else {
        await videoEl.requestPictureInPicture();
      }
    } catch {
      // PiP not supported
    }
  }

  function setSpeed(rate: number) {
    if (!videoEl) return;
    playbackRate = rate;
    videoEl.playbackRate = rate;
    showSpeedMenu = false;
  }

  function resetHideTimer() {
    showControls = true;
    if (hideTimer) clearTimeout(hideTimer);
    if (playing) {
      hideTimer = setTimeout(() => {
        showControls = false;
      }, 3000);
    }
  }

  function handleKeydown(e: KeyboardEvent) {
    if (!videoEl) return;
    // Only handle if the video player area has focus context
    const target = e.target as HTMLElement;
    if (target.tagName === "INPUT" || target.tagName === "TEXTAREA") return;

    switch (e.key) {
      case " ":
      case "k":
        e.preventDefault();
        togglePlay();
        break;
      case "ArrowLeft":
        e.preventDefault();
        videoEl.currentTime = Math.max(0, videoEl.currentTime - 5);
        break;
      case "ArrowRight":
        e.preventDefault();
        videoEl.currentTime = Math.min(duration, videoEl.currentTime + 5);
        break;
      case "ArrowUp":
        e.preventDefault();
        volume = Math.min(1, volume + 0.1);
        videoEl.volume = volume;
        break;
      case "ArrowDown":
        e.preventDefault();
        volume = Math.max(0, volume - 0.1);
        videoEl.volume = volume;
        break;
      case "f":
        e.preventDefault();
        toggleFullscreen();
        break;
      case "m":
        e.preventDefault();
        toggleMute();
        break;
    }
  }

  onMount(() => {
    return () => {
      if (hideTimer) clearTimeout(hideTimer);
    };
  });

  let progress = $derived(duration > 0 ? (currentTime / duration) * 100 : 0);
</script>

<div
  bind:this={containerEl}
  class="group relative flex h-full w-full items-center justify-center bg-black"
  onmousemove={resetHideTimer}
  onmouseleave={() => { if (playing) showControls = false; }}
  role="application"
  aria-label="Video player: {filename}"
  tabindex="-1"
  onkeydown={handleKeydown}
>
  <!-- svelte-ignore a11y_media_has_caption -->
  <video
    bind:this={videoEl}
    {src}
    class="h-full w-full"
    preload="metadata"
    onclick={togglePlay}
    ondblclick={toggleFullscreen}
    onplay={() => { playing = true; resetHideTimer(); }}
    onpause={() => { playing = false; showControls = true; }}
    onended={() => { playing = false; showControls = true; }}
    ontimeupdate={() => { if (!seeking) currentTime = videoEl?.currentTime ?? 0; }}
    onloadedmetadata={() => { duration = videoEl?.duration ?? 0; }}
    ondurationchange={() => { duration = videoEl?.duration ?? 0; }}
  ></video>

  <!-- Big center play button (when paused) -->
  {#if !playing}
    <button
      type="button"
      class="absolute inset-0 flex items-center justify-center"
      onclick={togglePlay}
      aria-label="Play"
    >
      <div class="flex h-16 w-16 items-center justify-center rounded-full bg-black/60 text-white backdrop-blur-sm">
        <Play class="ml-1 h-8 w-8" />
      </div>
    </button>
  {/if}

  <!-- Bottom controls overlay -->
  <div
    class="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/80 to-transparent px-4 pb-3 pt-8 transition-opacity duration-300"
    style:opacity={showControls ? 1 : 0}
    style:pointer-events={showControls ? "auto" : "none"}
  >
    <!-- Seek bar -->
    <div class="mb-2 flex items-center gap-2">
      <span class="text-xs tabular-nums text-white/80">{formatTime(currentTime)}</span>
      <div class="relative flex-1">
        <div class="absolute inset-y-0 left-0 flex items-center" style:width="{progress}%">
          <div class="h-1 w-full rounded-full bg-primary"></div>
        </div>
        <input
          type="range"
          min="0"
          max={duration || 0}
          step="0.1"
          value={currentTime}
          oninput={handleSeekInput}
          onchange={handleSeek}
          class="video-range relative z-10 w-full cursor-pointer"
        />
      </div>
      <span class="text-xs tabular-nums text-white/80">{formatTime(duration)}</span>
    </div>

    <!-- Control buttons -->
    <div class="flex items-center gap-1">
      <!-- Play/Pause -->
      <button
        type="button"
        class="rounded-md p-1.5 text-white/80 hover:text-white"
        onclick={togglePlay}
        title={playing ? "Pause (K)" : "Play (K)"}
      >
        {#if playing}
          <Pause class="h-5 w-5" />
        {:else}
          <Play class="h-5 w-5" />
        {/if}
      </button>

      <!-- Volume -->
      <button
        type="button"
        class="rounded-md p-1.5 text-white/80 hover:text-white"
        onclick={toggleMute}
        title={muted ? "Unmute (M)" : "Mute (M)"}
      >
        {#if muted || volume === 0}
          <VolumeX class="h-4 w-4" />
        {:else}
          <Volume2 class="h-4 w-4" />
        {/if}
      </button>
      <input
        type="range"
        min="0"
        max="1"
        step="0.01"
        value={muted ? 0 : volume}
        oninput={handleVolumeChange}
        class="video-range w-16 cursor-pointer"
      />

      <div class="flex-1"></div>

      <!-- Speed selector -->
      <div class="relative">
        <button
          type="button"
          class="rounded-md px-2 py-1 text-xs font-medium text-white/80 hover:bg-white/10 hover:text-white"
          onclick={() => { showSpeedMenu = !showSpeedMenu; }}
        >
          {playbackRate}x
        </button>
        {#if showSpeedMenu}
          <div class="absolute bottom-full right-0 mb-1 rounded-md border border-white/10 bg-black/90 py-1 backdrop-blur-sm">
            {#each SPEEDS as speed}
              <button
                type="button"
                class="block w-full px-4 py-1 text-left text-xs text-white/80 hover:bg-white/10 hover:text-white"
                class:text-primary={speed === playbackRate}
                onclick={() => setSpeed(speed)}
              >
                {speed}x
              </button>
            {/each}
          </div>
        {/if}
      </div>

      <!-- PiP -->
      <button
        type="button"
        class="rounded-md p-1.5 text-white/80 hover:text-white"
        onclick={togglePiP}
        title="Picture in Picture"
      >
        <PictureInPicture class="h-4 w-4" />
      </button>

      <!-- Fullscreen -->
      <button
        type="button"
        class="rounded-md p-1.5 text-white/80 hover:text-white"
        onclick={toggleFullscreen}
        title="Fullscreen (F)"
      >
        <Maximize class="h-4 w-4" />
      </button>
    </div>
  </div>
</div>

<style>
  .video-range {
    -webkit-appearance: none;
    appearance: none;
    height: 4px;
    background: rgba(255, 255, 255, 0.2);
    border-radius: 2px;
    outline: none;
  }

  .video-range::-webkit-slider-thumb {
    -webkit-appearance: none;
    appearance: none;
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: white;
    cursor: pointer;
    border: none;
  }

  .video-range::-moz-range-thumb {
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: white;
    cursor: pointer;
    border: none;
  }
</style>

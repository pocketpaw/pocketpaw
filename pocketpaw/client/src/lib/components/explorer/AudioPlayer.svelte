<script lang="ts">
  import { onMount } from "svelte";
  import Play from "@lucide/svelte/icons/play";
  import Pause from "@lucide/svelte/icons/pause";
  import Volume2 from "@lucide/svelte/icons/volume-2";
  import VolumeX from "@lucide/svelte/icons/volume-x";
  import SkipBack from "@lucide/svelte/icons/skip-back";
  import SkipForward from "@lucide/svelte/icons/skip-forward";

  let {
    src,
    filename,
    size = 0,
  }: {
    src: string;
    filename: string;
    size?: number;
  } = $props();

  let audioEl = $state<HTMLAudioElement | null>(null);
  let canvasEl = $state<HTMLCanvasElement | null>(null);
  let playing = $state(false);
  let currentTime = $state(0);
  let duration = $state(0);
  let volume = $state(1);
  let muted = $state(false);
  let seeking = $state(false);

  // Web Audio API for waveform
  let audioCtx: AudioContext | null = null;
  let analyser: AnalyserNode | null = null;
  let sourceNode: MediaElementAudioSourceNode | null = null;
  let animFrameId: number | null = null;
  let waveformInitialized = false;

  function formatTime(s: number): string {
    if (!isFinite(s) || s < 0) return "0:00";
    const mins = Math.floor(s / 60);
    const secs = Math.floor(s % 60);
    return `${mins}:${secs.toString().padStart(2, "0")}`;
  }

  function formatSize(bytes: number): string {
    if (bytes === 0) return "";
    const units = ["B", "KB", "MB", "GB"];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    return `${(bytes / Math.pow(1024, i)).toFixed(i > 0 ? 1 : 0)} ${units[i]}`;
  }

  function togglePlay() {
    if (!audioEl) return;
    if (playing) {
      audioEl.pause();
    } else {
      audioEl.play();
      if (!waveformInitialized) initWaveform();
    }
  }

  function skip(delta: number) {
    if (!audioEl) return;
    audioEl.currentTime = Math.max(0, Math.min(audioEl.currentTime + delta, duration));
  }

  function handleSeek(e: Event) {
    if (!audioEl) return;
    const val = parseFloat((e.target as HTMLInputElement).value);
    audioEl.currentTime = val;
    seeking = false;
  }

  function handleSeekInput(e: Event) {
    seeking = true;
    currentTime = parseFloat((e.target as HTMLInputElement).value);
  }

  function handleVolumeChange(e: Event) {
    if (!audioEl) return;
    volume = parseFloat((e.target as HTMLInputElement).value);
    audioEl.volume = volume;
    muted = volume === 0;
  }

  function toggleMute() {
    if (!audioEl) return;
    muted = !muted;
    audioEl.muted = muted;
  }

  function initWaveform() {
    if (waveformInitialized || !audioEl || !canvasEl) return;
    try {
      audioCtx = new AudioContext();
      analyser = audioCtx.createAnalyser();
      analyser.fftSize = 128;
      sourceNode = audioCtx.createMediaElementSource(audioEl);
      sourceNode.connect(analyser);
      analyser.connect(audioCtx.destination);
      waveformInitialized = true;
      drawWaveform();
    } catch {
      // Web Audio not available
    }
  }

  function drawWaveform() {
    if (!analyser || !canvasEl) return;
    const ctx = canvasEl.getContext("2d");
    if (!ctx) return;

    const bufferLength = analyser.frequencyBinCount;
    const dataArray = new Uint8Array(bufferLength);

    function draw() {
      if (!analyser || !canvasEl || !ctx) return;
      animFrameId = requestAnimationFrame(draw);

      analyser.getByteFrequencyData(dataArray);

      const w = canvasEl.width;
      const h = canvasEl.height;
      ctx.clearRect(0, 0, w, h);

      const barWidth = (w / bufferLength) * 1.5;
      let x = 0;

      for (let i = 0; i < bufferLength; i++) {
        const barHeight = (dataArray[i] / 255) * h * 0.85;

        const hue = 220 + (i / bufferLength) * 40;
        ctx.fillStyle = `hsla(${hue}, 70%, 60%, 0.8)`;

        const y = h - barHeight;
        ctx.beginPath();
        ctx.roundRect(x, y, barWidth - 1, barHeight, 2);
        ctx.fill();

        x += barWidth;
      }
    }

    draw();
  }

  onMount(() => {
    return () => {
      if (animFrameId !== null) cancelAnimationFrame(animFrameId);
      audioCtx?.close();
    };
  });

  let progress = $derived(duration > 0 ? (currentTime / duration) * 100 : 0);
</script>

<div class="flex h-full flex-col items-center justify-center gap-6 p-8">
  <!-- Waveform visualization -->
  <div class="flex w-full max-w-lg flex-col items-center gap-4">
    <canvas
      bind:this={canvasEl}
      width={512}
      height={120}
      class="w-full rounded-lg bg-muted/30"
    ></canvas>

    <!-- File info -->
    <div class="text-center">
      <p class="text-sm font-medium text-foreground">{filename}</p>
      {#if size > 0}
        <p class="text-xs text-muted-foreground">{formatSize(size)}</p>
      {/if}
    </div>
  </div>

  <!-- Controls -->
  <div class="flex w-full max-w-lg flex-col gap-3">
    <!-- Seek bar -->
    <div class="flex items-center gap-2">
      <span class="w-10 text-right text-xs tabular-nums text-muted-foreground">
        {formatTime(currentTime)}
      </span>
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
          class="audio-range relative z-10 w-full cursor-pointer"
        />
      </div>
      <span class="w-10 text-xs tabular-nums text-muted-foreground">
        {formatTime(duration)}
      </span>
    </div>

    <!-- Playback controls -->
    <div class="flex items-center justify-center gap-2">
      <!-- Volume -->
      <div class="flex items-center gap-1">
        <button
          type="button"
          class="rounded-md p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground"
          onclick={toggleMute}
          title={muted ? "Unmute" : "Mute"}
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
          class="audio-range w-16 cursor-pointer"
        />
      </div>

      <!-- Skip back -->
      <button
        type="button"
        class="rounded-md p-2 text-muted-foreground hover:bg-muted hover:text-foreground"
        onclick={() => skip(-10)}
        title="Skip back 10s"
      >
        <SkipBack class="h-4 w-4" />
      </button>

      <!-- Play/Pause -->
      <button
        type="button"
        class="flex h-10 w-10 items-center justify-center rounded-full bg-primary text-primary-foreground shadow-md hover:bg-primary/90"
        onclick={togglePlay}
        title={playing ? "Pause" : "Play"}
      >
        {#if playing}
          <Pause class="h-5 w-5" />
        {:else}
          <Play class="ml-0.5 h-5 w-5" />
        {/if}
      </button>

      <!-- Skip forward -->
      <button
        type="button"
        class="rounded-md p-2 text-muted-foreground hover:bg-muted hover:text-foreground"
        onclick={() => skip(10)}
        title="Skip forward 10s"
      >
        <SkipForward class="h-4 w-4" />
      </button>

      <!-- Spacer to balance volume section -->
      <div class="w-[88px]"></div>
    </div>
  </div>

  <!-- Hidden audio element -->
  <audio
    bind:this={audioEl}
    {src}
    preload="metadata"
    onplay={() => { playing = true; }}
    onpause={() => { playing = false; }}
    onended={() => { playing = false; }}
    ontimeupdate={() => { if (!seeking) currentTime = audioEl?.currentTime ?? 0; }}
    onloadedmetadata={() => { duration = audioEl?.duration ?? 0; }}
    ondurationchange={() => { duration = audioEl?.duration ?? 0; }}
  ></audio>
</div>

<style>
  .audio-range {
    -webkit-appearance: none;
    appearance: none;
    height: 4px;
    background: rgba(255, 255, 255, 0.1);
    border-radius: 2px;
    outline: none;
  }

  .audio-range::-webkit-slider-thumb {
    -webkit-appearance: none;
    appearance: none;
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: hsl(var(--primary));
    cursor: pointer;
    border: none;
  }

  .audio-range::-moz-range-thumb {
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: hsl(var(--primary));
    cursor: pointer;
    border: none;
  }
</style>

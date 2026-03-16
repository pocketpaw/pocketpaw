<script lang="ts">
  import { onMount } from "svelte";

  let {
    text,
    speed = 40,
    delay = 0,
    onDone = () => {},
  }: {
    text: string;
    speed?: number;
    delay?: number;
    onDone?: () => void;
  } = $props();

  let displayed = $state("");
  let done = $state(false);

  onMount(() => {
    let interval: ReturnType<typeof setInterval> | null = null;
    const timeout = setTimeout(() => {
      let i = 0;
      interval = setInterval(() => {
        if (i < text.length) {
          displayed += text[i];
          i++;
        } else {
          clearInterval(interval!);
          interval = null;
          done = true;
          onDone();
        }
      }, speed);
    }, delay);
    return () => {
      clearTimeout(timeout);
      if (interval) clearInterval(interval);
    };
  });
</script>

<span class="inline">
  {displayed}<span
    class={done ? "hidden" : "ml-0.5 inline-block h-5 w-0.5 animate-pulse bg-foreground align-middle"}
  ></span>
</span>

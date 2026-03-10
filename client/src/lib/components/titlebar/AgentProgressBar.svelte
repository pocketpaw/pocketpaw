<script lang="ts">
  import { activityStore } from "$lib/stores";
</script>

{#if activityStore.isAgentWorking}
  <div class="absolute bottom-0 left-0 h-[2px] w-full overflow-hidden">
    <div
      class="animate-progress-slide h-full w-1/3 rounded-full bg-primary opacity-70"
    ></div>
  </div>
{/if}

<script lang="ts">
  import { activityStore } from "$lib/stores";
  import ActivityCollapsed from "./ActivityCollapsed.svelte";
  import ActivityExpanded from "./ActivityExpanded.svelte";

  let isWorking = $derived(activityStore.isAgentWorking);
  let hasEntries = $derived(activityStore.entries.length > 0);

  // Persist expansion state
  let isExpanded = $state(
    typeof localStorage !== "undefined"
      ? localStorage.getItem("pocketpaw_activity_expanded") === "true"
      : false,
  );

  // Show panel when working, then fade after a delay
  let visible = $state(false);
  let hideTimer: ReturnType<typeof setTimeout> | null = null;

  $effect(() => {
    if (isWorking) {
      visible = true;
      if (hideTimer) {
        clearTimeout(hideTimer);
        hideTimer = null;
      }
    } else if (hasEntries && visible) {
      // Keep visible for 4 seconds after agent finishes
      hideTimer = setTimeout(() => {
        visible = false;
        hideTimer = null;
      }, 4000);
    }

    return () => {
      if (hideTimer) {
        clearTimeout(hideTimer);
        hideTimer = null;
      }
    };
  });

  function expand() {
    isExpanded = true;
    localStorage.setItem("pocketpaw_activity_expanded", "true");
  }

  function collapse() {
    isExpanded = false;
    localStorage.setItem("pocketpaw_activity_expanded", "false");
  }
</script>

{#if visible && hasEntries}
  <div
    class="mt-3 transition-opacity duration-300"
    class:opacity-100={isWorking}
    class:opacity-60={!isWorking}
  >
    {#if isExpanded}
      <ActivityExpanded onCollapse={collapse} />
    {:else}
      <ActivityCollapsed onExpand={expand} />
    {/if}
  </div>
{/if}

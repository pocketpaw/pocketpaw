<script lang="ts">
  import StyledFileIcon from "./StyledFileIcon.svelte";

  let {
    extension = "",
    isDir = false,
    size = 24,
    class: className = "",
  }: {
    extension?: string;
    isDir?: boolean;
    size?: number;
    class?: string;
  } = $props();
</script>

<span class={className}>
  <StyledFileIcon {extension} {isDir} {size} />
</span>

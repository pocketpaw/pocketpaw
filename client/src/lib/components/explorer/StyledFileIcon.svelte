<script lang="ts">
  import { getCategoryColor } from "./file-icon-colors";

  let {
    extension = "",
    isDir = false,
    size = 36,
  }: {
    extension?: string;
    isDir?: boolean;
    size?: number;
  } = $props();

  let resolved = $derived(getCategoryColor(extension));

  // Scale factor relative to default 36px
  let scale = $derived(size / 36);
</script>

{#if isDir}
  <!-- Folder icon: filled folder shape with tab -->
  <svg
    width={size}
    height={size}
    viewBox="0 0 36 36"
    fill="none"
    xmlns="http://www.w3.org/2000/svg"
  >
    <defs>
      <linearGradient id="folderGrad" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="#FBBF24" />
        <stop offset="100%" stop-color="#D97706" />
      </linearGradient>
    </defs>
    <!-- Folder tab -->
    <path
      d="M4 10 L4 8 Q4 6 6 6 L14 6 Q15 6 15.5 7 L17 10 Z"
      fill="url(#folderGrad)"
    />
    <!-- Folder body -->
    <rect x="3" y="10" width="30" height="20" rx="3" fill="url(#folderGrad)" />
    <!-- Highlight line -->
    <rect x="5" y="12" width="26" height="1.5" rx="0.75" fill="#FDE68A" opacity="0.5" />
  </svg>
{:else}
  <!-- File icon: rounded rect with dog-ear fold + extension label -->
  <svg
    width={size}
    height={size}
    viewBox="0 0 36 36"
    fill="none"
    xmlns="http://www.w3.org/2000/svg"
  >
    <!-- File body with dog-ear -->
    <path
      d="M6 4 L22 4 L30 12 L30 32 Q30 34 28 34 L8 34 Q6 34 6 32 Z"
      fill={resolved.fill}
      opacity="0.9"
    />
    <!-- Dog-ear fold -->
    <path
      d="M22 4 L22 10 Q22 12 24 12 L30 12 Z"
      fill="white"
      opacity="0.3"
    />
    <!-- Extension label -->
    {#if resolved.text}
      <text
        x="18"
        y="27"
        text-anchor="middle"
        font-size={resolved.text.length > 3 ? "7" : "8.5"}
        font-weight="700"
        font-family="Inter, system-ui, sans-serif"
        fill="white"
        opacity="0.95"
      >
        {resolved.text}
      </text>
    {/if}
  </svg>
{/if}

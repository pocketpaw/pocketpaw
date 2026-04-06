<script lang="ts">
  import type { ChatMessage } from "$lib/api";
  import UserMessage from "./UserMessage.svelte";
  import AssistantMessage from "./AssistantMessage.svelte";

  let {
    message,
    isLastAssistant = false,
    onRegenerate,
  }: {
    message: ChatMessage;
    isLastAssistant?: boolean;
    onRegenerate?: () => void;
  } = $props();
</script>

{#if message.role === "user"}
  <UserMessage {message} />
{:else}
  <AssistantMessage {message} {isLastAssistant} {onRegenerate} />
{/if}

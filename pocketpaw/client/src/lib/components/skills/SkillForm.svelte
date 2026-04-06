<script lang="ts">
  import type { Skill } from "$lib/api";
  import { chatStore } from "$lib/stores";
  import { goto } from "$app/navigation";
  import * as Dialog from "$lib/components/ui/dialog";

  let {
    skill,
    open = $bindable(false),
  }: {
    skill: Skill | null;
    open?: boolean;
  } = $props();

  let mainInput = $state("");
  let extraInstructions = $state("");

  function handleSubmit() {
    if (!skill || !mainInput.trim()) return;

    let message = mainInput.trim();
    if (extraInstructions.trim()) {
      message += `\n\n(${extraInstructions.trim()})`;
    }

    // Prefix with skill hint so the agent knows the context
    const fullMessage = `[Skill: ${skill.name}] ${message}`;
    chatStore.sendMessage(fullMessage);

    // Reset and close
    mainInput = "";
    extraInstructions = "";
    open = false;

    goto("/");
  }

  function handleClose() {
    mainInput = "";
    extraInstructions = "";
    open = false;
  }

  // Reset form when skill changes
  $effect(() => {
    if (skill) {
      mainInput = "";
      extraInstructions = "";
    }
  });
</script>

<Dialog.Root bind:open onOpenChange={(v) => { if (!v) handleClose(); }}>
  <Dialog.Content class="max-w-md">
    {#if skill}
      <Dialog.Header>
        <Dialog.Title class="text-base">{skill.name}</Dialog.Title>
        <Dialog.Description class="text-xs text-muted-foreground">
          {skill.description}
        </Dialog.Description>
      </Dialog.Header>

      <div class="flex flex-col gap-4 py-4">
        <div class="flex flex-col gap-1.5">
          <label for="skill-input" class="text-xs font-medium text-foreground">
            {skill.argument_hint || "What would you like to do?"}
          </label>
          <textarea
            id="skill-input"
            bind:value={mainInput}
            placeholder="Type here..."
            rows={3}
            class="w-full resize-none rounded-lg border border-border bg-muted/50 px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
          ></textarea>
        </div>

        <div class="flex flex-col gap-1.5">
          <label for="skill-extra" class="text-xs font-medium text-muted-foreground">
            Additional instructions (optional)
          </label>
          <input
            id="skill-extra"
            bind:value={extraInstructions}
            placeholder='e.g., "Focus on action items"'
            class="w-full rounded-lg border border-border bg-muted/50 px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
          />
        </div>
      </div>

      <Dialog.Footer>
        <button
          onclick={handleClose}
          class="rounded-lg border border-border px-4 py-2 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
        >
          Cancel
        </button>
        <button
          onclick={handleSubmit}
          disabled={!mainInput.trim()}
          class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-40"
        >
          Go
        </button>
      </Dialog.Footer>
    {/if}
  </Dialog.Content>
</Dialog.Root>

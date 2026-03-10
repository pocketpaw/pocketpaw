<script lang="ts">
  import type { Skill } from "$lib/api";

  let {
    skill,
    onUse,
  }: {
    skill: Skill;
    onUse: (skill: Skill) => void;
  } = $props();
</script>

<div class="flex flex-col gap-2 rounded-xl border border-border p-4 transition-colors hover:border-primary/30 hover:bg-accent/30">
  <div class="flex flex-col gap-1">
    <span class="text-sm font-semibold text-foreground">{skill.name}</span>
    <p class="line-clamp-2 text-xs text-muted-foreground">{skill.description}</p>
  </div>
  <button
    onclick={() => onUse(skill)}
    class="mt-auto self-start text-xs font-medium text-primary transition-opacity hover:opacity-80"
  >
    Use This &rarr;
  </button>
</div>

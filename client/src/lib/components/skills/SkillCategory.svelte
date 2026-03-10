<script lang="ts">
  import type { Skill } from "$lib/api";
  import type { Component } from "svelte";
  import SkillCard from "./SkillCard.svelte";

  let {
    name,
    icon: Icon,
    skills,
    onUseSkill,
  }: {
    name: string;
    icon: Component<any>;
    skills: Skill[];
    onUseSkill: (skill: Skill) => void;
  } = $props();
</script>

{#if skills.length > 0}
  <div class="flex flex-col gap-3">
    <div class="flex items-center gap-2">
      <Icon class="h-4 w-4 text-muted-foreground" strokeWidth={1.75} />
      <h3 class="text-sm font-semibold text-foreground">{name}</h3>
      <span class="text-[10px] text-muted-foreground">{skills.length}</span>
    </div>
    <div class="grid grid-cols-2 gap-2 lg:grid-cols-3">
      {#each skills as skill (skill.name)}
        <SkillCard {skill} onUse={onUseSkill} />
      {/each}
    </div>
  </div>
{/if}

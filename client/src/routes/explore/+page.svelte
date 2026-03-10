<script lang="ts">
  import { page } from "$app/state";
  import SkillBrowser from "$lib/components/skills/SkillBrowser.svelte";

  let category = $derived(page.url.searchParams.get("category") ?? "");
</script>

<SkillBrowser initialCategory={category} />

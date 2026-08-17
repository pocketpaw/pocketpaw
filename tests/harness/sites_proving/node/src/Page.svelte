<!-- Page.svelte — the ONE server-render entry for every Paw Site.
     Created for SG-1 (sites proving harness).

     WHAT: mirrors paw-sites/templates/src/routes/+page.svelte.tmpl — a real
     `<form method="POST" action=...>` wrapping `<Ripple {spec} {onEvent} />`.

     WHY it differs from the template in exactly one way: the template imports
     the spec from `$lib/spec` — a file the generator WRITES PER SITE, which is
     what forces a per-site `bun install` + Vite build. Here `spec` arrives as a
     RUNTIME PROP, so this component compiles ONCE and every site's spec flows
     through the same prebuilt bundle. `formAction` is likewise a prop (the
     template hardcodes `/api/submit`) so the harness can prove the action
     survives into the rendered HTML.

     onEvent is imported but inert on the static path, exactly as the template
     documents: under `csr = false` no Ripple runtime reaches the browser and the
     lead form submits via a native POST. It is imported here so the once-built
     bundle is identical in shape to what the per-site build produced. -->
<script lang="ts">
  import { Ripple } from '@ripple-ui/svelte';
  import { onEvent } from './onEvent.js';

  let { spec, formAction = '/api/submit' } = $props();
</script>

<form method="POST" action={formAction}>
  <Ripple {spec} {onEvent} />
</form>

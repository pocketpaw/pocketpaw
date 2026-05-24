<!--
  RippleRenderer.svelte — RFC 06 Position 1 spike, format-aware dispatch wrapper.
  Created: 2026-05-24 (push-back D — coexistence shape).

  This is the renderer entry point a host (paw-enterprise) would use. It takes
  ONE prop — a spec that's either a nested `UISpec` or a flat `FlatSpec` — and
  routes to the right renderer path based on the spec's `format` discriminator.

  Dispatch rules:
    - `spec.format === "flat"`  → render via <FlatRenderer> directly.
    - `spec.format === "nested"` (or undefined, the back-compat default)
                                  → call flatten(spec) on entry, then render
                                    via <FlatRenderer>.

  NOTE on the nested path: a "real" production wrapper would route nested
  through the existing 284-LOC NodeRenderer (no flatten-on-entry), and only
  the flat path would use the new flat renderer. The spike doesn't have a
  full NodeRenderer port, so the nested path flattens-on-entry instead.
  That's the test-equivalent — it proves the dispatch + format-aware routing
  works, and that the same logical spec renders identically through either
  storage path. The real shape lives in findings.md §4 + §8.

  Why the discriminator exists: it lets PR-1 ship "any caller can opt into
  flat per-pocket; nothing else changes" with `format: "nested"` as the
  back-compat default. The rollback story is "flip the discriminator on the
  stored spec" — proven by dispatch.test.ts to produce identical output.
-->
<script lang="ts">
  import { flatten, type FlatSpec, type UISpec } from './flatten.ts';
  import FlatRenderer from './FlatRenderer.svelte';

  interface Props {
    spec: UISpec | FlatSpec;
  }

  let { spec }: Props = $props();

  // Resolve the format discriminator. Missing → "nested" (back-compat).
  const format = $derived<'flat' | 'nested'>(
    (spec as { format?: 'flat' | 'nested' }).format ?? 'nested',
  );

  // Build the FlatSpec the inner renderer needs. Either the input already is
  // one, or we flatten the nested input on entry.
  const flat = $derived<FlatSpec>(
    format === 'flat' ? (spec as FlatSpec) : flatten(spec as UISpec),
  );
</script>

<FlatRenderer spec={flat} componentId={flat.root} />

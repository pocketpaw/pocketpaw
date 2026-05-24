<!--
  FlatRenderer.svelte — RFC 06 Position 1 spike, renderer proof-of-concept.
  Created: 2026-05-24.

  Renders a FlatSpec by resolving child IDs against the components map and
  recursing on those, instead of recursing on a nested `node.children` array.
  This is the minimum-viable companion to NodeRenderer.svelte for the spike.

  Differences from NodeRenderer.svelte that matter for the LOC comparison:
   - Recurses on string IDs, not nested UINode objects. Lookup is O(1) in the
     components map instead of an implicit pointer-chase.
   - Drops the bind / expression / event-handler / slot logic — that machinery
     is orthogonal to the flat vs nested decision, so it lives unchanged in
     the parent (this is a *demo* renderer, not a replacement for NodeRenderer).
     The point is to show that the *core recursion* boils down to one extra
     map lookup per recursion step.

  In a real port: the existing NodeRenderer's child loops would all become
  `{#each childIds as id} <Self componentId={id} ...> {/each}` with the
  resolveValue / expression / handler / slot machinery unchanged.
-->
<script lang="ts">
  import type { FlatNode, FlatSpec } from './flatten.ts';
  import Self from './FlatRenderer.svelte';

  interface Props {
    spec: FlatSpec;
    componentId: string;
  }

  let { spec, componentId }: Props = $props();

  const node = $derived<FlatNode | undefined>(spec.components[componentId]);
  const childIds = $derived<string[]>(node?.children ?? []);
  const elseChildIds = $derived<string[]>(node?.else_children ?? []);
</script>

{#if node}
  <div data-flat-node-id={componentId} data-flat-node-type={node.type}>
    {#if node.type === 'text' && node.props}
      <span>{node.props.text}</span>
    {:else if node.type === 'heading' && node.props}
      <h3>{node.props.text}</h3>
    {:else}
      <div class="ripple-flat-node ripple-{node.type}">
        {#each childIds as id (id)}
          <Self {spec} componentId={id} />
        {/each}
        {#if elseChildIds.length > 0}
          {#each elseChildIds as id (id)}
            <Self {spec} componentId={id} />
          {/each}
        {/if}
      </div>
    {/if}
  </div>
{:else}
  <div class="ripple-flat-missing" data-missing-id={componentId}>
    [missing component {componentId}]
  </div>
{/if}

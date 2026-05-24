// flatten.test.ts — RFC 06 Position 1 spike, test suite.
// Created: 2026-05-24 — Verifies the nested→flat→nested round-trip on real
// stored pocket specs and proves merge-by-name semantics (the OpenUI shape).
// Updated: 2026-05-24 — Added a corner-case feature describe block exercising
// node-level `each.items`/`item_as`/`index_as`, node-level `if.condition`,
// `if.else_children`, and child-level `slot` against four new fixtures.
// Updated: 2026-05-24 — Push-back C. Added a `gcOrphans boundary policy`
// describe block that pins the orphan-GC lifecycle contract: orphans
// accumulate across merges, the two named boundary helpers
// (`gcOnPersist` / `gcOnSnapshot`) drop them, `merge()` never shrinks the
// components map, and pre-persist undo can re-reach an orphaned subtree.
//
// Run: bun test temp/spike/flat-model/flatten.test.ts

import { describe, expect, test } from 'bun:test';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import {
  canonicalJson,
  componentCount,
  flatten,
  gcOnPersist,
  gcOnSnapshot,
  gcOrphans,
  merge,
  nestedNodeCount,
  unflatten,
  type FlatSpec,
  type UISpec,
} from './flatten.ts';

const FIXTURES_DIR = join(import.meta.dir, 'fixtures');

function loadFixtures(): Array<{ name: string; spec: UISpec }> {
  return readdirSync(FIXTURES_DIR)
    .filter((f) => f.endsWith('.spec.json'))
    .sort()
    .map((name) => ({
      name,
      spec: JSON.parse(readFileSync(join(FIXTURES_DIR, name), 'utf-8')) as UISpec,
    }));
}

const FIXTURES = loadFixtures();

// Strip every node-level `id` field from a nested spec so we can compare a
// fixture that lacks ids to a round-trip that *minted* them. The transform's
// job after id-stamping is to be lossless on the structural shape — the ids
// themselves are an artifact of the flat model, not the original spec.
// Also strips the top-level `format` field — push-back D adds that
// discriminator, but it's a storage-format artifact stamped by the transform
// (input fixtures pre-date it), so it shouldn't fail a structural-equality
// check. The dispatch test in dispatch.test.ts asserts on `format` directly.
function stripIds(spec: UISpec): UISpec {
  function strip(node: any): any {
    if (!node || typeof node !== 'object') return node;
    const out: any = {};
    for (const k of Object.keys(node)) {
      if (k === 'id') continue;
      out[k] = Array.isArray(node[k]) ? node[k].map(strip) : strip(node[k]);
    }
    return out;
  }
  const { format: _format, ...rest } = spec as Record<string, unknown>;
  return { ...(rest as UISpec), ui: strip(spec.ui) };
}

describe('round-trip on real stored pocket specs', () => {
  for (const { name, spec } of FIXTURES) {
    test(`${name}: unflatten(flatten(spec)) preserves structure (ids may be minted)`, () => {
      const flat = flatten(spec);
      const back = unflatten(flat);
      // The transform mints ids for nodes that lack them — that's the
      // ID-stamping pass which `normalize_ripple_spec.ensure_ids` already does
      // on persist. So we compare *after* stripping ids: the structural shape
      // must round-trip losslessly even if ids were minted.
      expect(canonicalJson(stripIds(back))).toBe(canonicalJson(stripIds(spec)));
    });

    test(`${name}: every nested node lands in the flat map exactly once`, () => {
      const flat = flatten(spec);
      expect(componentCount(flat)).toBe(nestedNodeCount(spec));
    });
  }

  test('id-stable fixture round-trips byte-for-byte (no id minting needed)', () => {
    // Pick a fixture that already has ids — business-dashboard came through
    // the normalizer's ensure_ids pass. Strip only the top-level `format`
    // (round-trip stamps it; ids should remain identical so this test still
    // proves no id-minting happened).
    const fixture = FIXTURES.find((f) => f.name === 'business-dashboard.spec.json')!;
    const flat = flatten(fixture.spec);
    const back = unflatten(flat);
    const stripFormat = (s: UISpec): UISpec => {
      const { format: _f, ...rest } = s as Record<string, unknown>;
      return rest as UISpec;
    };
    expect(canonicalJson(stripFormat(back))).toBe(canonicalJson(stripFormat(fixture.spec)));
  });
});

describe('corner-case features round-trip', () => {
  // The original four fixtures were all flex / grid / leaf-widget layouts and
  // did not stress node-level `each.items`, node-level `if.condition`,
  // `if.else_children`, or child-level `slot`. These four `corner-*` fixtures
  // were synthesized to match production patterns in ripple's own tests and
  // manifest entries (see each fixture's `_comment` for its provenance).
  // `flatten` claims to support these by symmetry with `children`; this block
  // verifies that against representative spec shapes.
  const CORNER_NAMES = [
    'corner-else-children.spec.json',
    'corner-each-items.spec.json',
    'corner-if-condition.spec.json',
    'corner-slot.spec.json',
  ];

  // Walk a FlatSpec's components map; return true when `predicate` matches at
  // least one component. Used by the feature-presence assertions below — a
  // fixture that doesn't make `flatten` emit the feature would silently pass
  // the round-trip check, so we gate on the presence of the targeted field.
  function anyComponent(flat: FlatSpec, predicate: (n: any) => boolean): boolean {
    for (const id of Object.keys(flat.components)) {
      if (predicate(flat.components[id])) return true;
    }
    return false;
  }

  test('all four corner fixtures are present', () => {
    const names = FIXTURES.map((f) => f.name);
    for (const n of CORNER_NAMES) {
      expect(names).toContain(n);
    }
  });

  test('corner-else-children: flatten emits at least one node with else_children', () => {
    const { spec } = FIXTURES.find((f) => f.name === 'corner-else-children.spec.json')!;
    const flat = flatten(spec);
    expect(
      anyComponent(flat, (n) => Array.isArray(n.else_children) && n.else_children.length > 0),
    ).toBe(true);
    // Bidirectional structural fidelity, as for every other fixture.
    expect(canonicalJson(stripIds(unflatten(flat)))).toBe(canonicalJson(stripIds(spec)));
    expect(componentCount(flat)).toBe(nestedNodeCount(spec));
  });

  test('corner-each-items: flatten emits a node with items + item_as (+ optionally index_as)', () => {
    const { spec } = FIXTURES.find((f) => f.name === 'corner-each-items.spec.json')!;
    const flat = flatten(spec);
    expect(
      anyComponent(
        flat,
        (n) => n.type === 'each' && typeof n.items === 'string' && typeof n.item_as === 'string',
      ),
    ).toBe(true);
    // index_as is optional in the manifest but present in this fixture — keep
    // it as a separate assertion so a regression on that field is loud.
    expect(anyComponent(flat, (n) => n.type === 'each' && typeof n.index_as === 'string')).toBe(
      true,
    );
    expect(canonicalJson(stripIds(unflatten(flat)))).toBe(canonicalJson(stripIds(spec)));
    expect(componentCount(flat)).toBe(nestedNodeCount(spec));
  });

  test('corner-if-condition: flatten emits an `if` node with a condition and children but no else_children', () => {
    const { spec } = FIXTURES.find((f) => f.name === 'corner-if-condition.spec.json')!;
    const flat = flatten(spec);
    expect(
      anyComponent(
        flat,
        (n) =>
          n.type === 'if' &&
          typeof n.condition === 'string' &&
          Array.isArray(n.children) &&
          n.children.length > 0,
      ),
    ).toBe(true);
    // This fixture is the "no-else" case. None of its `if` nodes should carry
    // an else_children array.
    expect(
      anyComponent(
        flat,
        (n) =>
          n.type === 'if' && Array.isArray(n.else_children) && n.else_children.length > 0,
      ),
    ).toBe(false);
    expect(canonicalJson(stripIds(unflatten(flat)))).toBe(canonicalJson(stripIds(spec)));
    expect(componentCount(flat)).toBe(nestedNodeCount(spec));
  });

  test('corner-slot: flatten emits at least one component with a non-default `slot` field', () => {
    const { spec } = FIXTURES.find((f) => f.name === 'corner-slot.spec.json')!;
    const flat = flatten(spec);
    expect(
      anyComponent(
        flat,
        (n) => typeof n.slot === 'string' && n.slot.length > 0 && n.slot !== 'default',
      ),
    ).toBe(true);
    expect(canonicalJson(stripIds(unflatten(flat)))).toBe(canonicalJson(stripIds(spec)));
    expect(componentCount(flat)).toBe(nestedNodeCount(spec));
  });

  test('round-trip is lossless even after a merge that touches a node carrying `slot` / `condition` / `items`', () => {
    // Sanity check: re-emitting a corner-feature node via merge() must preserve
    // its corner field. Catches a bug where a future merge optimization drops
    // unknown keys.
    const { spec } = FIXTURES.find((f) => f.name === 'corner-each-items.spec.json')!;
    const flat = flatten(spec);
    const eachId = Object.keys(flat.components).find(
      (id) => flat.components[id].type === 'each',
    )!;
    const eachNode = flat.components[eachId];
    const patched = merge(flat, {
      components: {
        [eachId]: { ...eachNode, item_as: 'row' },
      },
    });
    expect(patched.components[eachId].items).toBe(eachNode.items);
    expect(patched.components[eachId].item_as).toBe('row');
    expect(patched.components[eachId].index_as).toBe(eachNode.index_as);
    expect(patched.components[eachId].children).toEqual(eachNode.children);
  });
});

describe('merge-by-name (OpenUI Lang shape)', () => {
  const { spec } = FIXTURES.find((f) => f.name === 'team-activity.spec.json')!;
  const base = flatten(spec);

  test('single-node prop change is one entry in the patch', () => {
    // Pick a leaf text node and re-emit it with a new prop value.
    const ids = Object.keys(base.components);
    const leafId = ids.find((id) => {
      const n = base.components[id];
      return n.type === 'text' && !n.children;
    })!;
    const leaf = base.components[leafId];
    const patched = merge(base, {
      components: {
        [leafId]: { ...leaf, props: { ...(leaf.props ?? {}), text: 'CHANGED' } },
      },
    });
    expect(patched.components[leafId].props?.text).toBe('CHANGED');
    // Patch carried exactly one component entry.
    const patchJson = JSON.stringify({
      components: {
        [leafId]: { ...leaf, props: { ...(leaf.props ?? {}), text: 'CHANGED' } },
      },
    });
    expect(Object.keys(JSON.parse(patchJson).components)).toHaveLength(1);
  });

  test('sibling reorder is one entry in the patch (the parent)', () => {
    // Find a parent with >= 2 children.
    const parentId = Object.keys(base.components).find((id) => {
      const n = base.components[id];
      return Array.isArray(n.children) && n.children.length >= 2;
    })!;
    const parent = base.components[parentId];
    const reversed = [...parent.children!].reverse();
    const patched = merge(base, {
      components: { [parentId]: { ...parent, children: reversed } },
    });
    expect(patched.components[parentId].children).toEqual(reversed);
    // The two child nodes themselves are unmentioned in the patch.
    const patchComponents = { [parentId]: { ...parent, children: reversed } };
    expect(Object.keys(patchComponents)).toHaveLength(1);
  });

  test('subtree replace via parent.children re-stating; old ids become orphans (option a)', () => {
    // Find a parent with at least one child.
    const parentId = Object.keys(base.components).find((id) => {
      const n = base.components[id];
      return Array.isArray(n.children) && n.children.length >= 1;
    })!;
    const parent = base.components[parentId];
    const oldChildId = parent.children![0];

    // Emit a NEW node and re-state the parent's children list with the new id
    // in place of the old. Per OpenUI option (a), the old subtree stays in the
    // map — unreachable but present until gcOrphans is called.
    const newId = 'n_newchild';
    const newNode = { id: newId, type: 'text', props: { text: 'NEW' } };
    const newChildren = [newId, ...parent.children!.slice(1)];

    const patched = merge(base, {
      components: {
        [newId]: newNode,
        [parentId]: { ...parent, children: newChildren },
      },
    });

    expect(patched.components[newId]).toEqual(newNode);
    expect(patched.components[parentId].children).toEqual(newChildren);
    // Orphan still present.
    expect(patched.components[oldChildId]).toBeDefined();

    // GC actually drops it.
    const compact = gcOrphans(patched);
    expect(compact.components[oldChildId]).toBeUndefined();
    expect(compact.components[newId]).toBeDefined();
    expect(compact.components[parentId]).toBeDefined();
  });

  test('unmentioned ids are kept from base', () => {
    const someUntouchedId = Object.keys(base.components)[0];
    const patched = merge(base, { components: {} });
    expect(patched.components[someUntouchedId]).toEqual(base.components[someUntouchedId]);
  });
});

describe('gcOrphans boundary policy', () => {
  // Pins the orphan-GC lifecycle contract for push-back C: orphans accumulate
  // across edits in-memory and are dropped only at two named lifecycle
  // boundaries (gcOnPersist on the server write path, gcOnSnapshot on the
  // export path). merge() never GCs; that's how cheap undo stays possible.

  const { spec: baseSpec } = FIXTURES.find((f) => f.name === 'team-activity.spec.json')!;

  // Independent reachability count — walks from root, counts every node it
  // hits. Used to prove the post-GC components map matches reachability
  // without leaning on the GC helper itself for the assertion.
  function reachableCount(spec: FlatSpec): number {
    const seen = new Set<string>();
    function walk(id: string) {
      if (seen.has(id)) return;
      const node = spec.components[id];
      if (!node) return;
      seen.add(id);
      for (const cid of node.children ?? []) walk(cid);
      for (const cid of node.else_children ?? []) walk(cid);
    }
    walk(spec.root);
    return seen.size;
  }

  // Helper — perform one subtree-replace merge on the current spec, returning
  // the new spec plus the freshly minted child id and the old child id that
  // just got orphaned. Picks the parent's current first child every iteration
  // so a chained sequence makes the count grow predictably (one new node added
  // per merge, one old node now unreachable but still in the map).
  function replaceFirstChild(
    spec: FlatSpec,
    iteration: number,
  ): { spec: FlatSpec; parentId: string; newId: string; oldChildId: string } {
    const parentId = Object.keys(spec.components).find((id) => {
      const n = spec.components[id];
      return Array.isArray(n.children) && n.children.length >= 1;
    })!;
    const parent = spec.components[parentId];
    const oldChildId = parent.children![0];
    const newId = `n_orphtest_${iteration}`;
    const newNode = { id: newId, type: 'text', props: { text: `iter-${iteration}` } };
    const newChildren = [newId, ...parent.children!.slice(1)];
    const next = merge(spec, {
      components: {
        [newId]: newNode,
        [parentId]: { ...parent, children: newChildren },
      },
    });
    return { spec: next, parentId, newId, oldChildId };
  }

  test('orphans accumulate across N merges with no GC', () => {
    let current = flatten(baseSpec);
    const startCount = componentCount(current);
    const counts: number[] = [startCount];
    const orphanedIds: string[] = [];

    for (let i = 0; i < 5; i++) {
      const step = replaceFirstChild(current, i);
      current = step.spec;
      orphanedIds.push(step.oldChildId);
      counts.push(componentCount(current));
    }

    // Strictly monotonic — every merge adds exactly one new node id and never
    // removes the just-orphaned one, so the count climbs by ≥1 each step.
    for (let i = 1; i < counts.length; i++) {
      expect(counts[i]).toBeGreaterThan(counts[i - 1]);
    }
    // Final count strictly larger than the starting count.
    expect(counts[counts.length - 1]).toBeGreaterThan(startCount);
    // Every orphaned id is STILL in the components map (the whole point of
    // option a — orphans linger until a GC boundary).
    for (const id of orphanedIds) {
      expect(current.components[id]).toBeDefined();
    }

    // After gcOnPersist the count collapses to the reachable set.
    const persisted = gcOnPersist(current);
    expect(componentCount(persisted)).toBeLessThan(componentCount(current));
  });

  test('gcOnPersist drops orphans (componentCount equals reachableCount)', () => {
    let current = flatten(baseSpec);
    for (let i = 0; i < 5; i++) {
      current = replaceFirstChild(current, i).spec;
    }
    // Pre-GC: there are unreachable nodes in the map.
    expect(componentCount(current)).toBeGreaterThan(reachableCount(current));

    const persisted = gcOnPersist(current);
    // Post-GC: every node in the map is reachable from root.
    expect(componentCount(persisted)).toBe(reachableCount(persisted));
    // And the reachable count itself is unchanged by the GC (we only drop
    // unreachable nodes, never anything addressable from root).
    expect(reachableCount(persisted)).toBe(reachableCount(current));
  });

  test('gcOnSnapshot is equivalent to gcOnPersist', () => {
    let current = flatten(baseSpec);
    for (let i = 0; i < 5; i++) {
      current = replaceFirstChild(current, i).spec;
    }
    const viaPersist = gcOnPersist(current);
    const viaSnapshot = gcOnSnapshot(current);
    expect(componentCount(viaSnapshot)).toBe(componentCount(viaPersist));
    expect(canonicalJson(viaSnapshot)).toBe(canonicalJson(viaPersist));
  });

  test('merge() does NOT call gcOrphans — componentCount never shrinks', () => {
    const start = flatten(baseSpec);
    const startCount = componentCount(start);
    const step = replaceFirstChild(start, 0);
    // Contract: a subtree-replacing merge orphans the old child but does NOT
    // drop it. The map only grows; it cannot shrink as a side effect of merge.
    expect(componentCount(step.spec)).toBeGreaterThanOrEqual(startCount);
  });

  test('pre-persist undo works — orphaned subtree is still in the map and can be re-reached', () => {
    const start = flatten(baseSpec);
    const step = replaceFirstChild(start, 0);
    const { spec: afterMerge, parentId, oldChildId, newId } = step;

    // The orphaned id is STILL addressable in the components map even though
    // it's unreachable from root. That's the data the in-memory undo stack
    // would lean on.
    expect(afterMerge.components[oldChildId]).toBeDefined();
    // And reachability says it's not in the live tree right now.
    const reachableNow = (() => {
      const seen = new Set<string>();
      function walk(id: string) {
        if (seen.has(id)) return;
        const node = afterMerge.components[id];
        if (!node) return;
        seen.add(id);
        for (const cid of node.children ?? []) walk(cid);
        for (const cid of node.else_children ?? []) walk(cid);
      }
      walk(afterMerge.root);
      return seen;
    })();
    expect(reachableNow.has(oldChildId)).toBe(false);

    // Construct the patches that represent "undo this merge": restore the
    // parent's old children array (oldChildId back as first child). The new
    // id becomes the orphan now; the old subtree is reachable again.
    const parent = afterMerge.components[parentId];
    const restoredChildren = [oldChildId, ...parent.children!.slice(1)];
    const undone = merge(afterMerge, {
      components: {
        [parentId]: { ...parent, children: restoredChildren },
      },
    });
    expect(undone.components[oldChildId]).toBeDefined();
    expect(undone.components[parentId].children![0]).toBe(oldChildId);
    expect(undone.components[newId]).toBeDefined(); // still in map, now orphan
    // Reachability check — oldChildId is back in the live tree.
    const reachableAfterUndo = (() => {
      const seen = new Set<string>();
      function walk(id: string) {
        if (seen.has(id)) return;
        const node = undone.components[id];
        if (!node) return;
        seen.add(id);
        for (const cid of node.children ?? []) walk(cid);
        for (const cid of node.else_children ?? []) walk(cid);
      }
      walk(undone.root);
      return seen;
    })();
    expect(reachableAfterUndo.has(oldChildId)).toBe(true);

    // Sanity check the contract we're documenting: had we called gcOnPersist
    // BEFORE the undo, the old child would be gone and the undo could not
    // restore it. Prove that on a parallel branch.
    const persistedBeforeUndo = gcOnPersist(afterMerge);
    expect(persistedBeforeUndo.components[oldChildId]).toBeUndefined();
  });
});

describe('measurements (byte sizes)', () => {
  test('print per-fixture nested vs flat sizes', () => {
    const rows: Array<{
      name: string;
      nestedRawBytes: number;
      nestedStampedBytes: number;
      flatBytes: number;
      nodes: number;
    }> = [];
    for (const { name, spec } of FIXTURES) {
      const nestedRawBytes = JSON.stringify(spec).length;
      // The flat form ALWAYS carries an id per node. The fair comparison is
      // against a nested form that ALSO carries an id per node — that's the
      // state after `normalize_ripple_spec.ensure_ids`, which runs on every
      // persist today. Round-trip the spec to populate any missing ids, then
      // measure the stamped nested form.
      const flat = flatten(spec);
      const nestedStamped = unflatten(flat);
      const nestedStampedBytes = JSON.stringify(nestedStamped).length;
      const flatBytes = JSON.stringify(flat).length;
      rows.push({
        name,
        nestedRawBytes,
        nestedStampedBytes,
        flatBytes,
        nodes: componentCount(flat),
      });
    }
    console.log('\n=== nested vs flat byte sizes (raw vs id-stamped nested vs flat) ===');
    for (const r of rows) {
      const dStamped = (
        ((r.flatBytes - r.nestedStampedBytes) / r.nestedStampedBytes) *
        100
      ).toFixed(1);
      console.log(
        `  ${r.name.padEnd(32)} nodes=${String(r.nodes).padStart(3)}  raw=${String(r.nestedRawBytes).padStart(6)}B  stamped=${String(r.nestedStampedBytes).padStart(6)}B  flat=${String(r.flatBytes).padStart(6)}B  flat-vs-stamped=${dStamped}%`
      );
    }
    expect(rows.length).toBeGreaterThan(0);
  });

  test('print per-mutation patch sizes for one fixture', () => {
    const { spec } = FIXTURES.find((f) => f.name === 'team-activity.spec.json')!;
    const flat = flatten(spec);
    // Pick a leaf text node for a prop change.
    const leafId = Object.keys(flat.components).find((id) => {
      const n = flat.components[id];
      return n.type === 'text' && !n.children;
    })!;
    const leaf = flat.components[leafId];

    // nested op shape mirrors spec-mutator's node_prop_set
    const nestedPropOp = {
      action: 'node_prop_set',
      node_id: leafId,
      prop: 'text',
      value: 'CHANGED',
    };
    const nestedPropBytes = JSON.stringify(nestedPropOp).length;

    // flat patch — re-emit the one node.
    const flatPropPatch: Partial<FlatSpec> = {
      components: {
        [leafId]: { ...leaf, props: { ...(leaf.props ?? {}), text: 'CHANGED' } },
      },
    };
    const flatPropBytes = JSON.stringify(flatPropPatch).length;

    // Subtree replace — nested has node_replaced + a whole subtree blob; flat
    // has the new node(s) + parent re-stated.
    const parentId = Object.keys(flat.components).find((id) => {
      const n = flat.components[id];
      return Array.isArray(n.children) && n.children.length >= 1;
    })!;
    const parent = flat.components[parentId];
    const oldChildId = parent.children![0];
    const oldChild = flat.components[oldChildId];

    // Build the nested subtree to splice in (a fresh text node).
    const newSubtreeNested = { id: 'n_newchild', type: 'text', props: { text: 'NEW' } };
    const nestedReplaceOp = {
      action: 'node_replaced',
      node_id: oldChildId,
      subtree: newSubtreeNested,
    };
    const nestedReplaceBytes = JSON.stringify(nestedReplaceOp).length;

    const flatReplacePatch: Partial<FlatSpec> = {
      components: {
        n_newchild: { id: 'n_newchild', type: 'text', props: { text: 'NEW' } },
        [parentId]: {
          ...parent,
          children: ['n_newchild', ...(parent.children ?? []).slice(1)],
        },
      },
    };
    const flatReplaceBytes = JSON.stringify(flatReplacePatch).length;

    console.log('\n=== per-mutation patch sizes (team-activity.spec.json) ===');
    console.log(
      `  prop change:    nested op = ${nestedPropBytes}B   flat patch = ${flatPropBytes}B`
    );
    console.log(
      `  subtree swap:   nested op = ${nestedReplaceBytes}B   flat patch = ${flatReplaceBytes}B`
    );
    // Sanity: both work.
    expect(nestedPropBytes).toBeGreaterThan(0);
    expect(flatPropBytes).toBeGreaterThan(0);
    // Don't reference `oldChild` only to satisfy a lint — actually use it.
    expect(oldChild).toBeDefined();
  });
});

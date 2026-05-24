// flatten.test.ts — RFC 06 Position 1 spike, test suite.
// Created: 2026-05-24 — Verifies the nested→flat→nested round-trip on real
// stored pocket specs and proves merge-by-name semantics (the OpenUI shape).
// Updated: 2026-05-24 — Added a corner-case feature describe block exercising
// node-level `each.items`/`item_as`/`index_as`, node-level `if.condition`,
// `if.else_children`, and child-level `slot` against four new fixtures.
//
// Run: bun test temp/spike/flat-model/flatten.test.ts

import { describe, expect, test } from 'bun:test';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import {
  canonicalJson,
  componentCount,
  flatten,
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
  return { ...spec, ui: strip(spec.ui) };
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
    // the normalizer's ensure_ids pass.
    const fixture = FIXTURES.find((f) => f.name === 'business-dashboard.spec.json')!;
    const flat = flatten(fixture.spec);
    const back = unflatten(flat);
    expect(canonicalJson(back)).toBe(canonicalJson(fixture.spec));
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

// flatten.ts — RFC 06 Position 1 spike.
// Pure functions that translate Ripple's nested UISpec into an OpenUI-Lang-style
// flat namespace + back, plus the merge-by-name rule that replaces the eight ops
// in spec-mutator.ts with one.
//
// Created: 2026-05-24 — Initial spike for the captain-gated flat-component-model
// experiment. Self-contained (no Svelte / Zod dep) so bun test can run it.
//
// Shape:
//   FlatNode = a UINode minus its child arrays; child arrays become string[] of ids.
//   FlatSpec = { root: id, components: { id -> FlatNode }, version?, state?, data?, sources?, theme?, meta?, actions? }
//   The patch is also a FlatSpec — merge() replaces same-name nodes, adds new-name
//   nodes, and keeps un-mentioned nodes. Orphans (unreachable from root) live in
//   the components map until you GC them (see gcOrphans).

// -------- types --------

export interface UINode {
  type: string;
  id?: string;
  props?: Record<string, unknown>;
  bind?: string;
  show?: string;
  class?: string;
  style?: Record<string, string>;
  slot?: string;
  on_click?: unknown;
  on_change?: unknown;
  on_input?: unknown;
  on_submit?: unknown;
  on_focus?: unknown;
  on_blur?: unknown;
  items?: string;
  item_as?: string;
  index_as?: string;
  condition?: string;
  children?: UINode[];
  else_children?: UINode[];
  // Permissive — other `on_*` keys (on_close, on_open_change, …) are passed
  // through verbatim by NodeRenderer; mirror that here.
  [key: string]: unknown;
}

export interface UISpec {
  version?: string;
  state?: Record<string, unknown>;
  data?: Record<string, unknown>;
  sources?: Record<string, unknown>;
  ui: UINode;
  theme?: Record<string, unknown>;
  meta?: Record<string, unknown>;
  actions?: Record<string, unknown>;
  [key: string]: unknown;
}

// A flat node carries the original node's fields verbatim except that the two
// child arrays become string id arrays. Keeping the field names identical is
// deliberate — it makes the renderer change in FlatRenderer trivial (look up
// the id, recurse).
export interface FlatNode {
  type: string;
  id: string; // required in flat form — that's the whole point
  props?: Record<string, unknown>;
  bind?: string;
  show?: string;
  class?: string;
  style?: Record<string, string>;
  slot?: string;
  on_click?: unknown;
  on_change?: unknown;
  on_input?: unknown;
  on_submit?: unknown;
  on_focus?: unknown;
  on_blur?: unknown;
  items?: string;
  item_as?: string;
  index_as?: string;
  condition?: string;
  children?: string[];
  else_children?: string[];
  [key: string]: unknown;
}

export interface FlatSpec {
  version?: string;
  state?: Record<string, unknown>;
  data?: Record<string, unknown>;
  sources?: Record<string, unknown>;
  theme?: Record<string, unknown>;
  meta?: Record<string, unknown>;
  actions?: Record<string, unknown>;
  root: string;
  components: Record<string, FlatNode>;
  [key: string]: unknown;
}

// -------- id mint (mirrors spec-id.ts shape) --------

const ALPHABET = 'abcdefghijklmnopqrstuvwxyz0123456789';
const ID_LEN = 8;
let _idCounter = 0;

function newId(): string {
  // Deterministic-ish for tests: counter + a pinch of randomness, prefixed `n_`.
  let out = 'n_';
  const seed = (++_idCounter).toString(36).padStart(2, '0');
  for (let i = 0; i < ID_LEN; i++) {
    if (i < seed.length) {
      out += seed[i];
    } else {
      out += ALPHABET[Math.floor(Math.random() * ALPHABET.length)];
    }
  }
  return out;
}

// -------- flatten --------

/** Walk the nested tree; assign ids where missing; emit FlatSpec. */
export function flatten(spec: UISpec): FlatSpec {
  const components: Record<string, FlatNode> = {};
  const rootId = flattenNode(spec.ui, components, new Set());

  const out: FlatSpec = {
    root: rootId,
    components,
  };
  // Copy top-level fields except `ui` (replaced by root/components).
  for (const key of Object.keys(spec)) {
    if (key === 'ui') continue;
    out[key] = (spec as Record<string, unknown>)[key];
  }
  return out;
}

function flattenNode(
  node: UINode,
  components: Record<string, FlatNode>,
  seen: Set<string>
): string {
  // Mint or reuse the id.
  let id = node.id;
  if (!id || seen.has(id)) {
    id = newId();
  }
  seen.add(id);

  // Build the flat node — copy every field except the child arrays, then
  // replace those with id arrays.
  const flat: FlatNode = { ...(node as Record<string, unknown>), id, type: node.type } as FlatNode;
  delete flat.children;
  delete flat.else_children;

  if (Array.isArray(node.children) && node.children.length > 0) {
    flat.children = node.children.map((c) => flattenNode(c, components, seen));
  }
  if (Array.isArray(node.else_children) && node.else_children.length > 0) {
    flat.else_children = node.else_children.map((c) => flattenNode(c, components, seen));
  }

  components[id] = flat;
  return id;
}

// -------- unflatten --------

/** Reverse — reconstruct the nested UISpec from a FlatSpec. */
export function unflatten(flat: FlatSpec): UISpec {
  const ui = unflattenNode(flat.root, flat.components, new Set());
  const out: UISpec = { ui };
  for (const key of Object.keys(flat)) {
    if (key === 'root' || key === 'components') continue;
    out[key] = (flat as Record<string, unknown>)[key];
  }
  return out;
}

function unflattenNode(
  id: string,
  components: Record<string, FlatNode>,
  cycleGuard: Set<string>
): UINode {
  if (cycleGuard.has(id)) {
    throw new Error(`flat spec contains a cycle through ${id}`);
  }
  const flat = components[id];
  if (!flat) {
    throw new Error(`flat spec references unknown component id ${id}`);
  }
  cycleGuard.add(id);

  const node: UINode = { ...(flat as Record<string, unknown>) } as UINode;
  // Remove the flat-only `id` from the position where it lived; wait — UINode
  // does carry an optional id, and we want to keep it. So leave it.
  delete (node as Record<string, unknown>).children;
  delete (node as Record<string, unknown>).else_children;

  if (Array.isArray(flat.children) && flat.children.length > 0) {
    node.children = flat.children.map((cid) => unflattenNode(cid, components, new Set(cycleGuard)));
  }
  if (Array.isArray(flat.else_children) && flat.else_children.length > 0) {
    node.else_children = flat.else_children.map((cid) =>
      unflattenNode(cid, components, new Set(cycleGuard))
    );
  }

  return node;
}

// -------- merge-by-name --------

/**
 * OpenUI-style merge. `patch` is a partial FlatSpec (typically a partial
 * `components` map). For each id in patch.components:
 *   - same id present in base.components → replace wholesale
 *   - new id → added to the map
 *   - unmentioned id → kept from base
 * Top-level fields on the patch (version, state, root, theme, …) replace the
 * base's only if the patch sets them. `root` change is a re-root.
 */
export function merge(base: FlatSpec, patch: Partial<FlatSpec>): FlatSpec {
  const out: FlatSpec = {
    ...base,
    components: { ...base.components },
  };

  if (patch.components) {
    for (const id of Object.keys(patch.components)) {
      out.components[id] = patch.components[id];
    }
  }

  // Top-level scalar/dict fields replace if present.
  for (const key of Object.keys(patch)) {
    if (key === 'components') continue;
    if (patch[key] !== undefined) {
      (out as Record<string, unknown>)[key] = patch[key];
    }
  }
  return out;
}

/**
 * Garbage-collect components unreachable from `root`. Returns a new FlatSpec.
 * NOT called by merge — the OpenUI default is to keep orphans (option a). Use
 * this when you actually want to compact storage.
 */
export function gcOrphans(spec: FlatSpec): FlatSpec {
  const reachable = new Set<string>();
  function walk(id: string) {
    if (reachable.has(id)) return;
    reachable.add(id);
    const node = spec.components[id];
    if (!node) return;
    for (const cid of node.children ?? []) walk(cid);
    for (const cid of node.else_children ?? []) walk(cid);
  }
  walk(spec.root);

  const components: Record<string, FlatNode> = {};
  for (const id of reachable) {
    if (spec.components[id]) components[id] = spec.components[id];
  }
  return { ...spec, components };
}

// -------- utilities for measurement / preview --------

/** Total component count (including orphans). */
export function componentCount(spec: FlatSpec): number {
  return Object.keys(spec.components).length;
}

/** Walk the nested tree and count nodes. */
export function nestedNodeCount(spec: UISpec): number {
  let n = 0;
  function walk(node: UINode | undefined) {
    if (!node) return;
    n++;
    for (const c of node.children ?? []) walk(c);
    for (const c of node.else_children ?? []) walk(c);
  }
  walk(spec.ui);
  return n;
}

/** Deep-equal helper for round-trip tests (orders keys by sorting). */
export function canonicalJson(value: unknown): string {
  return JSON.stringify(canonicalize(value));
}

function canonicalize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalize);
  if (value && typeof value === 'object') {
    const out: Record<string, unknown> = {};
    for (const key of Object.keys(value).sort()) {
      out[key] = canonicalize((value as Record<string, unknown>)[key]);
    }
    return out;
  }
  return value;
}

// streaming-sim.ts — RFC 06 Position 1 spike, push-back B (streaming delta).
// Created: 2026-05-24 — Deterministic stream-position simulator. Measures, for
// a given UISpec (nested) or FlatSpec (flat-DFS / flat-insertion serialization
// orders), the renderable-node count at any byte offset X in the wire stream.
//
// What "renderable" means here:
//   - Nested: a node is renderable at offset X iff its closing `}` lands at
//     byte ≤ X. Parent linkage is structural — once a child object closes,
//     the JSON containment already places it inside the parent's children
//     array.
//   - Flat: a component is renderable at offset X iff (a) its `{...}` value
//     in the components map closed at byte ≤ X AND (b) it is reachable from
//     `root` via a chain p0=root → p1 → ... → pk where each p_{i+1}'s id
//     string in p_i's `children` (or `else_children`) array has closed at
//     byte ≤ X. The root is reachable iff the `"root":"<id>"` top-level
//     string has closed.
//
// The serializer is custom (not JSON.stringify) so it can record byte offsets
// inline. JSON.stringify's output is not byte-offset-indexed for free, and
// we don't want to re-parse for offsets — the serializer IS the source of
// truth for the wire bytes.
//
// Pure functions, no Svelte, no deps beyond the spike's existing flatten.ts.
// Run via streaming.test.ts.

import type { FlatNode, FlatSpec, UISpec, UINode } from './flatten.ts';
import { flatten } from './flatten.ts';

// -------- types --------

export type Format = 'nested' | 'flat-dfs' | 'flat-insertion' | 'flat-bfs';

/** A single wire serialization with the byte offsets where each renderable
 *  unit closes. */
export interface SerializedWire {
  format: Format;
  /** The full wire bytes (UTF-16 length === byte length for ASCII; the
   *  fixtures are ASCII-only, so we treat string length as byte length.
   *  If the corpus ever includes non-ASCII, swap to Buffer.byteLength. */
  text: string;
  /** Byte length. */
  totalBytes: number;
  /** Total number of components / nodes in this wire. */
  totalUnits: number;
  /** Per-unit close offsets. For nested: keyed by traversal path (e.g.
   *  "ui/children/0/children/1"). For flat: keyed by component id. The
   *  number is the byte offset of the closing `}` (1-past-the-last-byte). */
  unitClose: Map<string, number>;
  /** Reachability marker offsets — only populated for flat formats.
   *  For each parent id, maps each child id (drawn from the parent's
   *  `children` or `else_children` array) to the byte offset where that
   *  child id's string entry in the parent's array closed (the byte just
   *  past the closing `"`). For nested this is unused; parent linkage is
   *  structural. */
  childLinkClose: Map<string, Map<string, number>>;
  /** For flat only: the byte offset at which the top-level `"root":"<id>"`
   *  value string closed. Before that point, even the root is unreachable.
   *  For nested: undefined (the ui node is structurally the root). */
  rootLinkClose?: number;
  /** For flat only: the root's id, drawn from FlatSpec.root. */
  rootId?: string;
}

// -------- helpers --------

/** Strip the `_comment` top-level key from a fixture's loaded JSON. The
 *  corner-* fixtures carry one for provenance; it's not part of the spec. */
export function stripComment<T extends Record<string, unknown>>(spec: T): T {
  if ('_comment' in spec) {
    const { _comment: _c, ...rest } = spec;
    return rest as T;
  }
  return spec;
}

// -------- custom serializer with offset tracking --------

/** A tiny string-builder that knows its current length in bytes. Exposed so
 *  the serializer can emit and consult position in one place. */
class Wire {
  buf = '';
  len(): number {
    return this.buf.length;
  }
  push(s: string): void {
    this.buf += s;
  }
}

/** JSON.stringify-equivalent for a value (string/number/bool/null/array/object)
 *  that ALSO calls `onObjectClose(path)` after writing the closing `}` of any
 *  object value and `onArrayClose(path)` after the closing `]` of any array.
 *  `onValueClose(path)` fires for any value (including scalars). Paths are
 *  built from the caller — we just hand them back at close time. */
function emitValue(
  wire: Wire,
  value: unknown,
  path: string[],
  hooks: {
    onObjectClose?: (path: string[], endOffset: number) => void;
    onArrayClose?: (path: string[], endOffset: number) => void;
    onValueClose?: (path: string[], endOffset: number, kind: 'object' | 'array' | 'scalar' | 'string') => void;
    onObjectKey?: (path: string[], key: string, startOffset: number) => void;
    onArrayElement?: (path: string[], indexInParent: number, value: unknown, startOffset: number, endOffset: number) => void;
  }
): void {
  if (value === null) {
    wire.push('null');
    hooks.onValueClose?.(path, wire.len(), 'scalar');
    return;
  }
  if (typeof value === 'string') {
    wire.push(JSON.stringify(value));
    hooks.onValueClose?.(path, wire.len(), 'string');
    return;
  }
  if (typeof value === 'number' || typeof value === 'boolean') {
    wire.push(String(value));
    hooks.onValueClose?.(path, wire.len(), 'scalar');
    return;
  }
  if (Array.isArray(value)) {
    wire.push('[');
    for (let i = 0; i < value.length; i++) {
      if (i > 0) wire.push(',');
      const start = wire.len();
      emitValue(wire, value[i], [...path, String(i)], hooks);
      hooks.onArrayElement?.(path, i, value[i], start, wire.len());
    }
    wire.push(']');
    hooks.onArrayClose?.(path, wire.len());
    hooks.onValueClose?.(path, wire.len(), 'array');
    return;
  }
  if (typeof value === 'object') {
    wire.push('{');
    const keys = Object.keys(value as Record<string, unknown>);
    for (let i = 0; i < keys.length; i++) {
      const k = keys[i];
      if (i > 0) wire.push(',');
      const keyStart = wire.len();
      wire.push(JSON.stringify(k));
      wire.push(':');
      hooks.onObjectKey?.(path, k, keyStart);
      emitValue(wire, (value as Record<string, unknown>)[k], [...path, k], hooks);
    }
    wire.push('}');
    hooks.onObjectClose?.(path, wire.len());
    hooks.onValueClose?.(path, wire.len(), 'object');
    return;
  }
  // Fallback — shouldn't trigger on UISpec/FlatSpec.
  wire.push('null');
  hooks.onValueClose?.(path, wire.len(), 'scalar');
}

// -------- nested serialization --------

/** Serialize a nested UISpec. Reports objectClose for every node under
 *  `ui` keyed by the node's traversal path ("ui", "ui/children/0", ...). */
export function serializeNested(spec: UISpec): SerializedWire {
  const wire = new Wire();
  const unitClose = new Map<string, number>();
  // Order the top-level keys deterministically: put `ui` after the leading
  // metadata, mirroring how today's stored specs look (version, state, data,
  // ui, …). We use the spec's own key insertion order which already follows
  // that pattern in our fixtures.
  emitValue(wire, spec as unknown as Record<string, unknown>, [], {
    onObjectClose: (path, endOffset) => {
      // Only track nodes under `ui` — top-level shell isn't a "node."
      if (path.length === 0) return;
      if (path[0] !== 'ui') return;
      // Skip object children that aren't structural nodes (e.g. `props`,
      // `style`, `lifecycle`). Structural nodes are objects DIRECTLY under
      // `ui` or under a `children` / `else_children` array slot.
      if (!isNestedNodePath(path)) return;
      unitClose.set(path.join('/'), endOffset);
    },
  });

  return {
    format: 'nested',
    text: wire.buf,
    totalBytes: wire.len(),
    totalUnits: unitClose.size,
    unitClose,
    childLinkClose: new Map(),
  };
}

/** A traversal path is a "node" iff it is `ui` or it walks down only via
 *  `(children|else_children)/<index>` segment pairs from `ui`. Paths that
 *  dip through `props` / `style` / arbitrary other keys (e.g. the inner
 *  UINode inside `tabs[i].content`) are NOT counted as nodes — flatten()
 *  doesn't recurse into them, and the renderer wouldn't treat them as
 *  paintable subtrees at the same granularity. Keeping the two counters
 *  aligned makes "renderable %" comparable across nested and flat. */
function isNestedNodePath(path: string[]): boolean {
  if (path.length === 0) return false;
  if (path[0] !== 'ui') return false;
  if (path.length === 1) return true;
  // After "ui", path must be pairs of (children|else_children, digit).
  if ((path.length - 1) % 2 !== 0) return false;
  for (let i = 1; i < path.length; i += 2) {
    if (path[i] !== 'children' && path[i] !== 'else_children') return false;
    if (!/^\d+$/.test(path[i + 1])) return false;
  }
  return true;
}

// -------- flat serialization --------

interface FlatSerializeOptions {
  /** Order in which to emit components. */
  strategy: 'dfs' | 'insertion' | 'bfs';
  /** The top-level keys to emit BEFORE `components`. By default we emit a
   *  small canonical envelope (root first, then anything else that isn't
   *  components / format). This roughly matches what an SSE emitter would
   *  send first to give the renderer the most actionable bytes early. */
  envelopeOrder?: 'root-first' | 'as-inserted';
}

/** Serialize a FlatSpec with a configurable component-emission order. */
export function serializeFlat(spec: FlatSpec, options: FlatSerializeOptions): SerializedWire {
  const wire = new Wire();
  const unitClose = new Map<string, number>();
  const childLinkClose = new Map<string, Map<string, number>>();
  let rootLinkClose: number | undefined;

  const orderedComponentIds = orderComponentIds(spec, options.strategy);

  // Top-level key order: root, version, state, data, sources, theme, meta,
  // actions, components. format is dropped on the wire (it's metadata and
  // duplicating it would just add bytes). Put root first so the renderer
  // knows the root id immediately.
  const topLevelOrder = ['root', 'version', 'state', 'data', 'sources', 'theme', 'meta', 'actions'];
  const presentTop: string[] = [];
  for (const k of topLevelOrder) {
    if (k in spec) presentTop.push(k);
  }
  // Any other extra top-level keys (excluding components, format) we append
  // in insertion order, for forward-compat.
  for (const k of Object.keys(spec)) {
    if (k === 'components' || k === 'format') continue;
    if (!presentTop.includes(k)) presentTop.push(k);
  }

  wire.push('{');
  // Emit top-level fields.
  for (let i = 0; i < presentTop.length; i++) {
    const k = presentTop[i];
    if (i > 0) wire.push(',');
    wire.push(JSON.stringify(k));
    wire.push(':');
    if (k === 'root') {
      const rootId = spec.root;
      wire.push(JSON.stringify(rootId));
      rootLinkClose = wire.len();
    } else {
      // Plain emission for other envelope fields.
      emitValue(wire, (spec as Record<string, unknown>)[k], [k], {});
    }
  }

  // Now emit the components map.
  if (presentTop.length > 0) wire.push(',');
  wire.push(JSON.stringify('components'));
  wire.push(':');
  wire.push('{');
  for (let i = 0; i < orderedComponentIds.length; i++) {
    const id = orderedComponentIds[i];
    const node = spec.components[id];
    if (!node) continue; // Defensive — shouldn't happen on well-formed specs.
    if (i > 0) wire.push(',');
    wire.push(JSON.stringify(id));
    wire.push(':');
    // Emit the node value. Within this emission, track when `children` /
    // `else_children` array entries close — each entry is a string id, so
    // its close offset is the byte after the closing `"`.
    const linkMap = new Map<string, number>();
    childLinkClose.set(id, linkMap);
    emitValue(wire, node as unknown as Record<string, unknown>, ['components', id], {
      onValueClose: (path, endOffset, kind) => {
        // We care about string values that are entries of children /
        // else_children arrays directly on THIS node (not nested deeper).
        // The path looks like ['components', id, 'children', '0'] or
        // ['components', id, 'else_children', '2'] — length 4, with
        // path[2] in {children, else_children} and path[3] numeric.
        if (kind !== 'string') return;
        if (path.length !== 4) return;
        if (path[0] !== 'components' || path[1] !== id) return;
        if (path[2] !== 'children' && path[2] !== 'else_children') return;
        if (!/^\d+$/.test(path[3])) return;
        const childId = (node as FlatNode)[path[2] as 'children' | 'else_children']?.[
          Number(path[3])
        ];
        if (typeof childId === 'string') {
          linkMap.set(childId, endOffset);
        }
      },
      onObjectClose: (path, endOffset) => {
        // The component value itself closes when we get back to
        // ['components', id] — length 2, second segment is the id.
        if (path.length === 2 && path[0] === 'components' && path[1] === id) {
          unitClose.set(id, endOffset);
        }
      },
    });
  }
  wire.push('}'); // close components
  wire.push('}'); // close top-level

  const fmt: Format =
    options.strategy === 'dfs' ? 'flat-dfs' : options.strategy === 'bfs' ? 'flat-bfs' : 'flat-insertion';
  return {
    format: fmt,
    text: wire.buf,
    totalBytes: wire.len(),
    totalUnits: orderedComponentIds.length,
    unitClose,
    childLinkClose,
    rootLinkClose,
    rootId: spec.root,
  };
}

/** Compute the emission order for components under a given strategy. */
function orderComponentIds(spec: FlatSpec, strategy: 'dfs' | 'insertion' | 'bfs'): string[] {
  const allIds = Object.keys(spec.components);
  if (strategy === 'insertion') return allIds;

  const seen = new Set<string>();
  const out: string[] = [];

  if (strategy === 'dfs') {
    function visit(id: string): void {
      if (seen.has(id)) return;
      seen.add(id);
      out.push(id);
      const node = spec.components[id];
      if (!node) return;
      for (const cid of node.children ?? []) visit(cid);
      for (const cid of node.else_children ?? []) visit(cid);
    }
    visit(spec.root);
  } else if (strategy === 'bfs') {
    const queue: string[] = [spec.root];
    while (queue.length > 0) {
      const id = queue.shift()!;
      if (seen.has(id)) continue;
      seen.add(id);
      out.push(id);
      const node = spec.components[id];
      if (!node) continue;
      for (const cid of node.children ?? []) queue.push(cid);
      for (const cid of node.else_children ?? []) queue.push(cid);
    }
  }

  // Append any orphans (unreachable from root) at the tail in their
  // insertion order. The fixtures don't have orphans, but this keeps
  // totalUnits === Object.keys(components).length.
  for (const id of allIds) {
    if (!seen.has(id)) out.push(id);
  }
  return out;
}

// -------- renderable-set computation --------

/** Compute the set of renderable unit ids/paths for a given wire at a given
 *  byte offset. */
export function renderableAt(wire: SerializedWire, byteOffset: number): Set<string> {
  if (wire.format === 'nested') {
    return renderableNestedAt(wire, byteOffset);
  }
  return renderableFlatAt(wire, byteOffset);
}

function renderableNestedAt(wire: SerializedWire, byteOffset: number): Set<string> {
  const out = new Set<string>();
  for (const [path, closeOffset] of wire.unitClose) {
    if (closeOffset <= byteOffset) out.add(path);
  }
  return out;
}

function renderableFlatAt(wire: SerializedWire, byteOffset: number): Set<string> {
  // Compute reachability transitively. A component is reachable at offset X
  // iff (a) its value-close offset ≤ X AND (b) the chain root → … → it has
  // every link's child-string-close ≤ X.
  const out = new Set<string>();
  if (wire.rootId == null || wire.rootLinkClose == null) return out;
  if (wire.rootLinkClose > byteOffset) return out;
  // BFS from root.
  const queue: string[] = [];
  const seen = new Set<string>();
  // Root itself: must have its own value closed.
  const rootClose = wire.unitClose.get(wire.rootId);
  if (rootClose == null || rootClose > byteOffset) {
    // Root value not yet arrived — nothing renderable.
    return out;
  }
  out.add(wire.rootId);
  queue.push(wire.rootId);
  seen.add(wire.rootId);
  while (queue.length > 0) {
    const parent = queue.shift()!;
    const links = wire.childLinkClose.get(parent);
    if (!links) continue;
    for (const [child, linkClose] of links) {
      if (linkClose > byteOffset) continue; // Parent's array hasn't named this child yet.
      if (seen.has(child)) continue;
      const childClose = wire.unitClose.get(child);
      if (childClose == null || childClose > byteOffset) continue; // Child value not arrived.
      seen.add(child);
      out.add(child);
      queue.push(child);
    }
  }
  return out;
}

// -------- curve sampling --------

export interface CurvePoint {
  format: Format;
  fixture: string;
  totalBytes: number;
  totalUnits: number;
  byteOffset: number;
  byteOffsetPct: number;
  renderableCount: number;
  renderablePct: number;
}

/** Sample the renderable curve at the given byte-offset percentages.
 *  Percentages are 0..100. Returns one row per (format, percentage). */
export function sampleCurve(
  fixture: string,
  wire: SerializedWire,
  percentages: number[],
): CurvePoint[] {
  const out: CurvePoint[] = [];
  for (const pct of percentages) {
    const byteOffset = Math.floor((wire.totalBytes * pct) / 100);
    const renderable = renderableAt(wire, byteOffset);
    out.push({
      format: wire.format,
      fixture,
      totalBytes: wire.totalBytes,
      totalUnits: wire.totalUnits,
      byteOffset,
      byteOffsetPct: pct,
      renderableCount: renderable.size,
      renderablePct: wire.totalUnits === 0 ? 0 : (renderable.size / wire.totalUnits) * 100,
    });
  }
  return out;
}

/** Find the smallest byte offset at which at least N units are renderable.
 *  Uses a binary-search-equivalent scan over the unique close offsets.
 *  Returns -1 if N can never be reached. */
export function firstOffsetForRenderableCount(wire: SerializedWire, n: number): number {
  if (n <= 0) return 0;
  if (n > wire.totalUnits) return -1;
  // Collect candidate offsets — the set of all per-unit close offsets plus
  // (for flat) all child-link close offsets. We test each in ascending order
  // and return the first one that yields ≥ n renderable.
  const candidates = new Set<number>();
  for (const off of wire.unitClose.values()) candidates.add(off);
  if (wire.format !== 'nested') {
    if (wire.rootLinkClose != null) candidates.add(wire.rootLinkClose);
    for (const links of wire.childLinkClose.values()) {
      for (const off of links.values()) candidates.add(off);
    }
  }
  const sorted = [...candidates].sort((a, b) => a - b);
  for (const off of sorted) {
    const renderable = renderableAt(wire, off);
    if (renderable.size >= n) return off;
  }
  // Fallback — should never trigger because totalBytes always reaches all.
  return wire.totalBytes;
}

/** Find the byte offset at which the FIRST renderable unit appears (TTFP). */
export function ttfpOffset(wire: SerializedWire): number {
  return firstOffsetForRenderableCount(wire, 1);
}

// -------- top-level API for tests --------

/** Build all three wire serializations for a single spec. */
export function buildAllWires(spec: UISpec): { nested: SerializedWire; flatDfs: SerializedWire; flatInsertion: SerializedWire } {
  const nested = serializeNested(spec);
  const flatSpec = flatten(spec);
  const flatDfs = serializeFlat(flatSpec, { strategy: 'dfs' });
  const flatInsertion = serializeFlat(flatSpec, { strategy: 'insertion' });
  return { nested, flatDfs, flatInsertion };
}

/** Walks the nested spec; counts every node in the ui subtree. Used to
 *  cross-check totalUnits against the nested serializer's tracking. */
export function nestedUiNodeCount(spec: UISpec): number {
  let n = 0;
  function walk(node: UINode | undefined): void {
    if (!node) return;
    n++;
    for (const c of node.children ?? []) walk(c);
    for (const c of node.else_children ?? []) walk(c);
  }
  walk(spec.ui);
  return n;
}

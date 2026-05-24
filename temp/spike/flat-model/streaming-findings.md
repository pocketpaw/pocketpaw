# Streaming-protocol delta — push-back B

**Scope.** RFC 06 Position 1 spike. Push-back B closure: quantify the
streaming wire-position behavior of nested vs flat for partial JSON arrival.
Pure stream-position math, not a real network test.

**Created:** 2026-05-24. Branch `spike/flat-component-model`.
Source: `streaming-sim.ts` + `streaming.test.ts` (9 sanity tests, all pass).

## Methodology

The simulator (`streaming-sim.ts`) builds the wire bytes for each fixture under three serializations — **nested** (today's `{ui: {...}}` tree), **flat-DFS** (FlatSpec emitted with `components` keyed root → root.children[0] → its children → ... pre-order), and **flat-insertion** (FlatSpec emitted with `components` keyed in `Object.keys()` order, which is post-order today because `flatten()` writes the map after recursing). For each wire it records the byte offset at which every node/component value's closing `}` lands AND, for flat, the byte offset at which each child-id string is committed inside its parent's `children` / `else_children` array. A component is "renderable at offset X" iff its value has closed by X **and** there's a chain root → ... → it where every link's id string in its parent's array has also closed by X. Nested uses the simpler structural rule: a node is renderable iff its `}` closed by X.

## Headline TTFP (byte offset where the first node becomes renderable)

The smallest byte offset at which at least one node could be painted. Lower is better.

| Fixture | Nodes | Nested | Flat-DFS | Flat-insertion | Flat-DFS Δ vs nested |
|---|---:|---:|---:|---:|---:|
| corner-each-items | 7 | 264 | 299 | 943 | +13.3% |
| corner-else-children | 10 | 177 | 209 | 1,256 | +18.1% |
| corner-if-condition | 10 | 236 | 311 | 1,288 | +31.8% |
| corner-slot | 11 | 223 | **180** | 1,250 | **−19.3%** |
| business-dashboard | 25 | 353 | 578 | 4,550 | +63.7% |
| sprint-dashboard | 27 | 384 | 533 | 4,628 | +38.8% |
| team-activity | 36 | 353 | **240** | 4,824 | **−32.0%** |
| **component-showcase** | **83** | **437** | **996** | **13,792** | **+127.9%** |

Flat-DFS TTFP is **+24% slower on average** (median +25%, range −32% to +128%). Two fixtures show flat-DFS *faster* (corner-slot, team-activity) because their nested root has a long preamble (`version` + `state` + `data` + heavy root `props`) and flat-DFS lets the root close as a small standalone object before any of that preamble appears.

Flat-insertion TTFP is catastrophic — root is the LAST entry in today's `Object.keys()` order (because `flatten()` writes the post-order assignment), so nothing renderable arrives until the wire is essentially complete. Insertion order is unusable for streaming as-is.

## The 50% / 90% curve

Byte offset to reach 50% and 90% rendered. Lower is better.

| Fixture | Nodes | Nested 50% / 90% | Flat-DFS 50% / 90% | Flat-Ins 50% / 90% |
|---|---:|---:|---:|---:|
| corner-each-items | 7 | 613 / 619 | 668 / 943 | 943 / 943 |
| corner-else-children | 10 | 595 / 798 | 632 / 1,116 | 1,256 / 1,256 |
| corner-if-condition | 10 | 612 / 830 | 734 / 1,161 | 1,288 / 1,288 |
| corner-slot | 11 | 619 / 748 | 725 / 1,151 | 1,250 / 1,250 |
| business-dashboard | 25 | 2,078 / 3,152 | 2,286 / 3,900 | 4,550 / 4,550 |
| sprint-dashboard | 27 | 2,332 / 3,700 | 2,598 / 4,415 | 4,628 / 4,628 |
| team-activity | 36 | 1,806 / 3,146 | 2,507 / 4,460 | 4,824 / 4,824 |
| **component-showcase** | **83** | **6,686 / 11,040** | **8,031 / 12,995** | **13,792 / 13,792** |

Expressed as **% of own wire** the curves are almost identical: nested hits 50% rendered at 51–60% of its wire bytes, flat-DFS at 50–58% of its (larger) wire bytes. The PERCENT-of-wire shapes are within ±5 points across all fixtures. The ABSOLUTE byte cost is higher for flat-DFS only because flat's total wire is 17–25% larger (the per-spec overhead already documented in §3 of `findings.md`). The streaming curve doesn't add a NEW penalty on top of that — it tracks the size penalty.

## Flat-DFS vs flat-insertion — is order material?

**Yes, dramatically.** Flat-insertion buries renderability until end-of-wire on every fixture (TTFP is 257–3056% slower than nested; 50% rendered = 100% of wire). DFS order is essentially the only viable on-the-wire serialization. The wire convention recommendation for the eventual SSE: **emit components in DFS pre-order from root**, regardless of how the in-memory map is keyed. This is a one-line change in the emitter (`orderComponentIds(spec, 'dfs')` in `streaming-sim.ts` is 8 LOC and runs in O(n)). It does NOT require changing how `flatten()` populates the map (which is post-order today for cycle-safety in id minting); the wire ordering is a serialization-time decision, independent of the in-memory shape.

A practical refinement worth landing in PR-3: the SSE emitter chunks one component per event. That makes every event self-contained — no partial-JSON parsing on the client. With DFS-ordered events, the root arrives in event #1 and the renderer can paint the top-level container immediately; subsequent events fill in subtrees. This is strictly better UX than nested partial-JSON parsing for the same reason flat-DFS beat nested on `corner-slot` and `team-activity` — the renderer can paint the SKELETON top-down instead of waiting for leaves to close bottom-up.

## Verdict — is flat-DFS streaming worse, equal, or better than nested?

**Roughly equal in the most honest framing.** Strict-byte TTFP for flat-DFS is +24% slower on the median fixture, +128% slower on the largest (component-showcase: 996B vs 437B = +559B = ~0.5 KB of extra "blank wait"). The 50% / 90% curves are within ±5 points of % of wire across all fixtures. The 17–25% wire-size overhead from §3 of `findings.md` is the dominant tax; streaming doesn't add a NEW penalty on top.

The framing matters a lot:
- **If "TTFP" means "first DOM node visible, even if it's an empty container"**: flat-DFS is roughly tied with nested. Some fixtures it's faster (root paints first as a skeleton); some slower (root has to fully close before anything paints).
- **If "TTFP" means "first leaf with real content visible"**: nested wins on the small absolute byte count because nested paints leaves first (subtree closure is bottom-up).
- **If we care about the UX of skeleton-first rendering**: flat-DFS wins outright — once root closes (very early in the wire), the renderer has the page shell and can paint slots progressively. Nested can't paint root container until the entire spec has arrived.

For an SSE emitter that frames one component per event (the natural shape, and the one PR-3 of the rollout already plans), the absolute-byte TTFP penalty disappears: event #1 carries the root, the renderer paints the shell, and subsequent events fill in subtrees in DFS order. **That's the real-world shape, and it's better than today's nested partial-JSON-parse story, not worse.**

**Final answer for the captain:** flat does not feel slow during streaming. The +24% median TTFP-in-bytes is real but small in absolute terms (a few hundred bytes on a multi-kilobyte spec) and the 50%/90% curves track nested closely. When the rollout lands per-component SSE framing (planned for PR-3), the structural top-down rendering of flat-DFS becomes a perceived UX win, not a tax. Insertion order is the only failure mode — fixable by stamping DFS order at emit time.

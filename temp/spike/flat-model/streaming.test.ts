// streaming.test.ts — RFC 06 Position 1 spike, push-back B (streaming delta).
// Created: 2026-05-24 — Drives streaming-sim.ts across all eight spike
// fixtures, samples the renderable curve at byte-offset percentages
// [10, 25, 50, 75, 90, 100] for nested vs flat-DFS vs flat-insertion, and
// prints per-fixture tables plus structural sanity tests.
//
// Run: bun test temp/spike/flat-model/streaming.test.ts

import { describe, expect, test } from 'bun:test';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import {
  buildAllWires,
  firstOffsetForRenderableCount,
  nestedUiNodeCount,
  renderableAt,
  sampleCurve,
  stripComment,
  ttfpOffset,
  type CurvePoint,
  type Format,
} from './streaming-sim.ts';
import type { UISpec } from './flatten.ts';

const FIXTURES_DIR = join(import.meta.dir, 'fixtures');

function loadFixtures(): Array<{ name: string; spec: UISpec }> {
  return readdirSync(FIXTURES_DIR)
    .filter((f) => f.endsWith('.spec.json'))
    .sort()
    .map((name) => ({
      name,
      // Strip `_comment` from the corner-* fixtures — it's provenance metadata,
      // not part of the spec. A real client would ignore unknown top-level
      // keys; we strip so byte counts reflect the spec proper, not the doc.
      spec: stripComment(
        JSON.parse(readFileSync(join(FIXTURES_DIR, name), 'utf-8')) as Record<string, unknown>,
      ) as unknown as UISpec,
    }));
}

const FIXTURES = loadFixtures();
const PCT_SAMPLES = [10, 25, 50, 75, 90, 100];

describe('streaming sanity', () => {
  test('every fixture loads with a non-empty ui subtree', () => {
    for (const { name, spec } of FIXTURES) {
      expect(spec.ui).toBeDefined();
      expect(nestedUiNodeCount(spec)).toBeGreaterThan(0);
      // No leaked _comment.
      expect((spec as Record<string, unknown>)._comment).toBeUndefined();
      // Sanity log so a fixture rename is loud.
      void name;
    }
  });

  test('at 100% bytes every format renders 100% of units', () => {
    for (const { name, spec } of FIXTURES) {
      const { nested, flatDfs, flatInsertion } = buildAllWires(spec);
      for (const wire of [nested, flatDfs, flatInsertion]) {
        const r = renderableAt(wire, wire.totalBytes);
        expect(r.size).toBe(wire.totalUnits);
        void name;
      }
    }
  });

  test('nested totalUnits equals flat totalUnits for the same spec', () => {
    for (const { spec } of FIXTURES) {
      const { nested, flatDfs, flatInsertion } = buildAllWires(spec);
      expect(nested.totalUnits).toBe(flatDfs.totalUnits);
      expect(nested.totalUnits).toBe(flatInsertion.totalUnits);
      expect(nested.totalUnits).toBe(nestedUiNodeCount(spec));
    }
  });

  test('renderable count is monotone non-decreasing in byte offset', () => {
    // Pick the biggest fixture so the curve has enough samples to be a real
    // monotonicity check.
    const showcase = FIXTURES.find((f) => f.name === 'component-showcase.spec.json')!;
    const { nested, flatDfs, flatInsertion } = buildAllWires(showcase.spec);
    for (const wire of [nested, flatDfs, flatInsertion]) {
      let last = 0;
      // Walk by 5% increments — coarser than the curve sample but enough to
      // catch a non-monotone bug.
      for (let pct = 0; pct <= 100; pct += 5) {
        const off = Math.floor((wire.totalBytes * pct) / 100);
        const r = renderableAt(wire, off).size;
        expect(r).toBeGreaterThanOrEqual(last);
        last = r;
      }
    }
  });

  test('TTFP is strictly positive and ≤ totalBytes', () => {
    for (const { spec } of FIXTURES) {
      const { nested, flatDfs, flatInsertion } = buildAllWires(spec);
      for (const wire of [nested, flatDfs, flatInsertion]) {
        const ttfp = ttfpOffset(wire);
        expect(ttfp).toBeGreaterThan(0);
        expect(ttfp).toBeLessThanOrEqual(wire.totalBytes);
      }
    }
  });
});

describe('streaming curves (table dump)', () => {
  test('print per-fixture renderable curves for nested vs flat-DFS vs flat-insertion', () => {
    // For each fixture, run the simulator at all sample percentages for all
    // three formats. Print a single table per fixture.
    console.log(
      '\n=== streaming renderable% curves @ [10/25/50/75/90/100]% byte offset ===\n',
    );
    console.log(
      `${'fixture'.padEnd(34)} ${'format'.padEnd(16)}  ${'nodes'.padStart(5)}  ${'bytes'.padStart(6)}  ${'ttfp(B)'.padStart(8)}  ${'10%'.padStart(6)} ${'25%'.padStart(6)} ${'50%'.padStart(6)} ${'75%'.padStart(6)} ${'90%'.padStart(6)} ${'100%'.padStart(6)}`,
    );

    for (const { name, spec } of FIXTURES) {
      const wires = buildAllWires(spec);
      const rows: Array<{ fmt: Format; ttfp: number; samples: CurvePoint[]; wire: typeof wires.nested }> = [
        { fmt: 'nested' as Format, ttfp: ttfpOffset(wires.nested), samples: sampleCurve(name, wires.nested, PCT_SAMPLES), wire: wires.nested },
        { fmt: 'flat-dfs' as Format, ttfp: ttfpOffset(wires.flatDfs), samples: sampleCurve(name, wires.flatDfs, PCT_SAMPLES), wire: wires.flatDfs },
        { fmt: 'flat-insertion' as Format, ttfp: ttfpOffset(wires.flatInsertion), samples: sampleCurve(name, wires.flatInsertion, PCT_SAMPLES), wire: wires.flatInsertion },
      ];
      for (const r of rows) {
        const pctStrs = r.samples
          .map((s) => `${s.renderablePct.toFixed(1).padStart(5)}%`)
          .join(' ');
        console.log(
          `${name.padEnd(34)} ${r.fmt.padEnd(16)}  ${String(r.wire.totalUnits).padStart(5)}  ${String(r.wire.totalBytes).padStart(6)}  ${String(r.ttfp).padStart(8)}  ${pctStrs}`,
        );
      }
      console.log('');
    }
    expect(FIXTURES.length).toBeGreaterThan(0);
  });

  test('print 50% / 90% renderable byte-offset table per fixture per format', () => {
    console.log('\n=== byte offset to reach 50% / 90% rendered ===\n');
    console.log(
      `${'fixture'.padEnd(34)} ${'format'.padEnd(16)}  ${'totalB'.padStart(6)}  ${'@50%B'.padStart(7)}  ${'@90%B'.padStart(7)}  ${'@50%off/total'.padStart(14)}  ${'@90%off/total'.padStart(14)}`,
    );
    for (const { name, spec } of FIXTURES) {
      const wires = buildAllWires(spec);
      const entries: Array<{ fmt: Format; wire: typeof wires.nested }> = [
        { fmt: 'nested', wire: wires.nested },
        { fmt: 'flat-dfs', wire: wires.flatDfs },
        { fmt: 'flat-insertion', wire: wires.flatInsertion },
      ];
      for (const { fmt, wire } of entries) {
        const half = Math.ceil(wire.totalUnits * 0.5);
        const ninety = Math.ceil(wire.totalUnits * 0.9);
        const o50 = firstOffsetForRenderableCount(wire, half);
        const o90 = firstOffsetForRenderableCount(wire, ninety);
        const pct50 = ((o50 / wire.totalBytes) * 100).toFixed(1);
        const pct90 = ((o90 / wire.totalBytes) * 100).toFixed(1);
        console.log(
          `${name.padEnd(34)} ${fmt.padEnd(16)}  ${String(wire.totalBytes).padStart(6)}  ${String(o50).padStart(7)}  ${String(o90).padStart(7)}  ${(pct50 + '%').padStart(14)}  ${(pct90 + '%').padStart(14)}`,
        );
      }
      console.log('');
    }
    expect(FIXTURES.length).toBeGreaterThan(0);
  });

  test('print headline TTFP comparison per fixture', () => {
    console.log('\n=== TTFP (byte offset where first node becomes renderable) ===\n');
    console.log(
      `${'fixture'.padEnd(34)} ${'nodes'.padStart(5)}  ${'nested-ttfp'.padStart(12)}  ${'flatDFS-ttfp'.padStart(13)}  ${'flatINS-ttfp'.padStart(13)}  ${'flatDFS-Δ'.padStart(10)}  ${'flatINS-Δ'.padStart(10)}`,
    );
    for (const { name, spec } of FIXTURES) {
      const wires = buildAllWires(spec);
      const tN = ttfpOffset(wires.nested);
      const tD = ttfpOffset(wires.flatDfs);
      const tI = ttfpOffset(wires.flatInsertion);
      const dDPct = tN === 0 ? 0 : (((tD - tN) / tN) * 100).toFixed(1);
      const dIPct = tN === 0 ? 0 : (((tI - tN) / tN) * 100).toFixed(1);
      console.log(
        `${name.padEnd(34)} ${String(wires.nested.totalUnits).padStart(5)}  ${String(tN).padStart(12)}  ${String(tD).padStart(13)}  ${String(tI).padStart(13)}  ${(dDPct + '%').padStart(10)}  ${(dIPct + '%').padStart(10)}`,
      );
    }
    expect(FIXTURES.length).toBeGreaterThan(0);
  });
});

describe('flat-DFS vs flat-insertion delta', () => {
  test('flat-DFS reaches first-renderable no later than flat-insertion across fixtures', () => {
    // DFS emits root first, then root's first child, then its first
    // grandchild, etc. — every emitted component is reachable from root the
    // moment its value closes (provided its parent's children-array has the
    // link committed by then). Insertion order is the order the agent
    // happened to emit, which may or may not place ancestors before
    // descendants. So we expect DFS ≤ insertion, fixture-by-fixture.
    let strictlyBetter = 0;
    for (const { name, spec } of FIXTURES) {
      const { flatDfs, flatInsertion } = buildAllWires(spec);
      const tD = ttfpOffset(flatDfs);
      const tI = ttfpOffset(flatInsertion);
      expect(tD).toBeLessThanOrEqual(tI);
      if (tD < tI) strictlyBetter++;
      void name;
    }
    // Not asserted as a hard requirement, just informational.
    void strictlyBetter;
  });
});

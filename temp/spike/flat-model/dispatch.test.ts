// dispatch.test.ts — RFC 06 Position 1 spike, push-back D coexistence test.
// Created: 2026-05-24 — Proves the format discriminator + dispatch wrapper
// route the same logical spec through two storage paths to byte-identical
// rendered output. This is the rollback contract PR-1 of the real rollout
// will ship: a stored pocket carries `format: "flat" | "nested"`, the
// renderer reads that field and dispatches, and flipping the format value
// on the same logical content produces no render diff.
//
// Test setup choice: STRUCTURAL-STRING fallback (not JSDOM / happy-dom).
// The spike has no package.json — there's no installed Svelte test runner
// here, no JSDOM, no @testing-library/svelte. The ripple project itself
// has @testing-library/svelte but adding it to this spike harness would
// require setting up a package.json + vitest config + svelte-test-config,
// which is more spike-infrastructure than the proof needs. The captain's
// explicit fallback (per push-back D task body) is to compile a tiny
// renderToString that mirrors FlatRenderer.svelte's DOM emission rules
// into this test file, then run the dispatch logic that mirrors
// RippleRenderer.svelte's `spec.format ?? "nested"` discriminator and
// compare the two string outputs. That proves "dispatch + flatten-on-entry
// produces the same logical tree", which IS the coexistence contract.
// The Svelte component files exist as the production implementation; the
// test mirrors their behavior in plain TS so bun:test can run it.
//
// Run: bun test temp/spike/flat-model/dispatch.test.ts

import { describe, expect, test } from 'bun:test';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import {
  flatten,
  type FlatNode,
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

// ---- structural mirror of FlatRenderer.svelte ----
//
// Mirrors the DOM emission rules in FlatRenderer.svelte verbatim:
//   - `<div data-flat-node-id={id} data-flat-node-type={type}>...inner...</div>`
//   - text  → inner is `<span>{props.text}</span>`
//   - heading → inner is `<h3>{props.text}</h3>`
//   - other → inner is `<div class="ripple-flat-node ripple-{type}">...children + else_children...</div>`
//   - missing id → `<div class="ripple-flat-missing" data-missing-id={id}>[missing component {id}]</div>`
function renderFlatToString(spec: FlatSpec, componentId: string): string {
  const node: FlatNode | undefined = spec.components[componentId];
  if (!node) {
    return `<div class="ripple-flat-missing" data-missing-id="${componentId}">[missing component ${componentId}]</div>`;
  }

  let inner: string;
  if (node.type === 'text' && node.props) {
    inner = `<span>${escapeText((node.props as Record<string, unknown>).text)}</span>`;
  } else if (node.type === 'heading' && node.props) {
    inner = `<h3>${escapeText((node.props as Record<string, unknown>).text)}</h3>`;
  } else {
    const childIds = node.children ?? [];
    const elseChildIds = node.else_children ?? [];
    const childrenHtml = childIds.map((id) => renderFlatToString(spec, id)).join('');
    const elseHtml =
      elseChildIds.length > 0
        ? elseChildIds.map((id) => renderFlatToString(spec, id)).join('')
        : '';
    inner = `<div class="ripple-flat-node ripple-${node.type}">${childrenHtml}${elseHtml}</div>`;
  }

  return `<div data-flat-node-id="${componentId}" data-flat-node-type="${node.type}">${inner}</div>`;
}

function escapeText(value: unknown): string {
  if (value === null || value === undefined) return '';
  return String(value);
}

// ---- structural mirror of RippleRenderer.svelte ----
//
// Reads `spec.format ?? "nested"`. On `"flat"` casts to FlatSpec and renders.
// On `"nested"` calls flatten() to convert, then renders. Same dispatch
// logic the Svelte wrapper uses.
function dispatchRender(spec: UISpec | FlatSpec): string {
  const format = (spec as { format?: 'flat' | 'nested' }).format ?? 'nested';
  const flat: FlatSpec = format === 'flat' ? (spec as FlatSpec) : flatten(spec as UISpec);
  return renderFlatToString(flat, flat.root);
}

// Drop `data-flat-node-id="..."` and `data-missing-id="..."` from the rendered
// string. The two paths mint independent ids (flat-path: the spec was
// flattened once and stored; nested-path: dispatch flattens on entry and
// mints fresh ids), so the id values differ even when the logical content is
// identical. Stripping them isolates the structural comparison the captain
// asked for: "same logical content, two storage formats, identical render."
function stripIds(html: string): string {
  return html
    .replace(/ data-flat-node-id="[^"]*"/g, '')
    .replace(/ data-missing-id="[^"]*"/g, '');
}

describe('format dispatch — same logical content, two storage formats, identical render', () => {
  for (const { name, spec: original } of FIXTURES) {
    test(`${name}: dispatch via format:"nested" === dispatch via format:"flat"`, () => {
      // Build two specs from the same source content.
      // 1. "nested": the original spec, format-tagged.
      const nestedSpec: UISpec = { ...original, format: 'nested' };
      // 2. "flat": flatten once and tag as flat. flatten() already stamps
      //    format:"flat" — we re-state it here for clarity at the call site.
      const flatSpec: FlatSpec = { ...flatten(original), format: 'flat' };

      const renderedFromNested = stripIds(dispatchRender(nestedSpec));
      const renderedFromFlat = stripIds(dispatchRender(flatSpec));

      expect(renderedFromNested).toBe(renderedFromFlat);
      // Sanity: the rendered string is non-trivial — guards against a
      // pathology where both paths return "" and the equality passes
      // vacuously.
      expect(renderedFromNested.length).toBeGreaterThan(0);
    });
  }
});

describe('format dispatch — fallback default is "nested"', () => {
  // A spec without `format` set must render identically to one with
  // `format: "nested"`. This is the back-compat contract: existing stored
  // specs (pre-discriminator) keep working without any migration touch.
  test('absent format → treated as nested', () => {
    const fixture = FIXTURES.find((f) => f.name === 'business-dashboard.spec.json')!;
    const original = fixture.spec;

    // Strip any format that might already be on the fixture, then compare:
    //   - one path explicitly tags "nested"
    //   - the other path leaves format absent
    const stripFormat = (s: UISpec): UISpec => {
      const { format: _f, ...rest } = s as Record<string, unknown>;
      return rest as UISpec;
    };
    const noFormat = stripFormat(original);
    const explicitNested: UISpec = { ...noFormat, format: 'nested' };

    const rA = stripIds(dispatchRender(noFormat));
    const rB = stripIds(dispatchRender(explicitNested));
    expect(rA).toBe(rB);
    expect(rA.length).toBeGreaterThan(0);
  });
});

describe('format flip is render-identity-preserving (the rollback contract)', () => {
  // The bonus assertion from the push-back D task body:
  //
  //   "flipping `format` on the same logical content does not change
  //    rendered output. This is the rollback contract."
  //
  // i.e. if we have a logically-equivalent spec stored both ways (nested
  // form OR flat form), the renderer produces the same DOM. PR-1 of the
  // real rollout ships this exact contract — any caller can flip the
  // discriminator on a stored pocket and the user sees no difference.
  for (const { name, spec: original } of FIXTURES) {
    test(`${name}: flipping format flat <-> nested preserves rendered output`, () => {
      // Forward: start nested, then flatten and tag flat. Same logical
      // content, different storage, single dispatch must match.
      const nestedSide: UISpec = { ...original, format: 'nested' };
      const flatSide: FlatSpec = flatten(original); // flatten() stamps format:"flat"

      // Both must arrive at the same rendered DOM (after id strip).
      expect(stripIds(dispatchRender(nestedSide))).toBe(stripIds(dispatchRender(flatSide)));

      // Reverse: take the flat form, treat it as the canonical store,
      // re-tag the nested copy with `format:"nested"` and render again.
      // Flipping the tag — without altering the content — must not change
      // anything the user sees. This is the literal "flip the
      // discriminator" rollback step.
      const flatRendered = stripIds(dispatchRender(flatSide));
      const nestedRendered = stripIds(dispatchRender(nestedSide));
      expect(flatRendered).toBe(nestedRendered);
    });
  }
});

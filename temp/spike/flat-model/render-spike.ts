// render-spike.ts — RFC 06 Position 1 spike, side-by-side preview generator.
// Created: 2026-05-24.
//
// Produces preview.html with two columns per fixture: the nested UISpec
// rendered as a debug tree, and the flat FlatSpec rendered by walking child-id
// pointers. This is NOT the production renderer — it is a tiny structural
// renderer that lights up the recursion mechanic for both shapes so a human
// can eyeball the difference. The real Svelte renderer port is the next
// milestone if the captain greenlights.
//
// Run: bun temp/spike/flat-model/render-spike.ts

import { readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { flatten, type FlatNode, type FlatSpec, type UINode, type UISpec } from './flatten.ts';

const FIXTURES_DIR = join(import.meta.dir, 'fixtures');
const OUT = join(import.meta.dir, 'preview.html');

function loadFixtures() {
  return readdirSync(FIXTURES_DIR)
    .filter((f) => f.endsWith('.spec.json'))
    .sort()
    .map((name) => ({
      name,
      spec: JSON.parse(readFileSync(join(FIXTURES_DIR, name), 'utf-8')) as UISpec,
    }));
}

// ---- nested renderer (mirrors NodeRenderer.svelte's CORE recursion only) ----

function renderNested(node: UINode | undefined): string {
  if (!node) return '';
  const text = typeof node.props?.text === 'string' ? esc(node.props.text) : '';
  const kids = node.children ?? [];
  const childHtml = kids.map(renderNested).join('');
  const elseKids = node.else_children ?? [];
  const elseHtml = elseKids.length
    ? `<div class="else">else: ${elseKids.map(renderNested).join('')}</div>`
    : '';
  return `<div class="node node-${esc(node.type)}" data-type="${esc(node.type)}">
    <span class="type">${esc(node.type)}</span>${text ? `<span class="text"> "${text}"</span>` : ''}
    ${childHtml}${elseHtml}
  </div>`;
}

// ---- flat renderer (resolves child-id pointers) ----

function renderFlat(spec: FlatSpec, id: string, depth = 0): string {
  if (depth > 100) return '<div class="cycle">[recursion limit]</div>';
  const node = spec.components[id];
  if (!node) return `<div class="missing">[missing ${esc(id)}]</div>`;
  const text = typeof node.props?.text === 'string' ? esc(node.props.text) : '';
  const childIds = node.children ?? [];
  const childHtml = childIds.map((cid) => renderFlat(spec, cid, depth + 1)).join('');
  const elseIds = node.else_children ?? [];
  const elseHtml = elseIds.length
    ? `<div class="else">else: ${elseIds.map((cid) => renderFlat(spec, cid, depth + 1)).join('')}</div>`
    : '';
  return `<div class="node node-${esc(node.type)}" data-id="${esc(id)}" data-type="${esc(node.type)}">
    <span class="id">${esc(id)}</span> <span class="type">${esc(node.type)}</span>${text ? `<span class="text"> "${text}"</span>` : ''}
    ${childHtml}${elseHtml}
  </div>`;
}

function esc(s: string): string {
  return String(s).replace(/[&<>"]/g, (c) =>
    c === '&' ? '&amp;' : c === '<' ? '&lt;' : c === '>' ? '&gt;' : '&quot;'
  );
}

function row(name: string, spec: UISpec): string {
  const flat = flatten(spec);
  const nestedHtml = renderNested(spec.ui);
  const flatHtml = renderFlat(flat, flat.root);
  const nestedSize = JSON.stringify(spec).length;
  const flatSize = JSON.stringify(flat).length;
  const nodeCount = Object.keys(flat.components).length;
  return `<section class="fixture">
    <h2>${esc(name)} <span class="meta">(${nodeCount} nodes — nested ${nestedSize}B / flat ${flatSize}B)</span></h2>
    <div class="cols">
      <div class="col">
        <h3>Nested (recurses on node.children)</h3>
        <div class="render">${nestedHtml}</div>
      </div>
      <div class="col">
        <h3>Flat (resolves child IDs in components map)</h3>
        <div class="render">${flatHtml}</div>
      </div>
    </div>
  </section>`;
}

const fixtures = loadFixtures();
const body = fixtures.map(({ name, spec }) => row(name, spec)).join('\n');

const html = `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>RFC 06 Position 1 — flat vs nested renderer spike</title>
  <style>
    body { font-family: ui-sans-serif, system-ui, sans-serif; background: #0e1015; color: #e6e6e6; margin: 0; padding: 24px; }
    h1 { font-size: 18px; font-weight: 600; }
    h2 { font-size: 14px; font-weight: 600; margin: 28px 0 8px; color: #c9d4ff; }
    h3 { font-size: 12px; font-weight: 600; color: #8b94a7; margin: 0 0 8px; }
    .meta { font-weight: 400; color: #6b7280; font-size: 12px; }
    .cols { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
    .col { background: #14171f; border: 1px solid #232734; border-radius: 8px; padding: 12px; min-width: 0; }
    .render { font-family: ui-monospace, monospace; font-size: 11px; line-height: 1.5; overflow-x: auto; }
    .node { padding: 2px 0 2px 12px; border-left: 1px solid #2b3142; margin-left: 4px; }
    .type { color: #82d2ff; }
    .id { color: #888; font-size: 10px; }
    .text { color: #ffd479; }
    .else { color: #d29bff; padding-left: 8px; border-left: 1px dashed #5a3a8a; margin: 4px 0; }
    .missing { color: #ff7f7f; }
    .summary { background: #14171f; border: 1px solid #232734; border-radius: 8px; padding: 12px; margin-top: 24px; font-size: 12px; line-height: 1.6; }
  </style>
</head>
<body>
  <h1>RFC 06 Position 1 — flat vs nested component model</h1>
  <p style="color:#8b94a7; font-size: 12px; max-width: 80ch;">
    Side-by-side rendering of real stored pocket specs. Left column: the spec's
    nested <code>ui.children[]</code> recursion (today's renderer). Right
    column: the same spec flattened to <code>{root, components}</code>, with
    children resolved via id lookup (the OpenUI shape). Visually identical
    tree — the cost of the flat model is purely in the wire shape and renderer
    plumbing, not in what the user sees.
  </p>
  ${body}
  <div class="summary">
    See <code>findings.md</code> in the same folder for round-trip / byte-size
    / renderer-LOC / mutator-LOC numbers and the captain-facing recommendation.
  </div>
</body>
</html>
`;

writeFileSync(OUT, html);
console.log(`wrote ${OUT} (${html.length} bytes, ${fixtures.length} fixtures)`);
